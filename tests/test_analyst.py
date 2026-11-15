import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

res = load("nse-analyst", "results")
nc = res.nc
DIR = os.path.join(FIX, "nse-analyst")
CAL = os.path.join(FIX, "nse-news", "event-calendar.json")
NOW = nc.parse_now("2026-11-14 21:30")


def xbrl(sym, qe, basis="consolidated"):
    with gzip.open(os.path.join(DIR, "xbrl-%s-%s-%s.xml.gz" % (sym, qe, basis))) as fh:
        return res.parse_xbrl(fh.read())


def report(sym, quarters=5, basis="consolidated", notes=None):
    return res.report(res.Fetcher(offline=DIR), sym, quarters, basis, NOW, notes)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = res.main(args)
    return code, out.getvalue(), err.getvalue()


class RelianceXbrl(unittest.TestCase):
    def test_sep_2026_quarter(self):
        x = xbrl("RELIANCE", "30-SEP-2026")
        self.assertFalse(x["bank"])
        self.assertEqual((x["start"], x["end"], x["ytd_start"]), ("2026-07-01", "2026-09-30", "2026-04-01"))
        q = x["q"]
        self.assertEqual(nc.crore(q["revenue"]), "₹3,32,861 cr")
        self.assertEqual(nc.crore(q["profit_owners"]), "₹25,548 cr")
        self.assertEqual(q["eps_diluted"], 18.88)
        self.assertEqual(nc.crore(q["total_income"]), "₹3,38,654 cr")
        self.assertEqual(nc.crore(q["expenses"]), "₹3,00,041 cr")
        self.assertEqual(nc.crore(q["pbt"]), "₹38,613 cr")
        self.assertEqual(q["exceptional"], 0)
        # FourD is the half year: Apr-Sep
        self.assertEqual(nc.crore(x["ytd"]["revenue"]), "₹6,44,711 cr")
        self.assertEqual(x["meta"]["ReportingQuarter"], "Second quarter")
        self.assertEqual(res.fiscal_label(x["end"], x["meta"]), "Q2 FY27")

    def test_march_filing_quarter_is_jan_to_mar(self):
        x = xbrl("RELIANCE", "31-MAR-2026")
        self.assertEqual((x["start"], x["ytd_start"]), ("2026-01-01", "2025-04-01"))
        self.assertEqual(x["q"]["eps_diluted"], 12.54)
        self.assertEqual(nc.crore(x["ytd"]["profit_owners"]), "₹80,775 cr")  # the full year FY26
        self.assertEqual(res.fiscal_label(x["end"], x["meta"]), "Q4 FY26")

    def test_june_filing_has_no_year_to_date(self):
        x = xbrl("RELIANCE", "30-JUN-2026")
        self.assertIsNone(x["ytd_start"])
        self.assertEqual(x["ytd"], {})
        self.assertEqual(res.fiscal_label(x["end"], x["meta"]), "Q1 FY27")

    def test_not_xml(self):
        with self.assertRaises(res.ResultsError):
            res.parse_xbrl(b"<html>blocked")


