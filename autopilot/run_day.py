#!/usr/bin/env python3
"""The paper autopilot's day runner: runs the day-trader-india skills on a trading day with nobody at the
keyboard, on PAPER only, and writes the end-of-day report. Standard library only.

    python3 run_day.py tick        # what launchd runs every minute; does whatever the clock says is due
    python3 run_day.py status      # today's state, in plain words
    python3 run_day.py selftest    # python, config, Claude (headless) and Gmail, from where it is run
    python3 run_day.py progress    # the half-hourly progress email, when one is due (progress.py)
    python3 run_day.py journal     # rebuild the P&L journal: every trade, what it made or lost, and why
    python3 run_day.py eod --date 2026-11-13     # rebuild and resend a day's report
    NSE_TEST_MODE=1 python3 run_day.py replay --date 2026-11-13 --times 08:45,10:05,15:35

A tick is idempotent: the day's state lives in $NSE_TRADER_HOME/autopilot/<date>/state.json, so a tick that
crashes, a Mac that sleeps or a reboot only means the next tick carries on. launchd never starts a job that
is still running, and a lock file stops a hand-run tick from overlapping it.

The day, in IST (times from autopilot.json):
  08:40   once: the Nifty 50 list, the daily scan (context), the news, then Claude's pre-market veto
  09:16-  every tick with a paper trade open: ticket.py paper-check (fills, stops, targets, flat by 15:10)
  09:36-14:31, one minute after each 5-minute bar closes: an ORB scan; each new hit goes through
          nse-entry -> nse-exit -> nse-timemachine -> nse-budget -> nse-alerts quote -> nse-risk ->
          ticket.py autopilot-approve -> paper-fill, one symbol at a time. After the day's trade cap or
          loss limit, a hit is planned but not traded: a shadow, which the report replays.
  15:35   once: the last paper-check, the journal, report.py, Claude's analysis, the email, the bar archive and
          the P&L journal (journal.py, "journal" in autopilot.json).

Paper only, by code, twice: this runner refuses unless config.json says "broker": "paper" and
"autopilot": true, and ticket.py autopilot-approve checks both again. Nothing here changes config.json.
Not financial advice.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accounts  # noqa: E402
import autopilot_common as ac  # noqa: E402
import claude_run  # noqa: E402
import mail  # noqa: E402
import market  # noqa: E402
import report  # noqa: E402
import shorts  # noqa: E402

nc = ac.nc

ORB_FIRST_BAR = "09:35"          # the first bar after the 15-minute range (09:30) closes at 09:35
PAPER_FROM = "09:16"
SESSION = ("09:15", "15:30")
DOSSIER_STOP = "09:10"          # the deep scan never starts a stock after this
ENTRY_RETRIES = 3                # an ORB hit nse-entry does not (yet) call triggered is checked on 3 bars
EOD_ATTEMPTS = 3                 # an end of day that keeps failing gives up after 3 tries (and says so)
SAFE_REASON_CHARS = 400


def hm(t):
    return t.strftime("%H:%M")


def minutes(text):
    t = nc.parse_hhmm(text)
    return t.hour * 60 + t.minute


class Day(object):
    """One trading day of the autopilot. `now` is naive UTC; `runner` runs the skill scripts; `claude` and
    `send_mail` are swapped out in tests."""

    def __init__(self, home, now, settings, cfg, runner=None, claude=None, send_mail=None, bars=None,
                 index_move=None, day=None, fetch=None):
        self.home, self.now, self.settings, self.cfg = home, now, settings, cfg
        self.ist = nc.to_ist(now)
        self.day = day or self.ist.date().isoformat()
        self.st = ac.load_day(home, self.day)
        self.dir = ac.day_dir(home, self.day)
        now_text = self.ist.strftime("%Y-%m-%d %H:%M") if nc.test_mode() else None
        self.runner = runner or ac.Runner(home, self.day, settings, now_text)
        self.claude = claude or claude_run.ask
        self.send_mail = send_mail or mail.send_report
        self.bars = bars                    # report.py's 1-minute bar loader (tests pass a fake)
        self.index_move = index_move        # report.py's Nifty 50 move (tests pass a fake)
        # Yahoo charts for the tags and the archive; a test or replay never reaches the network unless it
        # passes a fake
        self.fetch = fetch or (None if nc.test_mode() else market.fetch_chart)

    # ------------------------------------------------------------ helpers

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def home_path(self, *parts):
        return os.path.join(self.home, *parts)

    def at_least(self, text):
        return minutes(hm(self.ist)) >= minutes(text)

    def stamp(self):
        return nc.utc_iso(self.now)

    def save(self):
        ac.save_day(self.home, self.st)

    def universe_args(self):
        u = self.settings.get("universe") or "nifty50"
        if isinstance(u, list):
            return ["--symbols", ",".join(nc.nse_symbol(s) for s in u)]
        return ["--universe", str(u)]

    # ------------------------------------------------------------ the tick

    def tick(self):
        status = nc.market_status(self.now, self.cfg)
        ph = self.st["phases"]
        if status["phase"] == "closed-day":
            if not ph.get("closed"):
                ph["closed"] = status.get("reason") or "closed"
                self.runner.note("closed", reason=ph["closed"])
                self.save()
            return 0
        if not self.at_least(self.settings["premarket_from"]) or (ph.get("eod") or {}).get("done"):
            return 0
        minute = hm(self.ist)
        if not self.st["ticks"] or self.st["ticks"][-1] != minute:
            self.st["ticks"].append(minute)
        if not ph.get("premarket") and not self.at_least(self.settings["eod_at"]):
            self.premarket()
        if self.at_least(PAPER_FROM) and not ph.get("eod"):
            self.paper_check()
            self.paper_check_accounts()
        bar = self.orb_due()
        if bar:
            self.orb(bar)
        if self.at_least(self.settings["eod_at"]) and not (ph.get("eod") or {}).get("done"):
            self.eod()
        self.save()
        return 0

    # ------------------------------------------------------------ pre-market

    def premarket(self):
        ph = self.st["phases"]
        ph["premarket"] = {"started": self.stamp()}
        self.save()
        if accounts.caps_of(self.settings):
            ph["premarket"]["accounts"] = [a["name"] for a in self.accounts(refresh=True)]
        if not nc.test_mode():
            self.runner.run("holidays", "alerts", ["holidays", "--refresh", "--if-stale"], parse=False, now=False)
        syms, names = self.universe()
        ph["premarket"]["universe"] = len(syms)
        if self.settings.get("tags"):
            reg = market.regime(self.fetch, self.ist.date()) if self.fetch else {"errors": ["no market data"]}
            ph["premarket"]["regime"] = reg
            self.runner.note("regime", **reg)
            self.save()
        if self.settings.get("daily_scan"):
            self.runner.run("scan_daily", "scan", ["daily"] + self.universe_args() +
                            ["--json", "--out", self.path("daily.json")])
        news = None
        if self.settings.get("news") and syms:
            r = self.runner.run("news", "news", ["--tickers"] + syms + ["--json"], timeout=900)
            news = r.data
            if news is not None:
                ac.write_json(self.path("news.json"), news)
            ph["premarket"]["news"] = {"code": r.code, "items": len((news or {}).get("items") or [])}
        if (self.settings.get("dossier") or {}).get("enabled") and syms:
            ph["premarket"]["dossier"] = self.dossier(syms)
            self.save()
        claude = self.settings.get("claude") or {}
        if not claude.get("enabled"):
            ph["premarket"]["veto"] = "off"
        elif self.at_least(self.settings["veto_by"]):
            ph["premarket"]["veto"] = "skipped: the pre-market started after %s" % self.settings["veto_by"]
            self.runner.note("veto", skipped=ph["premarket"]["veto"])
        else:
            brief = premarket_brief(self.day, syms, names, news, ac.read_json(self.path("daily.json")),
                                    ac.read_json(self.path("stocks", "index.json")))
            ac.write_json(self.path("premarket-brief.json"), brief)
            out = self.claude("premarket", brief, self.settings, self.path("claude-premarket.json"))
            vetoes = clean_vetoes((out or {}).get("vetoes"), syms) if out and out.get("ok") else []
            ac.write_json(self.path("vetoes.json"), {"vetoes": vetoes, "summary": (out or {}).get("summary"),
                                                      "ok": bool(out and out.get("ok")),
                                                      "error": (out or {}).get("error")})
            ph["premarket"]["veto"] = ("%d vetoed" % len(vetoes)) if out and out.get("ok") else \
                "Claude failed: %s" % (out or {}).get("error")
            self.runner.note("veto", vetoes=vetoes, ok=bool(out and out.get("ok")), error=(out or {}).get("error"))
        ph["premarket"]["done"] = self.stamp()
        self.save()

    def dossier(self, syms):
        """The deep scan: a file per stock (nse-news/dossier.py) in <day>/stocks/. It must never run into the
        session (a tick holds the lock while it runs, and the ORB scans and paper stops wait), so it stops starting
        new stocks at budget_s or DOSSIER_STOP (09:10), whichever is sooner, and is skipped with under 2 minutes."""
        dz = self.settings.get("dossier") or {}
        left = (minutes(DOSSIER_STOP) - minutes(hm(self.ist))) * 60
        budget = min(float(dz.get("budget_s") or 900), left)
        if budget < 120:
            self.runner.note("dossier", skipped="too close to the open")
            return {"skipped": "too close to the open (it stops at %s)" % DOSSIER_STOP}
        args = ["--tickers"] + syms + ["--out", self.path("stocks"), "--budget-s", "%d" % budget]
        if os.path.isfile(self.path("universe.txt")):
            args += ["--names", self.path("universe.txt")]
        if dz.get("results") is False:
            args.append("--no-results")
        r = self.runner.run("dossier", "dossier", args, parse=False, timeout=int(budget) + 240)
        idx = ac.read_json(self.path("stocks", "index.json")) or {}
        stocks = idx.get("stocks") or {}
        return {"code": r.code, "stocks": len(stocks), "of": len(syms),
                "flagged": sum(1 for v in stocks.values() if [f for f in v.get("flags") or []
                                                               if not f.startswith("Could not read")]),
                "failed": sorted(k for k, v in stocks.items() if v.get("failed")),
                "shared_errors": idx.get("shared_errors") or {}, "seconds": r.seconds}

    def universe(self):
        """(symbols, {symbol: name}) for the news and the veto."""
        u = self.settings.get("universe") or "nifty50"
        if isinstance(u, list):
            syms = [nc.nse_symbol(s) for s in u]
            return syms, {}
        out = self.path("universe.txt")
        self.runner.run("universe", "scan", ["universe", str(u), "--json", "--out", out], now=False)
        syms, names = [], {}
        try:
            with open(out, encoding="utf-8") as fh:
                for line in fh:
                    sym, _, name = line.strip().partition("|")
                    if sym:
                        syms.append(sym)
                        names[sym] = name
        except OSError:
            pass
        return syms, names

    def universe_symbols(self):
        """The day's universe from the pre-market's universe.txt, else asked for again."""
        try:
            with open(self.path("universe.txt"), encoding="utf-8") as fh:
                syms = [line.split("|")[0].strip() for line in fh if line.split("|")[0].strip()]
            if syms:
                return syms
        except OSError:
            pass
        return self.universe()[0]

    def vetoes(self):
        doc = ac.read_json(self.path("vetoes.json")) or {}
        return {v["symbol"]: v.get("reason") or "" for v in doc.get("vetoes") or [] if v.get("symbol")}

    def veto_categories(self):
        doc = ac.read_json(self.path("vetoes.json")) or {}
        return {v["symbol"]: v.get("category") or "OTHER" for v in doc.get("vetoes") or [] if v.get("symbol")}

    # ------------------------------------------------------------ paper trades

    def live_trades(self):
        paper = ac.read_json(self.home_path("state", "paper.json")) or {}
        return [t for t in paper.get("trades") or [] if t.get("status") in ("open", "filled")]

    def paper_check(self, force=False):
        if not force and not self.live_trades():
            return None
        return self.runner.run("paper_check", "ticket", ["paper-check"], parse=False)

    # ------------------------------------------------------------ the other paper accounts (accounts.py)

    def accounts(self, refresh=False):
        """[{name, cap, home, cfg, runner}] for each extra paper account; the folders are created or refreshed from
        the main config at the pre-market (or on first use). A cap equal to the main account's is the main account."""
        if getattr(self, "_accounts", None) is not None and not refresh:
            return self._accounts
        main_cap = int(self.cfg.get("max_trades_per_day") or 0)
        out = []
        for cap in accounts.caps_of(self.settings):
            if cap == main_cap:
                continue
            path = accounts.home_of(self.home, cap)
            if refresh or not os.path.isfile(os.path.join(path, "config.json")):
                accounts.ensure(self.home, self.cfg, cap)
            cfg = accounts.load_cfg(path) or {}
            if ac.guard(cfg):
                self.runner.note("accounts", account=accounts.name_of(cap), error=ac.guard(cfg))
                continue
            out.append({"name": accounts.name_of(cap), "cap": cap, "home": path, "cfg": cfg,
                        "filter": accounts.filter_of(self.settings, cap),
                        "runner": ac.Runner(path, self.day, self.settings, getattr(self.runner, "now_text", None))})
        self._accounts = out
        return out

    def paper_check_accounts(self, force=False):
        for a in self.accounts():
            if force or accounts.live_trades(a["home"]):
                a["runner"].run("paper_check", "ticket", ["paper-check"], parse=False)

    def trade_accounts(self, c, sym, plan_path):
        """The planned signal through each extra account: its own size, quote, risk check, approval and paper
        fill. One account failing never touches the main account or the others."""
        out = c.setdefault("accounts", {})
        for a in self.accounts():
            if (out.get(a["name"]) or {}).get("outcome"):
                continue
            try:
                out[a["name"]] = self.account_trade(a, sym, plan_path, c)
            except Exception as e:                   # an account's error is its own
                out[a["name"]] = {"outcome": "error", "reason": "%s: %s" % (type(e).__name__, ac.tail(str(e), 200))}
        self.save()

    def account_trade(self, a, sym, plan_path, c=None):
        if a.get("filter"):              # the account's own rule (accounts.filters), before anything is asked
            rvol = self.signal_rvol(c, sym)
            why = accounts.filter_reason(a["filter"], rvol)
            if why:
                return {"outcome": "filtered", "reason": why, "open_rvol": rvol}
        why = accounts.cap_reason(a["home"], a["cfg"], self.day)
        if why:
            return {"outcome": "shadow", "reason": why}
        home, run = a["home"], a["runner"].run
        plan = os.path.join(home, "plan.json")
        shutil.copyfile(plan_path, plan)
        r = run("size", "size", ["--plan", plan, "--write", "--json"], sym)
        sizing = (r.data or {}).get("sizing") or {}
        if not r.ok:
            return {"outcome": "size_refused",
                    "reason": "; ".join((r.data or {}).get("notes") or []) or ac.tail(r.err or r.out, 300)}
        run("quote", "alerts", ["quote", sym, "--write", os.path.join(home, "prices.json"), "--json"], sym)
        r = run("risk", "risk", ["--json"], sym)
        res = ((r.data or {}).get("results") or [{}])[0]
        if res.get("verdict") != "PASS":
            return {"outcome": "risk_block", "qty": sizing.get("qty"),
                    "reason": "; ".join(res.get("reasons") or []) or ac.tail(r.err or "no verdict", 300)}
        r = run("approve", "ticket", ["autopilot-approve", "--index", "1"], sym, parse=False)
        if not r.ok:
            return {"outcome": "approve_refused", "reason": ac.tail(r.err or r.out, 300)}
        r = run("paper_fill", "ticket", ["paper-fill", "--index", "1"], sym, parse=False)
        if not r.ok:
            return {"outcome": "fill_refused", "reason": ac.tail(r.err or r.out, 300)}
        return {"outcome": "traded", "qty": sizing.get("qty"), "bound_by": sizing.get("bound_by")}

    # ------------------------------------------------------------ opening-range breakouts

    def orb_due(self):
        """The 5-minute bar that closed at least a minute ago, if it has not been scanned yet."""
        now_m = minutes(hm(self.ist))
        if now_m < minutes(ORB_FIRST_BAR) + 1 or now_m > minutes(self.settings["orb_until"]):
            return None
        m = now_m - 1
        bar = m - m % 5
        text = "%02d:%02d" % divmod(bar, 60)
        if self.st["phases"].get("orb_last_bar") == text:
            return None
        return text

    def shorts_on(self):
        return str(self.settings.get("shorts") or "off").lower() == "shadow"

    def orb(self, bar):
        ph = self.st["phases"]
        side = ["--side", "both"] if self.shorts_on() else []
        r = self.runner.run("scan_orb", "scan", ["orb"] + self.universe_args() + side +
                            ["--json", "--out", self.path("orb.json")])
        ph["orb_last_bar"] = bar
        hits = (r.data or {}).get("hits") or []
        ph.setdefault("orb_scans", []).append({"bar": bar, "code": r.code, "hits": len(hits),
                                               "failed": r.code not in (0, 1)})
        if hits and self.settings.get("tags") and self.fetch:
            close = "%02d:%02d" % divmod(minutes(bar) + 5, 60)     # the scanned bar closed 5 minutes after it began
            try:
                ph.setdefault("index_by_bar", {})[close] = market.index_move(self.fetch, self.ist.date(), close)
            except Exception as e:
                ph.setdefault("index_by_bar", {})[close] = {"error": ac.tail(str(e), 160)}
        self.save()
        for h in sorted(hits, key=lambda h: (h.get("at") or "", h.get("symbol") or "")):
            if h.get("symbol"):
                short = h.get("side") == "short"
                try:
                    self.candidate_short(h) if short else self.candidate(h)
                except Exception as e:     # one bad symbol never stops the day
                    key = ("short:" if short else "") + nc.nse_symbol(h["symbol"])
                    c = self.st["candidates"].setdefault(key, {"symbol": nc.nse_symbol(h["symbol"])})
                    if short:
                        c["side"] = "short"
                    self.finish(c, "error", "%s: %s" % (type(e).__name__, e))

    def open_rvol(self, sym):
        """Today's opening volume (09:20-09:30) over its 20-day median (market.opening_rvol), once per tick; None
        before 09:30, without a fetcher, or when it cannot be worked out."""
        memo = self.__dict__.setdefault("_open_rvol", {})
        if sym not in memo:
            v = None
            if self.fetch and self.at_least("09:30"):
                try:
                    v = market.opening_rvol(self.home, self.fetch, sym, self.day)
                except Exception:
                    v = None
            memo[sym] = v
        return memo[sym]

    def signal_rvol(self, c, sym):
        """The signal's opening volume, kept on the candidate so every account and the report see the same number."""
        if c is None:
            return self.open_rvol(sym)
        if c.get("open_rvol") is None:
            c["open_rvol"] = self.open_rvol(sym)
        return c["open_rvol"]

    def tags_for(self, sym, at):
        """What was true when the signal fired: the regime before the open, the Nifty's move so far, the rank."""
        if not self.settings.get("tags") and not self.settings.get("rank_file"):
            return None
        tags = {}
        ph = self.st["phases"]
        if self.settings.get("tags"):
            reg = (ph.get("premarket") or {}).get("regime") or {}
            for k in ("nifty_trend", "vix_band"):
                if reg.get(k):
                    tags[k] = reg[k]
            idx = (ph.get("index_by_bar") or {}).get(at or "")
            if not idx:              # the hit fired on an earlier bar: the nearest bar at or before it
                earlier = sorted(k for k in (ph.get("index_by_bar") or {}) if at and k <= at)
                idx = (ph.get("index_by_bar") or {}).get(earlier[-1]) if earlier else None
            if idx and idx.get("tag"):
                tags["nifty_today"] = idx["tag"]
            band = market.rvol_band(self.open_rvol(sym))    # research.py's lead, measured live
            if band:
                tags["open_volume"] = band
        rank = rank_tag(self.settings.get("rank_file"), sym, self.ist.date())
        if rank:
            tags["rank"] = rank
        return tags

    def charges_r_limit(self):
        v = (self.settings.get("cost_filter") or {}).get("max_charges_r")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def candidate_short(self, h):
        """A breakdown, in shadow: planned (shorts.plan) and replayed by the report, never traded."""
        sym = nc.nse_symbol(h["symbol"])
        key = "short:" + sym
        c = self.st["candidates"].get(key)
        if c and c.get("final"):
            return
        c = {"symbol": sym, "side": "short", "name": h.get("name"), "first_seen": hm(self.ist),
             "scan_at": h.get("at"), "scan_price": h.get("price"), "or_high": h.get("or_high"),
             "or_low": h.get("or_low"), "pct_over": h.get("pct_over"), "daily_setups": self.daily_setups(sym),
             "trigger": {"at": h.get("at"), "price": h.get("price")}}
        self.st["candidates"][key] = c
        tags = self.tags_for(sym, h.get("at"))
        if tags is not None:
            c["tags"] = tags
        age = minutes(hm(self.ist)) - minutes(h.get("at") or hm(self.ist))
        limit = int(self.cfg.get("trigger_max_age_minutes") or 30)
        if age > limit:
            self.finish(c, "stale_trigger", "the breakdown fired at %s, %d minutes before the runner saw it "
                                            "(over trigger_max_age_minutes, %d)" % (h.get("at"), age, limit))
            return
        try:
            p = shorts.plan(h, self.cfg)
        except shorts.ShortPlanError as e:
            self.finish(c, "short_refused", str(e))
            return
        c["plan"] = p
        mx = self.charges_r_limit()
        c["cost_ok"] = None if mx is None else p["charges_r"] <= mx
        self.finish(c, "short_shadow", "shadow only, never traded: entry %s, stop %s, target %s, %d shares, "
                                       "charges %.2fR" % (p["entry"], p["stop"], p["target"], p["quantity"],
                                                          p["charges_r"]))

    def finish(self, c, outcome, reason=None):
        c["outcome"] = outcome
        c["final"] = True
        c["decided_at"] = hm(self.ist)
        if reason:
            c["reason"] = ac.tail(plain_reason(reason), SAFE_REASON_CHARS)
        self.runner.note("candidate", symbol=c.get("symbol"), outcome=outcome, reason=c.get("reason"))
        self.save()

    def candidate(self, h):
        sym = nc.nse_symbol(h["symbol"])
        c = self.st["candidates"].get(sym)
        if c and c.get("final"):
            return
        if not c:
            c = {"symbol": sym, "name": h.get("name"), "first_seen": hm(self.ist), "scan_at": h.get("at"),
                 "scan_price": h.get("price"), "or_high": h.get("or_high"), "or_low": h.get("or_low"),
                 "pct_over": h.get("pct_over"), "tries": 0, "daily_setups": self.daily_setups(sym)}
            tags = self.tags_for(sym, h.get("at"))
            if tags is not None:
                c["tags"] = tags
            if self.settings.get("tags") or self.main_filter() or any(a.get("filter") for a in self.accounts()):
                c["open_rvol"] = self.open_rvol(sym)
            self.st["candidates"][sym] = c
        c["tries"] = c.get("tries", 0) + 1
        run = self.runner.run

        entry_file = self.path("entries", sym + ".json")
        os.makedirs(os.path.dirname(entry_file), exist_ok=True)
        r = run("entry", "entry", ["check", sym, "--setup", "orb", "--json", "--write", entry_file], sym)
        e = r.data or {}
        status = e.get("status")
        if status != "triggered":
            c["entry_status"] = status or "error"
            if status == "expired" or c["tries"] >= ENTRY_RETRIES:
                self.finish(c, "entry_" + (status or "error"), e.get("reason") or r.err or r.out)
            else:
                self.save()
            return
        trig = e.get("trigger") or {}
        c["trigger"] = {"at": trig.get("time"), "price": trig.get("price"), "triggered_at": e.get("triggered_at"),
                        "late_by_minutes": e.get("late_by_minutes"), "volume_x": trig.get("volume_x")}

        plan_path = self.home_path("plan.json")
        r = run("exit", "exit", ["plan", "--from", entry_file, "--write", plan_path, "--json"], sym)
        plan = r.data if r.ok else None
        if not plan:
            why = r.err or r.out
            # first seen over trigger_max_age_minutes after it fired: the runner was not running (Mac off/asleep)
            stale = "trigger_max_age_minutes" in (why or "")
            self.finish(c, "stale_trigger" if stale else "plan_refused", why)
            return
        ac.write_json(self.path("plans", sym + ".json"), plan)
        ch = plan.get("charges") or {}
        c["plan"] = {"entry": plan.get("entry"), "stop": plan.get("stop"), "target": plan.get("target"),
                     "worst_exit": plan.get("worst_exit"), "quantity": plan.get("quantity"),
                     "risk_per_share": plan.get("risk_per_share"),
                     "risk_per_share_worst": plan.get("risk_per_share_worst"),
                     "reward_risk": plan.get("reward_risk"), "flat_by": plan.get("flat_by"),
                     "max_loss": ch.get("max_loss_worst_with_charges"), "break_even": ch.get("break_even"),
                     "reward_risk_after_charges": ch.get("reward_risk_after_charges"),
                     "charges_r": shorts.charges_r_of_long(plan)}

        vetoed = self.vetoes()
        if sym in vetoed:
            c["veto_category"] = self.veto_categories().get(sym) or "OTHER"
            self.finish(c, "vetoed", "Claude's pre-market veto: %s" % (vetoed[sym] or "no reason given"))
            return
        mx, cr = self.charges_r_limit(), c["plan"]["charges_r"]
        if mx is not None:
            c["cost_ok"] = cr is None or cr <= mx
            if not c["cost_ok"]:
                self.finish(c, "cost_filter", "round-trip charges at the stop are %.2fR of the risk, over the "
                                              "%.2fR limit (cost_filter.max_charges_r)" % (cr, mx))
                return
        if self.accounts():
            self.trade_accounts(c, sym, plan_path)
        f = self.main_filter()           # the main account's own filter: after the other accounts have the signal
        if f:
            why = accounts.filter_reason(f, self.signal_rvol(c, sym))
            if why:
                self.finish(c, "filtered", why)
                return
        cap = self.cap_reason()
        if cap:
            self.finish(c, "shadow", cap)
            return

        tm_file = self.path("tm", sym + ".json")
        os.makedirs(os.path.dirname(tm_file), exist_ok=True)
        r = run("timemachine", "timemachine", ["run", "--plan", plan_path, "--json", "--write", tm_file], sym,
                timeout=600)
        c["tm"] = tm_summary(r.data) if r.ok and r.data else {"error": ac.tail(r.err or r.out, 300)}

        r = run("size", "size", ["--plan", plan_path, "--write", "--json"], sym)
        sizing = (r.data or {}).get("sizing") or {}
        if not r.ok:
            self.finish(c, "size_refused", "; ".join((r.data or {}).get("notes") or []) or r.err or r.out)
            return
        c["order"] = {"qty": sizing.get("qty"), "entry": sizing.get("entry"), "stop_limit": sizing.get("stop_limit"),
                      "bound_by": sizing.get("bound_by"), "cost": sizing.get("cost")}

        run("quote", "alerts", ["quote", sym, "--write", self.home_path("prices.json"), "--json"], sym)

        r = run("risk", "risk", ["--json"], sym)
        res = ((r.data or {}).get("results") or [{}])[0]
        c["risk"] = {"verdict": res.get("verdict"), "reasons": res.get("reasons") or [],
                     "warnings": res.get("warnings") or [], "max_loss": res.get("max_loss")}
        if res.get("verdict") != "PASS":
            self.finish(c, "risk_block", "; ".join(res.get("reasons") or []) or r.err or "no verdict")
            return

        r = run("approve", "ticket", ["autopilot-approve", "--index", "1"], sym, parse=False)
        if not r.ok:
            self.finish(c, "approve_refused", r.err or r.out)
            return
        r = run("paper_fill", "ticket", ["paper-fill", "--index", "1"], sym, parse=False)
        if not r.ok:
            self.finish(c, "fill_refused", r.err or r.out)
            return
        c["fill"] = ac.tail(r.out, 300)
        self.finish(c, "traded")

    def main_filter(self):
        """The main account's own filter (accounts.filters under its cap's name, "cap5" for 5 a day), or None."""
        cap = int(self.cfg.get("max_trades_per_day") or 0)
        return accounts.filter_of(self.settings, cap) if cap else None

    def daily_setups(self, sym):
        doc = ac.read_json(self.path("daily.json")) or {}
        for h in doc.get("hits") or []:
            if h.get("symbol") == sym:
                return h.get("setups") or []
        return []

    def cap_reason(self):
        """Why no more trades today (the trade cap or the loss limit), else None. nse-risk is the authority;
        this only saves running the Time Machine on a trade that cannot be taken."""
        state = ac.read_json(self.home_path("state", "broker.json")) or {}
        rows = [r for r in state.get("orders") or [] if isinstance(r, dict) and r.get("date") == self.day]
        gone = {r.get("fingerprint") for r in rows if r.get("status") == "cancelled" and r.get("fingerprint")}
        taken = sum(1 for r in rows if r.get("status") == "approved" and r.get("kind") == "entry"
                    and r.get("fingerprint") not in gone)    # an entry cancelled unfilled frees its slot
        cap = int(self.cfg.get("max_trades_per_day") or 0)
        if cap and taken >= cap:
            return "the day's cap of %d trades is used" % cap
        realised = sum(float(r.get("pnl_net") or 0) for r in rows if r.get("status") == "closed")
        limit = float(self.cfg.get("max_daily_loss_inr") or 0)
        if limit and realised <= -limit:
            return "the day's loss limit of %s is reached (%s realised)" % (nc.inr(limit), nc.inr(realised))
        return None

    # ------------------------------------------------------------ end of day

    def eod(self):
        """The end of the day; None if it failed. A failure is retried on the next ticks, at most
        EOD_ATTEMPTS times in all, so a bug can never mean a Claude run every minute."""
        ph = self.st["phases"]
        attempts = int((ph.get("eod") or {}).get("attempts") or 0) + 1
        ph["eod"] = {"started": self.stamp(), "attempts": attempts}
        self.save()
        try:
            return self._eod(ph)
        except Exception as e:
            ph["eod"]["error"] = "%s: %s" % (type(e).__name__, ac.tail(str(e), 300))
            self.runner.note("eod", error=ph["eod"]["error"], attempt=attempts)
            if attempts >= EOD_ATTEMPTS:
                ph["eod"]["done"] = self.stamp()          # give up for today: the error is in the state and log
                ac.notify(self.settings, "NSE autopilot: the report failed", ph["eod"]["error"])
            self.save()
            return None

    def _eod(self, ph):
        self.paper_check(force=True)
        self.paper_check_accounts(force=True)
        r = self.runner.run("journal", "ticket", ["journal", "--days", "7", "--json"])
        if r.data is not None:
            ac.write_json(self.path("journal.json"), r.data)
        r = self.runner.run("status", "ticket", ["status", "--json"])
        if r.data is not None:
            ac.write_json(self.path("status.json"), r.data)
        # a report caught up on a later day must not show that day's Nifty move
        index_move = self.index_move if self.day == self.ist.date().isoformat() else False
        rep = report.build(self.home, self.day, self.settings, self.cfg, bars=self.bars, index_move=index_move)
        ph["eod"]["report"] = rep["paths"]
        analysis = None
        claude = self.settings.get("claude") or {}
        if claude.get("enabled"):
            out = self.claude("eod", report.for_claude(self.home, self.day), self.settings,
                              self.path("claude-eod.json"))
            if out and out.get("ok") and out.get("text"):
                analysis = out["text"]
                ph["eod"]["analysis"] = "ok"
            else:
                ph["eod"]["analysis"] = "failed: %s" % (out or {}).get("error")
            self.runner.note("analysis", ok=bool(analysis), error=(out or {}).get("error"))
        else:
            ph["eod"]["analysis"] = "off"
        rep = report.write_outputs(self.home, self.day, rep["report"],
                                   analysis if analysis else (False if claude.get("enabled") else None))
        ph["eod"]["report"] = rep["paths"]
        m = self.settings.get("mail") or {}
        if m.get("enabled"):
            ok, why = self.send_mail(self.settings, rep["report"], rep["paths"], analysis)
            ph["eod"]["mail"] = "sent" if ok else "failed: %s" % why
            self.runner.note("mail", ok=ok, error=None if ok else why)
            if not ok:
                ac.notify(self.settings, "NSE autopilot: email failed", str(why))
        else:
            ph["eod"]["mail"] = "off"
        ac.notify(self.settings, "NSE autopilot (paper)", report.headline(rep["report"]))
        ph["eod"]["archive"] = self.archive()
        ph["eod"]["journal"] = self.journal()
        ph["eod"]["done"] = self.stamp()
        self.save()
        return rep

    def journal(self):
        """Rebuild the P&L journal (journal.py) after the report; like the archive, it can never fail the day."""
        if not (self.settings.get("journal") or {}).get("enabled"):
            return "off"
        try:
            import journal
            folder, n, note = journal.write_all(self.home, self.settings, self.cfg)
        except Exception as e:
            return "failed: %s: %s" % (type(e).__name__, ac.tail(str(e), 200))
        self.runner.note("pnl_journal", folder=folder, days=n, note=note)
        return "%d days in %s%s" % (n, folder, (" (%s)" % note) if note else "")

    def archive(self, plan=None, force=False):
        """Keep the day's bars (and fill any of the last 5 sessions that are missing). After the report and the
        email, and never able to fail them: the outcome is a line in the state."""
        a = self.settings.get("archive") or {}
        if not a.get("enabled") and not force:
            return "off"
        if not self.fetch:
            return "skipped: no market data (test or replay)"
        syms = self.universe_symbols() + [market.NIFTY, market.VIX]
        try:
            out = market.archive(self.home, syms, self.fetch, plan or market.daily_plan(),
                                 pause=float(a.get("pause_s") or 0.4))
        except Exception as e:
            return "failed: %s" % ac.tail(str(e), 200)
        self.runner.note("archive", **{k: out[k] for k in ("symbols", "requests", "written", "days")},
                         failed=len(out["failed"]))
        return "%d symbols, %d day files written, %d failed" % (out["symbols"], out["written"], len(out["failed"]))


