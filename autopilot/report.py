#!/usr/bin/env python3
"""The paper autopilot's end-of-day report. Every number here is computed in Python from the files the
skills wrote (the day's state and log, state/paper.json, state/broker.json, the journal); Claude only
adds words, in its own section. Standard library only.

    python3 report.py --date 2026-11-13        # rebuild a day's report from what is on disk

Writes $NSE_TRADER_HOME/reports/<date>.json, .md and .html, and keeps reports/scorecard.json: one row a
day, so the all-days numbers (win rate, average R, the Time Machine's calibration, what the shadow trades
say about the caps and vetoes) grow with every session.
"""

import argparse
import datetime
import html
import importlib.util
import json
import math
import os
import random
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402

nc = ac.nc
_TICKET = None

WHY = {
    "traded": "traded (paper)",
    "shadow": "planned, not traded: the day's trade cap or loss limit",
    "vetoed": "vetoed by Claude before the open",
    "risk_block": "blocked by nse-risk",
    "size_refused": "refused by nse-budget",
    "plan_refused": "refused by nse-exit",
    "stale_trigger": "first seen over 30 minutes after it fired (the runner was not running)",
    "entry_expired": "nse-entry: the setup expired",
    "entry_waiting": "nse-entry did not confirm the breakout",
    "entry_no-data": "nse-entry: no data",
    "entry_error": "nse-entry failed",
    "approve_refused": "autopilot-approve refused",
    "fill_refused": "paper-fill refused",
    "error": "the runner hit an error",
    "cost_filter": "skipped by the cost filter: charges too large a share of the risk",
    "short_shadow": "short (breakdown), shadow only: never traded",
    "short_refused": "short (breakdown): no plan fits",
}
SHORT_NOTE = ("Shorts are replayed, never traded. A replay cannot show a short's own risks: a stock locked at its "
              "upper circuit cannot be bought back, and a short not covered by the close goes to NSE's auction.")
EXIT_WORDS = {"target": "target", "stop": "stop", "flat_by": "flat by 15:10", "sold": "sold", "partial": "partial",
              "cancelled": "LIMIT never filled"}
# a step's exit code that is a decision, not a failure (no hits, nothing new, not triggered, a refusal)
DECISION_CODES = {"dossier": {0, 1}, "scan_orb": {0, 1}, "scan_daily": {0, 1}, "news": {0, 1}, "entry": {0, 1}, "exit": {0, 1},
                  "size": {0, 1}, "risk": {0, 1}, "quote": {0}, "timemachine": {0}, "approve": {0, 1},
                  "paper_fill": {0, 1}, "paper_check": {0}, "journal": {0}, "status": {0}, "universe": {0},
                  "holidays": {0}}
BUCKETS = ((0.0, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 1.01))


def ticket():
    """nse-broker/ticket.py, for its paper rules (walk) and its Yahoo 1-minute bars."""
    global _TICKET
    if _TICKET is None:
        path = os.path.join(ac.REPO, "nse-broker", "ticket.py")
        spec = importlib.util.spec_from_file_location("dti_autopilot_ticket", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TICKET = mod
    return _TICKET


def _f(x):
    try:
        return None if x is None or isinstance(x, bool) else float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return "n/a" if x is None else ("-" if x < 0 else "") + nc.inr(abs(x))


def signed_money(x):
    return "n/a" if x is None else ("+" if x > 0 else "-" if x < 0 else "") + nc.inr(abs(x))


def r_text(x):
    return "n/a" if x is None else "%+.2fR" % x


def pct(x, digits=1):
    return "n/a" if x is None else ("%." + str(digits) + "f%%") % (100.0 * x)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


# ---------------------------------------------------------------- data sources (swapped in tests)

def fetch_bars(sym):
    return ticket().fetch_bars(sym)


def nifty_move(timeout=15):
    """The Nifty 50 index on the day: {"close", "prev_close", "pct"} from Yahoo (^NSEI), or None."""
    url = nc.yahoo_chart_url("^NSEI", "1d", "5m")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            meta = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]["meta"]
        close, prev = _f(meta.get("regularMarketPrice")), _f(meta.get("chartPreviousClose"))
        if close and prev:
            return {"close": close, "prev_close": prev, "pct": round(close / prev - 1, 5)}
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- the pieces

def day_trades(home, day):
    paper = ac.read_json(os.path.join(home, "state", "paper.json")) or {}
    return [t for t in paper.get("trades") or [] if t.get("date") == day]


def journal_rows(home, day):
    doc = ac.read_json(os.path.join(ac.day_dir(home, day), "journal.json")) or {}
    return [r for r in doc.get("trades") or [] if r.get("date") == day]


def summary(rows, trades):
    nets = [r["net"] for r in rows if r.get("net") is not None]
    rs = [r.get("r") for r in rows if r.get("r") is not None]
    best = max(rows, key=lambda r: r.get("net") or 0) if rows else None
    worst = min(rows, key=lambda r: r.get("net") or 0) if rows else None
    return {"orders": len(trades),
            "filled": sum(1 for t in trades if t.get("fill_price") is not None),
            "never_filled": sum(1 for t in trades if t.get("status") == "cancelled"),
            "still_open": sum(1 for t in trades if t.get("status") in ("open", "filled")),
            "closed": len(rows), "wins": sum(1 for x in nets if x > 0), "losses": sum(1 for x in nets if x < 0),
            "net": round(sum(nets), 2), "gross": round(sum(r.get("gross") or 0 for r in rows), 2),
            "charges": round(sum(r.get("charges") or 0 for r in rows), 2),
            "total_r": round(sum(rs), 2) if rs else None, "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "win_rate": round(sum(1 for x in nets if x > 0) / float(len(nets)), 4) if nets else None,
            "best": {"symbol": best["symbol"], "net": best["net"]} if best else None,
            "worst": {"symbol": worst["symbol"], "net": worst["net"]} if worst else None}


def funnel(st, trades):
    cands = st.get("candidates") or {}
    ph = st.get("phases") or {}
    scans = ph.get("orb_scans") or []
    outcomes, blocks = {}, {}
    for c in cands.values():
        o = c.get("outcome") or "pending"
        outcomes[o] = outcomes.get(o, 0) + 1
        if o == "risk_block":
            for r in (c.get("risk") or {}).get("reasons") or [c.get("reason") or "no reason"]:
                key = re.sub(r"\d[\d,.]*", "N", str(r))[:160]
                blocks[key] = blocks.get(key, 0) + 1
    exits = {}
    for t in trades:
        k = t.get("exit_reason") or {"filled": "still open", "open": "LIMIT still resting"}.get(t.get("status"),
                                                                                                 t.get("status"))
        exits[k] = exits.get(k, 0) + 1
    pre = ph.get("premarket") or {}
    shorts = sum(1 for c in cands.values() if c.get("side") == "short")
    return {"universe": pre.get("universe"), "orb_scans": len(scans),
            "orb_scans_failed": sum(1 for s in scans if s.get("failed")), "signals": len(cands) - shorts,
            "short_signals": shorts, "outcomes": outcomes, "risk_blocks": blocks, "exits": exits,
            "stop_gaps": sum(1 for t in trades if t.get("stop_gap"))}


def trade_rows(st, rows, trades):
    """One row per paper trade today, joined with what the Time Machine said before it."""
    cands = st.get("candidates") or {}
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    out = []
    for t in trades:
        sym = t.get("symbol")
        c = cands.get(sym) or {}
        tm = c.get("tm") or {}
        j = (by_sym.get(sym) or [None])[0]
        hit = None
        if t.get("status") == "closed" and t.get("fill_price") is not None:
            hit = 1 if t.get("exit_reason") == "target" else 0
        out.append({"symbol": sym, "qty": t.get("qty"), "status": t.get("status"), "ordered_at": t.get("ordered_at"),
                    "fill": t.get("fill_price"), "fill_time": t.get("fill_time"), "exit": t.get("exit_price"),
                    "exit_time": t.get("exit_time"), "exit_reason": t.get("exit_reason") or t.get("status"),
                    "net": (j or {}).get("net", t.get("pnl_net")), "charges": (j or {}).get("charges", t.get("charges")),
                    "r": (j or {}).get("r"), "max_loss": (j or {}).get("planned_max_loss"),
                    "entry_slippage": (j or {}).get("entry_slippage"),
                    "exit_vs_ref": (j or {}).get("exit_vs_ref"), "minutes_held": (j or {}).get("minutes_held"),
                    "tm_p_target": tm.get("p_target"), "tm_p_stop": tm.get("p_stop"),
                    "tm_category": tm.get("category"), "tm_avg_R_net": tm.get("avg_R_net"),
                    "tm_history": tm.get("history"), "hit": hit, "daily_setups": c.get("daily_setups") or [],
                    "stop_gap": bool(t.get("stop_gap"))})
    return out


def calibration(pairs):
    """pairs: [(p_target, hit)]. The Brier score (0 is perfect; always saying the average hit rate scores
    the base line), and predicted vs actual by bucket."""
    pairs = [(float(p), int(h)) for p, h in pairs if p is not None and h is not None]
    if not pairs:
        return {"n": 0}
    n = len(pairs)
    brier = sum((p - h) ** 2 for p, h in pairs) / n
    rate = sum(h for _, h in pairs) / float(n)
    base = sum((rate - h) ** 2 for _, h in pairs) / n
    buckets = []
    for lo, hi in BUCKETS:
        inb = [(p, h) for p, h in pairs if lo <= p < hi]
        if inb:
            buckets.append({"from": lo, "to": min(hi, 1.0), "n": len(inb),
                            "predicted": round(sum(p for p, _ in inb) / len(inb), 4),
                            "actual": round(sum(h for _, h in inb) / float(len(inb)), 4)})
    return {"n": n, "brier": round(brier, 4), "base_brier": round(base, 4), "hit_rate": round(rate, 4),
            "avg_predicted": round(sum(p for p, _ in pairs) / n, 4), "buckets": buckets}