class Reliance(unittest.TestCase):
    def setUp(self):
        self.r = report("RELIANCE")

    def test_latest_quarter_and_margins(self):
        q = self.r["rows"][0]
        self.assertEqual((q["end"], q["quarter"], q["source"], q["basis"]),
                         ("2026-09-30", "Q2 FY27", "NSE XBRL", "consolidated"))
        self.assertEqual(q["top_line"], 3328610000000)
        self.assertEqual(q["net_profit"], 255480000000)
        self.assertEqual(q["eps_diluted"], 18.88)
        # EBITDA = 38,613 + 8,481 + 15,315 - 5,793 = 56,616 crore (derived)
        self.assertEqual(nc.crore(q["ebitda"]), "₹56,616 cr")
        self.assertEqual((q["ebitda_margin"], q["pbt_margin"], q["net_margin"]), (17.0, 11.6, 7.7))
        self.assertEqual(q["ebitda_margin_qoq"], 1.8)

    def test_yoy_from_the_year_earlier_filing(self):
        q = self.r["rows"][0]
        self.assertEqual(q["yoy_vs"], "2025-09-30")
        self.assertEqual(q["yoy_source"], "NSE XBRL")
        self.assertFalse(q["yoy_mixed"])
        self.assertEqual(q["top_line_yoy"], 28.6)      # vs 2,58,898 cr
        self.assertEqual(q["net_profit_yoy"], 40.6)    # vs 18,165 cr
        self.assertEqual(q["eps_yoy"], 40.7)           # vs 13.42
        self.assertEqual(q["top_line_qoq"], 6.7)       # vs 3,11,850 cr
        self.assertIsNone(self.r["rows"][1]["top_line_yoy"])  # Jun 2025 not read with --quarters 5

    def test_last_four_quarters(self):
        t = self.r["ttm"]
        self.assertEqual((t["from"], t["to"]), ("2025-12-31", "2026-09-30"))
        self.assertEqual(nc.crore(t["top_line"]), "₹12,12,828 cr")
        self.assertEqual(nc.crore(t["net_profit"]), "₹82,110 cr")
        self.assertEqual(t["eps_diluted"], 60.68)

    def test_quarters_and_labels(self):
        self.assertEqual([r["quarter"] for r in self.r["rows"]],
                         ["Q2 FY27", "Q1 FY27", "Q4 FY26", "Q3 FY26", "Q2 FY26"])
        self.assertEqual(self.r["name"], "Reliance Industries Limited")
        self.assertEqual(self.r["latest"]["qe"], "30-SEP-2026")

    def test_what_changed_in_words(self):
        lines = res.changes(self.r)
        self.assertEqual(lines[0], "Quarter ending 2026-09-30 (Q2 FY27): revenue from operations ₹3,32,861 cr "
                                   "(+28.6% vs a year earlier), net profit to owners ₹25,548 cr (+40.6%), "
                                   "diluted EPS ₹18.88 (+40.7%).")
        self.assertIn("EBITDA margin (derived) 17.0% (+1.8 points vs the quarter before).", lines)

    def test_one_offs_are_flagged(self):
        row = res.build_row("2026-09-30", "2026-07-01", {"revenue": 1000, "pbt": 100, "other_income": 40,
                                                         "exceptional": -20, "profit": 70, "eps_diluted": 1.0},
                            "NSE XBRL", False, "standalone", "Q2 FY27")
        r = res.analyse([row])
        lines = res.changes({"rows": r["rows"], "ttm": {}})
        self.assertTrue(any("Exceptional items" in l for l in lines))
        self.assertTrue(any("Other income is 40% of PBT" in l for l in lines))


class Filings(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DIR, "integrated-filing-RELIANCE.json")) as fh:
            self.f = res.parse_filings(json.load(fh))

    def test_one_filing_per_quarter(self):
        self.assertEqual(len(self.f), 14)
        picked, basis = res.pick_filings(self.f, "consolidated", 5)
        self.assertEqual(basis, "consolidated")
        self.assertEqual([p["qe"] for p in picked],
                         ["30-SEP-2026", "30-JUN-2026", "31-MAR-2026", "31-DEC-2025", "30-SEP-2025"])
        self.assertTrue(picked[0]["xbrl"].endswith("INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml"))
        sa, _ = res.pick_filings(self.f, "standalone", 1)
        self.assertTrue(sa[0]["xbrl"].endswith("_1750535_30102026093155_WEB.xml"))

    def test_a_revision_wins_and_standalone_only_companies(self):
        rev = dict(self.f[0], broadcast="02-Nov-2026 10:00:00", xbrl="revised.xml", revision="Revised")
        picked, _ = res.pick_filings(self.f + [rev], "consolidated", 1)
        self.assertEqual(picked[0]["xbrl"], "revised.xml")
        only = [f for f in self.f if f["basis"] == "standalone"]
        picked, basis = res.pick_filings(only, "consolidated", 2)
        self.assertEqual(basis, "standalone")
        self.assertEqual(len(picked), 2)


