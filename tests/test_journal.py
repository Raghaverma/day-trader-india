"""The P&L journal (autopilot/journal.py): each trade's reason is worked out from prices by rule, the files are
rebuilt from the trader folder's own records, and the end of day writes it without ever failing the day."""

import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from _load import ROOT, FIX

sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import autopilot_common as ac  # noqa: E402
import journal  # noqa: E402
import market  # noqa: E402
from test_autopilot import DAY, HomeBase  # noqa: E402

CFG = {"max_trades_per_day": 3, "flat_by": "15:10"}


def bars(*rows):
    """[(HH:MM, o, h, l, c)] from (HH:MM, high, low) with open/close in the middle."""
    return [(t, (h + l) / 2, h, l, (h + l) / 2) for t, h, l in rows]


def trade(**kw):
    t = {"id": "X-1", "date": DAY, "symbol": "XYZ", "qty": 100, "entry_type": "LIMIT", "limit": 100.0,
         "ordered_at": "%s 09:37" % DAY, "status": "closed", "stop": {"type": "SL", "trigger": 99.0, "price": 98.5},
         "target": 102.0, "flat_by": "15:10", "max_loss": 150.0, "fill_time": "%s 09:38" % DAY, "fill_price": 100.0}
    t.update(kw)
    return t


CAND = {"scan_at": "09:35", "or_high": 99.9, "pct_over": 0.1, "trigger": {"at": "09:35", "volume_x": 1.4},
        "tm": {"p_target": 0.3, "category": "Close to break-even"}}