def mirror(bars):
    """A short on these bars is a long on their mirror image: prices negated, highs and lows swapped. The paper
    broker's long rules (ticket.walk) then read a SELL LIMIT that fills when a bar's high reaches it, a buy-stop
    that triggers on a bar's high and gaps when a bar opens over its limit, and a target reached on a bar's low."""
    return [(t, -o, -l, -h, -c) for t, o, h, l, c in bars]


def shadow(c, day, bars, cfg, step=1):
    """Replay a signal that was not traded with the paper broker's own rules (ticket.walk): a LIMIT at the
    plan's entry from the trigger, the SL stop, the 2R target as an alert, flat by 15:10. A short is replayed
    on the mirrored bars (mirror()), so it gets exactly the same rules, the other way up. step is the bar
    length in minutes (1, or 5 for the archive study's 5-minute replay)."""
    p = c.get("plan") or {}
    trig = c.get("trigger") or {}
    short = c.get("side") == "short"
    entry, stop, target, qty = _f(p.get("entry")), _f(p.get("stop")), _f(p.get("target")), p.get("quantity")
    row = {"symbol": c.get("symbol"), "side": "short" if short else "long", "why": c.get("outcome"),
           "reason": c.get("reason"), "entry": entry, "stop": stop, "target": target, "qty": qty,
           "at": trig.get("at") or c.get("decided_at"), "tags": c.get("tags"), "charges_r": p.get("charges_r"),
           "cost_ok": c.get("cost_ok")}
    if not (entry and stop and target and qty and row["at"]):
        row["result"] = "no plan"
        return row
    tk = ticket()
    d = datetime.date.fromisoformat(day)
    ordered = datetime.datetime.combine(d, nc.parse_hhmm(row["at"]))
    worst = _f(p.get("worst_exit")) or stop
    if short:
        # the one tick walk() uses (target minus a tick with no next bar) must be the stock's, not a negative price's
        cfg = dict(cfg, tick_sizes=dict(cfg.get("tick_sizes") or {},
                                        **{row["symbol"]: float(nc.order_tick(entry, row["symbol"], cfg))}))
        bars, entry_w, stop_w, target_w, worst_w = mirror(bars), -entry, -stop, -target, -worst
    else:
        entry_w, stop_w, target_w, worst_w = entry, stop, target, worst
    done = [b for b in bars if b[0].date() == d and b[0] + datetime.timedelta(minutes=step) <= ordered]
    if not done:
        row["result"] = "no data"
        return row
    stamp = ordered.strftime(tk.STAMP)
    t = {"id": "shadow-" + row["symbol"], "date": day, "symbol": row["symbol"], "qty": int(qty), "limit": entry_w,
         "ordered_at": stamp, "target": target_w, "flat_by": p.get("flat_by") or cfg.get("flat_by") or "15:10",
         "stop": {"type": "SL", "trigger": stop_w, "price": worst_w}}
    fill = None
    if done[-1][4] <= entry_w + 1e-9:
        fill = done[-1][4]
        t.update(status="filled", fill_time=stamp, fill_price=fill)
    else:
        t["status"] = "open"
    end = datetime.datetime.combine(d, datetime.time(23, 59))
    exit_ = None
    for kind, when, price, _note in tk.walk(t, bars, end, cfg, step=step):
        if kind == "fill":
            fill = price
        elif kind == "cancel":
            row["result"] = "never filled"
            return row
        elif kind in ("stop", "target", "flat_by"):
            exit_, row["result"], row["exit_at"] = price, kind, when.strftime("%H:%M")
            break
    if fill is None or exit_ is None:
        row["result"] = row.get("result") or "no exit in the bars"
        return row
    if short:
        fill, exit_ = round(-fill, 2), round(-exit_, 2)
        gross = round(int(qty) * (fill - exit_), 2)
        ch = nc.intraday_charges(int(qty), exit_, fill, cfg)["total"]      # bought back at the exit, sold at the fill
    else:
        gross = round(int(qty) * (exit_ - fill), 2)
        ch = nc.intraday_charges(int(qty), fill, exit_, cfg)["total"]
    row.update(fill=fill, exit=exit_, gross=gross, charges=ch, net=round(gross - ch, 2))
    ml = _f(p.get("max_loss"))
    row["r"] = round(row["net"] / ml, 2) if ml else None
    row["max_loss"] = ml
    row["bp_r"] = bp_r(qty, fill, exit_, ml)
    return row


# ---------------------------------------------------------------- slippage (every paper fill is exact)

SLIP_BPS = (0, 1, 2, 3, 5)


def bp_r(qty, fill, exit_, max_loss):
    """The R one basis point of slippage on each side costs a trade: the entry 0.01% worse and the exit 0.01%
    worse, long or short, over the trade's max loss. The paper broker fills a LIMIT on a touch and a stop at
    its trigger, so real fills can only be worse; R at s bps a side is r - s * bp_r. Charges are held at the
    paper fills (a few bps moves them by a fraction of a paisa)."""
    try:
        q, f, x, ml = int(qty), float(fill), float(exit_), float(max_loss)
    except (TypeError, ValueError):
        return None
    if q <= 0 or ml <= 0 or f <= 0 or x <= 0:
        return None
    return round((f + x) * q / 1e4 / ml, 5)


def breakeven_bps(pairs):
    """pairs: [(r, bp_r)]. The slippage a side, in bps, at which the average R reaches zero: mean(r) / mean(bp_r).
    0 when the average is not above zero even with exact fills; None with no pairs."""
    pairs = [(float(r), float(k)) for r, k in pairs if r is not None and k]
    if not pairs:
        return None
    m = sum(r for r, _ in pairs) / len(pairs)
    k = sum(k for _, k in pairs) / len(pairs)
    return round(m / k, 1) if m > 0 else 0.0


def by_day_means(rows, bps=0):
    """rows: [{"day", "r", "bp_r"}] -> each day's average R at `bps` a side, in date order."""
    days = {}
    for o in rows:
        days.setdefault(o.get("day") or "?", []).append(o["r"] - bps * (o.get("bp_r") or 0))
    return [sum(v) / len(v) for _, v in sorted(days.items())]


def slippage(rows, levels=SLIP_BPS):
    """The edge at each slippage level, a side: rows are [{"day", "r", "bp_r"}]; a row without bp_r (an old
    scorecard day) is left out. Per signal and per day (the per-day verdict is the honest one)."""
    rows = [o for o in rows if o.get("r") is not None and o.get("bp_r")]
    out = {"n": len(rows), "days": len({o.get("day") for o in rows}),
           "breakeven_bps": breakeven_bps([(o["r"], o["bp_r"]) for o in rows]),
           "avg_bp_r": round(sum(o["bp_r"] for o in rows) / len(rows), 4) if rows else None, "levels": []}
    if not rows:
        return out
    for s in levels:
        rs = [o["r"] - s * o["bp_r"] for o in rows]
        day = edge_stats(by_day_means(rows, s), unit="days")        # the bootstrap runs over days: cheap
        out["levels"].append(dict(_mean_needed(rs), bps=s, day_mean_r=day.get("mean_r"), day_ci95=day.get("ci95"),
                                  days_needed=day.get("trades_needed"), verdict=day.get("verdict")))
    return out


def _mean_needed(rs):
    """The average and the (1.96 sd / mean)^2 trades it would take to tell, without a bootstrap."""
    n = len(rs)
    m = sum(rs) / n
    out = {"mean_r": round(m, 3), "trades_needed": None}
    if n > 1 and m:
        sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
        out["trades_needed"] = int(math.ceil((1.96 * sd / abs(m)) ** 2)) if sd > 0 else None
    return out


def shadows(st, day, cfg, bars=None):
    loader = bars or fetch_bars
    rows, cache = [], {}
    for c in sorted((st.get("candidates") or {}).values(), key=lambda c: c.get("first_seen") or ""):
        if c.get("outcome") in (None, "traded") or not c.get("plan"):
            continue
        sym = c["symbol"]          # a stock can be a long and a short signal on one day: one bar fetch for both
        try:
            if sym not in cache:
                cache[sym] = loader(sym)
            rows.append(shadow(c, day, cache[sym], cfg))
        except Exception as e:
            rows.append({"symbol": sym, "side": c.get("side") or "long", "why": c.get("outcome"),
                         "at": (c.get("trigger") or {}).get("at"), "charges_r": (c.get("plan") or {}).get("charges_r"),
                         "result": "could not replay: %s" % ac.tail(str(e), 120)})
    by_why = {}
    for r in rows:
        w = by_why.setdefault(r.get("why") or "?", {"n": 0, "replayed": 0, "net": 0.0, "wins": 0, "targets": 0,
                                                     "stops": 0})
        w["n"] += 1
        if r.get("net") is not None:
            w["replayed"] += 1
            w["net"] = round(w["net"] + r["net"], 2)
            w["wins"] += 1 if r["net"] > 0 else 0
        w["targets"] += 1 if r.get("result") == "target" else 0
        w["stops"] += 1 if r.get("result") == "stop" else 0
    return {"rows": rows, "by_why": by_why}


def execution(trade_list):
    filled = [t for t in trade_list if t.get("fill") is not None and t.get("exit") is not None]
    gross = sum(abs((t["exit"] - t["fill"]) * (t.get("qty") or 0)) for t in filled)
    charges = sum(t.get("charges") or 0 for t in filled)
    return {"avg_entry_slippage": mean([t.get("entry_slippage") for t in filled]),
            "avg_exit_vs_target": mean([t.get("exit_vs_ref") for t in filled if t.get("exit_reason") == "target"]),
            "avg_exit_vs_stop": mean([t.get("exit_vs_ref") for t in filled if t.get("exit_reason") == "stop"]),
            "avg_minutes_held": mean([t.get("minutes_held") for t in filled]),
            "charges_share_of_gross_move": round(charges / gross, 4) if gross else None,
            "stop_gaps": sum(1 for t in trade_list if t.get("stop_gap"))}