class Bank(unittest.TestCase):
    def test_bank_tags(self):
        x = xbrl("HDFCBANK", "30-SEP-2026")
        self.assertTrue(x["bank"])
        q = x["q"]
        self.assertEqual(nc.crore(q["interest_earned"]), "₹95,132 cr")
        self.assertEqual(nc.crore(q["total_income"]), "₹1,29,359 cr")
        self.assertEqual(nc.crore(q["profit_owners"]), "₹20,700 cr")
        self.assertEqual(q["eps_diluted"], 13.41)
        # a consolidated bank filing carries 0 for NPA: not reported there, so blank
        self.assertIsNone(q["gross_npa_pct"])
        s = xbrl("HDFCBANK", "30-SEP-2026", "standalone")["q"]
        self.assertEqual((s["gross_npa_pct"], s["net_npa_pct"], s["cet1_pct"]), (1.11, 0.37, 18.87))
        self.assertEqual(s["eps_diluted"], 12.63)

    def test_bank_growth(self):
        r = report("HDFCBANK")
        self.assertTrue(r["bank"])
        q = r["rows"][0]
        self.assertEqual(nc.crore(q["nii"]), "₹44,464 cr")  # 95,132 - 50,667
        self.assertEqual((q["nii_yoy"], q["net_profit_yoy"], q["eps_yoy"], q["top_line_yoy"]),
                         (10.5, 5.6, 5.5, 9.1))
        self.assertIsNone(q["ebitda"])
        self.assertEqual(r["ttm"]["eps_diluted"], 51.88)
        lines = res.changes(r)
        self.assertIn("NPA ratios are not in the consolidated filing: run with --standalone for them.", lines)
        self.assertTrue(any(l.startswith("Exceptional items of -₹837 cr") for l in lines))

    def test_bank_cli(self):
        code, out, err = run(["HDFCBANK", "--standalone", "--quarters", "1", "--offline", DIR, "--no-calendar",
                              "--now", "2026-11-14 21:30"])
        self.assertEqual(code, 0, err)
        self.assertIn("HDFC Bank Limited (HDFCBANK). Standalone", out)
        self.assertIn("Asset quality: gross NPA 1.11%, net NPA 0.37%, provisions ₹3,574 cr.", out)
        self.assertNotIn("EBITDA", out)


