#!/usr/bin/env python3
"""Replay every opening-range signal in the bar archive, long and short, with the paper broker's own rules, and
say whether there is an edge. Standard library only; reads $NSE_TRADER_HOME/archive (run_day.py archive).

    python3 study.py                      # every day that has both 5-minute and 1-minute bars
    python3 study.py --from 2026-11-01 --json

For each archived day: the scanner's ORB rule on the 5-minute bars (nse-scanner scan_orb, --side both, up to
14:30) finds each stock's first breakout and first breakdown; each is planned (long: nse-exit's or-mid stop
and 2R target, rebuilt here; short: shorts.plan) and sized by nse_common.size_qty against config.json, then
replayed on the day's 1-minute bars by report.shadow (ticket.walk: LIMIT entry, SL stop, target as an alert,
flat by 15:10). Every signal is counted, as in the report's "every signal" group; the trade cap, the loss
limit, Claude's veto and nse-risk are NOT applied here.

Signals on one day are not independent (the market moves them together), so the verdict that matters is the
one on DAILY averages: n is the number of days, not trades. Paper, after estimated charges; not advice.

Slippage. Every paper fill is exact (a LIMIT fills on a touch, a stop at its trigger), so real fills can only be
worse. Each signal carries bp_r, the R that one basis point of slippage on each side costs it, and the study
shows the edge at 0, 1, 2, 3 and 5 bps a side and the break-even slippage (report.slippage).

    python3 study.py --bars 5m            # replay on 5-minute bars: every day with 5-minute bars (Yahoo keeps
                                          # 60 days of those, 30 of 1-minute ones)
    python3 study.py --calibrate          # on days with both, the 5-minute replay against the 1-minute one

A 5-minute replay is coarser: the target's market sell waits for the next 5-minute bar's open, and a bar that
touches both the stop and the target counts as the stop, which happens more often on a 5-minute bar.
--calibrate measures that difference on the days that have both, so the 5-minute result can be read with it.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402
import market  # noqa: E402
import report  # noqa: E402
import shorts  # noqa: E402
import sizes  # noqa: E402

nc = ac.nc
_SCAN = None


def scanner():
    global _SCAN
    if _SCAN is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dti_study_scan", os.path.join(ac.REPO, "nse-scanner", "scan.py"))
        _SCAN = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SCAN)
    return _SCAN


STEP = {"1m": 1, "5m": 5}


def days_with(home, intervals):
    root = os.path.join(home, "archive")
    have = lambda iv: set(os.listdir(os.path.join(root, iv))) if os.path.isdir(os.path.join(root, iv)) else set()
    out = None
    for iv in intervals:
        out = have(iv) if out is None else out & have(iv)
    return sorted(out or [])


def days_with_both(home):
    return days_with(home, ("1m", "5m"))


def days_for(home, bars):
    """The days a replay on `bars` can use: 1m needs both (5-minute signals, 1-minute replay); 5m needs 5m."""
    return days_with_both(home) if bars == "1m" else days_with(home, ("5m",))


def load(home, interval, day):
    folder = os.path.join(home, "archive", interval, day)
    out = {}
    for f in sorted(os.listdir(folder)):
        if f.startswith("_") or not f.endswith(".json.gz"):
            continue                                        # ^NSEI / ^INDIAVIX are context, not tradeable
        doc = market.read_day(os.path.join(folder, f))
        if doc and doc.get("bars"):
            out[doc["symbol"]] = doc["bars"]
    return out


def series(bars):
    """Archive rows -> the scanner's series shape."""
    return {"t": [b[0] for b in bars], "o": [b[1] for b in bars], "h": [b[2] for b in bars],
            "l": [b[3] for b in bars], "c": [b[4] for b in bars], "v": [b[5] or 0 for b in bars], "meta": {}}


def minute_bars(bars):
    """Archive rows -> ticket.walk's (ist datetime, o, h, l, c), rounded like ticket.parse_bars."""
    return [(nc.ist_from_epoch(b[0]), round(float(b[1]), 2), round(float(b[2]), 2), round(float(b[3]), 2),
             round(float(b[4]), 2)) for b in bars if None not in b[1:5]]