# ---------------------------------------------------------------- pieces the tests reach directly

def tm_summary(doc):
    mc = doc.get("mc") or {}
    hist = doc.get("history") or {}
    verdict = doc.get("verdict") or ""
    return {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "p_neither": mc.get("p_neither"),
            "avg_R": mc.get("avg_R"), "avg_R_net": mc.get("avg_R_net"), "r_at_target": mc.get("r_at_target"),
            "history": {k: hist.get(k) for k in ("sessions", "triggered", "target", "stop", "close", "avg_R",
                                                 "avg_R_net")},
            "verdict": verdict, "category": verdict.split(":")[0].strip() if verdict else None}


def premarket_brief(day, syms, names, news, daily, stocks=None):
    """What Claude reads before the open: compact, so the prompt stays small. `stocks` is the deep scan's
    index.json: each stock's flags (results or a board meeting close, an ex-date, the F&O ban, insider selling,
    a pledge, a bulk deal, a big move), only for the stocks that have one."""
    items = []
    for i in (news or {}).get("items") or []:
        items.append({"ticker": i.get("ticker"), "time_ist": i.get("time_ist"), "kind": i.get("kind"),
                      "source": i.get("source"), "title": (i.get("title") or "")[:240], "tags": i.get("tags") or [],
                      "important": bool(i.get("important"))})
    items.sort(key=lambda x: (not x["important"], x.get("time_ist") or ""), reverse=False)
    return {"date": day, "universe": [{"symbol": s, "name": names.get(s)} for s in syms],
            "news_status": (news or {}).get("status") if news else "not run or failed",
            "news_failure": (news or {}).get("failure"),
            "news_items": items[:400], "results_calendar": ((news or {}).get("calendar") or [])[:100],
            "moves": ((news or {}).get("moves") or [])[:100],
            "daily_setups": [{"symbol": h.get("symbol"), "setups": h.get("setups")}
                             for h in (daily or {}).get("hits") or []],
            "stock_flags": {s: [f for f in (v.get("flags") or []) if not f.startswith("Could not read")]
                            for s, v in sorted(((stocks or {}).get("stocks") or {}).items())
                            if [f for f in (v.get("flags") or []) if not f.startswith("Could not read")]},
            "deep_scan": "ok" if (stocks or {}).get("stocks") else "not run or failed"}


