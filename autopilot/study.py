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


def days_with_both(home):
    root = os.path.join(home, "archive")
    have = lambda iv: set(os.listdir(os.path.join(root, iv))) if os.path.isdir(os.path.join(root, iv)) else set()
    return sorted(have("1m") & have("5m"))


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


def study(home, cfg, max_charges_r, days, until="14:30"):
    scan = scanner()
    rows = []
    for day in days:
        d = datetime.date.fromisoformat(day)
        five, one = load(home, "5m", day), load(home, "1m", day)
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
            r = report.shadow(c, day, minute_bars(one[h["symbol"]]), cfg)
            if r.get("r") is None:
                continue
            rows.append({"day": day, "symbol": h["symbol"], "side": side, "at": h["at"], "r": r["r"],
                         "net": r["net"], "result": r["result"], "charges_r": p["charges_r"],
                         "cost_ok": p["charges_r"] <= max_charges_r})
    return rows


GROUPS = (("Long, every signal", lambda r: r["side"] == "long"),
          ("Long, charges within the filter", lambda r: r["side"] == "long" and r["cost_ok"]),
          ("Long, skipped by the filter", lambda r: r["side"] == "long" and not r["cost_ok"]),
          ("Short, every signal", lambda r: r["side"] == "short"),
          ("Short, charges within the filter", lambda r: r["side"] == "short" and r["cost_ok"]),
          ("Short, skipped by the filter", lambda r: r["side"] == "short" and not r["cost_ok"]))


def summarise(rows):
    out = {}
    for name, keep in GROUPS:
        sel = [r for r in rows if keep(r)]
        by_day = {}
        for r in sel:
            by_day.setdefault(r["day"], []).append(r["r"])
        daily = [sum(v) / len(v) for _, v in sorted(by_day.items())]
        out[name] = {"signals": report.edge_stats([r["r"] for r in sel], unit="signals"),
                     "days": report.edge_stats(daily, unit="days"),
                     "targets": sum(1 for r in sel if r["result"] == "target"),
                     "stops": sum(1 for r in sel if r["result"] == "stop"), "net": round(sum(r["net"] for r in sel), 2)}
    return out


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Replay every archived ORB signal, long and short (paper rules).")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--max-charges-r", type=float, help="the cost filter to split on (default: autopilot.json, or 0.2)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    home = ac.trader_home()
    cfg = ac.load_cfg(home)
    settings = ac.load_settings(home)
    mx = a.max_charges_r if a.max_charges_r is not None else \
        ((settings.get("cost_filter") or {}).get("max_charges_r") or 0.2)
    days = [d for d in days_with_both(home) if (not a.start or d >= a.start) and (not a.end or d <= a.end)]
    if not days:
        print("no day in %s has both 1- and 5-minute bars: run run_day.py archive --backfill first"
              % os.path.join(home, "archive"), file=sys.stderr)
        return 1
    rows = study(home, cfg, float(mx), days)
    res = summarise(rows)
    if a.json:
        print(json.dumps({"days": days, "max_charges_r": mx, "groups": res, "signals": rows}, indent=2))
        return 0
    print("ORB study, paper rules, after estimated charges: %d days (%s to %s), %d signals replayed. Cost filter "
          "split at %.2fR. Not advice." % (len(days), days[0], days[-1], len(rows), mx))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