def long_plan(hit, cfg):
    """nse-exit's ORB plan (or-mid stop rounded down, 2R target), the mirror of shorts.plan."""
    sym = nc.nse_symbol(hit["symbol"])
    e = shorts.grid(hit["price"], "nearest", sym, cfg)
    oh, ol = shorts.grid(hit["or_high"], "nearest", sym, cfg), shorts.grid(hit["or_low"], "nearest", sym, cfg)
    stop = shorts.grid((oh + ol) / 2, "down", sym, cfg)
    if stop >= e:
        raise shorts.ShortPlanError("stop not under the entry")
    tick = nc.order_tick(e, sym, cfg)
    risk = e - stop
    if risk < max(e * shorts.MIN_RISK_PCT / 100, shorts.MIN_RISK_TICKS * tick):
        raise shorts.ShortPlanError("too tight")
    target = shorts.grid(e + 2 * risk, "nearest", sym, cfg)
    qty = nc.size_qty(e, stop, sym, cfg)
    if qty < 1:
        raise shorts.ShortPlanError("no whole share fits")
    worst = nc.loss_at_stop(qty, e, stop, sym, cfg)
    per = nc.dec(nc.intraday_charges(qty, e, stop, cfg)["total"]) / qty
    return {"side": "long", "entry": float(e), "stop": float(stop), "target": float(target),
            "worst_exit": float(worst["exit_price"]), "quantity": int(qty), "flat_by": cfg.get("flat_by") or "15:10",
            "max_loss": float(worst["total"]), "charges_r": round(float(per / risk), 3)}


def study(home, cfg, max_charges_r, days, until="14:30", bars="1m"):
    """Every ORB signal on `days`, found on the 5-minute bars and replayed on `bars` ("1m", or "5m" for days whose
    1-minute bars are gone)."""
    if bars not in STEP:
        raise ValueError("bars must be 1m or 5m")
    scan = scanner()
    rows = []
    for day in days:
        d = datetime.date.fromisoformat(day)
        five = load(home, "5m", day)
        one = load(home, "1m", day) if bars == "1m" else five
        stage1 = {s: series(b) for s, b in five.items()}
        cutoff = datetime.datetime.combine(d, nc.parse_hhmm(until)) + datetime.timedelta(minutes=1)
        hits, _, _ = scan.scan_orb(stage1, sorted(stage1), d, cutoff, 15, cfg=cfg, side="both")
        for h in hits:
            side = h.get("side") or "long"
            try:
                p = shorts.plan(h, cfg) if side == "short" else long_plan(h, cfg)
            except shorts.ShortPlanError:
                continue
            if h["symbol"] not in one:
                continue
            c = {"symbol": h["symbol"], "side": side, "outcome": side, "trigger": {"at": h["at"]}, "plan": p}
            r = report.shadow(c, day, minute_bars(one[h["symbol"]]), cfg, step=STEP[bars])
            if r.get("result") == "never filled":
                rows.append({"day": day, "symbol": h["symbol"], "side": side, "at": h["at"], "r": None, "net": 0.0,
                             "result": "never filled", "charges_r": p["charges_r"], "entry": p["entry"],
                             "stop": p["stop"], "fill": None, "exit": None, "exit_at": None, "bp_r": None,
                             "qty": p["quantity"], "max_loss": p["max_loss"],
                             "cost_ok": p["charges_r"] <= max_charges_r,
                             "bar_volume": bar_volume(five[h["symbol"]], d, h.get("bar_start"))})
                continue
            if r.get("r") is None:
                continue
            rows.append({"day": day, "symbol": h["symbol"], "side": side, "at": h["at"], "r": r["r"],
                         "net": r["net"], "result": r["result"], "charges_r": p["charges_r"],
                         "cost_ok": p["charges_r"] <= max_charges_r, "entry": p["entry"], "stop": p["stop"],
                         "fill": r["fill"], "exit": r["exit"], "exit_at": r.get("exit_at"), "bp_r": r.get("bp_r"),
                         "qty": p["quantity"], "max_loss": p["max_loss"],
                         "bar_volume": bar_volume(five[h["symbol"]], d, h.get("bar_start"))})
    return rows