class Reasons(unittest.TestCase):
    def test_gave_back_a_winner(self):
        t = trade(exit_time="%s 11:00" % DAY, exit_price=99.0, exit_reason="stop", gross=-100.0, charges=20.0,
                  pnl_net=-120.0)
        b = bars(("09:38", 100.2, 99.9), ("09:50", 101.7, 100.5), ("10:30", 100.5, 99.5), ("11:00", 99.4, 98.9),
                 ("12:00", 99.2, 98.0), ("15:29", 98.6, 98.4))
        r = journal.explain(t, CAND, b, [], CFG)
        self.assertEqual(r["why_category"], "Gave back a winner")
        self.assertEqual(r["best_R"], 1.7)
        self.assertEqual(r["result"], "LOSS")
        self.assertEqual(r["R"], -0.8)
        self.assertIn("85% of the way", r["why"])
        self.assertIn("the stop saved about", r["why"])

    def test_late_fill_on_a_pullback(self):
        t = trade(fill_time="%s 10:30" % DAY, exit_time="%s 11:00" % DAY, exit_price=99.0, exit_reason="stop",
                  gross=-100.0, charges=20.0, pnl_net=-120.0)
        b = bars(("09:40", 100.8, 100.3), ("09:50", 101.0, 100.4), ("10:30", 100.3, 100.0), ("10:40", 100.2, 99.6),
                 ("11:00", 99.5, 98.9))
        r = journal.explain(t, CAND, b, [], CFG)
        self.assertEqual(r["why_category"], "Late fill on a pullback")
        self.assertIn("waited 53 min", r["why"])
        self.assertIn("+1.00R", r["why"])

    def test_failed_breakout_against_a_rising_market(self):
        t = trade(exit_time="%s 10:00" % DAY, exit_price=99.0, exit_reason="stop", gross=-100.0, charges=20.0,
                  pnl_net=-120.0)
        b = bars(("09:38", 100.2, 99.9), ("09:45", 100.3, 99.6), ("10:00", 99.4, 98.9), ("11:00", 102.5, 101.0))
        nifty = bars(("09:38", 25000, 25000), ("10:00", 25100, 25100))
        r = journal.explain(t, CAND, b, nifty, CFG)
        self.assertEqual(r["why_category"], "Failed breakout")
        self.assertIn("recovered and reached the target at 11:00", r["why"])
        self.assertIn("the stock's own weakness", r["why"])
        self.assertEqual(r["nifty_pct_during"], 0.4)

    def test_target(self):
        t = trade(exit_time="%s 10:10" % DAY, exit_price=102.0, exit_reason="target", gross=200.0, charges=20.0,
                  pnl_net=180.0)
        b = bars(("09:38", 100.2, 99.9), ("09:50", 99.9, 99.2), ("10:10", 102.1, 101.4))
        r = journal.explain(t, CAND, b, [], CFG)
        self.assertEqual((r["why_category"], r["result"], r["minutes_held"]), ("Target hit", "WIN", 32))
        self.assertIn("went against us first", r["why"])

    def test_never_filled_price_ran_away(self):
        t = trade(status="cancelled", exit_reason="cancelled", fill_time=None, fill_price=None)
        b = bars(("09:37", 100.6, 100.2), ("09:50", 101.0, 100.4), ("10:17", 102.2, 101.5), ("15:10", 103.0, 102.5))
        r = journal.explain(t, CAND, b, [], CFG)
        self.assertEqual((r["why_category"], r["result"]), ("Never filled: price ran away", "NO FILL"))
        self.assertEqual(r["missed_gross"], 200.0)
        self.assertIn("would have reached the 2R target at 10:17", r["why"])
        self.assertIn("one of the day's 3 trades", r["why"])

    def test_a_limit_cancelled_after_its_minutes_freed_its_slot(self):
        t = trade(status="cancelled", exit_reason="cancelled", fill_time=None, fill_price=None)
        b = bars(("09:37", 100.6, 100.2), ("09:50", 101.0, 100.4), ("10:00", 100.3, 99.5), ("15:10", 103.0, 102.5))
        note = "the LIMIT buy did not fill within 15 minutes of the order (entry_ttl_minutes)"
        r = journal.explain(t, CAND, b, [], CFG, note)
        self.assertEqual(r["why_category"], "Never filled: price ran away")      # 09:37-09:52 never reached 100
        self.assertIn("in the 15 minutes the order was live the lowest price was ₹100.20", r["why"])
        self.assertIn("cancelled after 15 minutes unfilled (entry_ttl_minutes), which freed its slot", r["why"])
        self.assertNotIn("blocked a later signal", r["why"])
        self.assertIsNone(journal.ttl_of("the LIMIT buy never filled by flat_by 15:10"))

    def test_an_unfilled_order_still_says_what_happened(self):
        t = trade(status="cancelled", exit_reason="cancelled", fill_time=None, fill_price=None)
        b = bars(("09:37", 100.6, 100.2), ("09:50", 101.0, 100.4))
        note = "the LIMIT buy did not fill within 15 minutes of the order (entry_ttl_minutes)"
        story = journal.explain(t, CAND, b, [], CFG, note)["story"]
        self.assertEqual(story[1:], ["Order: LIMIT buy 100 at ₹100 at 09:37; stop ₹99, target ₹102 (2R).",
                                     "Not filled: cancelled at 09:52, 15 minutes after the order, with no trade at "
                                     "or under the limit."])

    def test_a_cancelled_limit_gives_its_slot_back(self):
        note = "the LIMIT buy did not fill within 15 minutes of the order (entry_ttl_minutes)"
        ts = [trade(id="A", ordered_at="%s 09:37" % DAY), trade(id="B", ordered_at="%s 09:37" % DAY,
                                                                  status="cancelled", fingerprint="fb"),
              trade(id="C", ordered_at="%s 09:42" % DAY), trade(id="D", ordered_at="%s 09:57" % DAY),
              trade(id="E", ordered_at="%s 09:40" % DAY, status="cancelled", fingerprint="fe")]
        # B gave its slot back at 09:52; E has no 15-minute note (an old LIMIT that waited all day): it keeps it
        self.assertEqual(journal.slot_numbers(ts, {"fb": note, "fe": "never filled by flat_by"}),
                         {"A": 1, "B": 2, "E": 3, "C": 4, "D": 4})

    def test_no_bars_still_explains(self):
        t = trade(exit_time="%s 10:00" % DAY, exit_price=99.0, exit_reason="stop", gross=-100.0, charges=20.0,
                  pnl_net=-120.0)
        r = journal.explain(t, {}, [], [], CFG)
        self.assertEqual(r["result"], "LOSS")
        self.assertTrue(r["why_category"])


