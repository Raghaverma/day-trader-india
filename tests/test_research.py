"""autopilot/research.py: features at the signal, the exit rules, the policies, the other setups, and the live
opening-volume tag (market.opening_rvol)."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from _load import ROOT
from test_autopilot import CFG_PATH, DAY, fixture_archive, chart_doc, nc, market

import research  # noqa: E402

CFG = nc.load_config(CFG_PATH)
PLAN = {"entry": 100.0, "stop": 99.0, "target": 102.0, "quantity": 100, "max_loss": 110.0, "flat_by": "15:10"}


def minute_path(closes, start="10:00", day=DAY):
    """1-minute bars: each bar opens at the last close, and its high/low reach the close +-0.05."""
    t0 = datetime.datetime.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append((t0 + datetime.timedelta(minutes=i), prev, max(prev, c) + 0.05, min(prev, c) - 0.05, c))
        prev = c
    return out


class ExitRules(unittest.TestCase):
    def exit(self, bars, rule):
        return research.walk_exit(bars, DAY, "10:00", True, 100.0, PLAN, rule, CFG, "TEST")

    def test_a_winner_that_comes_back(self):
        # +1.2R by 10:03, then all the way back through the stop
        bars = minute_path([100.0, 100.6, 101.0, 101.2, 100.8, 100.4, 100.0, 99.6, 99.2, 98.9, 98.8])
        self.assertEqual(self.exit(bars, "plan")[0], "stop")
        be = self.exit(bars, "breakeven")
        self.assertEqual(be[0], "breakeven")
        self.assertGreater(be[2], 100.0)                       # out at the entry plus the charges, not at the stop
        tr = self.exit(bars, "trail")
        self.assertEqual(tr[0], "trail")
        self.assertGreater(tr[2], 99.0)

    def test_the_target_sells_at_the_next_open(self):
        bars = minute_path([100.0, 100.8, 101.6, 102.2, 102.4])
        self.assertEqual(self.exit(bars, "plan"), ("target", "10:04", 102.2))
        self.assertNotEqual(self.exit(bars, "trail")[0], "target")      # the trail has no target

    def test_the_time_stop(self):
        bars = minute_path([100.0 + 0.01 * (i % 3) for i in range(60)])
        self.assertEqual(self.exit(bars, "time")[:2], ("time", "10:45"))
        self.assertEqual(self.exit(bars, "plan")[0], "flat_by")

    def test_r_after_charges(self):
        net, r = research.r_of(PLAN, 100.0, 102.0, CFG)
        self.assertLess(net, 200.0)
        self.assertEqual(r, round(net / 110.0, 3))


class Policies(unittest.TestCase):
    def row(self, sym, at="09:35", day=DAY, filled=True, r=0.5, sector="IT", **kw):
        out = {"day": day, "at": at, "symbol": sym, "sector": sector, "filled": filled, "r": {"plan": r} if filled
               else {}, "rvol": None, "rs": None, "range_needed": None}
        out.update(kw)
        return out

    def test_an_unfilled_order_frees_its_slot_and_the_sector_cap(self):
        rows = [self.row("A"), self.row("B", filled=False), self.row("C"), self.row("D", at="09:40"),
                self.row("E", at="09:40")]
        self.assertEqual([r["symbol"] for r in research.pick(rows)], ["A", "B", "C", "D"])
        self.assertEqual([r["symbol"] for r in research.pick(rows, sector_cap=1)], ["A"])

    def test_the_rank_within_a_batch(self):
        batch = [self.row("A", rvol=1.0, rs=0.0, range_needed=1.0), self.row("B", rvol=3.0, rs=1.0, range_needed=0.5),
                 self.row("C")]
        self.assertEqual([r["symbol"] for r in research.score_batch(batch)], ["B", "A", "C"])

    def test_terciles_and_day_stats(self):
        rows = [self.row("S%d" % i, r=float(i), rvol=float(i)) for i in range(9)]
        t = research.terciles(rows, "rvol")
        self.assertEqual([x["avg_r"] for x in t], [1.0, 4.0, 7.0])
        st = research.day_stats(rows, days=[DAY, "2026-11-12"])
        self.assertEqual((st["days"], st["trades"]), (2, 9))      # a day with no trade counts as 0R


class FeaturesAndTheRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        fixture_archive(self.d)
        shutil.copy(CFG_PATH, os.path.join(self.d, "config.json"))

    def test_features_need_five_days_before(self):
        arch = research.Archive(self.d)
        f = research.features(arch, DAY, "RELIANCE", "10:05", 1334.0, 1344.4)
        self.assertIsNone(f["rvol"])                          # the fixture has 4 days before the 13th
        self.assertEqual(f["sector"], "unknown")

    def test_the_whole_run_on_the_fixture_archive(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.d}), redirect_stdout(out):
            self.assertEqual(research.main(["--bars", "1m"]), 0)
        text = out.getvalue()
        for h in ("### 1. Which ORB signals to take", "### 2. How to get out", "### 3. The setups side by side",
                  "first 3 a day in time order (now)", "ranked, one stock per sector"):
            self.assertIn(h, text)
        with open(os.path.join(self.d, "reports", "research.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc["1m"]["rows"]
        rel = [r for r in rows if r["symbol"] == "RELIANCE" and r["setup"] == "orb"]
        # ordered at 10:06 (a minute after the breakout bar), the price had run from 1,334.00 and never came back
        # within the 15 minutes: the live rules leave it unfilled, and it has no exits to walk
        self.assertEqual((rel[0]["filled"], rel[0]["result"], rel[0]["r"]), (False, "never filled", {}))


class OpeningVolumeTag(unittest.TestCase):
    def test_against_its_median(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        epoch = lambda day, hhmm: int((nc.to_utc(datetime.datetime.strptime(day + " " + hhmm, "%Y-%m-%d %H:%M"))
                                       - datetime.datetime(1970, 1, 1)).total_seconds())
        for i in range(6):
            day = (datetime.date(2026, 11, 5) + datetime.timedelta(days=i)).isoformat()
            market.write_day(d, "RELIANCE", "5m", day, [(epoch(day, "09:15"), 1, 1, 1, 1, 0),
                                                        (epoch(day, "09:20"), 1, 1, 1, 1, 1000),
                                                        (epoch(day, "09:25"), 1, 1, 1, 1, 1000),
                                                        (epoch(day, "09:30"), 1, 1, 1, 1, 99999)], "test")
        doc = chart_doc("RELIANCE", [(datetime.datetime(2026, 11, 13, 9, m), 1, 1, 1, 1) for m in (15, 20, 25, 30)])
        fetch = lambda sym, interval, rng=None, period=None: doc
        self.assertEqual(market.opening_rvol(d, fetch, "RELIANCE", DAY), 1.5)       # 3 x 1,000 vs a 2,000 median
        self.assertEqual(market.rvol_band(1.5), "1.5x or more")
        self.assertEqual(market.rvol_band(0.5), "under 0.8x")
        self.assertIsNone(market.opening_rvol(d, fetch, "RELIANCE", "2026-11-07"))   # 2 days before: too few


if __name__ == "__main__":
    unittest.main()