def bar_volume(bars, day, start):
    """Shares traded in the 5-minute bar that fired the signal (starting at `start`, IST HH:MM), or None."""
    if not start:
        return None
    want = datetime.datetime.combine(day, nc.parse_hhmm(start))
    for b in bars:
        if nc.ist_from_epoch(b[0]) == want:
            return b[5] or None
    return None


GROUPS = (("Long, every signal", lambda r: r["side"] == "long"),
          ("Long, charges within the filter", lambda r: r["side"] == "long" and r["cost_ok"]),
          ("Long, skipped by the filter", lambda r: r["side"] == "long" and not r["cost_ok"]),
          ("Short, every signal", lambda r: r["side"] == "short"),
          ("Short, charges within the filter", lambda r: r["side"] == "short" and r["cost_ok"]),
          ("Short, skipped by the filter", lambda r: r["side"] == "short" and not r["cost_ok"]))


def summarise(rows):
    out = {}
    for name, keep in GROUPS:
        sel = [r for r in rows if keep(r) and r["r"] is not None]
        by_day = {}
        for r in sel:
            by_day.setdefault(r["day"], []).append(r["r"])
        daily = [sum(v) / len(v) for _, v in sorted(by_day.items())]
        out[name] = {"signals": report.edge_stats([r["r"] for r in sel], unit="signals"),
                     "days": report.edge_stats(daily, unit="days"),
                     "targets": sum(1 for r in sel if r["result"] == "target"),
                     "stops": sum(1 for r in sel if r["result"] == "stop"), "net": round(sum(r["net"] for r in sel), 2),
                     "slippage": report.slippage(sel)}
    return out


SLIP_SHOWN = ("Long, every signal", "Long, charges within the filter", "Short, charges within the filter")


def slippage_table(res, by_size=None):
    """The edge at each slippage level a side, for the groups that matter, and each budget's break-even."""
    lines = ["%-33s %9s %7s %7s %8s %17s %6s  %s" % ("group", "slip/side", "avg R", "needed", "day avg",
                                                       "95% per day", "days", "per-day verdict")]
    ci = lambda c: ("%+.3f to %+.3f" % tuple(c)) if c else "n/a"
    na = lambda x: "n/a" if x is None else str(x)
    for name in SLIP_SHOWN:
        s = (res.get(name) or {}).get("slippage") or {}
        for lv in s.get("levels") or []:
            lines.append("%-33s %5d bps %+7.3f %7s %+8.3f %17s %6s  %s" % (
                name, lv["bps"], lv["mean_r"], na(lv.get("trades_needed")), lv.get("day_mean_r") or 0,
                ci(lv.get("day_ci95")), na(lv.get("days_needed")), lv.get("verdict")))
        if s.get("levels"):
            lines.append("%-33s break-even slippage %s a side (one bp costs %sR a trade)" % (
                "", "n/a" if s.get("breakeven_bps") is None else "%.1f bps" % s["breakeven_bps"], s.get("avg_bp_r")))
    for sz in by_size or []:
        t = sz["totals"]
        lines.append("%-33s break-even slippage %s a side; avg R at 1/2/3/5 bps: %s" % (
            "The autopilot's rules at " + sz["label"],
            "n/a" if t.get("breakeven_bps") is None else "%.1f bps" % t["breakeven_bps"],
            " / ".join("%+.3f" % t["avg_r_at_bps"][k] for k in ("1", "2", "3", "5") if k in t.get("avg_r_at_bps", {}))
            or "n/a"))
    return "\n".join(lines)