def health(st, log):
    failures = []
    for e in log:
        step, code = e.get("step"), e.get("code")
        if step not in DECISION_CODES or "code" not in e:
            continue
        if code is None or code not in DECISION_CODES[step]:
            failures.append({"at": e.get("at"), "step": step, "symbol": e.get("symbol"), "code": code,
                             "error": ac.tail(e.get("stderr") or e.get("stdout") or "", 240)})
    ticks = st.get("ticks") or []
    seen = sorted({t for t in ticks if "09:16" <= t <= "15:30"})
    gaps, prev = [], None
    for t in seen:
        if prev:
            a = int(prev[:2]) * 60 + int(prev[3:])
            b = int(t[:2]) * 60 + int(t[3:])
            if b - a > 3:
                gaps.append("%s-%s" % (prev, t))
        prev = t
    ph = st.get("phases") or {}
    pre = ph.get("premarket") or {}
    return {"failures": failures, "failures_by_step": _count(f["step"] for f in failures),
            "session_minutes_seen": len(seen), "session_minutes": 375, "gaps": gaps,
            "first_tick": seen[0] if seen else None, "last_tick": seen[-1] if seen else None,
            "premarket": "ran" if pre.get("done") else ("started, not finished" if pre else "did not run"),
            "news": pre.get("news"), "veto": pre.get("veto"), "dossier": pre.get("dossier"),
            "closed": ph.get("closed")}


def _count(items):
    out = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def build(home, day, settings, cfg, bars=None, index_move=None):
    """The report dict for `day`, written out (without Claude's analysis yet). Returns {"report", "paths"}."""
    st = ac.load_day(home, day)
    log = ac.read_log(home, day)
    trades = day_trades(home, day)
    rows = journal_rows(home, day)
    tl = trade_rows(st, rows, trades)
    offline = settings.get("offline") or {}
    if bars is None and offline.get("shadow_bars"):          # tests and replays: saved 1-minute charts
        pattern = offline["shadow_bars"][0]
        bars = lambda sym: ticket().read_bars_file(pattern.replace("{sym}", sym))
    if index_move is None and nc.test_mode():
        index_move = False                                   # a replay never reaches for today's index
    move = (index_move or nifty_move)() if index_move is not False else None
    sh = shadows(st, day, cfg, bars)
    plans = {c["symbol"]: {"entry": (c.get("plan") or {}).get("entry"), "stop": (c.get("plan") or {}).get("stop"),
                           "at": (c.get("trigger") or {}).get("at")}
             for c in (st.get("candidates") or {}).values()
             if (c.get("side") or "long") == "long" and c.get("plan")}
    vetoes = ac.read_json(os.path.join(ac.day_dir(home, day), "vetoes.json")) or {}
    rep = {"date": day, "generated_at": nc.utc_iso(), "paper": True,
           "rules": {k: cfg.get(k) for k in ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr",
                                             "max_trades_per_day", "no_entries_before", "no_entries_after",
                                             "flat_by", "min_reward_risk")},
           "universe": settings.get("universe"),
           "day": summary(rows, trades), "trades": tl, "funnel": funnel(st, trades),
           "time_machine": calibration([(t["tm_p_target"], t["hit"]) for t in tl]),
           "shadows": sh, "execution": execution(tl), "health": health(st, log), "benchmark": move,
           "vetoes": vetoes.get("vetoes") or [], "veto_summary": vetoes.get("summary"),
           "regime": ((st.get("phases") or {}).get("premarket") or {}).get("regime"),
           "candidates": [dict({k: c.get(k) for k in ("symbol", "first_seen", "scan_at", "outcome", "reason",
                                                      "daily_setups", "tags", "cost_ok")},
                               side=c.get("side") or "long", charges_r=(c.get("plan") or {}).get("charges_r"))
                          for c in sorted((st.get("candidates") or {}).values(),
                                          key=lambda c: c.get("first_seen") or "")],
           "plans": plans}
    budgets = settings.get("sizes") or []
    if budgets:
        rep["sizes"] = account_sizes(rep, cfg, budgets, (settings.get("cost_filter") or {}).get("max_charges_r"))
    rep["instruments"] = instruments(cfg, settings)
    wi = settings.get("whatif") or {}
    if wi.get("enabled"):
        rep["whatif"] = what_if(rep, cfg, wi.get("margin"), (settings.get("cost_filter") or {}).get("max_charges_r"))
    tc = settings.get("trade_caps") or {}
    if tc.get("enabled"):
        rep["caps"] = trade_caps(rep, cfg, tc.get("caps"), tc.get("budget"),
                                 (settings.get("cost_filter") or {}).get("max_charges_r"))
    rep["stocks"] = stock_files(home, day, rep)
    return write_outputs(home, day, rep, None)


def trade_caps(rep, cfg, caps=None, budget=None, max_charges_r=None):
    """The day's signals at 3, 5, 8 and 10 trades a day (whatif.caps_for_report); a failure never stops the report."""
    import whatif
    try:
        return whatif.caps_for_report(rep, cfg, [int(c) for c in (caps or whatif.DEFAULT_CAPS)], budget,
                                      None if max_charges_r is None else float(max_charges_r))
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, ac.tail(str(e), 200))}


def instruments(cfg, settings):
    try:
        import whatif
        return whatif.instruments(cfg, settings)
    except Exception as e:
        return "could not describe: %s" % ac.tail(str(e), 120)


def what_if(rep, cfg, margin=None, max_charges_r=None):
    """Why only N trades, and the whole principal (whatif.build); one failure never stops the report."""
    import whatif
    try:
        return whatif.build(rep, cfg, float(margin or whatif.DEFAULT_MARGIN),
                            None if max_charges_r is None else float(max_charges_r))
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, ac.tail(str(e), 200))}


STOCK_FLAG_ROWS = 25


def stock_files(home, day, rep):
    """The morning's stock files (nse-news/dossier.py, run by the pre-market): where they are, the traded and
    vetoed stocks' flags and results, and every stock with a flag. None when the deep scan did not run."""
    folder = os.path.join(ac.day_dir(home, day), "stocks")
    idx = ac.read_json(os.path.join(folder, "index.json"))
    if not isinstance(idx, dict) or not isinstance(idx.get("stocks"), dict):
        return None
    stocks = idx["stocks"]
    traded = [t["symbol"] for t in rep.get("trades") or []]
    vetoed = [v.get("symbol") for v in rep.get("vetoes") or [] if v.get("symbol")]
    pick = []
    for sym in traded + [v for v in vetoed if v not in traded]:
        row = stocks.get(sym)
        if row is not None:
            pick.append(dict(row, symbol=sym, why="traded" if sym in traded else "vetoed"))
    flagged = [dict(symbol=sym, flags=[f for f in row.get("flags") or [] if not f.startswith("Could not read")])
               for sym, row in sorted(stocks.items())]
    flagged = [f for f in flagged if f["flags"]]
    return {"dir": folder, "built_at": idx.get("built_at"), "count": len(stocks), "picked": pick,
            "flagged": flagged, "shared_errors": idx.get("shared_errors") or {},
            "failed": sorted(sym for sym, row in stocks.items() if row.get("failed"))}


def account_sizes(rep, cfg, budgets, max_charges_r=None):
    """Today's signals at each budget (sizes.simulate); one failure never stops the report."""
    import sizes
    try:
        sigs = sizes.from_report(rep)
        out = []
        for b in budgets:
            s = sizes.simulate(sigs, cfg, float(b), None if max_charges_r is None else float(max_charges_r))
            out.append({"budget": s["budget"], "label": s["label"], "totals": s["totals"], "skipped": s["skipped"],
                        "trades": s["trades"]})
        return out
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, ac.tail(str(e), 200))}


# ---------------------------------------------------------------- scorecard (all days)

def score_row(rep):
    d = rep["day"]
    cands = {(c.get("side") or "long", c.get("symbol")): c for c in rep.get("candidates") or []}
    outcomes = []          # every signal with an R, traded or replayed: what the edge statistics are built on
    for t in rep["trades"]:
        if t.get("r") is not None:
            c = cands.get(("long", t["symbol"])) or {}
            outcomes.append({"symbol": t["symbol"], "side": "long", "why": "traded", "r": t["r"],
                             "tags": c.get("tags"), "charges_r": c.get("charges_r"), "cost_ok": c.get("cost_ok"),
                             "bp_r": bp_r(t.get("qty"), t.get("fill"), t.get("exit"), t.get("max_loss"))})
    for s in rep["shadows"]["rows"]:
        if s.get("r") is not None:
            outcomes.append({"symbol": s["symbol"], "side": s.get("side") or "long", "why": s.get("why"),
                             "r": s["r"], "tags": s.get("tags"), "charges_r": s.get("charges_r"),
                             "cost_ok": s.get("cost_ok"), "bp_r": s.get("bp_r")})
    return {"net": d["net"], "charges": d["charges"], "closed": d["closed"], "wins": d["wins"],
            "losses": d["losses"], "total_r": d["total_r"], "signals": rep["funnel"]["signals"],
            "short_signals": rep["funnel"].get("short_signals") or 0,
            "outcomes": rep["funnel"]["outcomes"], "nifty_pct": (rep.get("benchmark") or {}).get("pct"),
            "tm": [{"symbol": t["symbol"], "p": t["tm_p_target"], "hit": t["hit"], "category": t["tm_category"],
                    "r": t["r"], "daily_setup": bool(t["daily_setups"])} for t in rep["trades"]],
            "shadows": {k: {"n": v["n"], "replayed": v["replayed"], "net": v["net"], "wins": v["wins"]}
                        for k, v in rep["shadows"]["by_why"].items()},
            "signal_rs": outcomes,
            "sizes": {s["label"]: {"budget": s["budget"], "net": s["totals"]["net"], "trades": s["totals"]["trades"],
                                   "wins": s["totals"]["wins"], "charges": s["totals"]["charges"]}
                      for s in rep.get("sizes") or [] if isinstance(rep.get("sizes"), list)},
            "whatif": whatif_row(rep.get("whatif")),
            "caps": caps_row(rep.get("caps")),
            "failures": len(rep["health"]["failures"]), "gaps": len(rep["health"]["gaps"])}


