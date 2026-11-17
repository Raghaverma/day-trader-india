"""nse-timemachine tests. Offline, on tests/fixtures/nse-timemachine/RELIANCE-60d5m.json.gz
(Yahoo v8 chart for RELIANCE.NS, range=60d interval=5m: 60 NSE sessions, 4,500 bars,
18 Aug to 13 Nov 2026). The plan is the worked example from nse-exit: entry 1,334.00 at
10:05 IST, stop 1,328.80, 2R target 1,344.40, flat by 15:10. Seed 19, 10,000 replays,
the original's convention."""

import gzip
import hashlib
import io
import json
import os
import random
import shutil
import statistics
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX, ROOT

tm = load("nse-timemachine", "timemachine")
nc = tm.nc

DIR = os.path.join(FIX, "nse-timemachine")
REL = os.path.join(DIR, "RELIANCE-60d5m.json.gz")
with gzip.open(REL, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-11-13 10:05"
NOW_UTC = tm.parse_now(NOW)

# the plan nse-exit writes for the worked example (trader/plan.json)
with open(os.path.join(FIX, "nse-alerts", "plan-RELIANCE.json"), encoding="utf-8") as _fh:
    PLAN = json.load(_fh)

# fingerprints of the seed-19 run on the fixture
BANDS = "8333dc8639cc5c72"
PATHS = "a11b28940794c863"
TRADES = "7d608287c360da89"
HIST = {"lo": 1306.3, "hi": 1358.44,
        "bins": [92, 95, 150, 206, 273, 385, 449, 541, 634, 697, 815, 799, 784, 674, 699, 570, 499, 408, 344, 266,
                 183, 112, 92, 71]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return nc.ist_from_epoch(ts)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---- the original trader-timemachine simulate(), verbatim (MIT), as the reference for the draws.
# It clamps the slot at its bar 77; padding our pool with copies of the flat-by slot makes that
# clamp identical to ours, so equal output proves the same random draws in the same order.
O_BARS, O_SAMPLE, O_BAND, O_Q, O_BINS = 78, 160, 2000, (0.05, 0.25, 0.5, 0.75, 0.95), 24


def _pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def original_simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(O_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < O_SAMPLE
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(O_BAND):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(_pct(col, q), 2) for q in O_Q])
    lo_, hi_ = _pct(srt, 0.01), _pct(srt, 0.99)
    W = (hi_ - lo_) / O_BINS
    bins = [0] * O_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < O_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(_pct(srt, 0.5), 2), "p5_close": round(_pct(srt, 0.05), 2),
        "p95_close": round(_pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": O_BAND, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


class WorkedExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:10"))
        self.assertEqual(m["pool_sessions"], 58)
        self.assertEqual(m["bars_left"], 61)                      # the 10:05 bar ... the 15:05 bar
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-18", "2026-11-12"))
        self.assertEqual(m["p_target"], 0.2258)
        self.assertEqual(m["p_stop"], 0.6907)
        self.assertEqual(m["p_neither"], 0.0835)
        self.assertEqual(m["p_green"], 0.2878)
        self.assertEqual(m["median_close"], 1331.84)
        self.assertEqual(m["p5_close"], 1314.07)
        self.assertEqual(m["p95_close"], 1350.33)
        self.assertEqual(m["avg_R"], -0.199)
        self.assertEqual(m["r_at_target"], 2.0)

    def test_fingerprints(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)
        self.assertEqual(m["bands"][-1], [1314.2, 1324.63, 1331.97, 1339.38, 1350.41])

    def test_same_draws_as_the_original(self):
        meta, bars, _ = tm.parse_bars(tm.chart_result(TEXT, "RELIANCE"), NOW_UTC)
        sessions, _ = tm.full_sessions(tm.split_sessions(bars), "2026-11-13", 70)
        pool = tm.build_pool(sessions)
        self.assertEqual(len(pool), 71)                           # slots 0..70: 09:15 to the 15:05 bar
        padded = pool + [pool[70]] * (O_BARS - len(pool))
        for window in (2, 0):
            ours = tm.simulate(pool, 1334.0, 1328.8, 1344.4, 9, 70, 3000, 19, window)
            ref = original_simulate(padded, 1334.0, 1328.8, 1344.4, 9, 70, 3000, 19, window)
            self.assertEqual(ours, ref, window)
        ours = {k: v for k, v in self.doc["mc"].items() if k in ("p_target", "p_stop", "p_neither", "avg_R")}
        ref = original_simulate(padded, 1334.0, 1328.8, 1344.4, 9, 70)
        self.assertEqual(ours, {k: ref[k] for k in ours})

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (58, 31))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (7, 15, 9, 13))
        self.assertEqual(h["avg_R"], -0.03)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"], first["outcome"]),
                         ("2026-08-20", "12:55", 1315.0, 1311.1, 1322.8, "close"))
        for t in h["trades"]:                                     # every price on the NSE tick
            for k in ("entry", "stop", "target"):
                self.assertTrue(nc.on_tick(t[k], nc.tick_size(t[k])), (t["date"], k))
        self.assertNotIn("2026-11-13", [t["date"] for t in h["trades"]])

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Leans toward the stop: at 2R the target has to come first in 33 % of the "
                                              "replays that hit one side; it did in 25 % (target first 22.6 % vs stop "
                                              "first 69.1 %).")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.246, 0.333))
        self.assertEqual(self.doc["verdict_notes"], [
            "After charges on 37 shares (₹52.71 a round trip at the target, ₹52.33 at the stop): +1.73R at the "
            "target, -1.27R at the stop, so the target has to come first in 42 % of the replays that hit one side; "
            "it did in 25 %. Average R after charges -0.471."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1334.0] * 5)
        self.assertEqual(len(m["bands"]), 62)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:05", "15:10", 62))
        self.assertTrue(all(p[0] == 1334.0 and len(p) == 62 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", 1334.0, 1328.8, 1344.4, 9, "10:05"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["exchange"], p["tick"]),
                         (5.2, 2.0, "15:10", "NSE", "0.10"))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["created_by"], "nse-timemachine")
        self.assertEqual(self.doc["data"]["symbol"], "RELIANCE.NS")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy on NSE, Friday 2026-11-13: entry ₹1,334.00 at 10:05 IST (bar 9), stop "
                     "₹1,328.80, target ₹1,344.40 (2R, risk ₹5.20 a share).",
                     "10,000 replays", "10:05 to 15:10 IST (61 five-minute bars)", "seed 19", "58 whole sessions",
                     "target first   22.6 %", "stop first     69.1 %", "neither        8.3 %    still open at 15:10",
                     "green          28.8 %", "median ₹1,331.84, 90 % of replays between ₹1,314.07 and "
                     "₹1,350.33", "Average R: -0.199",
                     "After charges on 37 shares (sized from your config's caps): average R -0.471, green 27.7 %",
                     "₹52.71 at the target (0.27R) and ₹52.33 at the stop (0.27R)",
                     "fired 31 times: 7 target, 15 stop, 9 neither. Average R -0.030, -0.289 after charges.",
                     "Left out 1 session(s) with a gap before 15:10: 2026-10-15.", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "$", "dollar", "New York", " ET"):
            self.assertNotIn(word, text)


