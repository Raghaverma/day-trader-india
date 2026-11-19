#!/usr/bin/env python3
"""The paper autopilot's progress email: every half hour on a trading day, what the autopilot is doing, the
signals, every paper trade and the money (made, lost, still open). Paper only. Standard library only.

launchd runs `run.sh progress` every minute (com.nse-autopilot-progress) and this decides whether a mail is due:

  premarket_from + 2 min (08:42)   it will run today (or, on a closed day, why not) and that the first tick ran
  09:00, 09:30 ... 15:30           the update
  16:00                            only if the 15:35 report's email has not gone out: the day's numbers anyway

It never takes the tick's lock and only reads the day's files, so it still reports when a tick is stuck. What it
sent lives in $NSE_TRADER_HOME/autopilot/<date>/progress.json. Settings, all optional, in autopilot.json:

  "progress_mail": {"enabled": true, "every_min": 30, "to": null (= mail.to), "grace_min": 20, "wrapup_at": "16:00"}

    python3 progress.py              # send whatever is due now
    python3 progress.py --show       # print the mail that would go out now; send nothing
    python3 progress.py --send-now   # send the update now, due or not
"""

import argparse
import collections
import datetime
import os
import subprocess
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402
import mail  # noqa: E402
import market  # noqa: E402

nc = ac.nc

DEFAULTS = {"enabled": True, "every_min": 30, "to": None, "grace_min": 20, "wrapup_at": "16:00"}
START_DELAY_MIN = 2          # the first mail waits for the first tick, so it can say the tick ran
MAX_ATTEMPTS = 3             # a mail that keeps failing is tried on 3 ticks, then given up (and logged)
STALE_TICK_MIN = 5           # no tick for this long in the working day = a warning in the mail
ORB_FIRST_SCAN = "09:36"

OUTCOMES = collections.OrderedDict([
    ("traded", "traded (paper)"),
    ("shadow", "planned, not traded: the day's trade cap or loss limit (the report replays it)"),
    ("vetoed", "vetoed by Claude's pre-market check"),
    ("cost_filter", "skipped: charges too big against the risk"),
    ("risk_block", "blocked by the risk checks"),
    ("entry_expired", "breakout faded before nse-entry confirmed it"),
    ("entry_waiting", "breakout not confirmed by nse-entry"),
    ("entry_error", "nse-entry could not read it"),
    ("stale_trigger", "seen too late after it fired"),
    ("plan_refused", "no valid stop/target plan"),
    ("size_refused", "could not be sized inside the limits"),
    ("approve_refused", "the paper approval refused it"),
    ("fill_refused", "the paper fill refused it"),
    ("short_shadow", "short breakdown, shadow only (never traded)"),
    ("short_refused", "short breakdown, no valid plan (shadow)"),
    ("stale_trigger_short", "short breakdown seen too late"),
    ("error", "error while processing"),
    ("pending", "still being checked"),
])
EXIT_WORDS = {"stop": "STOPPED OUT", "target": "TARGET HIT", "flat_by": "SQUARED OFF (flat by)",
              "cancelled": "CANCELLED", "sold": "SOLD"}
INFRA_STEPS = ("holidays", "universe", "scan_daily", "news", "scan_orb", "paper_check", "journal", "status")


def cfg_of(settings):
    p = dict(DEFAULTS)
    p.update(settings.get("progress_mail") or {})
    return p


def mins(text):
    t = nc.parse_hhmm(text)
    return t.hour * 60 + t.minute


def hm(m):
    return "%02d:%02d" % divmod(int(m), 60)


def ist_hm(stamp):
    """HH:MM IST from a state stamp like 2026-11-19T03:12:00Z (or '' if it cannot be read)."""
    if not stamp:
        return ""
    try:
        t = datetime.datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return str(stamp)
    return (t + datetime.timedelta(hours=5, minutes=30)).strftime("%H:%M")


def money(x):
    return nc.inr(x or 0, 0)


def signed(x):
    x = float(x or 0)
    return ("+" if x > 0 else "") + nc.inr(x, 0)


