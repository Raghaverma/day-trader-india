#!/usr/bin/env python3
"""The P&L journal: every paper trade, what it made or lost, and WHY, kept in a folder of its own (by default on
the Desktop) and rebuilt after each day's report. Paper only. Standard library only.

    python3 journal.py               # rebuild the journal from every day's report
    python3 journal.py --print DAY   # print one day's write-up

The folder (autopilot.json "journal": {"enabled": true, "dir": "~/Desktop/NSE Trading P&L"}):

    README.md          what is here and how it is kept
    Summary.md         running totals, the reasons trades won or lost, the signals not taken, day by day
    P&L ledger.csv     one row per paper order (all days): the numbers and the reason
    Daily P&L.csv      one row per day, with the running total
    days/<date>.md     the day in full: each trade's story, the market, the signals not taken, Claude's analysis
    My notes.md        yours: created once, never touched again

Everything is rebuilt from the trader folder's own records (reports/<date>.json, state/paper.json, the day's
state and the archived 1-minute bars), so the journal can always be deleted and rebuilt. Every number and every
reason is computed here by rule, from prices; nothing is a guess. If the folder cannot be written (macOS can
refuse a background job the Desktop), the journal goes to <trader folder>/journal instead and says so.
"""

import argparse
import csv
import datetime
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402
import market  # noqa: E402

nc = ac.nc

DEFAULT_DIR = "~/Desktop/NSE Trading P&L"
LATE_FILL_MIN = 15           # a LIMIT that filled this long after the order: a pullback fill
GAVE_BACK_R = 1.0            # a stopped trade that had been this far in profit "gave back a winner"
NO_FOLLOW_R = 0.5            # a stopped trade that never got this far "never got going"

LEDGER_COLS = ["date", "symbol", "result", "why_category", "signal_at", "ordered_at", "fill_at", "fill_price", "qty",
               "position_value", "stop", "target", "exit_at", "exit_price", "exit_reason", "minutes_held", "gross",
               "charges", "net", "R", "best_R", "best_at", "worst_R", "nifty_pct_during", "tm_p_target",
               "tm_verdict", "breakout_pct_over_range", "volume_x", "why"]
DAILY_COLS = ["date", "orders", "filled", "wins", "losses", "no_fills", "made", "lost", "charges", "net",
              "running_net", "total_R", "nifty_pct", "signals", "not_taken_cap", "not_taken_cap_net_if_taken",
              "vetoed_net_if_taken", "shorts_shadow_net", "headline"]


# ---------------------------------------------------------------- small helpers

def signed(x, decimals=0):
    if x is None:
        return "n/a"
    x = float(x)
    return ("+" if x > 0 else "") + nc.inr(x, decimals)


def money(x, decimals=0):
    return "n/a" if x is None else nc.inr(x, decimals)


def px(x):
    return "n/a" if x is None else nc.inr(x, 2)


def hhmm(stamp):
    """'2026-11-19 09:38' or '09:38' -> '09:38'."""
    if not stamp:
        return None
    m = re.search(r"(\d{2}:\d{2})", str(stamp))
    return m.group(1) if m else None


def mins(t):
    return int(t[:2]) * 60 + int(t[3:5]) if t else None


def r2(x):
    return None if x is None else round(float(x), 2)


def bars_of(home, day, sym):
    """[(HH:MM, o, h, l, c)] from the archived 1-minute bars (5-minute if that is all there is), or []."""
    for interval in ("1m", "5m"):
        doc = market.read_day(market.archive_path(home, interval, day, sym))
        if doc and doc.get("bars"):
            return [(nc.ist_from_epoch(b[0]).strftime("%H:%M"), b[1], b[2], b[3], b[4]) for b in doc["bars"]
                    if b[4] is not None]
    return []


def window(bars, after=None, upto=None, include_after=False):
    out = []
    for b in bars:
        if after and (b[0] < after or (b[0] == after and not include_after)):
            continue
        if upto and b[0] > upto:
            continue
        out.append(b)
    return out