class Files(HomeBase):
    def setUp(self):
        super(Files, self).setUp()
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out)
        self.settings["journal"] = {"enabled": True, "dir": self.out}
        self.write_settings()

    def archive_reliance(self):
        with open(os.path.join(FIX, "nse-entry", "RELIANCE-2026-11-13-1m.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        for day, rows in market.split_by_day("RELIANCE", "1m", doc).items():
            market.write_day(self.d, "RELIANCE", "1m", day, rows, "test")

    def test_the_end_of_day_writes_the_journal(self):
        self.archive_reliance()
        self.replay()
        st = self.state()
        self.assertIn("1 days in %s" % self.out, st["phases"]["eod"]["journal"])
        for name in ("README.md", "Summary.md", "P&L ledger.csv", "Daily P&L.csv", "My notes.md",
                     os.path.join("days", DAY + ".md")):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
        with open(os.path.join(self.out, "P&L ledger.csv"), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "RELIANCE")
        self.assertTrue(rows[0]["why"])
        paper = self.doc("state", "paper.json")["trades"][0]
        self.assertAlmostEqual(float(rows[0]["net"]), paper["pnl_net"])
        with open(os.path.join(self.out, "days", DAY + ".md"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("RELIANCE", page)
        self.assertIn("## The strategy", page)
        self.assertIn("**Why it was bought: the autopilot's steps**", page)
        self.assertIn("**10:05, the signal:** a 5-minute bar closed at ₹1,334, above the opening-range high", page)
        self.assertIn("On the daily chart it was also a 20-day breakout.", page)
        self.assertIn("**The size (nse-budget):** 37 shares, ₹49,358, set by the max loss a trade", page)
        self.assertIn("**The risk check (nse-risk):** PASS.", page)
        self.assertIn("**Why it made or lost money:**", page)
        self.assertNotIn("the cost filter (round-trip", page)          # no cost filter in this config

    def test_every_paper_account_gets_its_trades_and_reasons(self):
        self.archive_reliance()
        self.settings["accounts"] = {"enabled": True, "caps": [3, 5], "filters": {"cap5": {"min_open_rvol": 1.5}}}
        self.write_settings()
        self.set_cfg(broker="paper", max_trades_per_day=5)         # the main account: 5 a day with the filter
        with mock.patch("run_day.Day.open_rvol", return_value=2.4):
            self.replay()
        with open(os.path.join(self.out, "days", DAY + ".md"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("# %s: main account (5 a day + volume filter) +₹328.40 net" % DAY, page)
        self.assertIn("| 5 trades a day, opening volume 1.5x or more only (main account) | 1 |", page)
        self.assertIn("## 5 trades a day, opening volume 1.5x or more only (main account): +₹328.40 net", page)
        self.assertIn("## 3 trades a day: +₹328.40 net", page)
        self.assertIn("#### 1. RELIANCE: WIN, +₹328.40", page)
        self.assertEqual(page.count("**Why it made or lost money:**"), 1)    # the 3-a-day account's: told once
        self.assertIn("Same signal, order, fill and exit as in the 5 a day + volume filter account above, with 37 "
                      "shares", page)
        self.assertIn("opening volume 2.40x its 20-day median, at least the 1.5x this account needs", page)
        self.assertIn("| 5 trades a day, opening volume 1.5x or more only (main account) | 5 | ₹1,500 | opening "
                      "volume 1.5x or more |", page)
        self.assertIn("### What the opening-volume filter kept out", page)
        self.assertIn("It kept out none of today's signals.", page)
        with open(os.path.join(self.out, "P&L ledger.csv"), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([(r["account"], r["main"], r["symbol"]) for r in rows],
                         [("5 a day + volume filter", "yes", "RELIANCE"), ("3 a day", "no", "RELIANCE")])
        with open(os.path.join(self.out, "Daily P&L.csv"), encoding="utf-8") as fh:
            daily = list(csv.DictReader(fh))
        self.assertEqual([(r["account"], r["net"], r["running_net"]) for r in daily],
                         [("5 a day + volume filter", "328.4", "328.4"), ("3 a day", "328.4", "328.4")])
        with open(os.path.join(self.out, "Summary.md"), encoding="utf-8") as fh:
            summ = fh.read()
        self.assertIn("| 5 trades a day, opening volume 1.5x or more only (main account now) | 1 | 1 | 1 | 0 |", summ)
        self.assertIn("| Date | Nifty | 5 a day + volume filter | 3 a day | The main account's trades |", summ)
        self.assertIn("| **+₹328.40** | +₹328.40 | RELIANCE Target hit |", summ)

    def test_what_the_main_accounts_filter_kept_out_is_replayed(self):
        self.archive_reliance()
        self.settings["accounts"] = {"enabled": False, "filters": {"cap3": {"min_open_rvol": 1.5}}}
        self.write_settings()
        self.set_cfg(broker="paper")
        with mock.patch("run_day.Day.open_rvol", return_value=1.11):
            self.replay()
        with open(os.path.join(self.out, "days", DAY + ".md"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("No paper orders today.", page)
        self.assertIn("It kept out 1 signal. Replayed with the same rules, 1 of 1 made money", page)
        self.assertIn("| RELIANCE | 10:05 | 1.11x | target |", page)
        with open(os.path.join(self.out, "Summary.md"), encoding="utf-8") as fh:
            self.assertIn("| Kept out by the opening-volume filter | +", fh.read())

    def test_pdfs_at_the_top_of_the_folder_printed_only_when_a_page_changed(self):
        self.archive_reliance()
        self.settings["journal"]["pdf"] = True
        self.write_settings()
        printed = []

        def fake_render(home, html_path, pdf_path):
            self.assertTrue(html_path.endswith(".html"))                 # WebKit opens only an .html name
            with open(html_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<!doctype html>", page)
            printed.append(os.path.basename(pdf_path))
            with open(pdf_path, "wb") as fh:
                fh.write(b"%PDF-1.4 test")
        with mock.patch("pdf.render", side_effect=fake_render):
            self.replay()
            self.assertEqual(sorted(printed), [DAY + ".pdf", "Summary.pdf"])
            for name in (DAY + ".pdf", "Summary.pdf"):
                self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
            with open(os.path.join(self.out, ".pdf", "Summary.html"), encoding="utf-8") as fh:
                self.assertIn('href="%s.pdf"' % DAY, fh.read())              # the summary links to the PDFs
            del printed[:]
            with mock.patch.object(journal.nc, "utc_now", return_value=journal.nc.utc_now()):
                journal.write_all(self.d, ac.load_settings(self.d), ac.load_cfg(self.d))
            self.assertNotIn(DAY + ".pdf", printed)                         # the day page did not change

    def test_a_pdf_failure_is_reported_and_stops_the_rest(self):
        self.replay()
        self.settings["journal"]["pdf"] = True
        self.write_settings()
        import pdf
        with mock.patch("pdf.render", side_effect=pdf.PdfError("no session")) as r:
            folder, n, note = journal.write_all(self.d, ac.load_settings(self.d), ac.load_cfg(self.d))
        self.assertEqual(r.call_count, 1)
        self.assertIn("PDF: %s.pdf: no session" % DAY, note)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "days", DAY + ".md")))   # the text pages are there

    def test_my_notes_is_never_overwritten(self):
        self.replay()
        notes = os.path.join(self.out, "My notes.md")
        with open(notes, "w", encoding="utf-8") as fh:
            fh.write("mine")
        journal.write_all(self.d, ac.load_settings(self.d), ac.load_cfg(self.d))
        with open(notes, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "mine")

    def test_an_unwritable_folder_falls_back_to_the_trader_folder(self):
        self.replay()
        real = journal._write
        calls = []

        def flaky(folder, days, note, *rest):
            calls.append(folder)
            if len(calls) == 1:
                raise PermissionError("Operation not permitted")
            return real(folder, days, note, *rest)
        with mock.patch.object(journal, "_write", side_effect=flaky):
            folder, n, note = journal.write_all(self.d, ac.load_settings(self.d), ac.load_cfg(self.d))
        self.assertEqual(folder, os.path.join(self.d, "journal"))
        self.assertIn("PermissionError", note)

    def test_a_journal_failure_never_fails_the_day(self):
        with mock.patch.object(journal, "write_all", side_effect=RuntimeError("boom")):
            self.replay()
        eod = self.state()["phases"]["eod"]
        self.assertTrue(eod.get("done"))
        self.assertIn("failed: RuntimeError: boom", eod["journal"])

    def test_off_by_default(self):
        self.settings.pop("journal")
        self.write_settings()
        self.replay()
        self.assertEqual(self.state()["phases"]["eod"]["journal"], "off")
        self.assertEqual(os.listdir(self.out), [])


if __name__ == "__main__":
    unittest.main()