def px(x):
    return "-" if x is None else nc.inr(x, 2)


# ---------------------------------------------------------------- when a mail is due

def slots(settings, pm):
    """[(kind, minute)] for an open day: the start, then every every_min up to (not at) the end of day."""
    start = mins(settings["premarket_from"])
    end = mins(settings["eod_at"])
    every = max(5, int(pm.get("every_min") or 30))
    out = [("start", start + START_DELAY_MIN)]
    m = start - start % every + every
    while m < end:
        if m > start + START_DELAY_MIN:
            out.append(("update", m))
        m += every
    return out


def due(now_m, settings, pm, sent, closed, eod):
    """(kind, key) of the mail due now, or None."""
    def open_for(key):
        s = sent.get(key) or {}
        return not s.get("ok") and int(s.get("attempts") or 0) < MAX_ATTEMPTS

    start = mins(settings["premarket_from"])
    if closed:
        if start + START_DELAY_MIN <= now_m < mins(settings["eod_at"]) and open_for("closed"):
            return "closed", "closed"
        return None
    wrap = mins(pm.get("wrapup_at") or "16:00")
    if wrap <= now_m <= wrap + 120:
        # the 15:35 report's own email is the end-of-day mail; this one only stands in when it did not go out
        # (once the end of day is finished, or 30 minutes past wrapup_at if it is still running)
        settled = eod.get("done") or now_m >= wrap + 30
        if eod.get("mail") != "sent" and settled and open_for("wrapup"):
            return "wrapup", "wrapup"
        return None
    past = [s for s in slots(settings, pm) if s[1] <= now_m]
    if not past:
        return None
    kind, m = past[-1]
    if now_m - m > int(pm.get("grace_min") or 20) or now_m >= mins(settings["eod_at"]):
        return None
    key = hm(m)
    return (kind, key) if open_for(key) else None


# ---------------------------------------------------------------- what is true now

def launchd_job(label):
    """{'loaded': bool, 'pid': int|None, 'last_exit': int|None}."""
    try:
        p = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": None, "pid": None, "last_exit": None}
    if p.returncode != 0:
        return {"loaded": False, "pid": None, "last_exit": None}
    out = {"loaded": True, "pid": None, "last_exit": None}
    for line in p.stdout.splitlines():
        line = line.strip().rstrip(";")
        for key, name in (('"PID" = ', "pid"), ('"LastExitStatus" = ', "last_exit")):
            if line.startswith(key):
                try:
                    out[name] = int(line[len(key):])
                except ValueError:
                    pass
    return out


