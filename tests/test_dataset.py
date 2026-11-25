"""The dataset (autopilot/dataset.py): every signal on the archive with its features and its replayed outcome, the
live day's decisions and trades joined on, the tables and their README in step, and the end of day rebuilding it
without ever failing the day."""

import csv
import json
import os
import sys
import unittest
from unittest import mock

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import autopilot_common as ac  # noqa: E402
import dataset  # noqa: E402
from test_autopilot import DAY, HomeBase, fixture_archive  # noqa: E402


def read(folder, name):
    with open(os.path.join(folder, name + ".csv"), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Columns(unittest.TestCase):
    def test_labels_and_features_never_mix(self):
        for name, cols in dataset.TABLES:
            names = [c for c, _, _ in cols]
            self.assertEqual(len(names), len(set(names)), name)
        for c, kind, _ in dataset.SIGNAL_COLS:
            self.assertEqual(c.startswith("label_"), kind == dataset.LABEL, c)

    def test_the_readme_lists_every_column(self):
        meta = {"built_at": "now", "days": 1, "first_day": DAY, "last_day": DAY, "days_1m": 1, "live_days": [],
                "rows": {}}
        text = dataset.readme(meta)
        for name, cols in dataset.TABLES:
            self.assertIn("## %s.csv" % name, text)
            for c, _, _ in cols:
                self.assertIn("| `%s` |" % c, text)


class Build(HomeBase):
    def setUp(self):
        super(Build, self).setUp()
        fixture_archive(self.d)
        self.settings["dataset"] = {"enabled": True}
        self.write_settings()

    def test_the_archive_alone_gives_signals_with_features_and_labels(self):
        data = dataset.build(self.d, ac.load_settings(self.d), ac.load_cfg(self.d))
        sig = data["signals"]
        self.assertTrue(sig)
        rel = [s for s in sig if s["symbol"] == "RELIANCE" and s["date"] == DAY]
        self.assertEqual(len(rel), 1)
        r = rel[0]
        self.assertEqual(r["label_bars"], "1m")                 # the one day with 1-minute bars
        self.assertEqual(r["setup"], "orb_long")
        self.assertGreater(r["or_high"], r["or_low"])
        self.assertAlmostEqual(r["stop"], (r["or_high"] + r["or_low"]) / 2, delta=0.1)
        self.assertIn(r["label_result"], ("target", "stop", "flat_by", "never filled"))
        self.assertIsNone(r.get("live"))                        # no live record that day
        others = [s for s in sig if s["date"] != DAY]
        self.assertTrue(all(s["label_bars"] == "5m" for s in others))
        self.assertEqual(data["meta"]["live_days"], [])
        self.assertEqual(len(data["days"]), 5)

    def test_the_end_of_day_writes_it_with_the_live_day_joined(self):
        self.replay()
        eod = self.state()["phases"]["eod"]
        self.assertTrue(eod.get("done"))
        folder = os.path.join(self.d, "dataset")
        self.assertIn("in %s" % folder, eod["dataset"])
        for name in [n for n, _ in dataset.TABLES] + ["README", "meta", "notes"]:
            ext = {"README": ".md", "meta": ".json", "notes": ".jsonl"}.get(name, ".csv")
            self.assertTrue(os.path.isfile(os.path.join(folder, name + ext)), name)
        sig = read(folder, "signals")
        self.assertEqual(list(sig[0]), [c for c, _, _ in dataset.SIGNAL_COLS])
        rel = [s for s in sig if s["symbol"] == "RELIANCE" and s["date"] == DAY][0]
        paper = self.doc("state", "paper.json")["trades"][0]
        self.assertEqual((rel["live"], rel["live_outcome"], rel["live_traded"]), ("1", "traded", "1"))
        self.assertAlmostEqual(float(rel["live_net_inr"]), paper["pnl_net"])
        self.assertTrue(rel["tm_p_target"])
        trades = read(folder, "trades")
        self.assertEqual([(t["symbol"], t["account"], t["main"], t["slot"]) for t in trades],
                         [("RELIANCE", "cap3", "1", "1")])
        self.assertTrue(trades[0]["why"])
        self.assertIn("the signal:", trades[0]["decisions"])
        self.assertNotIn("**", trades[0]["decisions"])
        dec = [d for d in read(folder, "decisions") if d["symbol"] == "RELIANCE"]
        self.assertEqual([(d["account"], d["outcome"]) for d in dec], [("cap3", "traded")])
        days = {d["date"]: d for d in read(folder, "days")}
        self.assertEqual(days[DAY]["live"], "1")
        self.assertEqual(days[DAY]["main_account"], "cap3")
        acc = read(folder, "account_days")
        self.assertEqual([(a["account"], a["orders"]) for a in acc], [("cap3", "1")])
        meta = self.doc("dataset", "meta.json")
        self.assertEqual(meta["live_days"], [DAY])
        with open(os.path.join(folder, "notes.jsonl"), encoding="utf-8") as fh:
            notes = [json.loads(x) for x in fh]
        self.assertTrue(all(n["date"] == DAY and n["text"] for n in notes))

    def test_a_dataset_failure_never_fails_the_day(self):
        with mock.patch.object(dataset, "write_all", side_effect=RuntimeError("boom")):
            self.replay()
        eod = self.state()["phases"]["eod"]
        self.assertTrue(eod.get("done"))
        self.assertIn("failed: RuntimeError: boom", eod["dataset"])

    def test_off_by_default(self):
        self.settings.pop("dataset")
        self.write_settings()
        self.replay()
        self.assertEqual(self.state()["phases"]["eod"]["dataset"], "off")
        self.assertFalse(os.path.exists(os.path.join(self.d, "dataset")))


if __name__ == "__main__":
    unittest.main()