def plain_reason(text):
    """A script's refusal without its file paths ("exit.py: /Users/.../RELIANCE.json: the trigger is ...")."""
    text = re.sub(r"(?:~|/)[^\n]*?\.(?:json|gz|csv|txt)(?::\s*|\b)", "", str(text or ""))
    return " ".join(text.split())


RANK_MAX_AGE_DAYS = 4        # a Friday evening's ranks still describe Monday; a week-old file does not


def rank_tag(path, sym, today):
    """"top half" / "bottom half" from a rank file {"as_of": "YYYY-MM-DD", "ranks": {SYMBOL: 0..1}} (1 = best),
    "stale" when it is over RANK_MAX_AGE_DAYS old, None without a file or without the symbol."""
    if not path:
        return None
    doc = ac.read_json(os.path.expanduser(str(path)))
    if not isinstance(doc, dict):
        return None
    v = (doc.get("ranks") or {}).get(sym)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    try:
        age = (today - datetime.date.fromisoformat(str(doc.get("as_of")))).days
    except ValueError:
        return "stale"
    if age > RANK_MAX_AGE_DAYS:
        return "stale"
    return "top half" if v >= 0.5 else "bottom half"


VETO_CATEGORIES = ("RESULTS", "CORPORATE_ACTION", "NEGATIVE_NEWS", "REGULATORY_OR_BAN", "OTHER")