def calibrate(home, cfg, max_charges_r, days):
    """The 5-minute replay against the 1-minute one, signal by signal, on days that have both: how often the
    result agrees, and how far the 5-minute R is from the 1-minute R on average (per signal and per day)."""
    one = {(r["day"], r["symbol"], r["side"]): r for r in study(home, cfg, max_charges_r, days, bars="1m")}
    five = {(r["day"], r["symbol"], r["side"]): r for r in study(home, cfg, max_charges_r, days, bars="5m")}
    keys = sorted(set(one) & set(five))
    both = [k for k in keys if one[k]["r"] is not None and five[k]["r"] is not None]
    diffs = [five[k]["r"] - one[k]["r"] for k in both]
    by_day = {}
    for k, dlt in zip(both, diffs):
        by_day.setdefault(k[0], []).append(dlt)
    longs = lambda rows: [r for r in rows if r["side"] == "long"]
    return {"days": len(days), "signals": len(keys), "compared": len(both),
            "same_result": sum(1 for k in keys if one[k]["result"] == five[k]["result"]),
            "results": {res: [sum(1 for k in keys if one[k]["result"] == res),
                              sum(1 for k in keys if five[k]["result"] == res)]
                        for res in ("target", "stop", "flat_by", "never filled")},
            "mean_r_1m": round(sum(one[k]["r"] for k in both) / len(both), 3) if both else None,
            "mean_r_5m": round(sum(five[k]["r"] for k in both) / len(both), 3) if both else None,
            "diff_signals": report.edge_stats(diffs, unit="signals"),
            "diff_days": report.edge_stats([sum(v) / len(v) for _, v in sorted(by_day.items())], unit="days"),
            "breakeven_1m": report.slippage(longs([one[k] for k in both]))["breakeven_bps"],
            "breakeven_5m": report.slippage(longs([five[k] for k in both]))["breakeven_bps"]}


