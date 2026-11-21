#!/usr/bin/env python3
"""The P&L journal: every paper trade, what it made or lost, and WHY, kept in a folder of its own (by default on
the Desktop) and rebuilt after each day's report. Paper only. Standard library only.

    python3 journal.py               # rebuild the journal from every day's report
    python3 journal.py --print DAY   # print one day's write-up

The folder (autopilot.json "journal": {"enabled": true, "dir": "~/Desktop/NSE Trading P&L"}):

    README.md          what is here and how it is kept
    Summary.md         every paper account side by side, day by day, the reasons trades won or lost, the signals
                       the main account did not take
    P&L ledger.csv     one row per paper order, every account, all days: the numbers and the reason
    Daily P&L.csv      one row per account per day, with each account's running total
    days/<date>.md     the day in full: every account's trades, each one's story and reason, the market, the
                       signals not taken (the trade cap, an account's own filter), Claude's analysis
    My notes.md        yours: created once, never touched again

Everything is rebuilt from the trader folder's own records (reports/<date>.json, state/paper.json and each paper
account's accounts/cap<N>/state/paper.json, the day's state and the archived 1-minute bars), so the journal can always be deleted and rebuilt. Every number and every
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
import accounts  # noqa: E402
import autopilot_common as ac  # noqa: E402
import market  # noqa: E402

nc = ac.nc

DEFAULT_DIR = "~/Desktop/NSE Trading P&L"
LATE_FILL_MIN = 15           # a LIMIT that filled this long after the order: a pullback fill
GAVE_BACK_R = 1.0            # a stopped trade that had been this far in profit "gave back a winner"
NO_FOLLOW_R = 0.5            # a stopped trade that never got this far "never got going"

LEDGER_COLS = ["date", "account", "main", "symbol", "result", "why_category", "signal_at", "ordered_at", "fill_at", "fill_price", "qty",
               "position_value", "stop", "target", "exit_at", "exit_price", "exit_reason", "minutes_held", "gross",
               "charges", "net", "R", "best_R", "best_at", "worst_R", "nifty_pct_during", "tm_p_target",
               "tm_verdict", "breakout_pct_over_range", "volume_x", "why"]
DAILY_COLS = ["date", "account", "main", "orders", "filled", "wins", "losses", "no_fills", "made", "lost", "charges", "net",
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

def ttl_of(note):
    """The minutes in a paper cancel note "did not fill within 15 minutes of the order (entry_ttl_minutes)", else
    None (a LIMIT that waited to flat_by)."""
    m = re.search(r"within (\d+(?:\.\d+)?) minutes", note or "")
    return float(m.group(1)) if m and "entry_ttl_minutes" in note else None


def explain(t, cand, bars, nifty, cfg, cancel_note=None):
    """The trade's row: the numbers, the price path around it, a category and the reason in plain words.
    cancel_note: the paper broker's note when it cancelled an unfilled LIMIT (state/broker.json)."""
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
        flat = t.get("flat_by") or cfg.get("flat_by") or "15:10"
        after = window(bars, row["ordered_at"], flat, include_after=True)
        ttl = ttl_of(cancel_note) if status == "cancelled" else None
        live = after
        if ttl and row["ordered_at"]:           # the order's own life: from the order to its cancel
            end = mins(row["ordered_at"]) + int(ttl)
            live = window(bars, row["ordered_at"], "%02d:%02d" % (end // 60, end % 60), include_after=True) or after
        row["result"] = "NO FILL"
        if after:
            low = min(live, key=lambda b: b[3])
            high = max(after, key=lambda b: b[2])
            hit_t = next((b[0] for b in after if target and b[2] >= target), None)
            hit_s = next((b[0] for b in after if stop and b[3] <= stop), None)
            ran = low[3] > (t.get("limit") or 0)
            row["why_category"] = "Never filled: price ran away" if ran else "Never filled"
            why.append("The LIMIT buy at %s never filled: %s the lowest price was %s (%s), so the price never "
                       "came back to the entry; it went on to %s (%s)." % (
                           px(t.get("limit")), "in the %d minutes the order was live" % ttl if ttl
                           else "after the order", px(low[3]), low[0], px(high[2]), high[0]))
            if hit_t and (not hit_s or hit_t < hit_s) and qty and target and t.get("limit"):
                missed = qty * (target - t["limit"])
                why.append("Bought at the signal price it would have reached the 2R target at %s: about %s gross "
                           "missed." % (hit_t, signed(missed)))
                row["missed_gross"] = round(missed, 2)
        else:
            row["why_category"] = "Never filled"
            why.append("The LIMIT buy at %s never filled (no bars to say more)." % px(t.get("limit")))
        if ttl:
            why.append("The order was cancelled after %d minutes unfilled (entry_ttl_minutes), which freed its "
                       "slot for a later signal." % ttl)
        else:
            why.append("It still counted as one of the day's %s trades, so it blocked a later signal until the end "
                       "of the day." % (cfg.get("max_trades_per_day") or 3))
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

def short_label(cap, f=None):
    """"5 a day + volume filter": an account in a table column."""
    return "%d a day%s" % (int(cap or 0), " + volume filter" if f else "")


def day_accounts(home, day, rep, cfg):
    """The paper accounts that traded on `day`, the main account first: from the day's report (its "accounts"
    rows), or the main account alone on a day before there were others. "name" is the rules it traded by ("cap5"),
    so one set of rules lines up across days even when the main account changes; "home" is its folder."""
    rows = rep.get("accounts")
    out = []
    if isinstance(rows, list) and rows:
        for a in rows:
            cap = int(a.get("cap") or 0)
            out.append({"name": a.get("name") or accounts.name_of(cap), "cap": cap, "main": bool(a.get("main")),
                        "filter": a.get("filter"), "budget": a.get("budget"),
                        "home": home if a.get("main") else accounts.home_of(home, cap)})
    else:
        r = rep.get("rules") or {}
        cap = int(r.get("max_trades_per_day") or cfg.get("max_trades_per_day") or 0)
        out.append({"name": accounts.name_of(cap), "cap": cap, "main": True, "filter": r.get("filter"),
                    "budget": r.get("budget") or cfg.get("budget"), "home": home})
    for a in out:
        a["label"] = accounts.label_of(a["cap"], a["filter"], a["main"])
        a["short"] = short_label(a["cap"], a["filter"])
    out.sort(key=lambda a: (not a["main"], a["cap"]))
    return out


def cancel_notes(home, day):
    """{fingerprint: the paper broker's note} for the day's LIMIT buys it cancelled unfilled (state/broker.json)."""
    state = ac.read_json(os.path.join(home, "state", "broker.json")) or {}
    out = {}
    for r in state.get("orders") or []:
        if isinstance(r, dict) and r.get("date") == day and r.get("status") == "cancelled" and r.get("fingerprint"):
            out[r["fingerprint"]] = re.sub(r"^paper LIMIT buy cancelled: ", "", r.get("plain") or "")
    return out


def replays_of(rep):
    """{symbol: {"result", "net"}}: what each long signal did, from the main account's real trade or its replay."""
    out = {}
    for r in (rep.get("shadows") or {}).get("rows") or []:
        if r.get("side") != "short" and r.get("symbol"):
            out[r["symbol"]] = {"result": r.get("result"), "net": r.get("net")}
    for t in rep.get("trades") or []:
        if t.get("symbol"):
            out[t["symbol"]] = {"result": t.get("exit_reason"), "net": t.get("net")}
    return out


def build_account(A, day, rep, st, nifty, bars_for, cfg, replays):
    """One account's day: each trade explained, the totals, and the signals it did not take (the trade cap, its
    own filter), each with what it did when replayed."""
    paper = ac.read_json(os.path.join(A["home"], "state", "paper.json")) or {}
    notes = cancel_notes(A["home"], day)
    acfg = dict(cfg, max_trades_per_day=A["cap"])
    rows = []
    for t in sorted((t for t in paper.get("trades") or [] if t.get("date") == day),
                    key=lambda t: (t.get("ordered_at") or "", t.get("id") or "")):
        cand = st["candidates"].get(t["symbol"]) or {}
        r = explain(t, cand, bars_for(t["symbol"]), nifty, acfg, notes.get(t.get("fingerprint")))
        r.update(account=A["short"], main="yes" if A["main"] else "no")
        rows.append(r)
    closed = [r for r in rows if r.get("result") in ("WIN", "LOSS", "FLAT")]
    B = dict(A, rows=rows, orders=len(rows), filled=sum(1 for r in rows if r.get("fill_price") is not None),
             wins=sum(1 for r in closed if r["result"] == "WIN"),
             losses=sum(1 for r in closed if r["result"] == "LOSS"),
             no_fills=sum(1 for r in rows if r.get("result") == "NO FILL"),
             made=round(sum(r["net"] for r in closed if (r.get("net") or 0) > 0), 2),
             lost=round(sum(r["net"] for r in closed if (r.get("net") or 0) < 0), 2),
             charges=round(sum(r.get("charges") or 0 for r in closed), 2),
             net=round(sum(r.get("net") or 0 for r in closed), 2),
             total_R=round(sum(r.get("R") or 0 for r in closed), 2), cap_full_at=cap_time(rows, acfg))
    budget = float(A.get("budget") or cfg.get("budget") or 0)
    B["return_pct"] = round(100 * B["net"] / budget, 2) if budget else None
    longs = [c for c in rep.get("candidates") or [] if (c.get("side") or "long") == "long" and c.get("symbol")]
    rvol = {c["symbol"]: c.get("open_rvol") for c in longs}
    if A["main"]:
        sh = [c for c in (rep.get("shadows") or {}).get("rows") or [] if c.get("side") != "short"]
        caps = [c for c in sh if c.get("why") == "shadow"]
        kept = [dict(c, open_rvol=rvol.get(c.get("symbol"))) for c in sh if c.get("why") == "filtered"]
        no_size = sum(1 for c in longs if c.get("outcome") == "size_refused")
    else:
        caps, kept, no_size = [], [], 0
        for c in longs:
            o = ((c.get("accounts") or {}).get(A["name"]) or {}).get("outcome")
            no_size += o == "size_refused"
            if o in ("shadow", "filtered"):
                r = replays.get(c["symbol"]) or {}
                (caps if o == "shadow" else kept).append({
                    "symbol": c["symbol"], "at": c.get("scan_at") or c.get("first_seen"), "result": r.get("result"),
                    "net": r.get("net"), "open_rvol": c.get("open_rvol")})
    B.update(caps=caps, kept_out=kept, no_size=no_size, not_taken_cap=len(caps),
             cap_wins=sum(1 for c in caps if (c.get("net") or 0) > 0),
             not_taken_cap_net_if_taken=r2(sum(c.get("net") or 0 for c in caps)) if caps else None,
             kept_out_net_if_taken=r2(sum(c.get("net") or 0 for c in kept)) if kept else None)
    B["headline"] = headline_of(B)
    return B


def build_day(home, day, cfg):
    """The day for the journal: every paper account's trades and reasons, the main account's at the top level."""
    rep = ac.read_json(os.path.join(ac.reports_dir(home), "%s.json" % day))
    if not rep:
        return None
    st = ac.load_day(home, day)
    nifty = bars_of(home, day, market.NIFTY)
    memo = {}

    def bars_for(sym):
        if sym not in memo:
            memo[sym] = bars_of(home, day, sym)
        return memo[sym]
    replays = replays_of(rep)
    accs = [build_account(A, day, rep, st, nifty, bars_for, cfg, replays) for A in day_accounts(home, day, rep, cfg)]
    sh = (rep.get("shadows") or {}).get("by_why") or {}
    D = dict(accs[0])
    D.update(date=day, report=rep, accounts=accs, main=accs[0],
             nifty_pct=r2(100 * (rep.get("benchmark") or {}).get("pct"))
             if (rep.get("benchmark") or {}).get("pct") is not None else None,
             signals=(rep.get("funnel") or {}).get("signals"),
             vetoed_net_if_taken=r2((sh.get("vetoed") or {}).get("net")),
             shorts_shadow_net=r2((sh.get("short_shadow") or {}).get("net")))
    return D


def cap_time(rows, cfg):
    """When the day's trade cap was used up: the order time of the last allowed order."""
    cap = int(cfg.get("max_trades_per_day") or 0)
    times = sorted(r["ordered_at"] for r in rows if r.get("ordered_at"))
    return times[cap - 1] if cap and len(times) >= cap else None


def headline_of(B):
    if not B.get("orders"):
        return "no paper trades"
    return "%s net, %d won, %d lost, %d not filled" % (signed(B.get("net"), 2), B.get("wins") or 0,
                                                       B.get("losses") or 0, B.get("no_fills") or 0)


# ---------------------------------------------------------------- the words

def result_text(c):
    if c.get("result") == "no plan":
        return "not replayed (no size in the plan)"
    return EXIT_WORDS.get(c.get("result"), c.get("result") or "n/a")


EXIT_WORDS = {"target": "target", "stop": "stop", "flat_by": "flat by 15:10", "cancelled": "never filled"}


def day_markdown(D):
    rep = D["report"]
    reg = rep.get("regime") or {}
    accs = D["accounts"]
    multi = len(accs) > 1
    title = ("main account (%s) %s" % (D["main"]["short"], D["headline"])) if multi else D["headline"]
    L = ["# %s: %s" % (D["date"], title), "",
         "Paper trading only: no real money. Every number is from the autopilot's own records and Yahoo's 1-minute "
         "bars, net of estimated Zerodha charges.", "",
         "## The day in numbers", "", "| | |", "|---|---|"]
    if not multi:
        L += numbers_rows(D["main"])
    L += ["| Nifty 50 | %s |" % ("%+.2f%%" % D["nifty_pct"] if D["nifty_pct"] is not None else "n/a"),
          "| Market before the open | %s |" % ("Nifty trend %s, India VIX %s (%s)" % (
              reg.get("nifty_trend"), reg.get("vix") or "n/a", reg.get("vix_band") or "n/a")
              if reg.get("nifty_trend") else "n/a"),
          "| Breakout signals | %s |" % (D["signals"] if D["signals"] is not None else "n/a"), ""]
    if multi:
        L += ["Each paper account has %s of its own and trades the same signals by its own rules." %
              money(D["main"].get("budget") or 0), "",
              "| Account | Orders | Won | Lost | Not filled | Charges | Net | Return |",
              "|---|---|---|---|---|---|---|---|"]
        for B in accs:
            L.append("| %s | %d | %d | %d | %d | %s | **%s** (%+.2fR) | %s |" % (
                B["label"], B["orders"], B["wins"], B["losses"], B["no_fills"], money(B["charges"], 2),
                signed(B["net"], 2), B["total_R"],
                "%+.2f%%" % B["return_pct"] if B.get("return_pct") is not None else "n/a"))
        L.append("")
    seen = {}
    for B in accs:
        L += account_markdown(B, D, seen, multi)
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


def numbers_rows(B):
    return ["| Orders placed | %d (%d filled, %d never filled) |" % (B["orders"], B["filled"], B["no_fills"]),
            "| Won | %d trade%s, %s |" % (B["wins"], "" if B["wins"] == 1 else "s", signed(B["made"], 2)),
            "| Lost | %d trade%s, %s |" % (B["losses"], "" if B["losses"] == 1 else "s", signed(B["lost"], 2)),
            "| Charges | %s |" % money(B["charges"], 2),
            "| **Net** | **%s** (%s) |" % (signed(B["net"], 2), "%+.2fR" % B["total_R"])]


def account_markdown(B, D, seen, multi):
    """One account's part of the day page. On a day with one account it reads as the page itself (## headings);
    with several, each account is a ## section of its own. A trade another account above made the same way (same
    stock, fill and exit) is told once and referred to after that."""
    h = "###" if multi else "##"
    L = []
    if multi:
        L += ["## %s: %s" % (B["label"], B["headline"]), ""]
        if B["orders"]:
            L += ["%d order%s (%d filled, %d never filled): %d won %s, %d lost %s; charges %s; **net %s** (%+.2fR)."
                  % (B["orders"], "" if B["orders"] == 1 else "s", B["filled"], B["no_fills"], B["wins"],
                     signed(B["made"], 2), B["losses"], signed(B["lost"], 2), money(B["charges"], 2),
                     signed(B["net"], 2), B["total_R"]), ""]
    L += ["%s Each trade, and why it made or lost money" % h, ""]
    if not B["rows"]:
        L += ["No paper orders today.", ""]
    for i, r in enumerate(B["rows"], 1):
        L += ["%s# %d. %s: %s%s" % (h, i, r["symbol"], r.get("result"),
                                    (", %s" % signed(r["net"], 2)) if r.get("net") is not None else ""), ""]
        key = (r["symbol"], r.get("result"), r.get("fill_price"), r.get("fill_at"), r.get("exit_price"),
               r.get("exit_at"), r.get("ordered_at"))
        if key in seen:
            first, qty = seen[key]
            L += ["Same order, fill and exit as in the %s account above, with %s shares here (%s there): net %s "
                  "(%s). %s" % (first, r.get("qty"), qty, signed(r.get("net"), 2),
                                "%+.2fR" % r["R"] if r.get("R") is not None else "n/a",
                                "Why: %s." % r.get("why_category") if r.get("why_category") else ""), ""]
            continue
        seen[key] = (B["short"], r.get("qty"))
        L += ["- " + s for s in r.get("story") or []]
        L += ["", "**Why:** %s" % r.get("why"), ""]
    L += ["%s Why the day went the way it did" % h, ""]
    L += day_reasons(B, D, market_line=not multi)
    caps = B["caps"]
    if caps:
        wins = sorted(caps, key=lambda c: -(c.get("net") or 0))
        L += ["", "%s Signals the trade cap kept %s out of" % (h, "us" if B["main"] else "this account"), "",
              "%d later signals were planned but not traded because the day's %s trades were already used%s. "
              "Replayed with the same rules: %d of %d made money, %s in all if they had been taken%s." % (
                  len(caps), B["cap"] or "allowed",
                  (" (by %s)" % B["cap_full_at"]) if B["cap_full_at"] else "", B["cap_wins"], len(caps),
                  signed(B["not_taken_cap_net_if_taken"], 2),
                  "" if B["main"] else " (the main account's replay of each, or its real trade, at its size)"), "",
              "| Stock | Signal | Result | Net if taken |", "|---|---|---|---|"]
        for c in wins:
            L.append("| %s | %s | %s | %s |" % (c.get("symbol"), c.get("at"), result_text(c),
                                                signed(c.get("net"), 2) if c.get("net") is not None else "n/a"))
    if B.get("filter"):
        L += ["", "%s What the opening-volume filter kept out" % h, ""]
        kept = B["kept_out"]
        f = B["filter"].get("min_open_rvol")
        if not kept:
            L.append("This account takes a signal only when the stock's opening volume (09:20-09:30) is at least "
                     "%gx its 20-day median. It kept out none of today's signals." % f)
        else:
            nets = [c["net"] for c in kept if c.get("net") is not None]
            L += ["This account takes a signal only when the stock's opening volume (09:20-09:30) is at least %gx "
                  "its 20-day median. It kept out %d signal%s. Replayed with the same rules, %d of %d made money: %s "
                  "net if they had been taken%s." % (
                      f, len(kept), "" if len(kept) == 1 else "s", sum(1 for x in nets if x > 0), len(nets),
                      signed(sum(nets), 2) if nets else "n/a",
                      "" if B["main"] else " (the main account's replay of each, or its real trade, at its size)"),
                  "", "| Stock | Signal | Opening volume | Result | Net if taken |", "|---|---|---|---|---|"]
            for c in sorted(kept, key=lambda c: -(c.get("net") or 0)):
                L.append("| %s | %s | %s | %s | %s |" % (
                    c.get("symbol"), c.get("at"), "%.2fx" % c["open_rvol"] if c.get("open_rvol") is not None
                    else "not known", result_text(c), signed(c.get("net"), 2) if c.get("net") is not None else "n/a"))
    L.append("")
    return L


def day_reasons(B, D=None, market_line=True):
    rows = B["rows"]
    out = []
    if not rows:
        return ["No trades, so nothing was made or lost."]
    cats = {}
    for r in rows:
        cats.setdefault(r.get("why_category") or "?", []).append(r["symbol"])
    for k, v in cats.items():
        out.append("- **%s:** %s" % (k, ", ".join(v)))
    sig = [r.get("signal_at") for r in rows if r.get("signal_at")]
    if B["cap_full_at"] and B["not_taken_cap"]:
        out.append("- **The trade slots went to the first signals:** all %d were taken by %s, on the first "
                   "breakouts of the day, first come first served. Nothing ranks the signals, so later and better "
                   "ones (%d of %d replayed green) could not be taken." % (
                       len(rows), B["cap_full_at"], B["cap_wins"], B["not_taken_cap"]))
    elif B.get("no_size") and len(rows) < (B.get("cap") or 0):
        out.append("- **The cash, not the cap, set the limit:** %d of %d trades were used; %d later signal%s found "
                   "no size that fit while the money was in open trades." % (
                       len(rows), B["cap"], B["no_size"], "" if B["no_size"] == 1 else "s"))
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
    nifty = (D or B).get("nifty_pct")
    if market_line and sig and nifty is not None:
        out.append("- **The market:** the Nifty 50 closed %+.2f%%." % nifty)
    return out


def strategies(days, now_main=None):
    """Each set of rules across the days it ran, in cap order: {name: {"label", "short", "days": [(D, B)]}}.
    now_main: the name of the main account's rules today (config.json), marked in the tables."""
    out = {}
    for D in days:
        for B in D["accounts"]:
            s = out.setdefault(B["name"], {"name": B["name"], "cap": B["cap"], "days": []})
            s["label"] = accounts.label_of(B["cap"], B.get("filter"))
            s["short"], s["budget"] = B["short"], B.get("budget")
            s["days"].append((D, B))
    for s in out.values():
        s["now_main"] = s["name"] == now_main
    return sorted(out.values(), key=lambda s: (not s["now_main"], s["cap"]))


def summary_markdown(days, where, now_main=None):
    st = strategies(days, now_main)
    L = ["# NSE paper trading: profit and loss", "",
         "Updated %s IST after the day's report. Paper only: no real money." % nc.to_ist(nc.utc_now()).strftime(
             "%d %b %Y %H:%M"), "",
         "## The accounts, all days", "",
         "Each paper account has its own money and trades the day's signals by its own rules. A row follows one set "
         "of rules on every day it ran, whether as the main account or as an account of its own.", "",
         "| Account | Days | Orders | Won | Lost | Win rate | Charges | **Net** | Return | Best trade | Worst trade |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in st:
        rows = [r for _, B in s["days"] for r in B["rows"]]
        closed = [r for r in rows if r.get("result") in ("WIN", "LOSS", "FLAT")]
        wins = [r for r in closed if r["result"] == "WIN"]
        net = sum(r.get("net") or 0 for r in closed)
        best = max(closed, key=lambda r: r["net"]) if closed else None
        worst = min(closed, key=lambda r: r["net"]) if closed else None
        L.append("| %s | %d | %d | %d | %d | %s | %s | **%s** (%+.2fR) | %s | %s | %s |" % (
            s["label"] + (" (main account now)" if s["now_main"] else ""), len(s["days"]), len(rows), len(wins),
            sum(1 for r in closed if r["result"] == "LOSS"),
            "%.0f%%" % (100 * len(wins) / len(closed)) if closed else "n/a",
            money(sum(r.get("charges") or 0 for r in closed), 2), signed(net, 2),
            sum(r.get("R") or 0 for r in closed),
            "%+.2f%%" % (100 * net / float(s["budget"])) if s.get("budget") else "n/a",
            "%s %s %s" % (best["date"][5:], best["symbol"], signed(best["net"], 2)) if best else "n/a",
            "%s %s %s" % (worst["date"][5:], worst["symbol"], signed(worst["net"], 2)) if worst else "n/a"))
    L += ["", "## Day by day", "",
          "Bold: the main account that day. A dash: that account was not running yet.", "",
          "| Date | Nifty | %s | The main account's trades |" % " | ".join(s["short"] for s in st),
          "|---|---|%s---|" % ("---|" * len(st))]
    for D in days:
        cells = []
        for s in st:
            B = next((B for d, B in s["days"] if d is D), None)
            cells.append("—" if B is None else ("**%s**" if B["main"] else "%s") % signed(B["net"], 2))
        L.append("| [%s](days/%s.md) | %s | %s | %s |" % (
            D["date"], D["date"], "%+.2f%%" % D["nifty_pct"] if D["nifty_pct"] is not None else "n/a",
            " | ".join(cells),
            "; ".join("%s %s" % (r["symbol"], r.get("why_category")) for r in D["main"]["rows"]) or "no trades"))
    L += ["", "## Why trades made or lost money", "", "| Reason | Account | Trades | Net | Stocks |", "|---|---|---|---|---|"]
    for s in st:                       # account by account (the main account first), worst reason first
        cats = {}
        for D, B in s["days"]:
            for r in B["rows"]:
                cats.setdefault(r.get("why_category") or "?", []).append(r)
        for k, v in sorted(cats.items(), key=lambda kv: sum(r.get("net") or 0 for r in kv[1])):
            L.append("| %s | %s | %d | %s | %s |" % (k, s["short"], len(v), signed(sum(r.get("net") or 0 for r in v), 2),
                                                     ", ".join("%s (%s)" % (r["symbol"], r["date"][5:]) for r in v)))
    mains = [D["main"] for D in days]
    missed = sum(r.get("missed_gross") or 0 for B in mains for r in B["rows"])
    L += ["", "## The signals the main account did not take (replayed with the same rules)", "",
          "| | Net if taken |", "|---|---|",
          "| Kept out by the trade cap | %s |" % signed(sum(B["not_taken_cap_net_if_taken"] or 0 for B in mains), 2)]
    if any(B["kept_out"] for B in mains):
        L.append("| Kept out by the opening-volume filter | %s |" % signed(
            sum(B["kept_out_net_if_taken"] or 0 for B in mains), 2))
    L += ["| Vetoed by Claude before the open | %s |" % signed(sum(D["vetoed_net_if_taken"] or 0 for D in days), 2),
          "| Short breakdowns (shadow only) | %s |" % signed(sum(D["shorts_shadow_net"] or 0 for D in days), 2),
          "| LIMIT orders that never filled (gross, to the target) | %s |" % signed(missed, 2), "",
          "One day is noise. Read these after 20 or more trading days."]
    if where:
        L += ["", "_%s_" % where]
    return "\n".join(L) + "\n"


README = """# NSE Trading P&L

The paper autopilot's profit-and-loss journal: every trade it placed, what it made or lost, and why.
Paper trading only: no broker, no real money.

- **Summary.md**: every paper account side by side (totals, day by day), the reasons trades won or lost,
  and the signals the main account did not take.
- **P&L ledger.csv**: one row per order, every account, all days (opens in Numbers or Excel). `account` says
  which account ("5 a day + volume filter"), `main` whether it was the main account that day. The `why`
  column has the reason in words. `best_R` is how far the trade went our way before it ended (1R = the
  planned risk).
- **Daily P&L.csv**: one row per account per day, with each account's running total.
- **days/**: the full write-up of each day: every account's trades and why each made or lost money.
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
    now_main = accounts.name_of(cfg.get("max_trades_per_day") or 0)
    try:
        _write(target, days, None, now_main)
    except OSError as e:
        fallback = os.path.join(home, "journal")
        note = "could not write %s (%s); written to %s instead" % (target, e.__class__.__name__, fallback)
        _write(fallback, days, note, now_main)
        target = fallback
    return target, len(days), note


def _write(folder, days, note, now_main=None):
    os.makedirs(os.path.join(folder, "days"), exist_ok=True)

    def put(name, text):
        path = os.path.join(folder, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)

    put("README.md", README)
    put("Summary.md", summary_markdown(days, note, now_main))
    put("P&L ledger.csv", csv_text(LEDGER_COLS, [r for D in days for B in D["accounts"] for r in B["rows"]]))
    run, daily = {}, []
    for D in days:
        for B in D["accounts"]:
            run[B["name"]] = run.get(B["name"], 0.0) + B["net"]
            row = dict(B, date=D["date"], account=B["short"], main="yes" if B["main"] else "no",
                       running_net=round(run[B["name"]], 2), nifty_pct=D["nifty_pct"], signals=D["signals"])
            if B["main"]:
                row.update(vetoed_net_if_taken=D["vetoed_net_if_taken"], shorts_shadow_net=D["shorts_shadow_net"])
            else:
                row.update(vetoed_net_if_taken=None, shorts_shadow_net=None)
            daily.append(row)
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
