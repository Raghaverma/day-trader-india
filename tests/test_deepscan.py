"""The deep pre-market (autopilot/deepscan.py), offline: the desks' bookkeeping, the sit-outs run_day.py
applies, the report's review and the clock. No network, no Claude, no mail."""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import autopilot_common as ac  # noqa: E402
import deepscan  # noqa: E402

DAY = "2026-11-23"


def envelope(text, tokens=1000):
    return True, {"result": text, "usage": {"input_tokens": tokens, "output_tokens": 100},
                  "total_cost_usd": 0.01, "_seconds": 1.0}, None


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"NSE_TEST_MODE": "1", "NSE_TRADER_HOME": self.home})
        p.start()
        self.addCleanup(p.stop)
        self.dd = deepscan.deep_dir(self.home, DAY)
        os.makedirs(os.path.join(self.dd, "desks"))

    def tearDown(self):
        shutil.rmtree(self.home)

    def desk(self, runner, **deep):
        d = dict(deepscan.DEFAULTS, **deep)
        return deepscan.Desk(self.home, self.dd, {"claude": {"bin": "claude"}}, d, runner=runner)


class DeskTest(Base):
    def test_answer_parsed_saved_and_counted(self):
        calls = []

        def runner(settings, kind, prompt, timeout):
            calls.append(settings["claude"]["deep_model"])
            return envelope('Note.\n\n```json\n{"symbol": "INFY", "sit_out": false}\n```')
        d = self.desk(runner)
        out = d.call("company-INFY", "company", {"x": 1}, "sonnet")
        self.assertTrue(out["ok"])
        self.assertEqual(out["json"]["symbol"], "INFY")
        self.assertEqual(out["text"], "Note.")
        self.assertEqual(calls, ["sonnet"])
        self.assertEqual(d.usage["tokens"], 1100)
        again = d.call("company-INFY", "company", {"x": 1}, "sonnet")      # cached: no second call
        self.assertTrue(again["ok"])
        self.assertEqual(len(calls), 1)

    def test_usage_limit_stops_every_later_call(self):
        n = []

        def runner(settings, kind, prompt, timeout):
            n.append(1)
            return False, {"result": "Claude AI usage limit reached|1763870400"}, "exit 1: usage limit reached"
        d = self.desk(runner)
        self.assertFalse(d.call("macro", "macro", {}, "sonnet")["ok"])
        self.assertEqual(len(n), 1)                 # no retry after a usage limit
        self.assertIn("usage limit", d.stopped)
        out = d.call("company-TCS", "company", {}, "sonnet")
        self.assertIn("not run", out["error"])
        self.assertEqual(len(n), 1)

    def test_token_budget(self):
        d = self.desk(lambda *a: self.fail("called over the budget"), max_tokens=10)
        self.assertIn("budget", d.call("macro", "macro", {"big": "x" * 1000}, "sonnet")["error"])

    def test_no_json_is_not_ok_and_retried_once(self):
        n = []

        def runner(*a):
            n.append(1)
            return envelope("no block here")
        with mock.patch.object(deepscan.time, "sleep"):
            out = self.desk(runner).call("macro", "macro", {}, "sonnet")
        self.assertFalse(out["ok"])
        self.assertEqual(len(n), 2)