def whatif_row(wi):
    if not isinstance(wi, dict) or wi.get("error"):
        return {}
    out = {}
    for r in (wi.get("ladder") or []) + (wi.get("all_in") or []):
        if r["key"] == "rules":
            continue
        t = r["totals"]
        out[r["key"]] = {"label": r["label"], "budget": wi.get("budget"), "net": t["net"], "trades": t["trades"],
                         "wins": t["wins"], "charges": t["charges"]}
    return out


def caps_row(tc):
    if not isinstance(tc, dict) or tc.get("error"):
        return {}
    return {r["key"]: {"label": r["label"], "budget": r["budget"], "net": r["totals"]["net"],
                       "trades": r["totals"]["trades"], "wins": r["totals"]["wins"], "charges": r["totals"]["charges"]}
            for r in tc.get("rows") or []}


WHATIF_ORDER = ("ladder0", "ladder1", "ladder2", "ladder3", "all_in_1x", "all_in_margin")


def whatif_totals(days, order, field="whatif", rank=None):
    """All days for each what-if (or each trade cap, field="caps"): net, trades, wins, charges, the worst and best
    day, the per-day mean in rupees."""
    out = {}
    for d in order:
        for key, v in (days[d].get(field) or {}).items():
            s = out.setdefault(key, {"label": v.get("label"), "budget": v.get("budget"), "days": 0, "net": 0.0,
                                     "trades": 0, "wins": 0, "charges": 0.0, "daily": []})
            s["label"] = v.get("label") or s["label"]
            s["days"] += 1
            s["net"] = round(s["net"] + (v.get("net") or 0), 2)
            s["charges"] = round(s["charges"] + (v.get("charges") or 0), 2)
            s["trades"] += v.get("trades") or 0
            s["wins"] += v.get("wins") or 0
            s["daily"].append(v.get("net") or 0)
    for s in out.values():
        daily = s.pop("daily")
        st = edge_stats(daily, unit="days")
        s["per_day"] = {k: st.get(k) for k in ("mean_r", "ci95", "verdict")}
        s["worst_day"] = min(daily) if daily else None
        s["best_day"] = max(daily) if daily else None
        s["return_pct"] = round(100 * s["net"] / s["budget"], 3) if s.get("budget") else None
    rank = rank or (lambda k: WHATIF_ORDER.index(k) if k in WHATIF_ORDER else 99)
    return {k: out[k] for k in sorted(out, key=rank)}


def _cap_rank(key):
    try:
        return int(str(key).replace("cap", ""))
    except ValueError:
        return 999


# ---------------------------------------------------------------- is there an edge? (every number in R)

MIN_TRADES = 20
BOOT = 4000


def edge_stats(rs, seed=20261113, boot=BOOT, unit="trades"):
    """Average R with a 95% bootstrap interval (resampling the trades), the t statistic, the bootstrap share of
    averages above zero, and how many trades it would take for the interval to clear zero if the average and
    the spread held. A verdict in words; under MIN_TRADES it is always "too few to tell"."""
    rs = [float(r) for r in rs if r is not None]
    n = len(rs)
    if not n:
        return {"n": 0, "verdict": "no %s yet" % unit}
    m = sum(rs) / n
    out = {"n": n, "mean_r": round(m, 3), "total_r": round(sum(rs), 2),
           "win_rate": round(sum(1 for r in rs if r > 0) / float(n), 3)}
    if n > 1:
        sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
        out["sd_r"] = round(sd, 3)
        out["t"] = round(m / (sd / math.sqrt(n)), 2) if sd > 0 else None
        rng = random.Random(seed)
        means = sorted(sum(rng.choice(rs) for _ in range(n)) / n for _ in range(boot))
        lo, hi = means[int(0.025 * boot)], means[int(0.975 * boot) - 1]
        out["ci95"] = [round(lo, 3), round(hi, 3)]
        out["p_above_zero"] = round(sum(1 for x in means if x > 0) / float(boot), 3)
        if m != 0 and sd > 0:
            out["trades_needed"] = int(math.ceil((1.96 * sd / abs(m)) ** 2))
    if n < MIN_TRADES:
        out["verdict"] = "too few to tell (%d of %d %s)" % (n, MIN_TRADES, unit)
    elif out.get("ci95") and out["ci95"][0] > 0:
        out["verdict"] = "an edge: the 95% interval is above zero"
    elif out.get("ci95") and out["ci95"][1] < 0:
        out["verdict"] = "losing: the 95% interval is below zero"
    else:
        out["verdict"] = "no evidence either way: the 95% interval spans zero"
    return out


EDGE_GROUPS = (
    ("Long, traded (paper)", lambda o: o["side"] == "long" and o["why"] == "traded"),
    ("Long, skipped by the cost filter (replayed)", lambda o: o["side"] == "long" and o["why"] == "cost_filter"),
    ("Long, other signals not taken (replayed)",
     lambda o: o["side"] == "long" and o["why"] not in ("traded", "cost_filter")),
    ("Long, every signal (traded or replayed)", lambda o: o["side"] == "long"),
    ("Short, every breakdown (shadow)", lambda o: o["side"] == "short"),
    ("Short, charges within the cost filter (shadow)", lambda o: o["side"] == "short" and o.get("cost_ok") is True),
)
TAGS = ("nifty_trend", "vix_band", "nifty_today", "rank")


def edge(signal_rs):
    """The edge statistics by group, and every long and short signal split by each tag. signal_rs carry "day"
    (the scorecard adds it). Each group keeps its per-signal statistics at the top level and adds "per_day"
    (each day's average R: signals on one day move together, so this is the verdict that counts) and
    "slippage" (the edge at 0-5 bps a side, and the break-even slippage)."""
    def with_days(rows):
        g = edge_stats([o["r"] for o in rows])
        g["per_day"] = edge_stats(by_day_means(rows), unit="days")
        return g

    groups = {}
    for name, keep in EDGE_GROUPS:
        sel = [o for o in signal_rs if keep(o)]
        groups[name] = with_days(sel)
        groups[name]["slippage"] = slippage(sel)
    by_tag = {}
    for side in ("long", "short"):
        for tag in TAGS:
            vals = {}
            for o in signal_rs:
                v = (o.get("tags") or {}).get(tag)
                if o["side"] == side and v:
                    vals.setdefault(v, []).append(o)
            if vals:
                by_tag["%s / %s" % (side, tag)] = {v: with_days(rows) for v, rows in sorted(vals.items())}
    return {"groups": groups, "by_tag": by_tag}


def scorecard(home, day=None, rep=None):
    path = os.path.join(ac.reports_dir(home), "scorecard.json")
    doc = ac.read_json(path) or {}
    days = doc.get("days") or {}
    if rep is not None:
        days[day] = score_row(rep)
    order = sorted(days)
    equity, run = [], 0.0
    for d in order:
        run = round(run + (days[d].get("net") or 0), 2)
        equity.append({"date": d, "net": days[d].get("net"), "cumulative": run})
    tms = [t for d in order for t in days[d].get("tm") or []]
    closed = sum(days[d].get("closed") or 0 for d in order)
    wins = sum(days[d].get("wins") or 0 for d in order)
    rs = [t["r"] for t in tms if t.get("r") is not None]
    cats = {}
    for t in tms:
        if t.get("hit") is None:
            continue
        k = t.get("category") or "no Time Machine"
        c = cats.setdefault(k, {"n": 0, "targets": 0, "r": []})
        c["n"] += 1
        c["targets"] += t["hit"]
        if t.get("r") is not None:
            c["r"].append(t["r"])
    by_cat = {k: {"n": v["n"], "target_rate": round(v["targets"] / float(v["n"]), 4),
                  "avg_r": round(sum(v["r"]) / len(v["r"]), 2) if v["r"] else None} for k, v in cats.items()}
    setup = {}
    for label, flag in (("ORB + a daily setup", True), ("ORB alone", False)):
        xs = [t["r"] for t in tms if t.get("r") is not None and bool(t.get("daily_setup")) == flag]
        setup[label] = {"n": len(xs), "avg_r": round(sum(xs) / len(xs), 2) if xs else None}
    sh = {}
    for d in order:
        for k, v in (days[d].get("shadows") or {}).items():
            s = sh.setdefault(k, {"n": 0, "replayed": 0, "net": 0.0, "wins": 0})
            for f in ("n", "replayed", "wins"):
                s[f] += v.get(f) or 0
            s["net"] = round(s["net"] + (v.get("net") or 0), 2)
    totals = {"days": len(order), "closed": closed, "wins": wins,
              "win_rate": round(wins / float(closed), 4) if closed else None,
              "net": equity[-1]["cumulative"] if equity else 0.0,
              "charges": round(sum(days[d].get("charges") or 0 for d in order), 2),
              "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
              "max_drawdown": max_drawdown([e["cumulative"] for e in equity]),
              "time_machine": calibration([(t.get("p"), t.get("hit")) for t in tms]), "by_category": by_cat,
              "by_setup": setup, "shadows": sh,
              "edge": edge([dict(o, day=d) for d in order for o in days[d].get("signal_rs") or []]),
              "sizes": size_totals(days, order), "whatif": whatif_totals(days, order),
              "caps": whatif_totals(days, order, "caps", _cap_rank)}
    out = {"updated_at": nc.utc_iso(), "days": days, "equity": equity, "totals": totals}
    if rep is not None:
        ac.write_json(path, out)
    return out