class Charges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_charges_in_the_replays(self):
        m = self.doc["mc"]
        # the shared nc.size_qty on the default rules: the max loss at the SL limit decides, 37 shares (the old
        # private caps maths said 56, the position cap, and ignored the stop leg's limit)
        self.assertEqual((m["qty"], m["qty_from"]), (37, "config caps"))
        self.assertEqual(m["qty"], nc.size_qty(1334.0, 1328.8, "RELIANCE", dict(nc.DEFAULT_CONFIG)))
        self.assertEqual(m["charges_at_target"], nc.intraday_charges(37, 1334.0, 1344.4)["total"])
        self.assertEqual(m["charges_at_target"], 52.71)
        self.assertEqual(m["charges_at_stop"], nc.intraday_charges(37, 1334.0, 1328.8)["total"])
        self.assertEqual(m["charges_at_stop"], 52.33)
        self.assertEqual(m["r_at_target_net"], round(2.0 - 52.71 / (37 * 5.2), 4))
        self.assertEqual(m["r_at_stop_net"], round(-1.0 - 52.33 / (37 * 5.2), 4))
        self.assertEqual((m["avg_R_net"], m["p_green_net"]), (-0.471, 0.277))
        self.assertLess(m["avg_R_net"], m["avg_R"])
        self.assertLessEqual(m["p_green_net"], m["p_green"])

    def test_charges_in_the_history(self):
        h = self.doc["history"]
        self.assertEqual((h["qty"], h["avg_R_net"], h["green_net"]), (37, -0.289, 11))
        for t in h["trades"]:
            risk = t["entry"] - t["stop"]
            self.assertAlmostEqual(t["R_net"], round(t["R"] - t["charges"] / (37 * risk), 2), places=1)
            if t["outcome"] == "target":
                self.assertEqual(t["charges"], nc.intraday_charges(37, t["entry"], t["target"])["total"])
            if t["outcome"] == "stop":
                self.assertEqual(t["charges"], nc.intraday_charges(37, t["entry"], t["stop"])["total"])
        last = h["trades"][-1]
        self.assertEqual((last["date"], last["outcome"], last["charges"], last["R_net"]),
                         ("2026-11-12", "stop", 52.61, -1.24))

    def test_charges_never_change_the_gross_odds(self):
        a = machine(replays=500)["mc"]
        b = machine(replays=500, qty=200)["mc"]
        for k in ("p_target", "p_stop", "p_neither", "p_green", "avg_R", "median_close"):
            self.assertEqual(a[k], b[k], k)
        self.assertEqual((b["qty"], b["qty_from"], b["charges_at_target"]), (200, "--qty", 142.46))
        self.assertGreater(b["avg_R_net"], a["avg_R_net"])        # 200 shares hit Zerodha's ₹20 brokerage cap
        one = machine(replays=500, qty=1)["mc"]
        self.assertEqual((one["qty"], one["charges_at_target"]), (1, 1.42))

    def test_the_plans_quantity_and_the_config(self):
        m = machine(plan(quantity=100), replays=300)["mc"]
        self.assertEqual((m["qty"], m["qty_from"]), (100, "plan"))
        m = machine(replays=300, cfg=dict(nc.DEFAULT_CONFIG, budget=20000))["mc"]
        self.assertEqual(m["qty"], 11)                            # 75% of ₹20,000 / ₹1,334
        free = machine(replays=300, cfg=dict(nc.DEFAULT_CONFIG, charges={k: 0 for k in nc.DEFAULT_CHARGES}))["mc"]
        self.assertEqual((free["charges_at_target"], free["avg_R_net"]), (0.0, free["avg_R"]))
        with self.assertRaises(tm.TMError):
            machine(replays=10, qty=0)

    def test_the_size_is_the_shared_size_qty_not_a_private_caps_maths(self):
        # item 19: exit, timemachine and nse-budget all ask nc.size_qty
        self.assertFalse(hasattr(tm, "default_qty"))
        p = plan()
        cfg = dict(nc.DEFAULT_CONFIG)
        self.assertEqual(tm.estimate_qty(p, cfg), nc.size_qty(1334.0, 1328.8, "RELIANCE", cfg))
        self.assertEqual(tm.estimate_qty(p, cfg), 37)
        # the old private maths (500 / 5.20, the max order, the position cap) said 56
        self.assertEqual(tm.estimate_qty(p, dict(cfg, loss_basis="trigger")), 56)
        # a real account of 20,000 in portfolio.json: the position cap shrinks to 75% of it, 11 shares
        m = machine(replays=100, cash=20000, total=20000)["mc"]
        self.assertEqual((m["qty"], m["qty_from"]), (11, "config caps"))
        # a plan nse-exit wrote with quantity 0 (nothing fits) still needs one share for the charges estimate
        m = machine(plan(quantity=0, stop=1000.0, target=1668.0), replays=100)["mc"]
        self.assertEqual((m["qty"], m["qty_from"]), (1, "config caps"))
        self.assertEqual(machine(plan(quantity=37), replays=100)["mc"]["qty_from"], "plan")

    def test_a_target_that_does_not_pay_its_charges(self):
        mc = {"p_target": 0.5, "p_stop": 0.4, "p_neither": 0.1, "r_at_target": 0.1, "avg_R": 0.0, "qty": 1,
              "charges_at_target": 2.0, "charges_at_stop": 2.0, "r_at_target_net": -0.2, "r_at_stop_net": -1.3,
              "avg_R_net": -0.6}
        self.assertIn("the target does not even pay for the round trip", tm.charges_note(mc))
        self.assertIsNone(tm.charges_note({"p_target": 0.5, "p_stop": 0.4}))


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"]), (0.2308, 0.6906, 0.0786))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(58)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_ist_stamps_and_the_75_bar_session(self):
        meta, bars, dropped = tm.parse_bars(self.result, tm.parse_now(None))
        days = tm.split_sessions(bars)
        self.assertEqual(len(days), 60)
        first = bars[0]
        self.assertEqual((first["date"], first["start"], first["slot"]), ("2026-08-18", "09:15", 0))
        self.assertEqual(max(b["slot"] for b in bars), 74)        # 15:25, the 75th bar
        # Yahoo leaves the closing-session bars of NSE days empty: 105 bars with no price,
        # and the data's own time (15:28:04) marks the last 15:25 bar as in progress
        self.assertEqual(dropped, {"no_price": 105, "off_grid": 0, "not_complete": 1, "outside_hours": 0})
        self.assertEqual(len(days["2026-11-13"]), 73)
        entry = days["2026-11-13"][9]
        self.assertEqual((entry["start"], entry["c"]), ("10:00", 1334.0))
        self.assertEqual(ist(self.result["timestamp"][0]).strftime("%H:%M"), "09:15")

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, NOW_UTC)[1]
        self.assertEqual(len([b for b in bars if b["date"] == "2026-11-13"]), 10)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-13 10:02"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:55 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-13 04:35Z"), replays=200)["mc"]["pool_sessions"], 58)

    def test_whole_to_flat_by_and_the_closing_bars(self):
        full, short = tm.full_sessions(tm.split_sessions(tm.parse_bars(self.result, NOW_UTC)[1]), "2026-11-13", 70)
        self.assertEqual(len(full), 58)
        self.assertEqual(short, [{"date": "2026-10-15", "bars": 69, "of": 71}])   # 11:45 and 11:50 are empty
        # Yahoo's empty 15:15-15:25 bars are its gap, not a halt: they never drop a session
        full, short = tm.full_sessions(tm.split_sessions(tm.parse_bars(self.result, NOW_UTC)[1]), "2026-11-13")
        self.assertEqual((len(full), len(full[0][1])), (58, 75))
        self.assertEqual(short, [{"date": "2026-10-15", "bars": 70, "of": 72}])
        self.assertEqual(sum(1 for _, D in full if D[73] is None), 58)             # 15:20: empty every day
        self.assertEqual(sum(1 for _, D in full if D[72] is None), 6)              # 15:15: six days
        pool = tm.build_pool(full)
        self.assertEqual([len(pool[i]) for i in (70, 71, 72, 73, 74)], [58, 58, 52, 1, 21])
        self.assertEqual(pool[73], [(1.0, 1.0, 1.0, 1.0)])                         # no 15:20 bar anywhere: flat
        # time-of-day buckets stay aligned: the 15:25 bar is scaled to the last close before it
        d = dict(full)["2026-11-12"]
        self.assertEqual((d[72], d[73]), (None, None))
        self.assertAlmostEqual(pool[74][-1][3], d[74]["c"] / d[71]["c"], places=12)
        for until, bars_left in (("15:15", 62), ("15:20", 63), ("15:25", 64), ("15:30", 65)):
            m = machine(until=until, replays=200)["mc"]
            self.assertEqual((m["pool_sessions"], m["bars_left"], m["until"]), (58, bars_left, until))
        h = machine(until="15:30", replays=10)["history"]
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"]), (33, 7, 16, 10))
        late = [t for t in h["trades"] if t["outcome"] == "close"]
        self.assertTrue(all(t["R"] == round(t["R"], 2) for t in late))
        # with every bar filled the pool is what the original builds
        D = full[0][1][:72]
        self.assertEqual(tm.build_pool([("x", D)])[5], [tuple(D[5][k] / D[4]["c"] for k in "ohlc")])

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-11-12")
        self.assertNotIn("2026-11-13", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None      # 2026-08-18 09:40
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 106)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-14")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 whole session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-13")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for RELIANCE", str(cm.exception))

    def test_a_holiday_or_a_weekend_has_no_bars(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-24"), now=tm.parse_now("2026-11-24 10:05"))
        self.assertIn("no bars for that day: NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-14"), now=tm.parse_now("2026-11-14 10:05"))
        self.assertIn("no bars for that day: Saturday", str(cm.exception))

    def test_a_special_session_is_refused(self):
        cfg = dict(nc.DEFAULT_CONFIG, special_sessions={"2026-11-13": {"open": "18:15", "close": "19:15",
                                                                       "label": "Muhurat trading"}})
        with self.assertRaises(tm.TMError) as cm:
            machine(cfg=cfg, replays=10)
        self.assertIn("normal 09:15-15:30 session only", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-16"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:                 # 10:10 has not closed at 10:05
            machine(plan(entry_time="10:15", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:07", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:25 IST"),
            (dict(entry_bar=10), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1335.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(symbol="^NSEI"), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="13 Nov"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(flat_by="3pm"), "IST time like 15:10"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:05")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 9)
        self.assertEqual(plan(entry_time="10:05 AM", entry_bar=None)["entry_bar"], 9)
        self.assertEqual(plan(entry_time="2:30", entry_bar=None)["entry_bar"], 62)   # a bare 2:30 is 14:30
        self.assertEqual(plan(symbol="reliance.ns")["symbol"], "RELIANCE")

    def test_flat_by_ends_the_replay(self):
        doc = machine(plan(flat_by="15:00"), replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:00", 59, "15:00"))
        self.assertIn("still open at 15:00", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="15:00", replays=500)["mc"], m)
        self.assertEqual(machine(until="3:00 PM", replays=500)["mc"], m)
        no_flat = machine(plan(flat_by=None), replays=500)["mc"]
        self.assertEqual(no_flat["until"], "15:10")               # the config default
        self.assertEqual(machine(plan(flat_by=None), replays=500, cfg=dict(nc.DEFAULT_CONFIG, flat_by="15:00"))["mc"], m)
        for bad, words in (("10:05", "nothing to replay"), ("15:35", "no later than the 15:30 IST close"),
                           ("15:07", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1340.0, stop=1334.0, target=1352.0), replays=200)
        self.assertIn("outside that bar's range ₹1,331.50 to ₹1,334.00", doc["warnings"][0])

    def test_stops_round_away_from_the_entry_on_the_tick(self):
        self.assertEqual(tm.tick_away(1328.85, True), 1328.8)
        self.assertEqual(tm.tick_away(1328.85, False), 1328.9)
        self.assertEqual(tm.tick_away(1324.300048828125, True), 1324.3)
        self.assertEqual(tm.tick_away(1324.2999999999998, True), 1324.3)
        self.assertEqual(tm.tick_away(250.04, True), 250.0)
        self.assertEqual(tm.tick_away(5000.4, True), 5000.0)
        self.assertEqual(tm.tick_near(1344.43), 1344.4)
        self.assertEqual(tm.tick_near(1328.85, "RELIANCE", {"tick_sizes": {"RELIANCE": 0.05}}), 1328.85)

    def test_prices_near_a_slab_edge_use_the_order_tick(self):
        # nc.order_tick: the coarsest tick within 10% of the price, since NSE sets a stock's tick from an earlier
        # close. 249.996 on the old per-slab rule was 249.99 (0.01 grid): rejected when the stock's tick is 0.05.
        self.assertEqual(tm.tick_away(249.996, True), 249.95)
        self.assertEqual(tm.tick_away(997.97, True), 997.9)           # not 997.95: valid on the 0.05 AND the 0.10 grid
        self.assertEqual(tm.tick_away(997.97, False), 998.0)
        self.assertEqual(tm.tick_near(997.97), 998.0)
        self.assertEqual(tm.tick_near(1000.03), 1000.0)
        for x in (249.996, 997.97, 4999.97, 9999.9, 19999.5):
            for fn in (lambda v: tm.tick_away(v, True), tm.tick_near):
                v = fn(x)
                self.assertTrue(nc.on_tick(v, nc.order_tick(v)), x)
                self.assertTrue(nc.on_tick(v, nc.tick_size(v)), x)     # and on the slab's own grid
        # a per-symbol override still wins
        self.assertEqual(tm.tick_away(997.97, True, "ABC", {"tick_sizes": {"ABC": 0.05}}), 997.95)

    def test_the_history_replay_puts_its_prices_on_the_order_tick(self):
        h = machine(replays=10)["history"]
        for t in h["trades"]:
            for k in ("entry", "stop", "target"):
                self.assertTrue(nc.on_tick(t[k], nc.order_tick(t[k])), (t["date"], k))

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1324.3, target=1353.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 2.0))
        rule = machine(plan(stop_rule="atr", stop=1328.6, target=1344.8), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.4048, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="level"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1339.2, target=1328.8, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 15:10: the price at 15:10 decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_1r_on_the_fixture(self):
        doc = machine(plan(target=1339.2, target_r=1.0))
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"], m["avg_R_net"]),
                         (1.0, 0.4288, 0.5661, 0.0051, -0.137, -0.41))
        self.assertEqual(doc["verdict"], "Leans toward the stop: target first 42.9 % vs stop first 56.6 %.")
        self.assertIn("so the target has to come first in 64 % of the replays", doc["verdict_notes"][0])
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_net"]),
                         (31, 12, 13, 6, -0.081, -0.339))


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("M&M", "query1"))
        self.assertEqual(nc.UA, "Mozilla/5.0 (compatible)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("ZZZZ.NS", str(cm.exception))
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(PLAN, fh)
            code, text, err = run(["run", "--plan", p, "--now", NOW, "--replays", "300",
                                   "--config", os.path.join(d, "none.json")])
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w", encoding="utf-8") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")
        self.cfg = os.path.join(self.d, "trader", "config.json")
        self.env = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": os.path.join(self.d, "trader")})
        self.env.start()                      # default paths are this temp folder, never the real trader/

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.d)

    def args(self, *more):
        return ["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--config", self.cfg] + list(more)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(self.args("--write", self.out))
        self.assertEqual(code, 0, err)
        self.assertIn("target first   22.6 %", text)
        self.assertIn("Leans toward the stop: at 2R", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/nse-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual((doc["created_by"], doc["source"]), ("nse-timemachine", "offline"))
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"], doc["mc"]["avg_R_net"]), (0.2258, -0.199, -0.471))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (31, -0.03))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output_and_qty(self):
        code, text, _ = run(self.args("--json", "--qty", "56"))
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_stop"], doc["mc"]["qty"], doc["mc"]["qty_from"]), (0.6907, 56, "--qty"))
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:21], "Leans toward the stop")
        self.assertFalse(os.path.exists(self.out))

    def test_the_config_flat_by_and_charges(self):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"budget": 50000}, fh)
        code, text, _ = run(self.args("--json", "--replays", "300"))
        self.assertEqual(json.loads(text)["mc"]["qty"], 28)       # 75% of ₹50,000 / ₹1,334

    def test_the_portfolio_next_to_the_config_sizes_the_charges(self):
        with open(os.path.join(self.d, "trader", "portfolio.json"), "w", encoding="utf-8") as fh:
            json.dump({"cash": 20000, "positions": []}, fh)
        code, text, err = run(self.args("--json", "--replays", "200"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["mc"]["qty"], 11)                  # 75% of the 20,000 account / 1,334
        other = os.path.join(self.d, "other.json")
        with open(other, "w", encoding="utf-8") as fh:
            json.dump({"cash": 100000, "positions": []}, fh)
        code, text, err = run(self.args("--json", "--replays", "200", "--portfolio", other))
        self.assertEqual(json.loads(text)["mc"]["qty"], 37)                  # the max loss decides on a full account
        with open(os.path.join(self.d, "trader", "portfolio.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        code, text, err = run(self.args("--json", "--replays", "200"))
        self.assertEqual(code, 0)
        self.assertIn("could not be read", err)                              # said, not hidden
        self.assertEqual(json.loads(text)["mc"]["qty"], 37)

    def test_the_plans_quantity_from_nse_exit_is_used(self):
        raw = dict(PLAN, quantity=25, quantity_from="config caps", risk_per_share_worst=11.9, worst_exit=1322.1)
        with open(self.plan, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        code, text, err = run(self.args("--json", "--replays", "200"))
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["qty"], doc["mc"]["qty_from"]), (25, "plan"))
        self.assertEqual(doc["plan"]["quantity"], 25)

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "RELIANCE", "--date", "2026-11-13", "--entry", "1334",
                               "--stop", "1328.80", "--target", "1344.40", "--entry-time", "10:05", "--offline", REL,
                               "--now", NOW, "--config", self.cfg, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"], doc["mc"]["until"]), (0.2258, 0.2878, "15:10"))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (7, 15, -0.03))
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1334", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(REL, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     self.args("--until", "9:00"),
                     self.args("--until", "15:35"),
                     self.args("--now", "13/11/2026"),
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(self.args("--write", self.out))
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 10:05 IST entry to 15:10 (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1344.40 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1328.80 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1334.00 |") and l.endswith("  entry") for l in lines))
        self.assertIn("10:05", text)
        self.assertIn("15:10", text)
        self.assertTrue(lines[-2].startswith("Leans toward the stop"))
        self.assertTrue(lines[-1].startswith("After charges on 37 shares"))
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", REL, "--now", NOW, "--config", self.cfg])
        self.assertEqual(again, text)


def frontmatter(text):
    """The SKILL.md frontmatter the way a strict reader sees it: ({key: value}, body). Folded (>-) and
    plain values are joined with single spaces. Raises AssertionError on anything malformed."""
    assert text.startswith("---\n"), "no opening ---"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, "no closing ---"
    assert "\t" not in head, "a tab in the frontmatter"
    meta, key = {}, None
    for line in head.split("\n"):
        if not line.strip():
            continue
        if line[0] not in " -" and ":" in line:
            key, val = line.split(":", 1)
            val = val.strip()
            meta[key] = "" if val in (">-", ">", "|", "|-") else val.strip('"')
        elif key and line.startswith("  "):
            meta[key] = (meta[key] + " " + line.strip()).strip()
        else:
            raise AssertionError("cannot place the frontmatter line %r" % line)
    return meta, body


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-timemachine", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-timemachine\ndescription: >-\n"))
        head = text.split("---")[1]
        desc = head.split("description: >-")[1].split("argument-hint:")[0]
        self.assertGreater(len(" ".join(desc.split())), 200)
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("not financial advice", "**yes**", "${claude_skill_dir}", "seed 19", "75 five-minute bars",
                     "15:10", "charges", "22.6 %", "69.1 %", "8.3 %", "/nse-budget", "not a prediction", "nse"):
            self.assertIn(must, low)
        for never in ("—", "dollar", "pro" + "fit", "trader-", "new york"):
            self.assertNotIn(never, low.replace("day-trader-india", ""))     # the plugin's own name is fine

    def test_the_frontmatter_has_a_description_a_skill_list_can_show(self):
        # nse-timemachine showed up in a live skill list with NO description. The frontmatter parses (this reader is
        # stricter than most); what it was is a description of 942 characters, the longest-winded of the pack,
        # against a listing that has a budget. So: parse it, and keep it short (under 600 characters).
        with open(os.path.join(ROOT, "nse-timemachine", "SKILL.md"), encoding="utf-8") as fh:
            meta, body = frontmatter(fh.read())
        self.assertEqual(meta["name"], "nse-timemachine")
        self.assertEqual(sorted(meta), ["argument-hint", "description", "name"])
        d = meta["description"]
        self.assertTrue(d.startswith("Time Machine for NSE intraday trades."))
        self.assertGreater(len(d), 200)
        self.assertLess(len(d), 600, len(d))
        self.assertNotIn("  ", d)                                     # folded to single spaces
        low = d.lower()
        for phrase in ("run the time machine", "replay this trade", "what are the odds", "simulate this trade",
                       "monte carlo", "backtest", "coin flip", "/nse-exit", "/nse-budget"):
            self.assertIn(phrase, low)
        self.assertIn("10,000", d)
        self.assertTrue(meta["argument-hint"].startswith("["))
        self.assertTrue(body.lstrip().startswith("# nse-timemachine"))


if __name__ == "__main__":
    unittest.main()