class VetoTest(Base):
    def write(self, doc):
        ac.write_json(os.path.join(self.dd, "vetoes.json"), doc)

    def test_live_vetoes(self):
        s = {"deep": {"enabled": True, "apply": True}}
        self.assertEqual(deepscan.live_vetoes(self.home, DAY, s, ["TCS"])[0], [])
        self.assertIn("not ready", deepscan.live_vetoes(self.home, DAY, s, ["TCS"])[1])
        self.write({"ok": True, "vetoes": [{"symbol": "TCS", "category": "results", "reason": "Q3 today"},
                                           {"symbol": "NOTINUNIVERSE", "category": "OTHER", "reason": "x"}]})
        vs, why = deepscan.live_vetoes(self.home, DAY, s, ["TCS", "INFY"])
        self.assertEqual([(v["symbol"], v["category"], v["source"]) for v in vs], [("TCS", "RESULTS", "deep")])
        self.assertEqual(why, "1 applied")
        vs, why = deepscan.live_vetoes(self.home, DAY, {"deep": {"enabled": True, "apply": False}}, ["TCS"])
        self.assertEqual(vs, [])
        self.assertIn("shadow", why)
        self.assertEqual(deepscan.live_vetoes(self.home, DAY, {}, ["TCS"]), ([], "off"))
        self.write({"ok": False, "vetoes": [], "error": "timed out"})
        self.assertIn("did not answer", deepscan.live_vetoes(self.home, DAY, s, ["TCS"])[1])

    def test_review(self):
        ac.write_json(os.path.join(self.dd, "state.json"), {"phase1": {"done": "x"}, "phase2": {}})
        self.write({"ok": True, "vetoes": [{"symbol": "TCS", "category": "RESULTS", "reason": "r", "source": "deep"}],
                    "favoured": [{"symbol": "INFY", "conviction": 4, "why": "w"}, {"symbol": "SBIN", "conviction": 3}]})
        rep = {"trades": [{"symbol": "INFY", "net": 1200.0, "r": 0.5}],
               "shadows": {"rows": [{"symbol": "TCS", "why": "vetoed", "net": -800.0, "r": -0.4}]},
               "candidates": [], "vetoes": [{"symbol": "TCS", "source": "deep"}]}
        r = deepscan.review(self.home, DAY, rep)
        self.assertTrue(r["vetoes"][0]["applied"])
        self.assertEqual(r["vetoes_net_if_taken"], -800.0)
        self.assertEqual(r["favoured"][0]["outcome"]["what"], "traded")
        self.assertEqual(r["favoured"][1]["outcome"]["what"], "no signal")
        self.assertEqual(r["favoured_net"], 1200.0)
        import report
        md = report.to_markdown(report.deep_blocks(r))
        self.assertIn("Deep pre-market research", md)
        self.assertIn("TCS", md)

    def test_review_none_without_a_run(self):
        self.assertIsNone(deepscan.review(self.home, "2026-11-24", {}))


class DataTest(unittest.TestCase):
    def test_summarize_and_breadth(self):
        t0 = 1700000000
        rows = [(t0 + i * 86400, 100 + i, 101 + i, 99 + i, 100.0 + i, 1000) for i in range(260)]
        s = deepscan.summarize(rows)
        self.assertEqual(s["last"], 359.0)
        self.assertAlmostEqual(s["chg_1d"], 0.28, places=2)
        self.assertGreater(s["last"], s["sma200"])
        st = {"A": dict(s, industry="IT"), "B": dict(s, industry="IT", chg_1d=-1.0)}
        b = deepscan.breadth_stats({"stocks": st})
        self.assertEqual((b["advancers_1d"], b["decliners_1d"]), (1, 1))
        self.assertEqual(b["sectors"]["IT"]["stocks"], 2)

    def test_rss(self):
        xml = (b"<rss><channel><item><title>Nifty ends higher - Mint</title><source>Mint</source>"
               b"<pubDate>Fri, 20 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>")
        it = deepscan.rss_items(xml)[0]
        self.assertEqual(it["title"], "Nifty ends higher")
        self.assertEqual(it["time"], "2026-11-20 15:30 IST")


class ClockTest(Base):
    def settings(self, **deep):
        ac.write_json(os.path.join(self.home, "autopilot.json"), {"deep": dict({"enabled": True}, **deep)})

    def test_quiet_outside_its_hours_and_on_closed_days(self):
        self.settings()
        with mock.patch.object(ac, "load_cfg", return_value={}), \
                mock.patch.object(deepscan, "Day", side_effect=AssertionError("ran")):
            self.assertEqual(deepscan.tick(self.home, datetime.datetime(2026, 11, 22, 3, 0)), 0)    # Sun 08:30 IST
            self.assertEqual(deepscan.tick(self.home, datetime.datetime(2026, 11, 23, 23, 0)), 0)   # Tue 04:30 IST: holiday
            self.assertEqual(deepscan.tick(self.home, datetime.datetime(2026, 11, 23, 4, 0)), 0)    # Mon 09:30 IST: too late
            self.assertEqual(deepscan.tick(self.home, datetime.datetime(2026, 11, 22, 21, 0)), 0)   # Mon 02:30 IST: early
            with self.assertRaises(AssertionError):                                                 # Mon 04:30 IST: runs
                deepscan.tick(self.home, datetime.datetime(2026, 11, 22, 23, 0))

    def test_off_by_default(self):
        ac.write_json(os.path.join(self.home, "autopilot.json"), {})
        with mock.patch.object(deepscan, "Day", side_effect=AssertionError("ran")):
            self.assertEqual(deepscan.tick(self.home, datetime.datetime(2026, 11, 22, 23, 0)), 0)


if __name__ == "__main__":
    unittest.main()