def size_totals(days, order):
    """All days at each budget: net, trades, wins, charges, the days counted, and the per-day edge in rupees."""
    out = {}
    for d in order:
        for lab, v in (days[d].get("sizes") or {}).items():
            s = out.setdefault(lab, {"budget": v.get("budget"), "days": 0, "net": 0.0, "trades": 0, "wins": 0,
                                     "charges": 0.0, "daily": []})
            s["days"] += 1
            s["net"] = round(s["net"] + (v.get("net") or 0), 2)
            s["charges"] = round(s["charges"] + (v.get("charges") or 0), 2)
            s["trades"] += v.get("trades") or 0
            s["wins"] += v.get("wins") or 0
            s["daily"].append(v.get("net") or 0)
    for s in out.values():
        st = edge_stats(s.pop("daily"), unit="days")
        s["per_day"] = {k: st.get(k) for k in ("mean_r", "ci95", "verdict")}
        s["return_pct"] = round(100 * s["net"] / s["budget"], 3) if s.get("budget") else None
    return dict(sorted(out.items(), key=lambda kv: kv[1].get("budget") or 0))


def max_drawdown(curve):
    peak, worst = 0.0, 0.0
    for x in curve:
        peak = max(peak, x)
        worst = min(worst, x - peak)
    return round(worst, 2)


# ---------------------------------------------------------------- rendering

def headline(rep):
    d = rep["day"]
    nifty = rep.get("benchmark") or {}
    bits = ["%s: %s net after %s charges" % (rep["date"], signed_money(d["net"]), money(d["charges"])),
            "%d trade%s (%d win%s, %d loss%s)" % (d["closed"], "" if d["closed"] == 1 else "s", d["wins"],
                                                 "" if d["wins"] == 1 else "s", d["losses"],
                                                 "" if d["losses"] == 1 else "es"),
            "%d ORB signal%s" % (rep["funnel"]["signals"], "" if rep["funnel"]["signals"] == 1 else "s")]
    if nifty.get("pct") is not None:
        bits.append("Nifty 50 %+.2f%%" % (100 * nifty["pct"]))
    return ", ".join(bits)