def close_at(bars, t):
    """The close of the last bar at or before t."""
    best = None
    for b in bars:
        if b[0] <= t:
            best = b
    return best[4] if best else None


# ---------------------------------------------------------------- one trade

def explain(t, cand, bars, nifty, cfg):
    """The trade's row: the numbers, the price path around it, a category and the reason in plain words."""
    plan = cand.get("plan") or {}
    trig = cand.get("trigger") or {}
    tm = cand.get("tm") or {}
    stop = (t.get("stop") or {}).get("trigger") or plan.get("stop")
    target = t.get("target") or plan.get("target")
    entry = t.get("fill_price") if t.get("fill_price") is not None else t.get("limit")
    risk = (entry - stop) if entry is not None and stop is not None and entry > stop else None
    qty = t.get("qty")
    row = {"date": t.get("date"), "symbol": t.get("symbol"), "signal_at": trig.get("at") or cand.get("scan_at"),
           "ordered_at": hhmm(t.get("ordered_at")), "fill_at": hhmm(t.get("fill_time")),
           "fill_price": t.get("fill_price"), "qty": qty, "stop": stop, "target": target,
           "exit_at": hhmm(t.get("exit_time")), "exit_price": t.get("exit_price"),
           "exit_reason": t.get("exit_reason"), "gross": t.get("gross"), "charges": t.get("charges"),
           "net": t.get("pnl_net"), "tm_p_target": tm.get("p_target"), "tm_verdict": tm.get("category"),
           "breakout_pct_over_range": cand.get("pct_over"), "volume_x": trig.get("volume_x"),
           "position_value": round(qty * entry, 2) if qty and entry else None}
    max_loss = t.get("max_loss") or plan.get("max_loss")
    row["R"] = r2(t["pnl_net"] / max_loss) if t.get("pnl_net") is not None and max_loss else None
    if row["fill_at"] and row["exit_at"]:
        row["minutes_held"] = mins(row["exit_at"]) - mins(row["fill_at"])
    status = t.get("status")
    story, why = [], []

    def in_r(p):
        return round((p - entry) / risk, 2) if risk and p is not None and entry is not None else None

    tm_txt = ("The Time Machine had given it %.0f%% to reach the target first (%s)." %
              (100 * tm["p_target"], tm.get("category") or "no verdict")) if tm.get("p_target") is not None else ""
    weak = []
    if cand.get("pct_over") is not None and cand["pct_over"] < 0.05:
        weak.append("only %.2f%% over the opening-range high" % cand["pct_over"])
    if trig.get("volume_x") is not None and trig["volume_x"] < 1.0:
        weak.append("volume %.2fx normal" % trig["volume_x"])
    breakout = ("Signal: %s broke the 09:15-09:30 range high %s at %s (%s%s)." % (
        t.get("symbol"), px(cand.get("or_high")), row["signal_at"] or "?",
        "%.2f%% over" % cand["pct_over"] if cand.get("pct_over") is not None else "over",
        ", volume %.2fx normal" % trig["volume_x"] if trig.get("volume_x") is not None else ""))
    story.append(breakout)

    if status == "cancelled" or (status == "open" and not t.get("fill_price")):
        after = window(bars, row["ordered_at"], t.get("flat_by") or cfg.get("flat_by") or "15:10",
                       include_after=True)
        row["result"] = "NO FILL"
        if after:
            low = min(after, key=lambda b: b[3])
            high = max(after, key=lambda b: b[2])
            hit_t = next((b[0] for b in after if target and b[2] >= target), None)
            hit_s = next((b[0] for b in after if stop and b[3] <= stop), None)
            ran = low[3] > (t.get("limit") or 0)
            row["why_category"] = "Never filled: price ran away" if ran else "Never filled"
            why.append("The LIMIT buy at %s never filled: after the order the lowest price was %s (%s), so the "
                       "price never came back to the entry; it went on to %s (%s)." % (
                           px(t.get("limit")), px(low[3]), low[0], px(high[2]), high[0]))
            if hit_t and (not hit_s or hit_t < hit_s) and qty and target and t.get("limit"):
                missed = qty * (target - t["limit"])
                why.append("Bought at the signal price it would have reached the 2R target at %s: about %s gross "
                           "missed." % (hit_t, signed(missed)))
                row["missed_gross"] = round(missed, 2)
        else:
            row["why_category"] = "Never filled"
            why.append("The LIMIT buy at %s never filled (no bars to say more)." % px(t.get("limit")))
        why.append("It still counted as one of the day's %s trades, so it blocked a later signal until the end of "
                   "the day." % (cfg.get("max_trades_per_day") or 3))
        if tm_txt:
            why.append(tm_txt)
        row["story"], row["why"] = story, " ".join(why)
        return row

    delay = (mins(row["fill_at"]) - mins(row["ordered_at"])) if row["fill_at"] and row["ordered_at"] else 0
    story.append("Order: LIMIT buy %s at %s at %s; stop %s (risk %s a share), target %s (2R)." % (
        qty, px(t.get("limit")), row["ordered_at"], px(stop), px(risk), px(target)))
    story.append("Filled at %s at %s%s." % (px(t.get("fill_price")), row["fill_at"],
                                            " (%d min after the order)" % delay if delay >= 2 else ""))
    end = row["exit_at"] or "15:30"
    held = window(bars, row["fill_at"], end)
    if not held:
        held = window(bars, row["fill_at"], end, include_after=True)
    pre = window(bars, row["ordered_at"], row["fill_at"]) if delay >= 2 else []
    pre = [b for b in pre if b[0] < (row["fill_at"] or "")]
    if held:
        best = max(held, key=lambda b: b[2])
        worst = min(held, key=lambda b: b[3])
        row["best_R"], row["best_at"], row["worst_R"] = in_r(best[2]), best[0], in_r(worst[3])
        story.append("While held: best %s (%s, %s), worst %s (%s, %s)." % (
            px(best[2]), best[0], "%+.2fR" % row["best_R"] if row["best_R"] is not None else "",
            px(worst[3]), worst[0], "%+.2fR" % row["worst_R"] if row["worst_R"] is not None else ""))
    if nifty and row["fill_at"]:
        a, b = close_at(nifty, row["fill_at"]), close_at(nifty, end)
        if a and b:
            row["nifty_pct_during"] = round(100 * (b - a) / a, 2)
    pre_peak = max(pre, key=lambda b: b[2]) if pre else None

    reason = t.get("exit_reason")
    if status == "closed":
        row["result"] = "WIN" if (t.get("pnl_net") or 0) > 0 else "LOSS" if (t.get("pnl_net") or 0) < 0 else "FLAT"
        story.append("Exit: %s at %s at %s. Gross %s, charges %s, net %s (%s)." % (
            {"stop": "stopped out", "target": "target hit", "flat_by": "squared off (flat by)"}.get(reason, reason),
            px(t.get("exit_price")), row["exit_at"], signed(t.get("gross"), 2), money(t.get("charges"), 2),
            signed(t.get("pnl_net"), 2), "%+.2fR" % row["R"] if row["R"] is not None else "n/a"))
    else:
        row["result"] = "OPEN"

    if reason == "target":
        row["why_category"] = "Target hit"
        why.append("The breakout followed through: it reached the 2R target %s at %s, %s min after the fill." % (
            px(target), row["exit_at"], row.get("minutes_held")))
        if row.get("worst_R") is not None and row["worst_R"] < -0.5:
            why.append("It went against us first (to %+.2fR) before it turned." % row["worst_R"])
    elif reason == "stop":
        if delay >= LATE_FILL_MIN and pre_peak and in_r(pre_peak[2]) is not None and in_r(pre_peak[2]) >= 0.5:
            row["why_category"] = "Late fill on a pullback"
            why.append("Filled late, on the way down: the LIMIT waited %d min for the price to come back to %s. "
                       "The breakout had already run to %s (%s, %+.2fR) without us and was fading when it "
                       "filled." % (delay, px(t.get("fill_price")), px(pre_peak[2]), pre_peak[0], in_r(pre_peak[2])))
            if row.get("best_R") is not None:
                why.append("After the fill it never got above %+.2fR and fell to the stop." % row["best_R"])
        elif row.get("best_R") is not None and row["best_R"] >= GAVE_BACK_R:
            row["why_category"] = "Gave back a winner"
            pct = 100 * (row["best_R"] / 2.0)
            why.append("It was winning, then gave it all back: it reached %s at %s (%+.2fR, %.0f%% of the way to "
                       "the target %s) but turned and fell to the stop. Nothing protects a profit once made: the "
                       "stop stays where it started." % (px(best[2]), best[0], row["best_R"], pct, px(target)))
        elif row.get("best_R") is not None and row["best_R"] < NO_FOLLOW_R:
            row["why_category"] = "Failed breakout"
            why.append("The breakout never got going: the best it did was %+.2fR before it fell back through the "
                       "range to the stop." % row["best_R"])
        else:
            row["why_category"] = "Small move, then stopped"
            why.append("A small move up (best %+.2fR) and then back down to the stop." % (row.get("best_R") or 0))
        after = window(bars, row["exit_at"], "15:30")
        if after:
            low = min(after, key=lambda b: b[3])
            back = next((b[0] for b in after if target and b[2] >= target), None)
            last = after[-1][4]
            if back:
                why.append("After the stop it recovered and reached the target at %s: stopped out before the move." %
                           back)
            elif low[3] < t["exit_price"] and qty:
                why.append("After the stop it fell further, to %s by %s (the stop saved about %s more), and ended "
                           "the day at %s." % (px(low[3]), low[0], money(qty * (t["exit_price"] - low[3])), px(last)))
            else:
                why.append("After the stop it ended the day at %s." % px(last))
    elif reason == "flat_by":
        row["why_category"] = "Squared off at 15:10 (%s)" % ("profit" if (t.get("pnl_net") or 0) > 0 else "loss")
        why.append("Neither the stop nor the target by 15:10, so it was squared off at %s. Best was %+.2fR at %s." % (
            px(t.get("exit_price")), row.get("best_R") or 0, row.get("best_at") or "?"))
    elif status in ("open", "filled"):
        row["why_category"] = "Open"
        why.append("Still open when the journal was written.")
    else:
        row["why_category"] = str(reason or status or "?")
    if row.get("nifty_pct_during") is not None and reason in ("stop", "target", "flat_by"):
        n = row["nifty_pct_during"]
        if reason == "stop":
            why.append("The Nifty %s %.2f%% over the same time, so this was %s." % (
                "rose" if n > 0 else "fell", abs(n), "the stock's own weakness, not the market" if n > 0 else
                "partly the market falling"))
        else:
            why.append("The Nifty %s %.2f%% over the same time." % ("rose" if n >= 0 else "fell", abs(n)))
    if weak and reason == "stop":
        why.append("A weak breakout to begin with: %s." % " and ".join(weak))
    if tm_txt and reason in ("stop", "target", "flat_by"):
        why.append(tm_txt)
    row["story"], row["why"] = story, " ".join(why)
    return row


