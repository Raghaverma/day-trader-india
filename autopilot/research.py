#!/usr/bin/env python3
"""Research on the bar archive: which ORB signals to take, how to get out of them, and two other intraday setups.
Standard library only; paper replays; not advice.

    python3 autopilot/research.py                    # 1-minute days (the replay the paper broker uses) and 5-minute days
    python3 autopilot/research.py --bars 1m          # one of them
    python3 autopilot/research.py --from 2026-10-01 --json

Everything is replayed with the live execution rules (config.json's, else the paper broker's strictest): the order
a minute after the breakout bar closes (study.py --latency 1), a fill only on a trade-through, and a LIMIT that
has not filled in 15 minutes cancelled. Every number is R after estimated Zerodha charges, R being the plan's
max loss at the stop.

1. Which signals (ranking and filters). Each long ORB signal gets, from what was known when it fired:
     rvol           opening volume (09:20-09:30; Yahoo's 09:15 five-minute bar has no volume, so it is left out
                    every day) over its median for the 20 days before
     range_needed   how much of the stock's 14-day average daily range (high - low) the 2R target needs, measured
                    from the day's low so far: over 1.0 asks for a bigger day than usual
     rs             the stock's move from the previous close minus the Nifty's, at the signal (percentage points)
     sector         NSE's industry for the stock (the Nifty 50 list in the trader folder's cache)
     at_20d_high    the previous close was at or above the 20 days' highest high before it
   and the R of its replay. The policies take at most 3 a day (an unfilled order does not use a slot): the first 3
   in time order (now), each filter, and a rank within each scan batch (volume, strength, room), with and without
   one stock per sector. The R by tercile of each feature says whether it sorts winners from losers at all.
2. How to get out. The same filled entries walked with four exit rules: the plan (stop, 2R target as an alert,
   flat by 15:10); a breakeven stop (to the entry plus the round trip's charges once +1R was reached); a time stop
   (out at the bar's open if +0.5R was not reached 45 minutes after the fill); a trail (no target: after +1R the
   stop follows the lowest low of the last 5 bars).
3. Other setups, replayed the same way:
     vwap_pullback  after a strong open (at 09:45 up 0.8% or more from the open, opening volume 1.5x or more), a
                    LIMIT at the VWAP, stop under the day's low so far, 2R target; the order waits until 12:00
     afternoon      a quiet 12:30-13:30 (a range within half the 14-day average range), then the first 5-minute
                    close above it by 14:15; stop at the range's midpoint, 2R target
   A gap-and-go on news needs the news of each past day, which the archive does not keep.

20 days of 1-minute bars is one market regime: read every table as a lead to test on more days, not a result.
"""

import argparse
import datetime
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autopilot_common as ac  # noqa: E402
import market  # noqa: E402
import report  # noqa: E402
import shorts  # noqa: E402
import study  # noqa: E402

nc = ac.nc
EXEC = {"paper_fill": "through", "entry_ttl_minutes": 15}
LATENCY = 1
CAP = 3
HISTORY = 20
ADR_DAYS = 14
MIN_HISTORY = 5
TIME_STOP_MIN = 45
TRAIL_BARS = 5
EXIT_RULES = ("plan", "breakeven", "time", "trail")
EXIT_WORDS = {"plan": "the plan: stop, 2R target, flat by 15:10",
              "breakeven": "stop to breakeven (entry + charges) after +1R",
              "time": "time stop: out if not +0.5R in 45 minutes",
              "trail": "trail: no target; after +1R the stop follows the 5-bar low"}
DRIVE_PCT = 0.8
DRIVE_RVOL = 1.5
VWAP_UNTIL = "12:00"
QUIET = ("12:30", "13:30")
QUIET_ADR = 0.5
BREAK_BY = "14:15"
EPS = 1e-9


def hm(epoch):
    return nc.ist_from_epoch(epoch).strftime("%H:%M")


def mins(hhmm):
    t = nc.parse_hhmm(hhmm)
    return t.hour * 60 + t.minute