def blocks(rep, card, analysis=None):
    """The report as neutral blocks, rendered to Markdown and to HTML by the same code."""
    d, fn, ex, h = rep["day"], rep["funnel"], rep["execution"], rep["health"]
    out = [("h1", "NSE autopilot (paper): %s" % rep["date"]),
           ("lead", headline(rep)),
           ("p", "Paper trading only: no broker, no money. Fills come from Yahoo's free 1-minute bars and P&L is "
                 "net of estimated Zerodha charges. Not financial advice.")]
    r = rep["rules"]
    out.append(("p", "Rules: budget %s, max loss %s a trade and %s a day, at most %s trades, entries %s-%s, flat by "
                     "%s. Universe: %s." % (money(_f(r.get("budget"))), money(_f(r.get("max_loss_per_trade_inr"))),
                                            money(_f(r.get("max_daily_loss_inr"))), r.get("max_trades_per_day"),
                                            r.get("no_entries_before"), r.get("no_entries_after"), r.get("flat_by"),
                                            rep.get("universe") if not isinstance(rep.get("universe"), list)
                                            else ", ".join(rep["universe"]))))
    if rep.get("instruments"):
        out.append(("p", "What it trades: " + rep["instruments"]))
    out.append(("h2", "Trades"))
    if rep["trades"]:
        out.append(("table", ["Stock", "Qty", "In", "Out", "Exit", "Net", "R", "Held", "TM P(target)", "TM said"],
                    [[t["symbol"], t["qty"], _px(t["fill"]), _px(t["exit"]),
                      EXIT_WORDS.get(t["exit_reason"], t["exit_reason"]), signed_money(_f(t["net"])), r_text(t["r"]),
                      ("%s min" % t["minutes_held"]) if t["minutes_held"] is not None else "",
                      pct(t["tm_p_target"]), t["tm_category"] or "not run"] for t in rep["trades"]]))
        out.append(("p", "Day: %d closed, %d won, %d lost; gross %s, charges %s, net %s; total %s, average %s." % (
            d["closed"], d["wins"], d["losses"], signed_money(d["gross"]), money(d["charges"]),
            signed_money(d["net"]), r_text(d["total_r"]), r_text(d["avg_r"]))))
    else:
        out.append(("p", "No paper trades today."))

    out.append(("h2", "Every signal, start to finish"))
    items = ["Universe: %s stock(s). ORB scans: %d (%d failed). Signals (ORB hits): %d." % (
        fn["universe"] if fn["universe"] is not None else "?", fn["orb_scans"], fn["orb_scans_failed"], fn["signals"])]
    for k, n in sorted(fn["outcomes"].items(), key=lambda kv: -kv[1]):
        items.append("%d %s" % (n, WHY.get(k, k)))
    for k, n in sorted(fn["risk_blocks"].items(), key=lambda kv: -kv[1]):
        items.append("nse-risk: %d x %s" % (n, k))
    for k, n in sorted(fn["exits"].items(), key=lambda kv: -kv[1]):
        items.append("paper exits: %d x %s" % (n, EXIT_WORDS.get(k, k)))
    if fn["stop_gaps"]:
        items.append("%d stop(s) gapped through the SL limit" % fn["stop_gaps"])
    out.append(("ul", items))
    if rep.get("vetoes"):
        out.append(("p", "Claude's pre-market vetoes: " + "; ".join("%s (%s)" % (v["symbol"], v["reason"])
                                                                    for v in rep["vetoes"])))
    wi = rep.get("whatif")
    if isinstance(wi, dict) and wi.get("error"):
        out.append(("h2", "Why so few trades"))
        out.append(("p", "Could not work it out today: %s" % wi["error"]))
    elif wi:
        out.extend(why_blocks(wi))
    tc = rep.get("caps")
    if isinstance(tc, dict) and tc.get("error"):
        out.append(("h2", "Trades a day"))
        out.append(("p", "Could not work them out today: %s" % tc["error"]))
    elif tc and tc.get("rows"):
        out.extend(cap_blocks(tc))

    out.append(("h2", "Did the Time Machine call it?"))
    tm = rep["time_machine"]
    if tm.get("n"):
        out.append(("p", "Today: %d trade%s with a forecast. Average predicted P(target first) %s, actual %s. "
                         "Brier score %.3f (0 is perfect; %.3f is what always guessing today's hit rate scores)." % (
                             tm["n"], "" if tm["n"] == 1 else "s", pct(tm["avg_predicted"]), pct(tm["hit_rate"]),
                             tm["brier"], tm["base_brier"])))
    else:
        out.append(("p", "No closed trade with a Time Machine forecast today."))
    ct = card["totals"]["time_machine"]
    if ct.get("n"):
        out.append(("p", "All days: %d forecasts, predicted %s vs actual %s, Brier %.3f vs base %.3f." % (
            ct["n"], pct(ct["avg_predicted"]), pct(ct["hit_rate"]), ct["brier"], ct["base_brier"])))
        out.append(("table", ["P(target) bucket", "Trades", "Predicted", "Actual"],
                    [["%d-%d%%" % (100 * b["from"], 100 * b["to"]), b["n"], pct(b["predicted"]), pct(b["actual"])]
                     for b in ct["buckets"]]))
    if card["totals"]["by_category"]:
        out.append(("table", ["Time Machine verdict", "Trades", "Target first", "Average R"],
                    [[k, v["n"], pct(v["target_rate"]), r_text(v["avg_r"])]
                     for k, v in sorted(card["totals"]["by_category"].items())]))

    out.append(("h2", "Signals not taken, replayed"))
    sh = rep["shadows"]
    longs = [s for s in sh["rows"] if s.get("side") != "short"]
    if longs:
        out.append(("p", "Each signal that was planned but not traded, replayed on the day's 1-minute bars with the "
                         "paper broker's own rules (LIMIT at the plan's entry, SL stop, 2R target as an alert, "
                         "flat by 15:10)."))
        out.append(("table", ["Stock", "Why not traded", "Signal", "Charges", "In", "Result", "Net", "R"],
                    [[s["symbol"], WHY.get(s.get("why"), s.get("why")), s.get("at") or "", r_text(s.get("charges_r"))
                      .lstrip("+"), _px(s.get("fill")), s.get("result") or "", signed_money(s.get("net")),
                      r_text(s.get("r"))] for s in longs]))
        out.append(("ul", ["%s: %d signals, %d replayed, %d green, %s net if taken" % (
            WHY.get(k, k), v["n"], v["replayed"], v["wins"], signed_money(v["net"]))
            for k, v in sorted(sh["by_why"].items()) if not k.startswith("short")]))
    else:
        out.append(("p", "Every planned signal was traded, or there were none."))

    shorts_ = [s for s in sh["rows"] if s.get("side") == "short"]
    if shorts_ or fn.get("short_signals"):
        out.append(("h2", "The short side (shadow only)"))
        out.append(("p", "Opening-range breakdowns (the first 5-minute close under the range low), planned the mirror "
                         "image of the long plan and replayed with the same paper rules, the other way up. " +
                         SHORT_NOTE))
        if shorts_:
            out.append(("table", ["Stock", "Signal", "Charges", "Sold", "Result", "Bought back", "Net", "R"],
                        [[s["symbol"], s.get("at") or "", r_text(s.get("charges_r")).lstrip("+"), _px(s.get("fill")),
                          s.get("result") or "", _px(s.get("exit")), signed_money(s.get("net")), r_text(s.get("r"))]
                         for s in shorts_]))
            done = [s for s in shorts_ if s.get("net") is not None]
            out.append(("p", "Today: %d breakdown%s, %d replayed, %d green, %s net if taken." % (
                len(shorts_), "" if len(shorts_) == 1 else "s", len(done), sum(1 for s in done if s["net"] > 0),
                signed_money(round(sum(s["net"] for s in done), 2)))))

    sz = rep.get("sizes")
    if isinstance(sz, dict) and sz.get("error"):
        out.append(("h2", "The same signals at other account sizes"))
        out.append(("p", "Could not work them out today: %s" % sz["error"]))
    elif sz:
        out.append(("h2", "The same signals at other account sizes"))
        out.append(("p", "Today's long signals re-sized for each budget and run through that budget's own rules (rupee "
                         "limits scaled with the budget, the cost filter on its own charges, the trade cap, the daily "
                         "loss limit, the cash left free). Same fills and exits as the paper broker; no market impact, "
                         "so the biggest orders are optimistic."))
        out.append(("table", ["Budget", "Trades", "Won", "Net", "Charges", "Return", "Avg R", "Break-even slippage",
                              "Largest order"],
                    [[s["label"], s["totals"]["trades"], s["totals"]["wins"], signed_money(s["totals"]["net"]),
                      money(s["totals"]["charges"]), "%+.2f%%" % s["totals"]["return_pct"],
                      r_text(s["totals"]["avg_r"]),
                      "n/a" if s["totals"].get("breakeven_bps") is None else "%.1f bps" % s["totals"]["breakeven_bps"],
                      money(s["totals"]["largest_order"])] for s in sz]))
        rows = [[s["label"], t["symbol"], t["at"], t["qty"], money(t["order_value"]), _px(t["fill"]), _px(t["exit"]),
                 EXIT_WORDS.get(t["result"], t["result"] or ""),
                 signed_money(t["net"]) if t["fill"] is not None else "never filled", r_text(t["r"])]
                for s in sz for t in s["trades"]]
        if rows:
            out.append(("table", ["Budget", "Stock", "Signal", "Qty", "Order", "In", "Out", "Exit", "Net", "R"], rows))

    if wi and not wi.get("error"):
        out.extend(all_in_blocks(wi))
    if rep.get("stocks"):
        out.extend(stock_blocks(rep["stocks"]))

    out.append(("h2", "Execution"))
    out.append(("ul", ["Average entry slippage vs the plan: %s" % _px(ex["avg_entry_slippage"], signed=True),
                       "Average exit vs the target (target exits): %s" % _px(ex["avg_exit_vs_target"], signed=True),
                       "Average exit vs the stop (stop exits): %s" % _px(ex["avg_exit_vs_stop"], signed=True),
                       "Average time held: %s" % ("%.0f min" % ex["avg_minutes_held"]
                                                  if ex["avg_minutes_held"] is not None else "n/a"),
                       "Charges as a share of the gross move: %s" % pct(ex["charges_share_of_gross_move"]),
                       "Stops that gapped: %d" % ex["stop_gaps"]]))

    out.append(("h2", "Health"))
    hl = ["Pre-market: %s; news: %s; Claude's veto: %s." % (h["premarket"], news_words(h["news"]),
                                                             h["veto"] or "n/a"),
          "Session minutes the runner saw: %d of %d (first %s, last %s)%s." % (
              h["session_minutes_seen"], h["session_minutes"], h["first_tick"] or "-", h["last_tick"] or "-",
              ("; gaps: " + ", ".join(h["gaps"])) if h["gaps"] else ""),
          "Step failures: %s." % (", ".join("%s x%d" % kv for kv in sorted(h["failures_by_step"].items()))
                                   or "none")]
    dz = h.get("dossier")
    if dz:
        hl.insert(1, "Deep scan: %s." % (dz["skipped"] if dz.get("skipped") else "%d of %d stock files in %ss, %d "
                                         "with a flag%s" % (dz.get("stocks") or 0, dz.get("of") or 0,
                                                            dz.get("seconds"), dz.get("flagged") or 0,
                                                            ("; sources failed for " + ", ".join(dz["failed"][:8]))
                                                            if dz.get("failed") else "")))
    for f in h["failures"][:8]:
        hl.append("%s %s%s: %s" % ((f.get("at") or "")[11:16], f["step"],
                                   (" " + f["symbol"]) if f.get("symbol") else "", f.get("error") or
                                   "exit %s" % f.get("code")))
    out.append(("ul", hl))

    t = card["totals"]
    out.append(("h2", "All days so far"))
    out.append(("p", "%d day%s, %d closed trades, win rate %s, average %s, total %s net after %s charges, worst "
                     "drawdown %s." % (t["days"], "" if t["days"] == 1 else "s", t["closed"], pct(t["win_rate"]),
                                       r_text(t["avg_r"]), signed_money(t["net"]), money(t["charges"]),
                                       signed_money(t["max_drawdown"]))))
    if card["equity"]:
        out.append(("table", ["Date", "Net", "Cumulative", "Nifty 50"],
                    [[e["date"], signed_money(e["net"]), signed_money(e["cumulative"]),
                      ("%+.2f%%" % (100 * card["days"][e["date"]]["nifty_pct"]))
                      if card["days"][e["date"]].get("nifty_pct") is not None else "n/a"]
                     for e in card["equity"][-20:]]))
    bs = t["by_setup"]
    out.append(("p", "ORB with a daily setup: %d trades, average %s. ORB alone: %d trades, average %s." % (
        bs["ORB + a daily setup"]["n"], r_text(bs["ORB + a daily setup"]["avg_r"]), bs["ORB alone"]["n"],
        r_text(bs["ORB alone"]["avg_r"]))))
    if t["shadows"]:
        out.append(("ul", ["%s: %d signals, %d replayed, %d green, %s net if taken" % (
            WHY.get(k, k), v["n"], v["replayed"], v["wins"], signed_money(v["net"]))
            for k, v in sorted(t["shadows"].items())]))

    if t.get("sizes"):
        out.append(("p", "At each budget, all days (same signals, each budget's own rules):"))
        out.append(("table", ["Budget", "Days", "Trades", "Won", "Net", "Charges", "Return", "Average day",
                              "95% interval a day"],
                    [[lab, v["days"], v["trades"], v["wins"], signed_money(v["net"]), money(v["charges"]),
                      ("%+.2f%%" % v["return_pct"]) if v.get("return_pct") is not None else "n/a",
                      signed_money((v.get("per_day") or {}).get("mean_r")),
                      ("%s to %s" % tuple(signed_money(x) for x in v["per_day"]["ci95"]))
                      if (v.get("per_day") or {}).get("ci95") else "n/a"] for lab, v in t["sizes"].items()]))

    if t.get("caps"):
        out.append(("p", "Trades a day, all days (same signals, each cap with its own daily loss limit):"))
        out.append(("table", ["Trades a day", "Days", "Trades", "Won", "Net", "Return", "Worst day", "Best day",
                              "Average day", "95% interval a day"],
                    [[v["label"], v["days"], v["trades"], v["wins"], signed_money(v["net"]),
                      ("%+.2f%%" % v["return_pct"]) if v.get("return_pct") is not None else "n/a",
                      signed_money(v.get("worst_day")), signed_money(v.get("best_day")),
                      signed_money((v.get("per_day") or {}).get("mean_r")),
                      ("%s to %s" % tuple(signed_money(x) for x in v["per_day"]["ci95"]))
                      if (v.get("per_day") or {}).get("ci95") else "n/a"] for v in t["caps"].values()]))
    if t.get("whatif"):
        out.append(("p", "Why so few trades, and the whole principal, all days (same signals, each way's rules):"))
        out.append(("table", ["Way", "Days", "Trades", "Won", "Net", "Return", "Worst day", "Best day",
                              "Average day", "95% interval a day"],
                    [[v["label"], v["days"], v["trades"], v["wins"], signed_money(v["net"]),
                      ("%+.2f%%" % v["return_pct"]) if v.get("return_pct") is not None else "n/a",
                      signed_money(v.get("worst_day")), signed_money(v.get("best_day")),
                      signed_money((v.get("per_day") or {}).get("mean_r")),
                      ("%s to %s" % tuple(signed_money(x) for x in v["per_day"]["ci95"]))
                      if (v.get("per_day") or {}).get("ci95") else "n/a"] for v in t["whatif"].values()]))

    ed = t.get("edge") or {}
    if any(g.get("n") for g in (ed.get("groups") or {}).values()):
        out.append(("h2", "Is there an edge? (all days, in R)"))
        out.append(("p", "R after charges. Signals on one day move together (a falling market stops most breakouts at "
                         "once), so the sample is days, not trades: each day's average R, with a 95%% bootstrap "
                         "interval over days. An edge needs that whole interval above zero and at least %d days. Every "
                         "paper fill is exact (a LIMIT fills on a touch, a stop at its trigger), so real fills can only "
                         "be worse: \"Break-even\" is the slippage a side, in basis points (0.01%%), that would take "
                         "the average to zero." % MIN_TRADES))
        out.append(("table", EDGE_HEAD, [edge_row(name, g) for name, g in ed["groups"].items() if g.get("n")]))
        slip = [slip_row(name, lv) for name in SLIP_GROUPS
                for lv in ((ed["groups"].get(name) or {}).get("slippage") or {}).get("levels") or []]
        if slip:
            out.append(("p", "The same with slippage: the entry and the exit each that many bps worse than the paper "
                             "fill. \"Needed\" is roughly how many trades, or days, it would take to tell if the "
                             "average and spread held."))
            out.append(("table", ["Group", "Slippage a side", "Avg R", "Trades needed", "Avg day",
                                  "95% interval a day", "Days needed", "Verdict (days)"], slip))
        tag_rows = []
        for key, vals in sorted((ed.get("by_tag") or {}).items()):
            for v, g in vals.items():
                tag_rows.append(edge_row("%s = %s" % (key, v), g, breakeven=False))
        if tag_rows:
            out.append(("p", "The same, split by what was true when each signal fired (the market conditions). Nothing "
                             "was skipped on these tags: they only show whether a filter on them would have helped."))
            out.append(("table", ["Signals where"] + EDGE_HEAD[1:7] + ["Verdict (days)"], tag_rows))

    if analysis:
        out.append(("h2", "Claude's analysis"))
        out.append(("markdown", analysis))
    elif analysis is False:
        out.append(("h2", "Claude's analysis"))
        out.append(("p", "Claude's analysis is unavailable today; the numbers above are complete without it."))
    return out