# ---------------------------------------------------------------- one day

def build_day(home, day, cfg):
    rep = ac.read_json(os.path.join(ac.reports_dir(home), "%s.json" % day))
    if not rep:
        return None
    st = ac.load_day(home, day)
    paper = ac.read_json(os.path.join(home, "state", "paper.json")) or {}
    nifty = bars_of(home, day, market.NIFTY)
    rows = []
    for t in sorted((t for t in paper.get("trades") or [] if t.get("date") == day),
                    key=lambda t: (t.get("ordered_at") or "", t.get("id") or "")):
        cand = st["candidates"].get(t["symbol"]) or {}
        rows.append(explain(t, cand, bars_of(home, day, t["symbol"]), nifty, cfg))
    d = rep.get("day") or {}
    sh = (rep.get("shadows") or {}).get("by_why") or {}
    closed = [r for r in rows if r.get("result") in ("WIN", "LOSS", "FLAT")]
    made = sum(r["net"] for r in closed if (r.get("net") or 0) > 0)
    lost = sum(r["net"] for r in closed if (r.get("net") or 0) < 0)
    caps = [c for c in (rep.get("shadows") or {}).get("rows") or [] if c.get("why") == "shadow"]
    return {"date": day, "rows": rows, "report": rep,
            "orders": len(rows), "filled": sum(1 for r in rows if r.get("fill_price") is not None),
            "wins": sum(1 for r in closed if r["result"] == "WIN"),
            "losses": sum(1 for r in closed if r["result"] == "LOSS"),
            "no_fills": sum(1 for r in rows if r.get("result") == "NO FILL"),
            "made": round(made, 2), "lost": round(lost, 2),
            "charges": round(sum(r.get("charges") or 0 for r in closed), 2),
            "net": round(sum(r.get("net") or 0 for r in closed), 2),
            "total_R": round(sum(r.get("R") or 0 for r in closed), 2),
            "nifty_pct": r2(100 * (rep.get("benchmark") or {}).get("pct")) if (rep.get("benchmark") or {}).get(
                "pct") is not None else None,
            "signals": (rep.get("funnel") or {}).get("signals"),
            "not_taken_cap": len(caps), "cap_wins": sum(1 for c in caps if (c.get("net") or 0) > 0),
            "not_taken_cap_net_if_taken": r2((sh.get("shadow") or {}).get("net")),
            "vetoed_net_if_taken": r2((sh.get("vetoed") or {}).get("net")),
            "shorts_shadow_net": r2((sh.get("short_shadow") or {}).get("net")),
            "cap_full_at": cap_time(rows, cfg), "caps": caps, "headline": headline_of(d, rep),
            "cap": int(cfg.get("max_trades_per_day") or 0)}