def clean_vetoes(raw, universe):
    allowed = set(universe)
    out, seen = [], set()
    for v in raw or []:
        if not isinstance(v, dict):
            continue
        sym = nc.nse_symbol(v.get("symbol") or "")
        if sym and sym in allowed and sym not in seen:
            seen.add(sym)
            cat = re.sub(r"[^A-Z_]", "", str(v.get("category") or "").upper().replace(" ", "_"))
            out.append({"symbol": sym, "reason": str(v.get("reason") or "")[:300],
                        "category": cat if cat in VETO_CATEGORIES else "OTHER"})
    return out


# ---------------------------------------------------------------- commands

class Lock(object):
    def __init__(self, home):
        self.path = os.path.join(home, "autopilot", ".lock")
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:                # pragma: no cover
            return True
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


def setup(a):
    home = ac.trader_home()
    settings = ac.load_settings(home)
    cfg = ac.load_cfg(home)
    problem = ac.guard(cfg)
    if problem:
        raise ac.AutopilotError(problem)
    return home, settings, cfg


def unfinished_day(home, today):
    """The latest earlier day whose report never went out (the Mac was off at 15:35), or None."""
    root = os.path.join(home, "autopilot")
    if not os.path.isdir(root):
        return None
    for d in sorted(os.listdir(root), reverse=True):
        if len(d) == 10 and d < today:
            st = ac.load_day(home, d)
            ph = st["phases"]
            if ph.get("premarket") and not (ph.get("eod") or {}).get("done"):
                return d
            return None
    return None