class Fallback(unittest.TestCase):
    def test_whole_company_from_yahoo_when_nse_fails(self):
        notes = []
        r = report("TCS", notes=notes)  # no NSE list for TCS in the fixtures
        self.assertEqual(r["sources"], ["Yahoo"])
        self.assertTrue(any("numbers below come from Yahoo Finance" in n for n in notes))
        self.assertEqual([x["end"] for x in r["rows"]],
                         ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-06-30"])  # Yahoo skips Sep 2025
        q1 = r["rows"][1]
        self.assertEqual((q1["top_line_yoy"], q1["eps_yoy"]), (13.9, 4.6))
        self.assertIsNone(r["rows"][0]["top_line_yoy"])
        code, out, err = run(["TCS", "--offline", DIR, "--calendar", CAL, "--now", "2026-11-14 21:30"])
        self.assertEqual(code, 0, err)
        self.assertIn("Source: Yahoo Finance (fallback).", out)
        self.assertIn("38.37  yahoo", out)
        self.assertIn("revenue (Yahoo's total revenue)", out)

    def test_one_quarter_from_yahoo_is_marked(self):
        notes = []
        r = report("RELIANCE", quarters=7, notes=notes)
        by = {x["end"]: x for x in r["rows"]}
        self.assertEqual(by["2025-06-30"]["source"], "Yahoo")
        self.assertNotIn("2025-03-31", by)  # neither NSE (offline) nor Yahoo has it
        self.assertTrue(by["2026-06-30"]["yoy_mixed"])
        self.assertTrue(any("2025-06-30: XBRL unreadable, Yahoo's numbers used" in n for n in notes))
        out = res.render(r, NOW, None, notes)
        self.assertIn("+28.0%*", out)
        self.assertIn("* compared with a row from the other source", out)

    def test_yahoo_parse(self):
        with open(os.path.join(DIR, "yahoo-timeseries-RELIANCE.json")) as fh:
            y = res.parse_yahoo(json.load(fh))
        self.assertEqual(y["2026-09-30"]["profit_owners"], 255480000000)  # Yahoo matches NSE here
        self.assertEqual(y["2026-09-30"]["eps_diluted"], 18.88)
        self.assertNotIn("2025-09-30", y)


class CalendarAndDeadlines(unittest.TestCase):
    def test_next_board_meeting(self):
        with open(CAL, "rb") as fh:
            events = res.parse_calendar(json.load(fh))
        m = res.next_meeting(events, "SANDHAR", NOW)
        self.assertEqual((m["date"], m["days"], m["results"], m["this_week"]), ("2026-11-16", 2, True, True))
        self.assertIsNone(res.next_meeting(events, "RELIANCE", NOW))
        late = res.next_meeting(events, "LATTEYS", NOW)
        self.assertEqual((late["date"], late["this_week"]), ("2026-12-05", False))
        r = {"symbol": "SANDHAR", "name": None, "basis": "consolidated", "bank": False, "rows": [], "ttm": {},
             "latest": None, "sources": []}
        self.assertIn("Next board meeting: Mon 16 Nov 2026 in 2 days (Financial Results), reports results",
                      res.render(r, NOW, m))
        self.assertIn("It reports within the next 7 days.", res.render(r, NOW, m))

    def test_lodr_due_dates(self):
        self.assertEqual(res.results_due("2026-09-30"), ("2026-12-31", "2027-02-14"))
        self.assertEqual(res.results_due("2026-12-31"), ("2027-03-31", "2027-05-30"))
        self.assertEqual(res.results_due("2027-03-31"), ("2027-06-30", "2027-08-14"))
        r = report("RELIANCE")
        self.assertIsNone(res.overdue_note(r, NOW))
        note = res.overdue_note(r, nc.parse_now("2027-02-20 10:00"))
        self.assertIn("quarter ending 2026-12-31 were due by 2027-02-14", note)

    def test_cli_calendar_line(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--calendar", CAL, "--now", "2026-11-14 21:30"])
        self.assertEqual(code, 0, err)
        self.assertIn("Next board meeting: none for RELIANCE on NSE's event calendar", out)
        self.assertIn("Results for the quarter ending 2026-12-31 are due by 2027-02-14", out)


class Cli(unittest.TestCase):
    def test_metrics_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["RELIANCE", "TCS", "--offline", DIR, "--no-calendar", "--metrics", path,
                                  "--now", "2026-11-14 21:30"])
            self.assertEqual(code, 0, err)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        rows = {r["symbol"]: r for r in data["companies"]}
        self.assertEqual(sorted(rows), ["RELIANCE", "TCS"])
        self.assertEqual(data["currency"], "INR")
        self.assertEqual(rows["RELIANCE"]["eps_ttm"], 60.68)
        self.assertEqual(rows["RELIANCE"]["revenue_growth_yoy"], 28.6)
        self.assertEqual(rows["RELIANCE"]["ebitda_margin"], 17.0)
        self.assertEqual(rows["TCS"]["source"], "Yahoo")
        self.assertIn("wrote", out)
        self.assertNotIn("$", out)
        self.assertNotIn(chr(0x2014), out)

    def test_json_output(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--json", "--quarters", "4", "--no-calendar"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data[0]["result"]["rows"]), 4)
        self.assertEqual(data[0]["result"]["rows"][0]["values"]["revenue"], 3328610000000)
        self.assertEqual(data[0]["results_due"], ["2026-12-31", "2027-02-14"])

    def test_text_report(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--no-calendar", "--now", "2026-11-14 21:30"])
        self.assertEqual(code, 0, err)
        self.assertIn("Reliance Industries Limited (RELIANCE). Consolidated, ₹ crore except EPS", out)
        self.assertIn("Q2 FY27  2026-09-30   3,32,861   3,38,654   3,00,041    56,616    38,613     25,548   18.88",
                      out)
        self.assertIn("Last 4 quarters (2025-12-31 to 2026-09-30): revenue ₹12,12,828 cr, net profit to owners "
                      "₹82,110 cr, diluted EPS ₹60.68", out)

    def test_unknown_symbol_and_no_user_agent(self):
        code, _, err = run(["ZZZZ", "--offline", DIR, "--no-calendar"])
        self.assertEqual(code, 2)
        self.assertIn("results: ZZZZ:", err)
        self.assertNotIn("Traceback", err)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            res.main(["--help"])
        self.assertNotIn("user-agent", out.getvalue().lower())
        code, _, _ = run([])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