def cap_time(rows, cfg):
    """When the day's trade cap was used up: the order time of the last allowed order."""
    cap = int(cfg.get("max_trades_per_day") or 0)
    times = sorted(r["ordered_at"] for r in rows if r.get("ordered_at"))
    return times[cap - 1] if cap and len(times) >= cap else None


def headline_of(d, rep):
    if not d.get("orders"):
        return "no paper trades"
    return "%s net, %d won, %d lost, %d not filled" % (signed(d.get("net"), 2), d.get("wins") or 0,
                                                       d.get("losses") or 0, d.get("never_filled") or 0)


# ---------------------------------------------------------------- the words

def day_markdown(D):
    rep = D["report"]
    reg = rep.get("regime") or {}
    L = ["# %s: %s" % (D["date"], D["headline"]), "",
         "Paper trading only: no real money. Every number is from the autopilot's own records and Yahoo's 1-minute "
         "bars, net of estimated Zerodha charges.", "",
         "## The day in numbers", "",
         "| | |", "|---|---|",
         "| Orders placed | %d (%d filled, %d never filled) |" % (D["orders"], D["filled"], D["no_fills"]),
         "| Won | %d trade%s, %s |" % (D["wins"], "" if D["wins"] == 1 else "s", signed(D["made"], 2)),
         "| Lost | %d trade%s, %s |" % (D["losses"], "" if D["losses"] == 1 else "s", signed(D["lost"], 2)),
         "| Charges | %s |" % money(D["charges"], 2),
         "| **Net** | **%s** (%s) |" % (signed(D["net"], 2), "%+.2fR" % D["total_R"]),
         "| Nifty 50 | %s |" % ("%+.2f%%" % D["nifty_pct"] if D["nifty_pct"] is not None else "n/a"),
         "| Market before the open | %s |" % ("Nifty trend %s, India VIX %s (%s)" % (
             reg.get("nifty_trend"), reg.get("vix") or "n/a", reg.get("vix_band") or "n/a")
             if reg.get("nifty_trend") else "n/a"),
         "| Breakout signals | %s |" % (D["signals"] if D["signals"] is not None else "n/a"), ""]
    L += ["## Each trade, and why it made or lost money", ""]
    if not D["rows"]:
        L += ["No paper orders today.", ""]
    for i, r in enumerate(D["rows"], 1):
        L += ["### %d. %s: %s%s" % (i, r["symbol"], r.get("result"),
                                    (", %s" % signed(r["net"], 2)) if r.get("net") is not None else ""), ""]
        L += ["- " + s for s in r.get("story") or []]
        L += ["", "**Why:** %s" % r.get("why"), ""]
    L += ["## Why the day went the way it did", ""]
    L += day_reasons(D)
    caps = D["caps"]
    if caps:
        wins = sorted(caps, key=lambda c: -(c.get("net") or 0))
        L += ["", "## Signals the trade cap kept us out of", "",
              "%d later signals were planned but not traded because the day's %s trades were already used%s. "
              "Replayed with the same rules: %d of %d made money, %s in all if they had been taken." % (
                  len(caps), D["cap"] or "allowed",
                  (" (by %s)" % D["cap_full_at"]) if D["cap_full_at"] else "", D["cap_wins"], len(caps),
                  signed(D["not_taken_cap_net_if_taken"], 2)), "",
              "| Stock | Signal | Result | Net if taken |", "|---|---|---|---|"]
        for c in wins:
            L.append("| %s | %s | %s | %s |" % (c.get("symbol"), c.get("at"), c.get("result"), signed(c.get("net"), 2)))
    other = []
    if D["vetoed_net_if_taken"] is not None:
        other.append("- Claude's pre-market vetoes, replayed: %s if taken." % signed(D["vetoed_net_if_taken"], 2))
    if D["shorts_shadow_net"] is not None:
        other.append("- Short breakdowns (shadow only, never traded), replayed: %s." % signed(D["shorts_shadow_net"], 2))
    if other:
        L += ["", "## Other signals", ""] + other
    if rep.get("analysis"):
        L += ["", "## Claude's end-of-day analysis", "", str(rep["analysis"]).replace("\n## ", "\n### ")
              .replace("## Today in one paragraph", "### Today in one paragraph")]
    return "\n".join(L).rstrip() + "\n"