def cmd_tick(a):
    home, settings, cfg = setup(a)
    now = ac.parse_clock(a.now)
    with Lock(home) as got:
        if not got:
            print("another tick is running; nothing to do")
            return 0
        today = nc.to_ist(now).date().isoformat()
        late = unfinished_day(home, today)
        if late:
            Day(home, now, settings, cfg, day=late).eod()
        return Day(home, now, settings, cfg).tick()


def cmd_eod(a):
    home, settings, cfg = setup(a)
    now = ac.parse_clock(a.now)
    day = a.date or nc.to_ist(now).date().isoformat()
    rep = Day(home, now, settings, cfg, day=day).eod()
    if rep is None:
        st = ac.load_day(home, day)
        print("the report for %s failed: %s" % (day, (st["phases"].get("eod") or {}).get("error")), file=sys.stderr)
        return 1
    print(report.headline(rep["report"]))
    for k, v in sorted(rep["paths"].items()):
        print("  %s: %s" % (k, v))
    return 0


def cmd_replay(a):
    if not nc.test_mode():
        raise ac.AutopilotError("replay pins the clock, so it needs NSE_TEST_MODE=1 (and \"offline\" in "
                                "autopilot.json for the data)")
    home, settings, cfg = setup(a)
    times = [t.strip() for t in a.times.split(",") if t.strip()]
    for t in times:
        now = nc.parse_now("%s %s" % (a.date, t))
        Day(home, now, settings, cfg).tick()
        print("tick %s %s done" % (a.date, t))
    return 0


