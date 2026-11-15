"""nse-exit tests. Offline. The worked example is RELIANCE on Fri 13 Nov 2026
(real data): opening range 1,324.30-1,333.40, entry 1,334.00 at 10:05 IST, the
or-mid stop 1,328.85 rounds down to the 0.10 tick = 1,328.80. The entry.json
fixture is in the shape the original trader-entry writes; the ATR stop reads
the real 5-minute bars in tests/fixtures/nse-entry/."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("nse-exit", "exit")
nc = exitp.nc

ENTRY = os.path.join(FIX, "nse-exit", "entry-RELIANCE-2026-11-13.json")
BARS5 = os.path.join(FIX, "nse-entry", "RELIANCE-2026-11-13-5m.json")
with open(ENTRY, encoding="utf-8") as _fh:
    REL_ENTRY = json.load(_fh)

# trader/plan.json for the worked example: the original trader-exit fields plus exchange and tick
REL_PLAN = {"created_by": "nse-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-11-13", "setup": "orb",
            "entry": 1334.0, "entry_time": "10:05", "entry_bar": 9, "interval": "5m",
            "opening_range": {"high": 1333.4, "low": 1324.3, "minutes": 15},
            "stop": 1328.8, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
            "target": 1344.4, "target_r": 2.0, "target_label": "2R (two times the risk)",
            "risk_per_share": 5.2, "reward_risk": 2.0, "flat_by": "15:10",
            "exchange": "NSE", "tick": "0.10"}
ORIGINAL_FIELDS = ("created_by", "symbol", "side", "date", "setup", "entry", "entry_time", "entry_bar", "interval",
                   "opening_range", "stop", "stop_rule", "stop_label", "target", "target_r", "target_label",
                   "risk_per_share", "reward_risk", "flat_by", "stop_detail", "created_at", "note")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = exitp.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1333.40, ol=1324.30, atr=None, n=None, sym=None, cfg=None, spread=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, sym, cfg, spread)


class Math(unittest.TestCase):
    def test_the_worked_example_or_mid_2r(self):
        p = c(1334.0, "or-mid")
        self.assertEqual(p["stop"], Decimal("1328.80"))          # floor(1328.85) on the 0.10 tick
        self.assertEqual(p["risk"], Decimal("5.20"))
        self.assertEqual(p["target"], Decimal("1344.40"))        # the original's default 2R
        self.assertEqual(p["reward_risk"], Decimal("2.00"))
        self.assertEqual(p["tick"], Decimal("0.10"))
        self.assertEqual(p["risk_ticks"], 52)
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        self.assertEqual(p["stop_detail"], "(₹1,333.40 + ₹1,324.30) / 2 = ₹1,328.85, rounded down to "
                                           "the ₹0.10 tick")

    def test_1r(self):
        p = c(1334.0, "or-mid", "1R")
        self.assertEqual((p["target"], p["target_label"]), (Decimal("1339.20"), "1R (one times the risk)"))

    def test_raw_yahoo_floats_give_the_same_ticks(self):
        p = c(1334.0, "or-mid", "2R", 1333.4000244140625, 1324.300048828125)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1334.00"), Decimal("1328.80"),
                                                                Decimal("1344.40")))
        p = c(1333.9999, "or-mid")
        self.assertEqual(p["entry"], Decimal("1334.00"))

    def test_other_stops(self):
        p = c(1334.0, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1324.30"), Decimal("9.70"), Decimal("1353.40")))
        p = c(1334.0, "price:1328.85")
        self.assertEqual(p["stop"], Decimal("1328.80"))          # rounds away from the entry, onto the tick
        self.assertIn("rounded down to the ₹0.10 tick", p["stop_detail"])
        p = c(1334.0, "price:₹1,328.80")
        self.assertEqual((p["stop"], p["stop_detail"]), (Decimal("1328.80"), ""))
        p = c(1334.0, "pct:1")                                   # 1334 x 0.99 = 1320.66
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1320.60"), Decimal("13.40"), Decimal("1360.80")))
        p = c(1334.0, "atr:1.5", atr=3.55999755859375, n=10)     # 1334 - 5.34 = 1328.66
        self.assertEqual((p["stop"], p["target"]), (Decimal("1328.60"), Decimal("1344.80")))
        self.assertEqual(p["stop_label"], "1.5 x ATR(10) under the entry")
        self.assertIn("fewer than 14 so far", p["stop_detail"])

    def test_stop_rounds_down_at_every_slab_edge(self):
        # (entry, raw stop, stop on the NSE tick for the stop's own price)
        cases = [(260, "250.04", "250.00"),      # 0.05 tick above 250
                 (255, "249.996", "249.99"),     # 0.01 tick below 250
                 (1010, "1000.07", "1000.00"),   # 0.10 tick from 1,000
                 (1010, "999.99", "999.95"),     # 0.05 tick below 1,000
                 (5100, "5000.4", "5000.00"),    # 0.50 tick from 5,000
                 (5100, "4999.97", "4999.90"),   # 0.10 tick below 5,000
                 (10100, "10000.9", "10000.00"),  # 1.00 tick from 10,000
                 (10100, "9999.9", "9999.50"),   # 0.50 tick below 10,000
                 (21000, "20003", "20000.00"),   # 5.00 tick from 20,000
                 (20500, "19999.5", "19999.00")]  # 1.00 tick below 20,000
        for e, raw, want in cases:
            p = c(e, "price:" + raw, oh=None, ol=None)
            self.assertEqual(p["stop"], Decimal(want), raw)
            self.assertTrue(nc.on_tick(p["stop"], nc.tick_size(p["stop"])), raw)
            self.assertTrue(nc.on_tick(p["target"], nc.tick_size(p["target"])), raw)

    def test_target_on_the_tick_grid_at_an_edge(self):
        p = c(249.0, "price:248.37", oh=None, ol=None)         # 249 + 2 x 0.63 = 250.26 -> 0.05 tick
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("248.37"), Decimal("0.63"), Decimal("250.25")))
        p = c(995.0, "price:990", oh=None, ol=None)
        self.assertEqual(p["target"], Decimal("1005.00"))
        p = c(1334.0, "or-mid", "price:1344.43")
        self.assertEqual(p["target"], Decimal("1344.40"))        # half up to the nearest 0.10
        self.assertEqual(p["target_label"], "your price (2.0R)")

    def test_tick_override_from_config(self):
        p = c(1334.0, "or-mid", sym="RELIANCE", cfg={"tick_sizes": {"RELIANCE": 0.05}})
        self.assertEqual((p["stop"], p["tick"]), (Decimal("1328.85"), Decimal("0.05")))

    def test_too_tight_counts_ticks_and_the_spread(self):
        # 0.05% of 1,334 = 0.667: the floor on the 0.10 tick is 0.70 (7 ticks); 3 ticks would be 0.30
        with self.assertRaises(exitp.PlanError) as cm:
            c(1334.0, "price:1333.40")
        msg = str(cm.exception)
        for words in ("₹0.60 under the entry (6 ticks of ₹0.10)", "closer than ₹0.70 (7 ticks",
                      "3 spreads", "too tight to survive the spread"):
            self.assertIn(words, msg.replace("Too tight", "too tight"))
        self.assertEqual(c(1334.0, "price:1333.30")["risk"], Decimal("0.70"))
        # a cheap stock: 0.05% of 50 is 0.025, so three one-tick spreads (0.03) decide
        with self.assertRaises(exitp.PlanError) as cm:
            c(50.0, "price:49.98", oh=None, ol=None)
        self.assertIn("2 ticks of ₹0.01", str(cm.exception))
        self.assertEqual(c(50.0, "price:49.97", oh=None, ol=None)["risk"], Decimal("0.03"))
        # the spread you see in Kite's market depth widens the floor to three of it
        with self.assertRaises(exitp.PlanError) as cm:
            c(50.0, "price:49.90", oh=None, ol=None, spread=0.05)
        self.assertIn("3 spreads of ₹0.05", str(cm.exception))
        self.assertEqual(c(50.0, "price:49.85", oh=None, ol=None, spread=0.05)["risk"], Decimal("0.15"))
        # a spread under one tick still counts as a tick
        self.assertEqual(c(50.0, "price:49.97", oh=None, ol=None, spread=0.001)["risk"], Decimal("0.03"))

    def test_refusals_say_why(self):
        cases = [("price:1334", "2R", "at or above the entry"),
                 ("price:1335", "2R", "at or above the entry"),
                 ("or-mid", "price:1334", "at or below the entry"),
                 ("price:1333.9", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1334.0, stop, target)
            self.assertIn(why, str(cm.exception).replace("Too tight", "too tight"), stop)
        with self.assertRaises(exitp.PlanError):
            c(1328.0, "or-mid")                                  # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1334.0, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1334.0, "or-mid", oh=1324.30, ol=1333.40)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("RELIANCE", "5m", None, BARS5)
        day = exitp.datetime.date(2026, 11, 13)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=9)
        self.assertEqual(len(bars), 10)                          # 09:15 ... the 10:00 entry bar
        self.assertEqual(nc.ist_from_epoch(bars[0]["t"]), exitp.datetime.datetime(2026, 11, 13, 9, 15))
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1333.40 - 1326.40, places=3)   # no overnight gap in the first bar
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 10)
        self.assertAlmostEqual(atr, sum(trs) / 10, places=9)
        self.assertAlmostEqual(atr, 3.56, places=4)
        # the whole day: Yahoo's empty 15:20 bar and the 15:25 bar (not closed by 15:28) never count
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 73)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        with self.assertRaises(exitp.PlanError):
            exitp.load_chart("TCS", "5m", None, BARS5)           # the file holds RELIANCE.NS


class Charges(unittest.TestCase):
    def test_charges_break_even_and_r_after_charges(self):
        p = c(1334.0, "or-mid")
        ch = exitp.charges_for(p, 56, "--qty")
        self.assertEqual(ch["at_target"], 73.87)                 # PORTING.md: 56 at 1,334.00 out at 1,344.40
        self.assertEqual(ch["at_target"], nc.intraday_charges(56, 1334.0, 1344.4)["total"])
        self.assertEqual(ch["at_stop"], nc.intraday_charges(56, 1334.0, 1328.8)["total"])
        self.assertEqual(ch["at_stop"], 73.62)
        self.assertEqual((ch["per_share_at_target"], ch["per_share_at_stop"]), (1.32, 1.31))
        self.assertEqual(ch["max_loss"], 291.2)
        self.assertEqual(ch["max_loss_with_charges"], 364.82)
        # (56 x 10.40 - 73.87) / (56 x 5.20 + 73.62) = 508.53 / 364.82
        self.assertEqual(ch["reward_risk_after_charges"], 1.39)
        self.assertEqual(ch["break_even"], 1335.4)
        be = exitp.D("1335.40")
        self.assertGreaterEqual(56 * (be - exitp.D("1334")), exitp.D(str(nc.intraday_charges(56, 1334.0, be)["total"])))
        below = be - exitp.D("0.10")
        self.assertLess(56 * (below - exitp.D("1334")), exitp.D(str(nc.intraday_charges(56, 1334.0, below)["total"])))
        self.assertEqual(exitp.charges_for(p, 1, "--qty")["break_even"], 1335.5)
        with self.assertRaises(exitp.PlanError):
            exitp.charges_for(p, 0, "--qty")

    def test_default_qty_from_the_config_caps(self):
        cfg = dict(nc.DEFAULT_CONFIG)
        # 500 / 5.20 = 96, 80,000 / 1,334 = 59, 75% of 1,00,000 / 1,334 = 56
        self.assertEqual(exitp.default_qty(Decimal("1334"), Decimal("5.2"), cfg), 56)
        self.assertEqual(exitp.default_qty(Decimal("1334"), Decimal("20"), dict(cfg, max_loss_per_trade_inr=500)), 25)
        self.assertEqual(exitp.default_qty(Decimal("150000"), Decimal("500"), cfg), 1)

    def test_a_config_charge_override_flows_through(self):
        p = c(1334.0, "or-mid")
        cheap = exitp.charges_for(p, 56, "--qty", cfg={"charges": {"brokerage_cap": 0}})
        self.assertLess(cheap["at_target"], 73.87)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        shutil.copy(ENTRY, os.path.join("trader", "entry.json"))

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(["plan", "--now", "2026-11-13 10:06"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_json_to_plan_json(self):
        code, text, err = self.plan("--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REL_PLAN.items():
            self.assertEqual(p[k], v, k)
        for k in ORIGINAL_FIELDS:
            self.assertIn(k, p)
        self.assertEqual(p["created_at"], "2026-11-13T04:36:00Z")    # 10:06 IST
        self.assertIsInstance(p["tick"], str)
        self.assertIn("Stop          ₹1,328.80  opening-range midpoint: (₹1,333.40 + ₹1,324.30) / 2 = "
                      "₹1,328.85, rounded down to the ₹0.10 tick", text)
        self.assertIn("Target        ₹1,344.40  2R (two times the risk)", text)
        self.assertIn("Risk a share  ₹5.20 (0.39% of the entry, 52 ticks of ₹0.10)", text)
        self.assertIn("Reward:risk   2.0 (1.39 after charges)", text)
        self.assertIn("about ₹1.32 a share at the target, ₹1.31 at the stop (₹73.87 and ₹73.62 a "
                      "round trip on 56 shares", text)
        self.assertIn("Break-even    ₹1,335.40", text)
        self.assertIn("Flat by       15:10 IST", text)
        self.assertIn("cancel the stop, then sell", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/nse-timemachine", text)
        self.assertNotIn("$", text)
        self.assertNotIn("dollar", text.lower())
        self.assertNotIn("pro" + "fit", text.lower())
        self.assertNotIn("Note:", text)                               # today, a trading day, a known year
        code, text, _ = self.plan("--target", "1R", "--write")
        self.assertEqual((self.read_plan()["target"], self.read_plan()["target_r"]), (1339.2, 1.0))
        code, text, _ = run(["show", "--now", "2026-11-13 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("₹1,339.20", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(["show", "--now", "2026-11-16 09:20"])
        self.assertIn("not today", text)
        code, text, _ = run(["show", "--json"])
        self.assertEqual(json.loads(text)["exchange"], "NSE")

    def test_manual_flags_and_json(self):
        os.remove(os.path.join("trader", "entry.json"))
        code, text, err = self.plan("--symbol", "reliance.ns", "--entry", "1334", "--or-high", "1333.40",
                                    "--or-low", "1324.30", "--time", "10:05", "--entry-bar", "9", "--date",
                                    "2026-11-13", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        for k, v in REL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        self.assertEqual((p["charges"]["quantity"], p["charges"]["quantity_from"]), (56, "config caps"))
        code, text, _ = self.plan("--symbol", "RELIANCE", "--entry", "1334", "--or-high", "1333.40", "--or-low",
                                  "1324.30", "--date", "2026-11-13", "--qty", "10", "--json")
        self.assertEqual(json.loads(text)["charges"]["quantity"], 10)
        code, _, err = self.plan("--symbol", "not a symbol", "--entry", "10", "--stop", "pct:1")
        self.assertIn("not an NSE symbol", err)

    def test_the_entry_time_is_ist_and_12_hour_also_reads(self):
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, time="10:05 AM"))
        code, text, _ = self.plan("--json")
        self.assertEqual(json.loads(text)["entry_time"], "10:05")
        # --now is IST: 10:06 IST is 04:36 UTC, and the plan is for today
        self.assertNotIn("not today", text)
        code, text, _ = run(["plan", "--now", "2026-11-13 04:36Z", "--json"])
        self.assertEqual(json.loads(text)["created_at"], "2026-11-13T04:36:00Z")
        code, text, _ = run(["plan", "--now", "2026-11-12 23:59", "--json"])
        self.assertIn("not today (2026-11-12 IST)", json.loads(text)["warnings"][-1])
        code, _, err = run(["plan", "--now", "13/11/2026 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("IST", err)

    def test_atr_stop_reads_bars(self):
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", BARS5, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1328.6, 5.4, 1344.8))
        self.assertEqual((p["stop_rule"], p["atr_bars"], p["atr"]), ("atr", 10, 3.56))
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", BARS5, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1333.50", "--json")   # plan ahead just over the range high
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1333.5, 1328.8, None))

        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, setup="level", level=1333.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1334", "--or-high", "1333.40")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_15_10_config_and_late_entries(self):
        code, text, _ = self.plan("--json")
        self.assertEqual(json.loads(text)["flat_by"], "15:10")
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("Reward:risk 1.39 after charges is under your min_reward_risk 1.5", p["warnings"][0])
        self.write(os.path.join("trader", "config.json"), {"include_charges": False, "min_reward_risk": 1.5})
        self.assertEqual(json.loads(self.plan("--json")[1])["warnings"], [])     # 2.0 before charges passes
        os.remove(os.path.join("trader", "config.json"))

        code, text, _ = self.plan("--flat-by", "15:22", "--json")
        self.assertIn("Zerodha's 15:25 MIS auto square-off", json.loads(text)["warnings"][0])
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.assertIn("at or after the 15:30 IST close", err)
        code, _, err = self.plan("--flat-by", "09:00")
        self.assertIn("at or before the 09:15 IST open", err)
        code, _, err = self.plan("--flat-by", "3pm")
        self.assertIn("IST time like 15:10", err)
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, time="15:15"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_a_special_session_moves_flat_by(self):
        # Muhurat trading is configured, never assumed: the 15:10 flat-by keeps its 20 minutes before the close
        cfg = {"special_sessions": {"2026-11-08": {"open": "18:15", "close": "19:15", "label": "Muhurat trading"}}}
        self.write(os.path.join("trader", "config.json"), cfg)
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, date="2026-11-08", time="18:35"))
        code, text, err = run(["plan", "--now", "2026-11-08 18:36", "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "18:55")
        self.assertIn("Muhurat trading (18:15-19:15 IST)", p["warnings"][0])
        code, _, err = run(["plan", "--now", "2026-11-08 18:36", "--flat-by", "15:10"])
        self.assertIn("at or before the 18:15 IST open", err)

    def test_holidays_and_an_unknown_year(self):
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, date="2026-11-24"))
        code, text, _ = run(["plan", "--now", "2026-11-24 10:06", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("NSE is closed on 2026-11-24 (NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev)",
                      json.loads(text)["warnings"][0])
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, date="2027-01-04"))
        code, text, _ = run(["plan", "--now", "2027-01-04 10:06", "--json"])
        self.assertIn("NSE's 2027 holiday list is not known here yet", json.loads(text)["warnings"][0])
        # once nse-alerts has cached the 2027 list, the note goes away
        nc.write_json(os.path.join("trader", "state", nc.HOLIDAY_CACHE),
                      {"fetched_at": "2026-12-15T00:00:00Z", "source": "test", "holidays": {"2027-01-26": "Republic Day"},
                       "years": [2027]})
        code, text, _ = run(["plan", "--now", "2027-01-04 10:06", "--json"])
        self.assertEqual(json.loads(text)["warnings"], [])
        self.write(os.path.join("trader", "entry.json"), dict(REL_ENTRY, date="2026-11-14"))
        code, text, _ = run(["plan", "--now", "2026-11-14 10:06", "--json"])
        self.assertIn("(Saturday)", json.loads(text)["warnings"][0])

    def test_show_without_a_plan(self):
        code, text, _ = run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)
        self.assertEqual(run([])[0], 1)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-exit\ndescription: >-\n"))
        head = text.split("---")[1]
        desc = head.split("description: >-")[1].split("argument-hint:")[0]
        self.assertGreater(len(" ".join(desc.split())), 200)
        self.assertIn("argument-hint:", head)
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("not financial advice", "**yes**", "${claude_skill_dir}", "before the entry",
                     "a stop is a plan, not a guarantee", "day trading loses most people money", "/nse-timemachine",
                     "/nse-budget", "longs only", "₹1,328.80", "₹1,344.40", "₹5.20", "₹73.87",
                     "15:10", "tick", "cancel the stop, then sell", "too tight to survive the spread", "nse", "kite"):
            self.assertIn(must, low)
        for never in ("—", "$", "dollar", "pro" + "fit", "trader-"):
            self.assertNotIn(never, low.replace("${claude_skill_dir}", ""))


if __name__ == "__main__":
    unittest.main()