def day_reasons(D):
    rows = D["rows"]
    out = []
    if not rows:
        return ["No trades, so nothing was made or lost."]
    cats = {}
    for r in rows:
        cats.setdefault(r.get("why_category") or "?", []).append(r["symbol"])
    for k, v in cats.items():
        out.append("- **%s:** %s" % (k, ", ".join(v)))
    sig = [r.get("signal_at") for r in rows if r.get("signal_at")]
    if D["cap_full_at"] and D["not_taken_cap"]:
        out.append("- **The trade slots went to the first signals:** all %d were taken by %s, on the first "
                   "breakouts of the day, first come first served. Nothing ranks the signals, so later and better "
                   "ones (%d of %d replayed green) could not be taken." % (
                       len(rows), D["cap_full_at"], D["cap_wins"], D["not_taken_cap"]))
    tms = [r["tm_p_target"] for r in rows if r.get("tm_p_target") is not None]
    if tms:
        out.append("- **The Time Machine's odds for the trades taken:** %s (average %.0f%% to reach the target "
                   "first). It is shown, not used to choose." % (
                       ", ".join("%s %.0f%%" % (r["symbol"], 100 * r["tm_p_target"]) for r in rows
                                 if r.get("tm_p_target") is not None), 100 * sum(tms) / len(tms)))
    nf = [r for r in rows if r.get("result") == "NO FILL"]
    if nf:
        out.append("- **LIMIT orders at the signal price:** %s never filled because the price did not come back, "
                   "while %s filled only on the way back down. A LIMIT at the breakout price tends to fill the "
                   "breakouts that fail and miss the ones that run." % (
                       ", ".join(r["symbol"] for r in nf),
                       ", ".join(r["symbol"] for r in rows if r.get("why_category") == "Late fill on a pullback")
                       or "none"))
    if sig and D["nifty_pct"] is not None:
        out.append("- **The market:** the Nifty 50 closed %+.2f%%." % D["nifty_pct"])
    return out