def cmd_archive(a):
    """Save bars into archive/ now: the last 5 sessions, or with --backfill everything Yahoo still has (60 days
    of 5-minute bars, 28 days of 1-minute bars). Safe to repeat: a day is only rewritten by a fuller copy."""
    home, settings, cfg = setup(a)
    now = ac.parse_clock(None)
    with Lock(home) as got:
        if not got:
            print("a tick is running; try again in a minute", file=sys.stderr)
            return 1
        d = Day(home, now, settings, cfg)
        plan = market.backfill_plan(now) if a.backfill else market.daily_plan()
        print("archiving %s ..." % ("everything Yahoo still has" if a.backfill else "the last 5 sessions"))
        print(d.archive(plan=plan, force=True))
    print("in %s" % os.path.join(home, "archive"))
    return 0


def cmd_status(a):
    home = ac.trader_home()
    cfg = ac.load_cfg(home)
    now = ac.parse_clock(a.now)
    day = a.date or nc.to_ist(now).date().isoformat()
    st = ac.load_day(home, day)
    problem = ac.guard(cfg)
    print("NSE autopilot (paper only), %s, trader folder %s" % (day, home))
    print("  config: %s" % ("OK: paper, autopilot on" if not problem else "OFF: " + problem))
    ph = st["phases"]
    if ph.get("closed"):
        print("  market closed: %s" % ph["closed"])
    for k in ("premarket", "eod"):
        if ph.get(k):
            print("  %s: %s" % (k, json.dumps(ph[k], ensure_ascii=False)))
    scans = ph.get("orb_scans") or []
    if scans:
        print("  ORB scans: %d (last bar %s), %d failed" % (len(scans), ph.get("orb_last_bar"),
                                                            sum(1 for s in scans if s.get("failed"))))
    for sym, c in sorted(st["candidates"].items(), key=lambda kv: kv[1].get("first_seen") or ""):
        print("  %-11s %-15s %s" % (sym, c.get("outcome") or "pending", c.get("reason") or ""))
    paper = ac.read_json(os.path.join(home, "state", "paper.json")) or {}
    for t in paper.get("trades") or []:
        if t.get("date") == day:
            print("  paper %s %s %s qty %s fill %s exit %s %s net %s" % (
                t.get("id"), t.get("status"), t.get("symbol"), t.get("qty"), t.get("fill_price"),
                t.get("exit_price"), t.get("exit_reason") or "", t.get("pnl_net")))
    settings = ac.load_settings(home)
    if accounts.caps_of(settings):
        for s in accounts.all_summaries(home, day, settings, cfg):
            print("  account %-30s orders %d, open %d, closed %d (%d won), net %s, cash %s" % (
                s["label"], s["orders"], s["still_open"], s["closed"], s["wins"], s["net"], s.get("cash")))
    return 0