def tick_running():
    try:
        p = subprocess.run(["pgrep", "-f", "run_day.py tick"], capture_output=True, text=True, timeout=10)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def last_price(fetch, sym):
    """(price, 'HH:MM') from Yahoo 1-minute bars, or (None, why)."""
    if not fetch:
        return None, "no market data"
    try:
        rows = market.rows(fetch(nc.yahoo_symbol(sym), "1m", rng="1d"))
    except Exception as e:
        return None, ac.tail(str(e), 120)
    if not rows:
        return None, "no bars yet"
    t, _o, _h, _l, c, _v = rows[-1]
    at = datetime.datetime.fromtimestamp(t, datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    return c, at.strftime("%H:%M")


def snapshot(home, day, settings, cfg, now, fetch=None, jobs=None, running=None):
    """Everything the mail says, as plain data (tests pass jobs/running/fetch)."""
    st = ac.load_day(home, day)
    ph = st["phases"]
    ist = nc.to_ist(now)
    status = nc.market_status(now, cfg)
    paper = ac.read_json(os.path.join(home, "state", "paper.json")) or {}
    all_trades = paper.get("trades") or []
    today = [t for t in all_trades if t.get("date") == day]
    trades, made, lost, gmade, glost, charges, realised, opened = [], 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    for t in today:
        row = dict(t)
        if t.get("status") == "closed":
            net, gross = float(t.get("pnl_net") or 0), float(t.get("gross") or 0)
            realised += net
            charges += float(t.get("charges") or 0)
            if net > 0:
                made += net
            elif net < 0:
                lost += net
            if gross > 0:
                gmade += gross
            elif gross < 0:
                glost += gross
        elif t.get("status") == "filled":
            last, at = last_price(fetch, t["symbol"])
            row["last"], row["last_at"] = last, at
            if last is not None:
                g = round(t["qty"] * (last - t["fill_price"]), 2)
                ch = nc.intraday_charges(t["qty"], t["fill_price"], last, cfg)["total"]
                row["open_gross"], row["open_net"] = g, round(g - ch, 2)
                opened += row["open_net"]
        trades.append(row)
    closed_all = [t for t in all_trades if t.get("status") == "closed"]
    events = ac.read_log(home, day)
    problems = []
    for e in events:
        if e.get("step") in INFRA_STEPS and e.get("code") not in (0, 1):
            problems.append("%s %s: exit %s %s" % (ist_hm(e.get("at")), e.get("step"), e.get("code"),
                                                   ac.tail(e.get("stderr") or e.get("stdout") or "", 160)))
        elif e.get("error"):
            problems.append("%s %s: %s" % (ist_hm(e.get("at")), e.get("step"), ac.tail(str(e["error"]), 160)))
    cands = sorted(st["candidates"].values(), key=lambda c: (c.get("first_seen") or "", c.get("symbol") or ""))
    jobs = jobs if jobs is not None else {"tick": launchd_job("com.nse-autopilot"),
                                          "awake": launchd_job("com.keep-awake")}
    import accounts
    try:
        accs = accounts.all_summaries(home, day, settings, cfg) if accounts.caps_of(settings) else []
    except Exception:
        accs = []
    return {
        "accounts": accs,
        "day": day, "now": ist.strftime("%H:%M"), "now_m": ist.hour * 60 + ist.minute,
        "weekday": ist.strftime("%A %d %b %Y"), "status": status, "guard": ac.guard(cfg), "phases": ph,
        "ticks": st["ticks"], "candidates": cands, "trades": trades, "made": made, "lost": lost,
        "gross_made": gmade, "gross_lost": glost, "charges": charges, "realised": realised, "open_net": opened,
        "closed": [t for t in today if t.get("status") == "closed"],
        "filled": [t for t in today if t.get("status") == "filled"],
        "resting": [t for t in today if t.get("status") == "open"],
        "since": min((t.get("date") for t in all_trades if t.get("date")), default=None),
        "all_closed": len(closed_all), "all_net": sum(float(t.get("pnl_net") or 0) for t in closed_all),
        "all_wins": sum(1 for t in closed_all if float(t.get("pnl_net") or 0) > 0),
        "all_losses": sum(1 for t in closed_all if float(t.get("pnl_net") or 0) < 0),
        "vetoes": ac.read_json(os.path.join(ac.day_dir(home, day), "vetoes.json")) or {},
        "daily": ac.read_json(os.path.join(ac.day_dir(home, day), "daily.json")) or {},
        "problems": problems, "jobs": jobs,
        "tick_running": running if running is not None else tick_running(),
    }


# ---------------------------------------------------------------- the words

def doing_now(s, settings, cfg):
    ph, m = s["phases"], s["now_m"]
    pre, eod = ph.get("premarket") or {}, ph.get("eod") or {}
    if s["status"]["phase"] == "closed-day":
        return "NSE is closed today (%s). The autopilot does not trade today." % (s["status"].get("reason") or "closed")
    if s["guard"]:
        return "The autopilot is OFF and will NOT trade: %s." % s["guard"]
    if m < mins(settings["premarket_from"]):
        return "Waiting: the pre-market starts at %s." % settings["premarket_from"]
    if not pre:
        return "The pre-market was due at %s and has NOT started yet." % settings["premarket_from"]
    if not pre.get("done"):
        return ("Pre-market running since %s IST: the Nifty 50 list, the daily chart scan, the news, then "
                "Claude's veto (by %s)." % (ist_hm(pre.get("started")), settings["veto_by"]))
    if m < mins(ORB_FIRST_SCAN):
        return ("Pre-market done at %s IST. The opening range is 09:15-09:30; the first breakout scan runs at %s "
                "and entries are allowed 09:30-14:30." % (ist_hm(pre.get("done")), ORB_FIRST_SCAN))
    scans = ph.get("orb_scans") or []
    if m <= mins(settings["orb_until"]) and not eod:
        return ("Scanning: every 5-minute bar of the Nifty 50 is checked for opening-range breakouts; each hit goes "
                "entry -> stop/target -> Time Machine -> size -> risk check -> paper order. %d scans so far, "
                "last bar %s." % (len(scans), ph.get("orb_last_bar") or "none yet"))
    if not eod:
        return ("Entry window closed (14:30): no new trades. Open trades are watched every minute until flat by "
                "%s; the report and email at %s." % (cfg.get("flat_by") or "15:10", settings["eod_at"]))
    if not eod.get("done"):
        return "End of day running since %s IST (attempt %s)." % (ist_hm(eod.get("started")), eod.get("attempts"))
    return ("Day finished at %s IST. Report email: %s. Claude's analysis: %s."
            % (ist_hm(eod.get("done")), eod.get("mail") or "?", eod.get("analysis") or "?"))


def health(s, settings):
    out = []
    tick, awake = s["jobs"].get("tick") or {}, s["jobs"].get("awake") or {}
    if tick.get("loaded") is False:
        out.append("!! The autopilot's launchd job (com.nse-autopilot) is NOT loaded: nothing will run.")
    elif tick.get("loaded"):
        out.append("Autopilot job: loaded, runs every minute%s." % (
            "" if not tick.get("last_exit") else " (last exit status %s)" % tick["last_exit"]))
    if awake.get("pid"):
        out.append("Mac: kept awake by caffeinate (pid %s); the display sleeps as normal." % awake["pid"])
    else:
        out.append("!! The keep-awake job (com.keep-awake) is not running: the Mac could sleep.")
    m, active = s["now_m"], s["status"]["phase"] != "closed-day" and not s["guard"]
    done = (s["phases"].get("eod") or {}).get("done")
    if active and m >= mins(settings["premarket_from"]) + STALE_TICK_MIN and not done:
        last = s["ticks"][-1] if s["ticks"] else None
        if s["tick_running"]:
            out.append("A tick is running right now%s." % ((" (started %s)" % last) if last else ""))
        elif not last:
            out.append("!! No tick has run today yet.")
        elif m - mins(last) > STALE_TICK_MIN:
            out.append("!! The last tick was at %s, %d minutes ago: the autopilot may be stuck or the Mac slept."
                       % (last, m - mins(last)))
        else:
            out.append("Last tick %s IST (%d today)." % (last, len(s["ticks"])))
    return out


def premarket_lines(s):
    pre = s["phases"].get("premarket") or {}
    if not pre:
        return ["Not run yet."]
    out = []
    reg = pre.get("regime") or {}
    bits = ["%d stocks in the universe" % (pre.get("universe") or 0)]
    if reg.get("nifty_trend"):
        bits.append("Nifty trend %s" % reg["nifty_trend"])
    if reg.get("vix_band"):
        bits.append("India VIX %s" % reg["vix_band"])
    out.append(", ".join(bits) + ".")
    hits = s["daily"].get("hits") or []
    if hits:
        out.append("Daily chart scan: %d stocks with a setup (%s%s)." % (
            len(hits), ", ".join(h.get("symbol", "?") for h in hits[:8]), ", ..." if len(hits) > 8 else ""))
    news = pre.get("news") or {}
    if news:
        out.append("News: %s items read%s." % (news.get("items"), "" if news.get("code") in (0, None) else
                                               " (the news step exited %s)" % news.get("code")))
    out.append("Claude's veto: %s." % (pre.get("veto") or "pending"))
    v = s["vetoes"]
    for x in v.get("vetoes") or []:
        out.append("  - vetoed %s: %s" % (x.get("symbol"), x.get("reason") or "no reason given"))
    if v.get("summary"):
        out.append("  Claude: %s" % ac.tail(str(v["summary"]), 500))
    return out


def trade_lines(s, cfg):
    if not s["trades"]:
        return ["No paper trades yet today."]
    out = []
    for i, t in enumerate(s["trades"], 1):
        stop = (t.get("stop") or {}).get("trigger")
        head = "%d. %s, %s shares" % (i, t.get("symbol"), t.get("qty"))
        if t.get("fill_price") is not None:
            head += ", bought at %s (%s)" % (px(t["fill_price"]), (t.get("fill_time") or "")[11:16])
        else:
            head += ", LIMIT buy at %s" % px(t.get("limit"))
        head += ", stop %s, target %s" % (px(stop), px(t.get("target")))
        st = t.get("status")
        if st == "closed":
            tail = "%s at %s (%s): gross %s, charges %s, net %s" % (
                EXIT_WORDS.get(t.get("exit_reason"), str(t.get("exit_reason") or "closed").upper()),
                px(t.get("exit_price")), (t.get("exit_time") or "")[11:16], signed(t.get("gross")),
                money(t.get("charges")), signed(t.get("pnl_net")))
        elif st == "filled":
            if t.get("last") is not None:
                tail = "OPEN: last %s (%s): %s gross, %s after charges if sold now" % (
                    px(t["last"]), t.get("last_at"), signed(t.get("open_gross")), signed(t.get("open_net")))
            else:
                tail = "OPEN (no live price: %s)" % t.get("last_at")
            if t.get("stop_gap"):
                tail += " !! the stop triggered but did not fill since %s" % t["stop_gap"][11:16]
        elif st == "open":
            tail = "resting, not filled yet (cancelled if not filled by %s)" % (t.get("flat_by") or cfg.get("flat_by"))
        else:
            tail = (st or "?").upper()
        out.append("%s\n   -> %s" % (head, tail))
    return out


def signal_lines(s):
    ph = s["phases"]
    scans = ph.get("orb_scans") or []
    out = []
    if scans:
        failed = sum(1 for x in scans if x.get("failed"))
        out.append("Breakout scans: %d (last bar %s), %d bar%s with hits%s." % (
            len(scans), ph.get("orb_last_bar"), sum(1 for x in scans if x.get("hits")),
            "" if sum(1 for x in scans if x.get("hits")) == 1 else "s",
            ", !! %d scan%s FAILED" % (failed, "" if failed == 1 else "s") if failed else ""))
    else:
        out.append("Breakout scans: none yet (they start at %s)." % ORB_FIRST_SCAN)
    longs = [c for c in s["candidates"] if c.get("side") != "short"]
    shorts = [c for c in s["candidates"] if c.get("side") == "short"]
    if not longs and not shorts:
        out.append("Signals: none yet.")
        return out
    count = collections.Counter(c.get("outcome") or "pending" for c in longs)
    if longs:
        out.append("Long signals: %d (%s)." % (len(longs), ", ".join(
            "%d %s" % (n, k.replace("_", " ")) for k, n in count.most_common())))
    for c in longs:
        o = c.get("outcome") or "pending"
        line = "  %s %s: %s" % (c.get("first_seen") or "--:--", c.get("symbol"), OUTCOMES.get(o, o))
        if c.get("reason") and o != "traded":
            line += " (%s)" % ac.tail(c["reason"], 200)
        out.append(line)
    if shorts:
        out.append("Short breakdowns, shadow only, never traded: %d (%s)." % (
            len(shorts), ", ".join(c.get("symbol", "?") for c in shorts[:12])))
    return out


def money_lines(s, cfg):
    closed, filled = len(s["closed"]), len(s["filled"])
    out = [
        "Made (winning trades, after charges): %s" % signed(s["made"]),
        "Lost (losing trades, after charges):  %s" % signed(s["lost"]),
        "Charges paid on closed trades:        %s" % money(s["charges"]),
        "Realised today, net:                  %s  (%d closed trade%s)" % (
            signed(s["realised"]), closed, "" if closed == 1 else "s"),
    ]
    if filled:
        out.append("Open trades, if sold now, net:        %s  (%d open)" % (signed(s["open_net"]), filled))
        out.append("Today in all, net:                    %s" % signed(s["realised"] + s["open_net"]))
    out.append("Gross before charges: %s won, %s lost." % (signed(s["gross_made"]), signed(s["gross_lost"])))
    cap = int(cfg.get("max_trades_per_day") or 0)
    limit = float(cfg.get("max_daily_loss_inr") or 0)
    taken = len([t for t in s["trades"] if t.get("status") != "cancelled"])
    out.append("Limits: %d of %d trades used today; daily loss limit %s; max loss per trade %s; budget %s." % (
        taken, cap, money(limit), money(cfg.get("max_loss_per_trade_inr")), money(cfg.get("budget"))))
    if s["all_closed"]:
        out.append("Paper account since %s: %d closed trades (%d won, %d lost), net %s." % (
            s["since"], s["all_closed"], s["all_wins"], s["all_losses"], signed(s["all_net"])))
    return out


def account_lines(s):
    out = []
    for a in s.get("accounts") or []:
        used = a["orders"]
        out.append("%-34s %d of %d trades used, %d open, %d closed (%d won, %d lost), net %s" % (
            a["label"] + ":", used, a["cap"], a["still_open"], a["closed"], a["wins"], a["losses"], signed(a["net"])))
    return out


def subject(kind, s):
    tag = "[PAPER] NSE autopilot"
    if kind == "closed":
        return "%s: market closed today (%s), not trading" % (tag, s["status"].get("reason") or "closed")
    if s["guard"]:
        return "%s: OFF today, will NOT trade" % tag
    if kind == "start":
        return "%s: trading day %s, running" % (tag, s["day"])
    n_open = len(s["filled"])
    bits = ["%s net today" % signed(s["realised"] + s["open_net"]),
            "%d trade%s closed" % (len(s["closed"]), "" if len(s["closed"]) == 1 else "s")]
    if n_open:
        bits.append("%d open" % n_open)
    head = "end of day %s" % s["day"] if kind == "wrapup" else "%s update" % s["now"]
    warn = " !!" if any(h.startswith("!!") for h in s.get("_health") or []) or s["problems"] else ""
    return "%s %s%s: %s" % (tag, head, warn, ", ".join(bits))


def body(kind, s, settings, cfg, pm):
    s["_health"] = health(s, settings)
    lines = ["NSE paper autopilot, %s, %s IST" % (s["weekday"], s["now"]), ""]
    if kind == "closed":
        lines += [doing_now(s, settings, cfg), "Next trading day: %s IST." % s["status"].get("next_open"), ""]
        lines += s["_health"]
        lines += ["", "Paper only: no broker, no real money. Not financial advice."]
        return "\n".join(lines)
    if kind == "start":
        will = "WILL NOT run" if s["guard"] or (s["jobs"].get("tick") or {}).get("loaded") is False else "WILL run"
        lines += ["Today is a trading day (NSE %s-%s). The autopilot %s today." % (
            s["status"].get("opens"), s["status"].get("closes"), will),
            "Plan: pre-market %s (scan, news, Claude's veto by %s); breakout scans %s-%s; entries 09:30-14:30; "
            "every trade flat by %s; full report with Claude's analysis emailed at %s. Updates every %s minutes." % (
                settings["premarket_from"], settings["veto_by"], ORB_FIRST_SCAN, settings["orb_until"],
                cfg.get("flat_by") or "15:10", settings["eod_at"], pm.get("every_min")), ""]
    if kind == "wrapup":
        eod = s["phases"].get("eod") or {}
        lines += ["The %s report's email did not go out (end of day: %s, email: %s), so here are the day's "
                  "numbers." % (settings["eod_at"], "done" if eod.get("done") else "not finished",
                                eod.get("mail") or "not sent"), ""]
    lines += ["WHAT IT IS DOING NOW", doing_now(s, settings, cfg)] + s["_health"] + [""]
    lines += ["MONEY TODAY (paper)"] + money_lines(s, cfg) + [""]
    if s.get("accounts"):
        lines += ["PAPER ACCOUNTS (realised today, after charges)"] + account_lines(s) + [""]
    lines += ["TRADES"] + trade_lines(s, cfg) + [""]
    lines += ["SIGNALS"] + signal_lines(s) + [""]
    lines += ["PRE-MARKET"] + premarket_lines(s) + [""]
    if s["problems"]:
        lines += ["PROBLEMS"] + ["  " + p for p in s["problems"][-10:]] + [""]
    if kind != "wrapup":
        nxt = [m for _k, m in slots(settings, pm) if m > s["now_m"]]
        lines.append("Next update %s IST; the full report at %s." % (hm(nxt[0]), settings["eod_at"]) if nxt
                     else "The full report comes at %s IST." % settings["eod_at"])
    lines += ["Paper only: no broker, no real money. Yahoo prices are unofficial and can lag. Not financial advice."]
    return "\n".join(lines)


# ---------------------------------------------------------------- sending

def send(settings, pm, subj, text, smtp=None, lookup=None):
    m = mail._cfg(settings)
    if pm.get("to"):
        m["to"] = [pm["to"]] if isinstance(pm["to"], str) else list(pm["to"])
    bad = mail.problems(m)
    if bad:
        return False, "; ".join(bad)
    msg = EmailMessage()
    msg["Subject"] = subj
    msg["From"] = m["from"]
    msg["To"] = ", ".join(m["to"])
    msg.set_content(text)
    return mail.deliver(m, msg, smtp, lookup)


def run(home, settings, cfg, now, show=False, force=False, sender=None, fetch=None, jobs=None, running=None):
    """0 always for launchd (a failure is logged and retried); prints only when something happened."""
    pm = cfg_of(settings)
    if not pm.get("enabled") and not (show or force):
        return 0
    ist = nc.to_ist(now)
    day = ist.date().isoformat()
    now_m = ist.hour * 60 + ist.minute
    path = os.path.join(ac.day_dir(home, day), "progress.json")
    sent = ac.read_json(path) or {}
    closed = nc.market_status(now, cfg)["phase"] == "closed-day"
    eod = ac.load_day(home, day)["phases"].get("eod") or {}
    if force or show:
        kind, key = ("closed" if closed else "update"), "manual-" + hm(now_m)
    else:
        d = due(now_m, settings, pm, sent, closed, eod)
        if not d:
            return 0
        kind, key = d
    fetch = fetch if fetch is not None else (None if nc.test_mode() else market.fetch_chart)
    s = snapshot(home, day, settings, cfg, now, fetch=fetch, jobs=jobs, running=running)
    text = body(kind, s, settings, cfg, pm)
    subj = subject(kind, s)
    if show:
        print("Subject: %s\n\n%s" % (subj, text))
        return 0
    ok, why = (sender or send)(settings, pm, subj, text)
    rec = sent.get(key) or {}
    rec.update({"kind": kind, "at": hm(now_m), "ok": ok, "attempts": int(rec.get("attempts") or 0) + 1,
                "subject": subj})
    if not ok:
        rec["error"] = why
    sent[key] = rec
    ac.write_json(path, sent)
    ac.log_event(home, day, {"at": nc.utc_iso(now), "step": "progress_mail", "slot": key, "ok": ok,
                             "error": None if ok else why})
    print("progress mail %s (%s): %s" % (key, kind, why if ok else "FAILED: %s" % why))
    if not ok and rec["attempts"] >= MAX_ATTEMPTS:
        ac.notify(settings, "NSE autopilot: progress email failed", str(why))
    return 0


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="The paper autopilot's half-hourly progress email.")
    ap.add_argument("--show", action="store_true", help="print the mail that would go out now; send nothing")
    ap.add_argument("--send-now", action="store_true", help="send an update now, due or not")
    ap.add_argument("--now", help="test only (NSE_TEST_MODE=1): an IST time like \"2026-11-13 10:05\"")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    home = ac.trader_home()
    settings = ac.load_settings(home)
    cfg = ac.load_cfg(home)
    return run(home, settings, cfg, ac.parse_clock(a.now), show=a.show, force=a.send_now)


if __name__ == "__main__":
    sys.exit(main())