def _skips(skipped):
    return ", ".join("%s %d" % (k, n) for k, n in sorted(skipped.items(), key=lambda kv: -kv[1])) or "nothing"


def _share(x, of):
    return ("%.1f%%" % (100.0 * x / of)) if x is not None and of else "n/a"


def why_blocks(wi):
    L = wi["limits"]
    n, filled = L["entries"], L["entries"] - len(L["unfilled"])
    out = [("h2", "Why only %d trade%s%s" % (n, "" if n == 1 else "s",
                                             (" (%d filled)" % filled) if filled != n else ""))]
    items = []
    if L["cap"]:
        items.append("Trade cap: at most %d entries a day (max_trades_per_day in config.json). A placed entry counts "
                     "even if its LIMIT never fills%s." % (L["cap"], ("; today %s used a slot without filling"
                                                                     % ", ".join(L["unfilled"])) if L["unfilled"]
                                                          else ""))
    if L["daily"]:
        items.append("Daily loss limit: %s. Each entry keeps its whole max loss (%s) reserved for the rest of the "
                     "day, even after it closes, so %s / %s = %s entries at most." % (
                         money(L["daily"]), money(L["per_trade"]), money(L["daily"]), money(L["per_trade"]),
                         L["entries_by_loss_limit"] if L["entries_by_loss_limit"] is not None else "n/a"))
    cash = "Cash: %s less the %g%% kept back = %s, and no margin." % (money(L["budget"]), L["buffer_pct"],
                                                                    money(L["usable_cash"]))
    if L["avg_order"]:
        cash += " Sized to lose about %s at the stop, today's orders averaged %s, so about %d fit at once" % (
            money(L["per_trade"]), money(L["avg_order"]), L["orders_that_fit"] or 0)
        if L["orders_total"] > L["usable_cash"]:
            cash += " (today's %d entry orders added up to %s: sized in the same minute, each against all the " \
                    "cash)" % (L["entries"], money(L["orders_total"]))
        cash += "."
    items.append(cash)
    out.append(("p", "Three rules each stop the day at about three entries:"))
    out.append(("ul", items))
    if L["capped"]:
        out.append(("p", "Signals these limits kept out today: %d. Replayed, they would have made %s net (see "
                         "\"Signals not taken, replayed\")." % (L["capped"], signed_money(L["capped_net"]))))
    out.append(("p", "The same signals with the limits lifted one at a time. Same fills and exits as the paper "
                     "broker; each signal sized by today's rules from the cash left free, in the order they fired. "
                     "\"Most in stock at once\" is the money the way needed at its busiest minute."))
    out.append(("table", ["Rules", "Trades", "Won", "Net", "Return", "Most in stock at once", "Kept out"],
                [[r["label"], r["totals"]["trades"], r["totals"]["wins"], signed_money(r["totals"]["net"]),
                  "%+.2f%%" % r["totals"]["return_pct"], money(r["peak_open"]), _skips(r["skipped"])]
                 for r in wi["ladder"]]))
    return out


def cap_blocks(tc):
    rows = tc["rows"]
    caps = [r["cap"] for r in rows]
    head = "%s trades a day at %s" % (", ".join(str(c) for c in caps[:-1]) + (" and %d" % caps[-1]
                                                                             if len(caps) > 1 else str(caps[0])),
                                      _lab(tc["budget"]))
    out = [("h2", head)]
    out.append(("p", "The day's signals run through the paper account's rules at each number of trades a day, at "
                     "%s: the max loss a trade, the cost filter, Claude's veto, the cash (less the amount kept back, "
                     "no margin) and a daily loss limit of that many times the max loss a trade, so the last trade "
                     "is never kept out by the loss rule. Same fills and exits as the paper broker. The paper "
                     "account itself trades %s a day." % (_lab(tc["budget"]), tc.get("live_cap") or "?")))
    out.append(("table", ["Trades a day", "Daily loss limit", "Trades", "Won", "Net", "Charges", "Return", "Avg R",
                          "Most in stock at once", "Kept out"],
                [[r["cap"], money(r["daily_loss"]), r["totals"]["trades"], r["totals"]["wins"],
                  signed_money(r["totals"]["net"]), money(r["totals"]["charges"]), "%+.2f%%" % r["totals"]["return_pct"],
                  r_text(r["totals"]["avg_r"]), money(r["peak_open"]), _skips(r["skipped"])] for r in rows]))
    trows = [[r["cap"], x["symbol"], x["at"], x["qty"], money(x["order_value"]), _px(x["fill"]), _px(x["exit"]),
              EXIT_WORDS.get(x["result"], x["result"] or ""),
              signed_money(x["net"]) if x["fill"] is not None else "never filled", r_text(x.get("r"))]
             for r in rows for x in r["trades"]]
    if trows:
        out.append(("table", ["Trades a day", "Stock", "Signal", "Qty", "Order", "In", "Out", "Exit", "Net", "R"],
                    trows))
    out.append(("p", "When the money is already in open trades, a later signal is kept out (\"no size fits\"): "
                     "at %s, sized to lose the max loss at the stop, only two or three orders fit at once, so a "
                     "higher cap adds trades only when earlier ones have closed." % _lab(tc["budget"])))
    return out


def all_in_blocks(wi):
    b, m = wi["budget"], wi["margin"]
    out = [("h2", "If every trade used the whole %s" % _lab(b))]
    out.append(("p", "The same signals with no risk sizing: each takes all the money that is free (nothing kept "
                     "back, no trade cap, no daily loss limit), and waits while at least half of it is in another "
                     "trade. Once with the money alone, once with %gx intraday margin." % m))
    rows = []
    for r in wi["all_in"]:
        t, big = r["totals"], r["largest"] or {}
        st = big.get("stress") or {}
        rows.append([r["label"], t["trades"], t["wins"], signed_money(t["net"]), "%+.2f%%" % t["return_pct"],
                     signed_money((r["worst_trade"] or {}).get("net")), money(big.get("order_value")),
                     "%s (%s)" % (money(big.get("max_loss")), _share(big.get("max_loss"), b)),
                     "-%s (%s)" % (money(st.get("10")), _share(st.get("10"), b)) if st else "n/a",
                     "-%s (%s)" % (money(st.get("20")), _share(st.get("20"), b)) if st else "n/a"])
    out.append(("table", ["Way", "Trades", "Won", "Net", "Return", "Worst trade", "Largest position",
                          "Its loss at the stop", "If it fell 10%", "If it fell 20%"], rows))
    rules, one, lev = (wi["all_in"] + [None, None, None])[:3]
    notes = []
    if rules and one and lev:
        notes.append("Under today's rules a stop costs at most %s (%s of the money). All in, today's largest stop "
                     "would have cost %s (%s); with %gx margin %s (%s)." % (
                         money(wi["limits"]["per_trade"]), _share(wi["limits"]["per_trade"], b),
                         money((one["largest"] or {}).get("max_loss")), _share((one["largest"] or {}).get("max_loss"), b),
                         m, money((lev["largest"] or {}).get("max_loss")),
                         _share((lev["largest"] or {}).get("max_loss"), b)))
        waited = sum(n for k, n in one["skipped"].items() if k.startswith("no size"))
        if waited:
            notes.append("All in, %d signal%s had to wait or were missed because the money was already in a trade."
                         % (waited, "" if waited == 1 else "s"))
    notes.append("A stop is not a floor: a stock that gaps, is halted or locks at its lower circuit can fall 10-20%% "
                 "before it can be sold. At %gx margin a %d%% fall in one position loses the whole %s, and more."
                 % (m, int(round(100 / m)), _lab(b)))
    notes.append("Assumptions: %gx is the intraday (MIS) leverage Zerodha usually gives on large stocks (a 20%% "
                 "margin); it differs by stock and the broker squares MIS positions off near 15:20. Fills are the "
                 "paper broker's with no market impact, so the largest orders are the least believable." % m)
    out.append(("ul", notes))
    trows = [[r["label"], x["symbol"], x["at"], x["qty"], money(x["order_value"]), _px(x["fill"]), _px(x["exit"]),
              EXIT_WORDS.get(x["result"], x["result"] or ""),
              signed_money(x["net"]) if x["fill"] is not None else "never filled"]
             for r in wi["all_in"][1:] for x in r["trades"]]
    if trows:
        out.append(("table", ["Way", "Stock", "Signal", "Qty", "Position", "In", "Out", "Exit", "Net"], trows))
    return out


def _built(stamp):
    try:
        return nc.ist_text(nc.parse_now(stamp))
    except (TypeError, ValueError):
        return "this morning"


def _lab(budget):
    import sizes
    return sizes.label(budget)