def clock(m):
    return "%02d:%02d" % (m // 60, m % 60)


def good(bars):
    return [b for b in bars or [] if None not in b[1:5]]


def sectors(home):
    """{SYMBOL: industry} from the newest NSE index list in the trader folder's cache."""
    out = {}
    for path in sorted(glob.glob(os.path.join(home, "cache", "ind_*list-*.csv")))[-3:]:
        try:
            import csv
            with open(path, encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    sym = (r.get("Symbol") or "").strip()
                    if sym:
                        out[sym] = (r.get("Industry") or "").strip() or "unknown"
        except OSError:
            continue
    return out


class Archive(object):
    def __init__(self, home):
        self.home = home
        self.days5 = study.days_with(home, ("5m",))
        self.days1 = study.days_with_both(home)
        self._five, self._one, self._nifty, self._daily = {}, {}, {}, None
        self.sectors = sectors(home)

    def five(self, day):
        if day not in self._five:
            self._five[day] = study.load(self.home, "5m", day)
        return self._five[day]

    def one(self, day):
        if day not in self._one:
            self._one[day] = study.load(self.home, "1m", day)
        return self._one[day]

    def nifty(self, day):
        if day not in self._nifty:
            doc = market.read_day(market.archive_path(self.home, "5m", day, market.NIFTY)) or {}
            self._nifty[day] = good(doc.get("bars"))
        return self._nifty[day]

    def daily(self):
        """{SYMBOL: {day: {o, h, l, c, or_vol}}} from the 5-minute bars."""
        if self._daily is None:
            out = {}
            for day in self.days5:
                for sym, bars in self.five(day).items():
                    rows = good(bars)
                    if rows:
                        out.setdefault(sym, {})[day] = {
                            "o": rows[0][1], "h": max(b[2] for b in rows), "l": min(b[3] for b in rows),
                            "c": rows[-1][4], "or_vol": sum(b[5] or 0 for b in rows if hm(b[0]) < "09:30")}
            self._daily = out
        return self._daily

    def prior(self, sym, day):
        d = self.daily().get(sym) or {}
        return [(k, d[k]) for k in sorted(d) if k < day]


# ---------------------------------------------------------------- 1. features at the signal

def features(arch, day, sym, at, entry, target):
    """What was known when the signal fired at `at` (the breakout bar's close time)."""
    out = {"rvol": None, "range_needed": None, "used": None, "rs": None, "stock_chg": None, "nifty_chg": None,
           "at_20d_high": None, "sector": arch.sectors.get(sym, "unknown")}
    prior = arch.prior(sym, day)
    today = (arch.daily().get(sym) or {}).get(day)
    if len(prior) < MIN_HISTORY or not today:
        return out
    vols = [p["or_vol"] for _, p in prior[-HISTORY:] if p["or_vol"]]
    if vols and today["or_vol"]:
        out["rvol"] = round(today["or_vol"] / statistics.median(vols), 2)
    span = prior[-ADR_DAYS:]
    adr = sum(p["h"] - p["l"] for _, p in span) / len(span)
    seen = [b for b in good(arch.five(day).get(sym)) if mins(hm(b[0])) + 5 <= mins(at)]
    if seen and adr > 0:
        lo, hi = min(b[3] for b in seen), max(b[2] for b in seen)
        out["range_needed"] = round((target - lo) / adr, 2)
        out["used"] = round((hi - lo) / adr, 2)
    prev_day, prev = prior[-1]
    out["stock_chg"] = round(100 * (entry / prev["c"] - 1), 2)
    nb = [b for b in arch.nifty(day) if mins(hm(b[0])) + 5 <= mins(at)]
    npb = arch.nifty(prev_day)
    if nb and npb:
        out["nifty_chg"] = round(100 * (nb[-1][4] / npb[-1][4] - 1), 2)
        out["rs"] = round(out["stock_chg"] - out["nifty_chg"], 2)
    if len(prior) >= 21:
        out["at_20d_high"] = prev["c"] >= max(p["h"] for _, p in prior[-21:-1])
    return out


# ---------------------------------------------------------------- signals and their replays

def plan_from(sym, price, hi, lo, cfg):
    return study.long_plan({"symbol": sym, "price": price, "or_high": hi, "or_low": lo}, cfg)


def orb_signals(arch, cfg, days, until="14:30"):
    scan = study.scanner()
    out = []
    for day in days:
        d = datetime.date.fromisoformat(day)
        stage1 = {s: study.series(b) for s, b in arch.five(day).items()}
        cutoff = datetime.datetime.combine(d, nc.parse_hhmm(until)) + datetime.timedelta(minutes=1)
        hits, _, _ = scan.scan_orb(stage1, sorted(stage1), d, cutoff, 15, cfg=cfg, side="long")
        for h in hits:
            try:
                p = study.long_plan(h, cfg)
            except shorts.ShortPlanError:
                continue
            out.append({"setup": "orb", "day": day, "symbol": h["symbol"], "at": h["at"], "plan": p,
                        "latency": LATENCY, "exec": {}, "hit": h})
    return out


def vwap_signals(arch, cfg, days):
    """After a strong open, a LIMIT at the VWAP (see the module notes)."""
    out = []
    for day in days:
        for sym, bars in arch.five(day).items():
            rows = good(bars)
            first = [b for b in rows if hm(b[0]) < "09:45"]
            if len(first) < 5 or not rows:
                continue
            f = features(arch, day, sym, "09:45", first[-1][4], first[-1][4])
            if f["rvol"] is None or f["rvol"] < DRIVE_RVOL:
                continue
            day_open, last = first[0][1], first[-1][4]
            if last < day_open * (1 + DRIVE_PCT / 100.0):
                continue
            vol = sum(b[5] or 0 for b in first)
            if not vol:
                continue
            vwap = sum((b[2] + b[3] + b[4]) / 3.0 * (b[5] or 0) for b in first) / vol
            tick = nc.order_tick(vwap, sym, cfg)
            entry = float(nc.round_to_tick(nc.dec(vwap), tick, "down"))
            low = min(b[3] for b in first)
            stop = float(nc.round_to_tick(nc.dec(low) - tick, tick, "down"))
            if not (stop < entry < last):
                continue
            risk_pct = 100 * (entry - stop) / entry
            if risk_pct < 0.3 or risk_pct > 2.0:
                continue
            try:
                p = plan_from(sym, entry, entry, 2 * stop - entry, cfg)     # midpoint of (entry, 2*stop-entry) = stop
            except shorts.ShortPlanError:
                continue
            ttl = mins(VWAP_UNTIL) - mins("09:45")
            out.append({"setup": "vwap_pullback", "day": day, "symbol": sym, "at": "09:45", "plan": p, "latency": 0,
                        "exec": {"entry_ttl_minutes": ttl}})
    return out


def afternoon_signals(arch, cfg, days):
    """A quiet midday range, then the first 5-minute close above it (see the module notes)."""
    out = []
    for day in days:
        for sym, bars in arch.five(day).items():
            rows = good(bars)
            quiet = [b for b in rows if QUIET[0] <= hm(b[0]) < QUIET[1]]
            prior = arch.prior(sym, day)
            if len(quiet) < 10 or len(prior) < MIN_HISTORY:
                continue
            span = prior[-ADR_DAYS:]
            adr = sum(p["h"] - p["l"] for _, p in span) / len(span)
            hi, lo = max(b[2] for b in quiet), min(b[3] for b in quiet)
            if adr <= 0 or hi - lo > QUIET_ADR * adr or (hi - lo) / lo < 0.002:
                continue
            for b in rows:
                start = hm(b[0])
                if start < QUIET[1] or mins(start) + 5 > mins(BREAK_BY):
                    continue
                if b[4] > hi + EPS:
                    try:
                        p = plan_from(sym, b[4], hi, lo, cfg)
                    except shorts.ShortPlanError:
                        break
                    out.append({"setup": "afternoon", "day": day, "symbol": sym, "at": clock(mins(start) + 5),
                                "plan": p, "latency": LATENCY, "exec": {}})
                    break
    return out


def replay(arch, sig, cfg, bars_kind):
    """report.shadow with the live execution rules; adds fill_at and, for the exit rules, the bars."""
    day, sym = sig["day"], sig["symbol"]
    src = arch.one(day) if bars_kind == "1m" else arch.five(day)
    if sym not in src:
        return None
    bars = study.minute_bars(src[sym])
    c = {"symbol": sym, "side": "long", "outcome": sig["setup"], "trigger": {"at": sig["at"]}, "plan": sig["plan"]}
    ex = dict(cfg, **EXEC)
    ex.update(sig.get("exec") or {})
    r = report.shadow(c, day, bars, ex, step=study.STEP[bars_kind], latency=sig.get("latency", LATENCY))
    r["bars"] = bars
    return r


# ---------------------------------------------------------------- 2. exit rules

def walk_exit(bars, day, fill_at, immediate, fill, plan, rule, cfg, sym, step=1):
    """(reason, "HH:MM", price) for one filled long under an exit rule. A stop fills at its trigger (at the open if
    the bar opens under it); the target is an alert that sells at the next bar's open; a bar touching both counts
    as the stop; flat by plan["flat_by"] at that bar's close."""
    d = datetime.date.fromisoformat(day)
    t0 = datetime.datetime.combine(d, nc.parse_hhmm(fill_at))
    first = t0 if immediate else t0 + datetime.timedelta(minutes=step)
    flat = datetime.datetime.combine(d, nc.parse_hhmm(plan.get("flat_by") or "15:10"))
    stop, target, qty = float(plan["stop"]), float(plan["target"]), int(plan["quantity"])
    risk = fill - stop
    tick = nc.order_tick(fill, sym, cfg)
    per_share = nc.dec(nc.intraday_charges(qty, fill, fill, cfg)["total"]) / qty
    be = float(nc.round_to_tick(nc.dec(fill) + per_share, tick, "up"))
    by_time = {b[0]: b for b in bars}
    peak, lows, last = fill, [], None
    for when, o, h, l, c in bars:
        if when.date() != d or when < first:
            continue
        if when > flat:
            break
        last = (when, c)
        if rule == "time" and when >= t0 + datetime.timedelta(minutes=TIME_STOP_MIN) and peak < fill + 0.5 * risk:
            return ("time", when.strftime("%H:%M"), o)
        if l <= stop + EPS:
            return ("stop" if stop <= float(plan["stop"]) + EPS else rule, when.strftime("%H:%M"),
                    o if o < stop else stop)
        if rule != "trail" and h >= target - EPS and when < flat:
            nxt = by_time.get(when + datetime.timedelta(minutes=step))
            price = nxt[1] if nxt is not None else float(nc.round_to_tick(nc.dec(target) - tick, tick, "down"))
            return ("target", (nxt[0] if nxt is not None else when).strftime("%H:%M"), price)
        if when == flat:
            return ("flat_by", when.strftime("%H:%M"), c)
        peak = max(peak, h)
        lows.append(l)
        if peak >= fill + risk - EPS:
            if rule == "breakeven":
                stop = max(stop, be)
            elif rule == "trail":
                stop = max(stop, min(lows[-TRAIL_BARS:]))
    if last is None:
        return None
    return ("flat_by", last[0].strftime("%H:%M"), last[1])


def r_of(plan, fill, exit_, cfg):
    qty = int(plan["quantity"])
    gross = qty * (exit_ - fill)
    net = round(gross - nc.intraday_charges(qty, fill, exit_, cfg)["total"], 2)
    return net, round(net / float(plan["max_loss"]), 3) if plan.get("max_loss") else None


# ---------------------------------------------------------------- running it

def run(arch, cfg, bars_kind, days):
    """Every signal of every setup on `days`, replayed on `bars_kind`, with its features and its R under each
    exit rule. -> [row]"""
    step = study.STEP[bars_kind]
    sigs = orb_signals(arch, cfg, days) + vwap_signals(arch, cfg, days) + afternoon_signals(arch, cfg, days)
    rows = []
    for s in sigs:
        r = replay(arch, s, cfg, bars_kind)
        if r is None or r.get("result") in ("no plan", "no data"):
            continue
        p = s["plan"]
        row = {"setup": s["setup"], "day": s["day"], "symbol": s["symbol"], "at": s["at"], "entry": p["entry"],
               "stop": p["stop"], "target": p["target"], "qty": p["quantity"], "max_loss": p["max_loss"],
               "filled": r.get("fill") is not None and r.get("fill_at") is not None, "fill": r.get("fill"),
               "result": r.get("result"), "r": {}, "net": {}}
        row.update(features(arch, s["day"], s["symbol"], s["at"], p["entry"], p["target"]))
        if row["filled"]:
            for rule in EXIT_RULES:
                e = walk_exit(r["bars"], s["day"], r["fill_at"], r.get("fill_immediate"), r["fill"], p, rule, cfg,
                              s["symbol"], step)
                if e is None:
                    continue
                row["net"][rule], row["r"][rule] = r_of(p, r["fill"], e[2], cfg)
                row.setdefault("exits", {})[rule] = e[:2]
        rows.append(row)
    return rows


def day_stats(rows, rule="plan", per="total", days=()):
    """Per-day R with edge_stats: the day's total R over every day in `days` (a day with no trade is 0R), or the
    day's average R a trade over the days that had one."""
    by_day = {d: [] for d in days} if per == "total" else {}
    for r in rows:
        by_day.setdefault(r["day"], [])
        if r["filled"] and rule in r["r"]:
            by_day[r["day"]].append(r["r"][rule])
    vals = [(sum(v) if per == "total" else sum(v) / len(v)) for _, v in sorted(by_day.items()) if v or per == "total"]
    st = report.edge_stats(vals, unit="days")
    trades = [x for v in by_day.values() for x in v]
    return {"days": len(by_day), "trades": len(trades), "avg_r": round(sum(trades) / len(trades), 3) if trades else None,
            "day_mean": st.get("mean_r"), "ci95": st.get("ci95"), "verdict": st.get("verdict")}


def score_batch(batch):
    """Rank-sum within one scan batch: higher opening volume, more strength against the Nifty, less of the day's
    range needed. Missing values rank last."""
    def ranks(key, reverse):
        have = sorted([r for r in batch if r.get(key) is not None], key=lambda r: r[key], reverse=reverse)
        out = {id(r): i for i, r in enumerate(have)}
        return lambda r: out.get(id(r), len(batch))
    rv, rs, rn = ranks("rvol", True), ranks("rs", True), ranks("range_needed", False)
    return sorted(batch, key=lambda r: (rv(r) + rs(r) + rn(r), r["symbol"]))


POLICIES = [
    ("first 3 a day in time order (now)", None, False, None),
    ("opening volume at least 1.5x its 20-day median", lambda r: (r.get("rvol") or 0) >= 1.5, False, None),
    ("the 2R target within the stock's typical day", lambda r: r.get("range_needed") is not None and
     r["range_needed"] <= 1.0, False, None),
    ("stronger than the Nifty at the signal", lambda r: (r.get("rs") or -99) > 0, False, None),
    ("all three filters", lambda r: (r.get("rvol") or 0) >= 1.5 and r.get("range_needed") is not None and
     r["range_needed"] <= 1.0 and (r.get("rs") or -99) > 0, False, None),
    ("ranked in each batch (volume, strength, room)", None, True, None),
    ("ranked, one stock per sector", None, True, 1),
]


def pick(rows, keep=None, ranked=False, sector_cap=None, cap=CAP):
    """The signals a policy takes: at most `cap` filled a day (an unfilled order frees its slot), in time order,
    each scan batch ranked when `ranked`, at most `sector_cap` a sector."""
    out = []
    by_day = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    for day, rs in sorted(by_day.items()):
        taken, sectors_ = 0, {}
        batches = {}
        for r in rs:
            batches.setdefault(r["at"], []).append(r)
        for at in sorted(batches, key=mins):
            batch = score_batch(batches[at]) if ranked else sorted(batches[at], key=lambda r: r["symbol"])
            for r in batch:
                if taken >= cap:
                    break
                if keep is not None and not keep(r):
                    continue
                if sector_cap and sectors_.get(r["sector"], 0) >= sector_cap:
                    continue
                out.append(r)
                if r["filled"]:
                    taken += 1
                    sectors_[r["sector"]] = sectors_.get(r["sector"], 0) + 1
    return out


def terciles(rows, key, rule="plan"):
    have = sorted([r for r in rows if r["filled"] and r.get(key) is not None and rule in r["r"]], key=lambda r: r[key])
    if len(have) < 9:
        return []
    n = len(have)
    out = []
    for i, name in enumerate(("low", "middle", "high")):
        part = have[i * n // 3:(i + 1) * n // 3]
        rs = [r["r"][rule] for r in part]
        out.append({"third": name, "from": part[0][key], "to": part[-1][key], "n": len(part),
                    "avg_r": round(sum(rs) / len(rs), 3), "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3)})
    return out


def summarise(rows, days=()):
    orb = [r for r in rows if r["setup"] == "orb"]
    out = {"signals": {s: sum(1 for r in rows if r["setup"] == s) for s in ("orb", "vwap_pullback", "afternoon")},
           "filled": {s: sum(1 for r in rows if r["setup"] == s and r["filled"]) for s in
                      ("orb", "vwap_pullback", "afternoon")},
           "policies": [], "terciles": {}, "exits": {}, "setups": {}}
    for label, keep, ranked, sector_cap in POLICIES:
        sel = pick(orb, keep, ranked, sector_cap)
        out["policies"].append(dict(day_stats(sel, days=days), label=label))
    out["policies"].append(dict(day_stats(orb, days=days), label="every signal, no cap (for reference)"))
    for key in ("rvol", "range_needed", "rs", "used"):
        out["terciles"][key] = terciles(orb, key)
    flag = [r for r in orb if r["filled"] and r.get("at_20d_high") is not None and "plan" in r["r"]]
    out["at_20d_high"] = {str(v): {"n": len(xs), "avg_r": round(sum(xs) / len(xs), 3) if xs else None}
                          for v in (True, False) for xs in [[r["r"]["plan"] for r in flag if r["at_20d_high"] is v]]}
    for setup in ("orb", "vwap_pullback", "afternoon"):
        part = [r for r in rows if r["setup"] == setup]
        out["exits"][setup] = {rule: dict(day_stats(part, rule, per="mean"), label=EXIT_WORDS[rule])
                               for rule in EXIT_RULES}
        out["setups"][setup] = dict(day_stats(pick(part), "plan", days=days), signals=len(part),
                                    filled=sum(1 for r in part if r["filled"]))
    return out


# ---------------------------------------------------------------- text

def _ci(ci):
    return ("%+.3f to %+.3f" % tuple(ci)) if ci else "n/a"


def _r(x):
    return "n/a" if x is None else "%+.3f" % x


def text(res, days, bars_kind):
    s = res["summary"]
    L = ["## %s bars: %d days, %s to %s" % ("1-minute" if bars_kind == "1m" else "5-minute", len(days), days[0],
                                             days[-1]), "",
         "Signals (filled with the live rules): ORB %d (%d), VWAP pullback %d (%d), afternoon breakout %d (%d)." % (
             s["signals"]["orb"], s["filled"]["orb"], s["signals"]["vwap_pullback"], s["filled"]["vwap_pullback"],
             s["signals"]["afternoon"], s["filled"]["afternoon"]), "",
         "### 1. Which ORB signals to take (at most 3 filled a day; the plan's exit)", "",
         "| Policy | Trades | Avg R a trade | Avg R a day | 95% interval a day | Verdict (days) |", "|---|---|---|---|---|---|"]
    for p in s["policies"]:
        L.append("| %s | %d | %s | %s | %s | %s |" % (p["label"], p["trades"], _r(p["avg_r"]), _r(p["day_mean"]),
                                                   _ci(p["ci95"]), p["verdict"]))
    L += ["", "Does a feature sort winners from losers? Every filled ORB signal, by thirds of the feature:", "",
          "| Feature | Third | Range | Signals | Avg R | Win rate |", "|---|---|---|---|---|---|"]
    names = {"rvol": "opening volume / 20-day median", "range_needed": "share of the 14-day range the target needs",
             "rs": "move vs the Nifty (points)", "used": "share of the 14-day range already used"}
    for key, rows in s["terciles"].items():
        for t in rows:
            L.append("| %s | %s | %s to %s | %d | %s | %.0f%% |" % (names[key], t["third"], t["from"], t["to"], t["n"],
                                                               _r(t["avg_r"]), 100 * t["win_rate"]))
    h = s["at_20d_high"]
    L += ["", "Previous close at a 20-day high: %s signals, %s avg R; not: %s signals, %s avg R." % (
        h["True"]["n"], _r(h["True"]["avg_r"]), h["False"]["n"], _r(h["False"]["avg_r"])), "",
        "### 2. How to get out (every filled signal; average R a trade per day)", "",
        "| Setup | Exit rule | Trades | Avg R a trade | Day average | 95% interval a day | Verdict (days) |",
        "|---|---|---|---|---|---|---|"]
    for setup, rules in s["exits"].items():
        for rule, e in rules.items():
            L.append("| %s | %s | %d | %s | %s | %s | %s |" % (setup, e["label"], e["trades"], _r(e["avg_r"]),
                                                            _r(e["day_mean"]), _ci(e["ci95"]), e["verdict"]))
    L += ["", "### 3. The setups side by side (first 3 filled a day, the plan's exit)", "",
          "| Setup | Signals | Filled | Trades taken | Avg R a trade | Avg R a day | 95% interval a day | Verdict |",
          "|---|---|---|---|---|---|---|---|"]
    for setup, v in s["setups"].items():
        L.append("| %s | %d | %d | %d | %s | %s | %s | %s |" % (setup, v["signals"], v["filled"], v["trades"],
                                                              _r(v["avg_r"]), _r(v["day_mean"]), _ci(v["ci95"]),
                                                              v["verdict"]))
    return "\n".join(L) + "\n"


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Ranking, exit rules and other setups on the bar archive (paper).")
    ap.add_argument("--bars", choices=("1m", "5m", "both"), default="both")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true", help="do not write reports/research.md and .json")
    a = ap.parse_args(argv)
    home = ac.trader_home()
    cfg = ac.load_cfg(home)
    arch = Archive(home)
    pick_days = lambda ds: [d for d in ds if (not a.start or d >= a.start) and (not a.end or d <= a.end)]
    out, parts = {}, []
    for kind in (("1m", "5m") if a.bars == "both" else (a.bars,)):
        days = pick_days(arch.days1 if kind == "1m" else arch.days5)
        if not days:
            continue
        rows = run(arch, cfg, kind, days)
        res = {"days": days, "rows": rows}
        res["summary"] = summarise(rows, days)
        out[kind] = res
        parts.append(text(res, days, kind))
    if not out:
        print("research: no archived days (run run_day.py archive --backfill first)", file=sys.stderr)
        return 1
    head = ("# Research on the bar archive: ranking, exits, other setups\n\nPaper replays with the live execution "
            "rules (the order a minute after the breakout bar closes, a fill only on a trade-through, a LIMIT "
            "cancelled after 15 minutes unfilled); R after estimated Zerodha charges. A small sample of days is one "
            "market regime: read these as leads, not results. Not advice.\n\n")
    body = head + "\n".join(parts)
    if not a.no_write:
        folder = ac.reports_dir(home)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "research.md"), "w", encoding="utf-8") as fh:
            fh.write(body)
        clean = {k: {"days": v["days"], "summary": v["summary"],
                     "rows": [{x: y for x, y in r.items() if x != "bars"} for r in v["rows"]]} for k, v in out.items()}
        ac.write_json(os.path.join(folder, "research.json"), clean)
    if a.json:
        print(json.dumps({k: {"days": v["days"], "summary": v["summary"]} for k, v in out.items()}, indent=1))
    else:
        sys.stdout.write(body)
        if not a.no_write:
            print("\nWritten: %s" % os.path.join(ac.reports_dir(home), "research.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