def summary_markdown(days, where):
    rows = [r for D in days for r in D["rows"]]
    closed = [r for r in rows if r.get("result") in ("WIN", "LOSS", "FLAT")]
    wins = [r for r in closed if r["result"] == "WIN"]
    losses = [r for r in closed if r["result"] == "LOSS"]
    net = sum(r.get("net") or 0 for r in closed)
    L = ["# NSE paper trading: profit and loss", "",
         "Updated %s IST after the day's report. Paper only: no real money." % nc.to_ist(nc.utc_now()).strftime(
             "%d %b %Y %H:%M"), "",
         "## All days", "", "| | |", "|---|---|",
         "| Days | %d (%d with trades) |" % (len(days), sum(1 for D in days if D["rows"])),
         "| Orders | %d (%d filled, %d never filled) |" % (len(rows), sum(1 for r in rows if r.get("fill_price")),
                                                         sum(1 for r in rows if r.get("result") == "NO FILL")),
         "| Won | %d, %s |" % (len(wins), signed(sum(r["net"] for r in wins), 2)),
         "| Lost | %d, %s |" % (len(losses), signed(sum(r["net"] for r in losses), 2)),
         "| Win rate | %s |" % ("%.0f%%" % (100 * len(wins) / len(closed)) if closed else "n/a"),
         "| Charges | %s |" % money(sum(r.get("charges") or 0 for r in closed), 2),
         "| **Net** | **%s** (%s) |" % (signed(net, 2), "%+.2fR" % sum(r.get("R") or 0 for r in closed)),
         "| Best trade | %s |" % (("%s %s %s" % (max(closed, key=lambda r: r["net"])["date"],
                                               max(closed, key=lambda r: r["net"])["symbol"],
                                               signed(max(closed, key=lambda r: r["net"])["net"], 2)))
                                  if closed else "n/a"),
         "| Worst trade | %s |" % (("%s %s %s" % (min(closed, key=lambda r: r["net"])["date"],
                                                min(closed, key=lambda r: r["net"])["symbol"],
                                                signed(min(closed, key=lambda r: r["net"])["net"], 2)))
                                   if closed else "n/a"), ""]
    L += ["## Why trades made or lost money", "", "| Reason | Trades | Net | Stocks |", "|---|---|---|---|"]
    cats = {}
    for r in rows:
        cats.setdefault(r.get("why_category") or "?", []).append(r)
    for k, v in sorted(cats.items(), key=lambda kv: sum(r.get("net") or 0 for r in kv[1])):
        L.append("| %s | %d | %s | %s |" % (k, len(v), signed(sum(r.get("net") or 0 for r in v), 2),
                                            ", ".join("%s (%s)" % (r["symbol"], r["date"][5:]) for r in v)))
    missed = sum(r.get("missed_gross") or 0 for r in rows)
    L += ["", "## The signals not taken (replayed with the same rules)", "",
          "| | Net if taken |", "|---|---|",
          "| Kept out by the trade cap | %s |" % signed(sum(D["not_taken_cap_net_if_taken"] or 0 for D in days), 2),
          "| Vetoed by Claude before the open | %s |" % signed(sum(D["vetoed_net_if_taken"] or 0 for D in days), 2),
          "| Short breakdowns (shadow only) | %s |" % signed(sum(D["shorts_shadow_net"] or 0 for D in days), 2),
          "| LIMIT orders that never filled (gross, to the target) | %s |" % signed(missed, 2), "",
          "One day is noise. Read these after 20 or more trading days.", "",
          "## Day by day", "", "| Date | Orders | Won | Lost | Not filled | Net | Running total | Nifty | Notes |",
          "|---|---|---|---|---|---|---|---|---|"]
    run = 0.0
    for D in days:
        run += D["net"]
        L.append("| [%s](days/%s.md) | %d | %d | %d | %d | %s | %s | %s | %s |" % (
            D["date"], D["date"], D["orders"], D["wins"], D["losses"], D["no_fills"], signed(D["net"], 2),
            signed(run, 2), "%+.2f%%" % D["nifty_pct"] if D["nifty_pct"] is not None else "n/a",
            "; ".join("%s %s" % (r["symbol"], r.get("why_category")) for r in D["rows"]) or "no trades"))
    if where:
        L += ["", "_%s_" % where]
    return "\n".join(L) + "\n"