def stock_blocks(sf):
    out = [("h2", "Stock files (the deep scan before the open)")]
    out.append(("p", "Built %s for %d stocks: price and trend, the latest results, board meetings and ex-dates, 30 "
                     "days of NSE filings, a week of headlines, shareholding, pledges, insider trades, bulk and block "
                     "deals and the F&O ban list. Each stock's file: %s/<SYMBOL>.md (the traded stocks' files are "
                     "attached to this email)." % (
                         _built(sf.get("built_at")), sf["count"], sf["dir"])))
    for p in sf["picked"]:
        bits = ["%s (%s%s):" % (p["symbol"], p["why"], (", " + p["name"]) if p.get("name") else "")]
        bits.append(("; ".join(p.get("flags") or []) or "no flags") + ".")
        if p.get("results"):
            bits.append("Results: " + p["results"])
        out.append(("p", " ".join(bits)))
    if sf["flagged"]:
        out.append(("p", "Every stock in the universe with a flag this morning:"))
        out.append(("table", ["Stock", "Flags"], [[f["symbol"], "; ".join(f["flags"])]
                                                 for f in sf["flagged"][:STOCK_FLAG_ROWS]]))
        if len(sf["flagged"]) > STOCK_FLAG_ROWS:
            out.append(("p", "... and %d more in the folder's index.json." % (len(sf["flagged"]) - STOCK_FLAG_ROWS)))
    bad = ["%s: %s" % (k, v) for k, v in sorted(sf["shared_errors"].items())]
    if sf["failed"]:
        bad.append("some sources failed for %d stock%s (%s)" % (len(sf["failed"]), "" if len(sf["failed"]) == 1
                                                                else "s", ", ".join(sf["failed"][:10])))
    if bad:
        out.append(("p", "Could not read: " + "; ".join(bad) + "."))
    return out


EDGE_HEAD = ["Group", "Signals", "Avg R", "Win rate", "Days", "Avg day", "95% interval a day", "Break-even",
             "Verdict (days)"]
SLIP_GROUPS = ("Long, traded (paper)", "Long, every signal (traded or replayed)")


def _ci(ci):
    return ("%+.2f to %+.2f" % tuple(ci)) if ci else "n/a"


def _na(x):
    return "n/a" if x is None else x


def edge_row(name, g, breakeven=True):
    """One group: per signal, then per day (the verdict is the per-day one), then the break-even slippage."""
    d = g.get("per_day") or {}
    row = [name, g["n"], r_text(g.get("mean_r")), pct(g.get("win_rate"), 0), d.get("n") or 0,
           r_text(d.get("mean_r")), _ci(d.get("ci95"))]
    if breakeven:
        be = (g.get("slippage") or {}).get("breakeven_bps")
        row.append("n/a" if be is None else "%.1f bps" % be)
    row.append(d.get("verdict") or g.get("verdict"))
    return row


def slip_row(name, lv):
    return [name, "%d bps" % lv["bps"], r_text(lv.get("mean_r")), _na(lv.get("trades_needed")),
            r_text(lv.get("day_mean_r")), _ci(lv.get("day_ci95")), _na(lv.get("days_needed")), lv.get("verdict")]


def news_words(n):
    if not n:
        return "not run"
    code = n.get("code")
    if code == 3:
        return "FAILED (every news source was down)"
    if code not in (0, 1):
        return "failed (exit %s)" % code
    return "%d new item%s" % (n.get("items") or 0, "" if n.get("items") == 1 else "s")


def _px(x, signed=False):
    if x is None:
        return "n/a"
    return ("%+.2f" % x) if signed else nc.inr(x, keep_zeros=True)


def to_markdown(bl):
    lines = []
    for b in bl:
        kind = b[0]
        if kind == "h1":
            lines += ["# " + b[1], ""]
        elif kind == "h2":
            lines += ["## " + b[1], ""]
        elif kind == "lead":
            lines += ["**" + b[1] + "**", ""]
        elif kind == "p":
            lines += [b[1], ""]
        elif kind == "ul":
            lines += ["- " + str(i) for i in b[1]] + [""]
        elif kind == "table":
            head, rows = b[1], b[2]
            lines.append("| " + " | ".join(str(x) for x in head) + " |")
            lines.append("|" + "|".join("---" for _ in head) + "|")
            for r in rows:
                lines.append("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |")
            lines.append("")
        elif kind == "markdown":
            lines += [b[1].strip(), ""]
    return "\n".join(lines)


CSS = """
:root{--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--soft:#f6f8fa;--good:#1a7f37;--bad:#cf222e}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;
--soft:#151b23;--good:#3fb950;--bad:#f85149}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,
Arial,sans-serif;margin:0;padding:16px}
main{max-width:860px;margin:0 auto}
h1{font-size:22px;margin:8px 0 4px}h2{font-size:17px;margin:26px 0 8px;padding-bottom:4px;
border-bottom:1px solid var(--line)}
.lead{font-size:17px;font-weight:600;margin:6px 0 10px}
p,li{color:var(--fg)}.muted{color:var(--muted)}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px;margin:6px 0 10px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap}
th{background:var(--soft)}
"""


def inline_md(text):
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


def md_to_html(text):
    """Enough Markdown for Claude's analysis: headings, bullet and numbered lists, paragraphs, bold, code."""
    out, para, lst = [], [], None

    def flush():
        nonlocal para, lst
        if para:
            out.append("<p>%s</p>" % inline_md(" ".join(para)))
            para = []
        if lst:
            out.append("<%s>%s</%s>" % (lst[0], "".join("<li>%s</li>" % inline_md(i) for i in lst[1]), lst[0]))
            lst = None

    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = min(len(m.group(1)) + 2, 6)
            out.append("<h%d>%s</h%d>" % (level, inline_md(m.group(2)), level))
            continue
        m = re.match(r"^\s*(?:[-*]|(\d+)[.)])\s+(.*)$", line)
        if m:
            if para:
                out.append("<p>%s</p>" % inline_md(" ".join(para)))
                para = []
            tag = "ol" if m.group(1) else "ul"
            if not lst or lst[0] != tag:
                if lst:
                    flush()
                lst = (tag, [])
            lst[1].append(m.group(2))
            continue
        if not line.strip():
            flush()
            continue
        if lst:
            flush()
        para.append(line.strip())
    flush()
    return "\n".join(out)


def to_html(bl, title):
    body = []
    for b in bl:
        kind = b[0]
        if kind == "h1":
            body.append("<h1>%s</h1>" % html.escape(b[1]))
        elif kind == "h2":
            body.append("<h2>%s</h2>" % html.escape(b[1]))
        elif kind == "lead":
            body.append('<p class="lead">%s</p>' % html.escape(b[1]))
        elif kind == "p":
            body.append("<p>%s</p>" % html.escape(b[1]))
        elif kind == "ul":
            body.append("<ul>%s</ul>" % "".join("<li>%s</li>" % html.escape(str(i)) for i in b[1]))
        elif kind == "table":
            head = "".join("<th>%s</th>" % html.escape(str(x)) for x in b[1])
            rows = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % html.escape(str(x)) for x in r) for r in b[2])
            body.append('<div class="wrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>'
                        % (head, rows))
        elif kind == "markdown":
            body.append(md_to_html(b[1]))
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body><main>\n%s\n</main></body></html>\n"
            % (html.escape(title), CSS, "\n".join(body)))


def paths_for(home, day):
    r = ac.reports_dir(home)
    return {"json": os.path.join(r, day + ".json"), "md": os.path.join(r, day + ".md"),
            "html": os.path.join(r, day + ".html"), "analysis": os.path.join(r, day + ".analysis.md"),
            "stocks": os.path.join(r, day + ".stocks.md"), "scorecard": os.path.join(r, "scorecard.json")}


def write_stock_files(path, rep):
    """The traded and vetoed stocks' full files, one after another, for the email. False when there are none."""
    sf = rep.get("stocks") or {}
    parts = []
    for p in sf.get("picked") or []:
        f = os.path.join(sf["dir"], "%s.md" % p["symbol"].replace("&", "_"))
        try:
            with open(f, encoding="utf-8") as fh:
                parts.append(fh.read().strip())
        except OSError:
            continue
    if not parts:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Stock files: %s\n\n" % rep["date"] + "\n\n---\n\n".join(parts) + "\n")
    return True


def write_outputs(home, day, rep, analysis):
    """Write the day's JSON, Markdown and HTML (with Claude's analysis when there is one) and the scorecard.
    analysis: text, None (not asked yet) or False (asked and unavailable)."""
    paths = paths_for(home, day)
    if analysis:
        rep["analysis"] = analysis
        with open(paths["analysis"], "w", encoding="utf-8") as fh:
            fh.write(analysis.strip() + "\n")
    card = scorecard(home, day, rep)
    ac.write_json(paths["json"], rep)
    bl = blocks(rep, card, analysis if analysis else (False if analysis is False else None))
    with open(paths["md"], "w", encoding="utf-8") as fh:
        fh.write(to_markdown(bl))
    with open(paths["html"], "w", encoding="utf-8") as fh:
        fh.write(to_html(bl, "NSE autopilot %s" % day))
    if not analysis:
        paths.pop("analysis")
    if not write_stock_files(paths["stocks"], rep):
        paths.pop("stocks")
    return {"report": rep, "paths": paths, "scorecard": card}


def for_claude(home, day):
    """What Claude reads for the end-of-day analysis: the day's report and the all-days scorecard, compact."""
    paths = paths_for(home, day)
    rep = ac.read_json(paths["json"]) or {}
    card = ac.read_json(paths["scorecard"]) or {}
    rep = dict(rep)
    rep.pop("analysis", None)
    return {"today": rep, "all_days": {"totals": card.get("totals"), "equity": (card.get("equity") or [])[-30:]}}


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Rebuild the paper autopilot's report for a day.")
    ap.add_argument("--date", help="YYYY-MM-DD (default today, IST)")
    ap.add_argument("--no-network", action="store_true", help="skip the shadow replays' bars and the Nifty move")
    a = ap.parse_args(argv)
    home = ac.trader_home()
    settings = ac.load_settings(home)
    cfg = ac.load_cfg(home)
    day = a.date or nc.to_ist(nc.utc_now()).date().isoformat()
    if a.no_network:
        out = build(home, day, settings, cfg, bars=lambda s: [], index_move=False)
    else:
        out = build(home, day, settings, cfg)
    try:                                   # keep Claude's analysis from the day's run, if there was one
        with open(paths_for(home, day)["analysis"], encoding="utf-8") as fh:
            out = write_outputs(home, day, out["report"], fh.read())
    except OSError:
        pass
    print(headline(out["report"]))
    for k, v in sorted(out["paths"].items()):
        print("  %s: %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