def calibration_text(c):
    if not c["compared"]:
        return "No signal could be replayed on both 1- and 5-minute bars."
    ci = lambda g: ("%+.3f to %+.3f" % tuple(g["ci95"])) if g.get("ci95") else "n/a"
    res = c["results"]
    return "\n".join([
        "5-minute replay vs 1-minute replay on the %d days that have both: %d signals, %d replayed both ways." % (
            c["days"], c["signals"], c["compared"]),
        "Same result (target, stop, flat by 15:10, never filled): %d of %d (%.0f%%)." % (
            c["same_result"], c["signals"], 100.0 * c["same_result"] / c["signals"]),
        "Results, 1m vs 5m: " + ", ".join("%s %d vs %d" % (k, v[0], v[1]) for k, v in res.items()) + ".",
        "Average R, 1m %+.3f, 5m %+.3f. 5m minus 1m: %+.3f a signal (95%% %s), %+.3f a day (95%% %s)." % (
            c["mean_r_1m"], c["mean_r_5m"], c["diff_signals"]["mean_r"], ci(c["diff_signals"]),
            c["diff_days"].get("mean_r") or 0, ci(c["diff_days"])),
        "Long break-even slippage on the same signals: 1m %s, 5m %s a side." % tuple(
            "n/a" if x is None else "%.1f bps" % x for x in (c["breakeven_1m"], c["breakeven_5m"])),
        "A negative difference means the 5-minute replay is harsher than the 1-minute one; read a 5-minute-only "
        "study with it."])


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Replay every archived ORB signal, long and short (paper rules).")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--max-charges-r", type=float, help="the cost filter to split on (default: autopilot.json, or 0.2)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bars", choices=("1m", "5m"), default="1m",
                    help="replay on 1-minute bars (days with both; the default) or on 5-minute bars (every day with "
                         "5-minute bars, which Yahoo keeps for 60 days)")
    ap.add_argument("--calibrate", action="store_true",
                    help="on days with both, compare the 5-minute replay with the 1-minute one, signal by signal")
    ap.add_argument("--sizes", nargs="?", const="default",
                    help="also run the autopilot's rules (cost filter, 3-trade cap, daily loss, cash) at these budgets, "
                         "comma separated rupees (default 1 lakh, 10 lakh, 50 lakh, 1 cr, 5 cr), and write every "
                         "trade to reports/sizes-study.csv and .md (sizes-study-5m.* with --bars 5m)")
    ap.add_argument("--caps", nargs="?", const="default",
                    help="also run the paper account's rules at these trades a day, comma separated (default "
                         "3,5,8,10), each with a daily loss limit of that many times the max loss a trade, at "
                         "--budget (default config.json's), and write every trade to reports/caps-study.csv and .md")
    ap.add_argument("--budget", type=float, help="the budget for --caps, rupees (default config.json's)")
    a = ap.parse_args(argv)
    home = ac.trader_home()
    cfg = ac.load_cfg(home)
    settings = ac.load_settings(home)
    mx = a.max_charges_r if a.max_charges_r is not None else \
        ((settings.get("cost_filter") or {}).get("max_charges_r") or 0.2)
    pick = lambda ds: [d for d in ds if (not a.start or d >= a.start) and (not a.end or d <= a.end)]
    if a.calibrate:
        days = pick(days_with_both(home))
        if not days:
            print("no day in %s has both 1- and 5-minute bars to compare" % os.path.join(home, "archive"),
                  file=sys.stderr)
            return 1
        c = calibrate(home, cfg, float(mx), days)
        print(json.dumps(c, indent=2) if a.json else calibration_text(c))
        return 0
    days = pick(days_for(home, a.bars))
    if not days:
        print("no day in %s has %s: run run_day.py archive --backfill first" % (
            os.path.join(home, "archive"), "both 1- and 5-minute bars" if a.bars == "1m" else "5-minute bars"),
            file=sys.stderr)
        return 1
    rows = study(home, cfg, float(mx), days, bars=a.bars)
    res = summarise(rows)
    by_size = None
    if a.sizes:
        budgets = sizes.DEFAULT_SIZES if a.sizes == "default" else [float(x) for x in a.sizes.split(",")]
        longs = [r for r in rows if r["side"] == "long"]
        by_size = [sizes.simulate(longs, cfg, b, float(mx)) for b in budgets]
        paths = write_sizes(home, by_size, days, a.bars)
    by_cap = None
    if a.caps:
        import whatif
        caps = whatif.DEFAULT_CAPS if a.caps == "default" else [int(x) for x in a.caps.split(",")]
        longs = [r for r in rows if r["side"] == "long"]
        by_cap = whatif.trade_caps(longs, cfg, caps, a.budget, float(mx))
        cpaths = write_caps(home, by_cap, days, a.bars)
    if a.json:
        print(json.dumps({"days": days, "bars": a.bars, "max_charges_r": mx, "groups": res, "signals": rows,
                          "sizes": by_size, "caps": by_cap}, indent=2))
        return 0
    print("ORB study, paper rules, after estimated charges: %d days (%s to %s), %d signals replayed on %s bars. "
          "Cost filter split at %.2fR. Not advice." % (len(days), days[0], days[-1], len(rows),
                                                      "1-minute" if a.bars == "1m" else "5-minute", mx))
    if a.bars == "5m":
        print("5-minute bars are coarser than the paper broker's 1-minute ones: run --calibrate to see by how much.")
    print("Per signal is flattering (signals on one day move together); the per-day verdict is the honest one.\n")
    print("%-33s %7s %7s %17s  %4s %7s %17s  %s" % ("group", "signals", "avg R", "95% per signal", "days",
                                                   "day avg", "95% per day", "per-day verdict"))
    ci = lambda g: ("%+.3f to %+.3f" % tuple(g["ci95"])) if g.get("ci95") else "n/a"
    for name, g in res.items():
        s, dd = g["signals"], g["days"]
        if not s.get("n"):
            continue
        print("%-33s %7d %+7.3f %17s  %4d %+7.3f %17s  %s" % (name, s["n"], s["mean_r"], ci(s), dd["n"],
                                                              dd.get("mean_r") or 0, ci(dd), dd["verdict"]))
    print("\nWith slippage: every paper fill is exact, so the entry and the exit are each made that many bps "
          "(0.01%) worse:")
    print(slippage_table(res, by_size))
    if by_size:
        print("\nThe autopilot's rules at each budget (long only; cost filter %.2fR, %s trades a day, daily loss "
              "limit; no market impact):" % (mx, cfg.get("max_trades_per_day")))
        print(sizes_table(by_size))
        print("\nEvery trade: %s and %s" % (paths["csv"], paths["md"]))
    if by_cap:
        print("\nTrades a day at %s (long only; cost filter %.2fR; each cap with a daily loss limit of that many "
              "times the max loss a trade; cash as configured; no market impact):" % (
                  sizes.label(by_cap[0]["budget"]), mx))
        print(caps_table(by_cap))
        print("\nEvery trade: %s and %s" % (cpaths["csv"], cpaths["md"]))
    return 0