README = """# NSE Trading P&L

The paper autopilot's profit-and-loss journal: every trade it placed, what it made or lost, and why.
Paper trading only: no broker, no real money.

- **Summary.md**: running totals, the reasons trades won or lost, the signals not taken, and each day.
- **P&L ledger.csv**: one row per order across all days (opens in Numbers or Excel). The `why` column has
  the reason in words. `best_R` is how far the trade went our way before it ended (1R = the planned risk).
- **Daily P&L.csv**: one row per day, with the running total.
- **days/**: the full write-up of each day.
- **My notes.md**: yours. The autopilot never changes it.

It is rebuilt after each trading day's 15:35 report from the autopilot's own records (reports, paper trades,
archived 1-minute bars), so every number matches the daily email. The reasons are worked out from prices
by fixed rules. To rebuild by hand:

    NSE_TRADER_HOME=~/Trading/nse-autopilot python3 ~/Trading/day-trader-india/autopilot/run_day.py journal
"""


def csv_text(cols, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return buf.getvalue()


# ---------------------------------------------------------------- writing

def report_days(home):
    folder = ac.reports_dir(home)
    if not os.path.isdir(folder):
        return []
    return sorted(f[:10] for f in os.listdir(folder) if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", f))


def folder_of(settings, home):
    j = settings.get("journal") or {}
    return os.path.expanduser(j.get("dir") or DEFAULT_DIR)


def write_all(home, settings, cfg, upto=None):
    """Rebuild the journal; returns (folder written, number of days, note)."""
    days = [D for D in (build_day(home, d, cfg) for d in report_days(home) if not upto or d <= upto) if D]
    target = folder_of(settings, home)
    note = None
    try:
        _write(target, days, None)
    except OSError as e:
        fallback = os.path.join(home, "journal")
        note = "could not write %s (%s); written to %s instead" % (target, e.__class__.__name__, fallback)
        _write(fallback, days, note)
        target = fallback
    return target, len(days), note


def _write(folder, days, note):
    os.makedirs(os.path.join(folder, "days"), exist_ok=True)

    def put(name, text):
        path = os.path.join(folder, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)

    put("README.md", README)
    put("Summary.md", summary_markdown(days, note))
    put("P&L ledger.csv", csv_text(LEDGER_COLS, [r for D in days for r in D["rows"]]))
    run, daily = 0.0, []
    for D in days:
        run += D["net"]
        daily.append(dict(D, running_net=round(run, 2)))
    put("Daily P&L.csv", csv_text(DAILY_COLS, daily))
    for D in days:
        put(os.path.join("days", "%s.md" % D["date"]), day_markdown(D))
    notes = os.path.join(folder, "My notes.md")
    if not os.path.exists(notes):
        put("My notes.md", "# My notes\n\nYour own notes on the trades. The autopilot never changes this file.\n")


def run(home, settings, cfg, show_day=None):
    if show_day:
        D = build_day(home, show_day, cfg)
        if not D:
            print("no report for %s" % show_day, file=sys.stderr)
            return 1
        print(day_markdown(D))
        return 0
    folder, n, note = write_all(home, settings, cfg)
    print("journal: %d day%s in %s%s" % (n, "" if n == 1 else "s", folder, (" (%s)" % note) if note else ""))
    return 0


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="The paper autopilot's P&L journal.")
    ap.add_argument("--print", dest="show_day", metavar="DAY", help="print one day's write-up instead")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    home = ac.trader_home()
    return run(home, ac.load_settings(home), ac.load_cfg(home), a.show_day)


if __name__ == "__main__":
    sys.exit(main())