def cmd_progress(a):
    import progress
    home = ac.trader_home()
    return progress.run(home, ac.load_settings(home), ac.load_cfg(home), ac.parse_clock(a.now), show=a.show,
                        force=a.send_now)


def cmd_journal(a):
    import journal
    home = ac.trader_home()
    return journal.run(home, ac.load_settings(home), ac.load_cfg(home), a.show_day)


def cmd_selftest(a):
    home = ac.trader_home()
    ok = True
    print("NSE autopilot selftest, trader folder %s" % home)
    print("  python %s at %s" % (sys.version.split()[0], sys.executable))
    try:
        settings = ac.load_settings(home)
        cfg = ac.load_cfg(home)
    except (ac.AutopilotError, ValueError, OSError) as e:
        print("  FAIL config: %s" % e)
        return 1
    problem = ac.guard(cfg)
    print("  %s config: %s" % ("FAIL" if problem else "OK", problem or "paper, autopilot on"))
    ok = ok and not problem
    st = nc.market_status(nc.utc_now(), cfg)
    print("  OK clock: %s IST, NSE %s, next open %s" % (st["now_ist"], st["phase"], st["next_open"]))
    if (settings.get("claude") or {}).get("enabled"):
        good, why = claude_run.selftest(settings)
        print("  %s claude: %s" % ("OK" if good else "FAIL", why))
        ok = ok and good
    else:
        print("  -- claude: off in autopilot.json")
    if (settings.get("mail") or {}).get("enabled"):
        good, why = mail.send_test(settings) if a.send_mail else mail.check(settings)
        print("  %s mail: %s" % ("OK" if good else "FAIL", why))
        ok = ok and good
    else:
        print("  -- mail: off in autopilot.json")
    ac.notify(settings, "NSE autopilot", "selftest %s" % ("passed" if ok else "FAILED"))
    print("selftest %s" % ("passed" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="The day-trader-india paper autopilot (paper only).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tick", help="do whatever is due now (launchd runs this every minute)")
    p.add_argument("--now", help="test only (NSE_TEST_MODE=1): an IST time like \"2026-11-13 10:05\"")
    p = sub.add_parser("eod", help="build (or rebuild) a day's report, ask Claude, send the email")
    p.add_argument("--date", help="YYYY-MM-DD (default today, IST)")
    p.add_argument("--now", help="test only")
    p = sub.add_parser("replay", help="test only: tick at the given IST times of a past day, on offline data")
    p.add_argument("--date", required=True)
    p.add_argument("--times", required=True, help="comma separated, like 08:45,10:05,15:35")
    p = sub.add_parser("status", help="a day's state in plain words")
    p.add_argument("--date")
    p.add_argument("--now", help="test only")
    p = sub.add_parser("selftest", help="check python, the config, headless Claude and Gmail")
    p.add_argument("--send-mail", action="store_true", help="also send a test email")
    p = sub.add_parser("progress", help="the half-hourly progress email (launchd runs this every minute)")
    p.add_argument("--show", action="store_true", help="print the mail that would go out now; send nothing")
    p.add_argument("--send-now", action="store_true", help="send an update now, due or not")
    p.add_argument("--now", help="test only")
    p = sub.add_parser("journal", help="rebuild the P&L journal (every trade, what it made or lost, and why)")
    p.add_argument("--print", dest="show_day", metavar="DAY", help="print one day's write-up instead")
    p = sub.add_parser("archive", help="save the last 5 sessions' 1- and 5-minute bars into archive/ now")
    p.add_argument("--backfill", action="store_true",
                   help="everything Yahoo still has: 60 days of 5-minute bars, 28 days of 1-minute bars")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"tick": cmd_tick, "eod": cmd_eod, "replay": cmd_replay, "status": cmd_status,
                "selftest": cmd_selftest, "archive": cmd_archive, "progress": cmd_progress,
                "journal": cmd_journal}[a.cmd](a)
    except (ac.AutopilotError, ValueError, OSError) as e:
        print("run_day.py: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