def caps_table(by_cap):
    lines = ["%-16s %11s %6s %5s %8s %13s %12s %8s %7s %12s %12s %12s %10s %s" % (
        "trades a day", "daily limit", "trades", "won", "win rate", "net", "charges", "return", "avg R", "best day",
        "worst day", "max dd", "green days", "average day")]
    for s in by_cap:
        t = s["totals"]
        days = s.get("days") or {}
        nets = [v["net"] for v in days.values()]
        lines.append("%-16s %11s %6d %5d %8s %13s %12s %7.2f%% %+7.2f %12s %12s %12s %10s %s" % (
            s["label"], report.money(s["daily_loss"]), t["trades"], t["wins"],
            ("%.0f%%" % (100.0 * t["wins"] / t["trades"])) if t["trades"] else "-", report.signed_money(t["net"]),
            report.money(t["charges"]), t["return_pct"], t["avg_r"] or 0, report.signed_money(t["best_day"]),
            report.signed_money(t["worst_day"]), report.signed_money(t["max_drawdown"]),
            "%d of %d" % (t["green_days"], len(nets)), report.signed_money(sum(nets) / len(nets) if nets else None)))
    return "\n".join(lines)


def write_caps(home, by_cap, days, bars="1m"):
    """Every trade at every number of trades a day, as CSV and Markdown, in reports/."""
    import csv
    folder = ac.reports_dir(home)
    os.makedirs(folder, exist_ok=True)
    base = "caps-study" if bars == "1m" else "caps-study-" + bars
    paths = {"csv": os.path.join(folder, base + ".csv"), "md": os.path.join(folder, base + ".md")}
    cols = ["day", "symbol", "at", "qty", "order_value", "fill", "exit", "exit_at", "result", "charges", "net", "r",
            "max_loss"]
    with open(paths["csv"], "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["trades_a_day"] + cols)
        for s in by_cap:
            for t in s["trades"]:
                w.writerow([s["cap"]] + [t.get(k) for k in cols])
    md = ["# ORB paper trades at %s trades a day, %s" % (", ".join(str(s["cap"]) for s in by_cap),
                                                         sizes.label(by_cap[0]["budget"]) if by_cap else ""), "",
          "%d days, %s to %s, replayed from the bar archive on %s bars with the paper broker's rules. Long only, "
          "with the cost filter, the cash (less the amount kept back, no margin) and a daily loss limit of that many "
          "times the max loss a trade. Claude's veto is not in the archive, so every signal counts. P&L after "
          "estimated Zerodha charges; exact fills, no market impact. Paper; not advice."
          % (len(days), days[0], days[-1], "1-minute" if bars == "1m" else "5-minute"), "", "```",
          caps_table(by_cap), "```", ""]
    for s in by_cap:
        t = s["totals"]
        md += ["## %s (daily loss limit %s)" % (s["label"], report.money(s["daily_loss"])), "",
               "%d trades (%d won, %d lost, %d limit orders never filled), %s net after %s charges, %.2f%% of the "
               "budget. Kept out: %s." % (t["trades"], t["wins"], t["losses"], t["unfilled"],
                                         report.signed_money(t["net"]), report.money(t["charges"]), t["return_pct"],
                                         ", ".join("%s %d" % kv for kv in sorted(s["skipped"].items())) or "none"), "",
               "| Day | Trades | Net | Equity after |", "|---|---|---|---|"]
        for d, v in sorted((s.get("days") or {}).items()):
            md.append("| %s | %d | %s | %s |" % (d, v["trades"], report.signed_money(v["net"]), report.money(v["equity"])))
        md.append("")
    with open(paths["md"], "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    return paths


def sizes_table(by_size):
    head = "%-10s %6s %5s %10s %13s %12s %8s %7s %10s %12s %12s %8s %s" % (
        "budget", "trades", "won", "win rate", "net", "charges", "return", "avg R", "break-even", "best day",
        "worst day", "max dd", "order size / bar volume (median, max, >10%)")
    lines = [head]
    for s in by_size:
        t = s["totals"]
        vol = ("%.1f%%, %.1f%%, %d" % (100 * t["median_volume_share"], 100 * t["max_volume_share"],
                                         t["over_10pct_of_bar"])) if t.get("max_volume_share") is not None else "n/a"
        be = "n/a" if t.get("breakeven_bps") is None else "%.1f bps" % t["breakeven_bps"]
        lines.append("%-10s %6d %5d %9s %13s %12s %7.2f%% %+7.2f %10s %12s %12s %8s %s" % (
            s["label"], t["trades"], t["wins"], ("%.0f%%" % (100.0 * t["wins"] / t["trades"])) if t["trades"] else "-",
            report.signed_money(t["net"]), report.money(t["charges"]), t["return_pct"], t["avg_r"] or 0, be,
            report.signed_money(t["best_day"]), report.signed_money(t["worst_day"]),
            report.signed_money(t["max_drawdown"]), vol))
    return "\n".join(lines)


def write_sizes(home, by_size, days, bars="1m"):
    """Every trade at every budget, as CSV and Markdown, in reports/ (a 5-minute replay gets its own files)."""
    import csv
    folder = ac.reports_dir(home)
    os.makedirs(folder, exist_ok=True)
    base = "sizes-study" if bars == "1m" else "sizes-study-" + bars
    paths = {"csv": os.path.join(folder, base + ".csv"), "md": os.path.join(folder, base + ".md")}
    cols = ["budget", "day", "symbol", "at", "qty", "order_value", "fill", "exit", "exit_at", "result", "gross",
            "charges", "net", "r", "charges_r", "bp_r", "max_loss", "volume_share"]
    with open(paths["csv"], "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in by_size:
            for t in s["trades"]:
                w.writerow([s["label"]] + [t.get(k) for k in cols[1:]])
    md = ["# ORB paper trades at five account sizes", "",
          "%d days, %s to %s, replayed from the bar archive on %s bars with the paper broker's rules. Long only; cost "
          "filter, 3-trade cap and daily loss limit applied at each size; P&L after estimated Zerodha charges. Fills "
          "are exact (no slippage) and assume no market impact, which is optimistic for the biggest orders (see the "
          "last column). Paper; not advice."
          % (len(days), days[0], days[-1], "1-minute" if bars == "1m" else "5-minute"), "", "```",
          sizes_table(by_size), "```", ""]
    for s in by_size:
        t = s["totals"]
        md += ["## %s" % s["label"], "",
               "%d trades (%d won, %d lost, %d limit orders never filled), %s net after %s charges, %.2f%% of the "
               "budget. Skipped: %s." % (t["trades"], t["wins"], t["losses"], t["unfilled"],
                                        report.signed_money(t["net"]), report.money(t["charges"]), t["return_pct"],
                                        ", ".join("%s %d" % kv for kv in sorted(s["skipped"].items())) or "none"), "",
               "| Day | Stock | Time | Qty | Order | In | Out | Exit | Net | R | Bar vol. |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for tr in s["trades"]:
            md.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                tr["day"], tr["symbol"], tr["at"], tr["qty"], report.money(tr["order_value"]),
                report._px(tr["fill"]), report._px(tr["exit"]),
                (tr["result"] or "") + (" " + tr["exit_at"] if tr.get("exit_at") else ""),
                report.signed_money(tr["net"]) if tr["fill"] is not None else "never filled",
                report.r_text(tr["r"]), ("%.1f%%" % (100 * tr["volume_share"])) if tr["volume_share"] is not None
                else "n/a"))
        md += ["", "| Day | Trades | Net | Equity after |", "|---|---|---|---|"]
        for d, v in sorted(s["days"].items()):
            md.append("| %s | %d | %s | %s |" % (d, v["trades"], report.signed_money(v["net"]), report.money(v["equity"])))
        md.append("")
    with open(paths["md"], "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    return paths


if __name__ == "__main__":
    sys.exit(main())
