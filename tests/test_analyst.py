import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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


# ---------------------------------------------------------------- a block page is never a filing

HTML = b"<!DOCTYPE html><html><head><title>Access Denied</title></head><body>Reference #18.4a</body></html>"


def good_xbrl():
    with gzip.open(os.path.join(DIR, "xbrl-RELIANCE-30-SEP-2026-consolidated.xml.gz")) as fh:
        return fh.read()


class Scripted(res.Fetcher):
    """A Fetcher whose network answers come from a list (bytes, or an Exception to raise)."""

    def __init__(self, cache_dir, answers):
        super(Scripted, self).__init__(offline=None, pause=0, cache_dir=cache_dir)
        self.answers, self.calls = list(answers), 0

    def get(self, url, name, nse=False):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


URL = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml"


class XbrlCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache", "nse-xbrl")
        self.file = os.path.join(self.cache, os.path.basename(URL) + ".gz")

    def test_check_xbrl(self):
        self.assertIsNone(res.check_xbrl(good_xbrl()))
        self.assertEqual(res.check_xbrl(b""), "empty")
        self.assertEqual(res.check_xbrl(b"  \n"), "empty")
        self.assertIn("HTML page", res.check_xbrl(HTML))
        self.assertIn("HTML page", res.check_xbrl(b"\n  <HTML><body>x</body></HTML>"))
        self.assertIn("not valid XML", res.check_xbrl(b"<xbrl><context>"))
        # well-formed XML that is an error page, not a filing
        self.assertIn("root is <Error>", res.check_xbrl(b"<Error><Code>AccessDenied</Code></Error>"))
        self.assertIn("no reporting period", res.check_xbrl(b"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance'/>"))

    def test_a_block_page_is_not_cached_and_a_later_good_response_works(self):
        f = Scripted(self.cache, [HTML, good_xbrl()])
        with self.assertRaises(res.ResultsError) as cm:
            f.xbrl(URL, "x.xml")
        self.assertIn("an HTML page, not XBRL", str(cm.exception))
        self.assertIn("not cached", str(cm.exception))
        self.assertFalse(os.path.exists(self.cache) and os.listdir(self.cache))  # nothing on disk
        data = f.xbrl(URL, "x.xml")  # NSE answers properly this time
        self.assertIsNone(res.check_xbrl(data))
        self.assertEqual(os.listdir(self.cache), [os.path.basename(URL) + ".gz"])  # no .tmp left behind
        # and from now on the cache answers: no request
        again = Scripted(self.cache, [])
        self.assertEqual(again.xbrl(URL, "x.xml"), data)
        self.assertEqual(again.calls, 0)

    def test_empty_and_error_bodies_are_not_cached_either(self):
        for body in (b"", b"<Error><Code>AccessDenied</Code></Error>", b"Service Unavailable"):
            f = Scripted(self.cache, [body])
            with self.assertRaises(res.ResultsError):
                f.xbrl(URL, "x.xml")
        self.assertFalse(os.path.exists(self.cache) and os.listdir(self.cache))

    def test_a_cached_block_page_is_deleted_and_fetched_again(self):
        os.makedirs(self.cache)
        with gzip.open(self.file, "wb") as fh:  # what the old code wrote: any body starting with "<"
            fh.write(HTML)
        f = Scripted(self.cache, [good_xbrl()])
        data = f.xbrl(URL, "x.xml")
        self.assertEqual(f.calls, 1)
        self.assertIsNone(res.check_xbrl(data))
        with gzip.open(self.file) as fh:
            self.assertIsNone(res.check_xbrl(fh.read()))  # the file on disk is the good one now

    def test_a_cached_file_that_is_cut_off_or_corrupt_is_deleted_and_fetched_again(self):
        os.makedirs(self.cache)
        with open(self.file, "wb") as fh:
            fh.write(b"\x1f\x8b\x08\x00not really gzip at all")
        f = Scripted(self.cache, [good_xbrl()])
        self.assertIsNone(res.check_xbrl(f.xbrl(URL, "x.xml")))
        self.assertEqual(f.calls, 1)

    def test_a_cached_block_page_with_nse_still_blocking_leaves_nothing_behind(self):
        os.makedirs(self.cache)
        with gzip.open(self.file, "wb") as fh:
            fh.write(HTML)
        f = Scripted(self.cache, [HTML])
        with self.assertRaises(res.ResultsError):
            f.xbrl(URL, "x.xml")
        self.assertFalse(os.path.exists(self.file))  # the poisoned file is gone, not served forever

    def test_default_cache_is_in_the_trader_home(self):
        with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.tmp.name}):
            f = res.Fetcher()
        self.assertEqual(f.cache_dir, os.path.join(self.tmp.name, "cache", "nse-xbrl"))
        self.assertIsNone(res.Fetcher(cache_dir=None).cache_dir)


class WhenTheFeedIsWrong(unittest.TestCase):
    def test_the_newest_quarter_unreadable_says_the_table_starts_older(self):
        class Newest(res.Fetcher):
            def xbrl(self, url, name):
                if "30102026093254" in url:  # the Sep 2026 consolidated filing: NSE sends a block page
                    raise res.ResultsError("INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml is an HTML "
                                           "page, not XBRL; not cached")
                return super(Newest, self).xbrl(url, name)

        notes = []
        r = res.report(Newest(offline=DIR), "RELIANCE", 3, "consolidated", NOW, notes)
        self.assertEqual(r["rows"][0]["end"], "2026-09-30")  # Yahoo has that quarter (fixture): marked as such
        self.assertEqual(r["rows"][0]["source"], "Yahoo")
        self.assertTrue(any("2026-09-30: XBRL unreadable, Yahoo's numbers used" in n for n in notes))

        class NoYahoo(Newest):
            def get(self, url, name, nse=False):
                if "yahoo" in url:
                    raise res.ResultsError("could not reach yahoo")
                return super(NoYahoo, self).get(url, name, nse)

        notes = []
        r = res.report(NoYahoo(offline=DIR), "RELIANCE", 3, "consolidated", NOW, notes)
        self.assertEqual(r["rows"][0]["end"], "2026-06-30")
        self.assertTrue(any("could not read the XBRL" in n and "30-SEP-2026" in n for n in notes), notes)
        loud = [n for n in notes if "newest filing on NSE (quarter ending 2026-09-30) could not be read" in n]
        self.assertEqual(len(loud), 1, notes)
        self.assertIn("start at the quarter ending 2026-06-30, not at the latest results", loud[0])
        out = res.render(r, NOW, None, notes)
        self.assertIn("NOTE: RELIANCE: the newest filing on NSE", out)

    def test_an_xbrl_with_none_of_our_numbers_is_not_a_row_of_dashes(self):
        odd = (b"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance' "
               b"xmlns:in-capmkt='http://www.sebi.gov.in/xbrl/2017'>"
               b"<xbrli:context id='OneD'><xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate>"
               b"<xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>"
               b"<in-capmkt:SomethingNew contextRef='OneD'>5</in-capmkt:SomethingNew></xbrli:xbrl>")

        class Odd(res.Fetcher):
            def xbrl(self, url, name):
                return odd

            def get(self, url, name, nse=False):
                if "yahoo" in url:
                    raise res.ResultsError("could not reach yahoo")
                return super(Odd, self).get(url, name, nse)

        notes = []
        with self.assertRaises(res.ResultsError):  # nothing readable, and Yahoo has nothing either
            res.report(Odd(offline=DIR), "RELIANCE", 1, "consolidated", NOW, notes)
        self.assertTrue(any("the XBRL has none of the expected numbers" in n for n in notes), notes)

    def test_nse_filing_list_in_the_wrong_shape_is_not_no_filings(self):
        with self.assertRaises(res.ResultsError):
            res.parse_filings({"error": "Resource not found"})
        with self.assertRaises(res.ResultsError):
            res.parse_filings({"data": "nope"})
        self.assertEqual(res.parse_filings({"data": []}), [])  # a company with none: Yahoo takes over
        with self.assertRaises(res.ResultsError):
            res.parse_filings({"data": [{"XBRL": "x.xml", "qe": "30-SEP-2026"}]})  # a renamed field

    def test_a_filing_list_error_falls_back_to_yahoo_with_the_reason(self):
        class Blocked(res.Fetcher):
            def get(self, url, name, nse=False):
                if "integrated-filing" in url or name.startswith("integrated-filing"):
                    return b'{"error": "Resource not found"}'
                return super(Blocked, self).get(url, name, nse)

        notes = []
        r = res.report(Blocked(offline=DIR), "RELIANCE", 3, "consolidated", NOW, notes)
        self.assertEqual(r["sources"], ["Yahoo"])
        self.assertTrue(any("NSE filings list: NSE's filing list came back in an unexpected shape" in n
                            for n in notes), notes)

    def test_event_calendar_that_is_empty_or_unreadable_is_a_note_never_no_meeting(self):
        for body in ({"data": []}, [], {"message": "nope"}, [{"symbol": "TCS", "date": "2026/11/16"}]):
            with self.assertRaises(res.ResultsError):
                res.parse_calendar(body)
        for body, word in ((b'{"data": []}', "empty"), (b'{"message": "nope"}', "unexpected shape"),
                           (b'<html>blocked</html>', "not JSON")):
            with tempfile.TemporaryDirectory() as tmp:
                cal = os.path.join(tmp, "cal.json")
                with open(cal, "wb") as fh:
                    fh.write(body)
                code, out, err = run(["RELIANCE", "--offline", DIR, "--calendar", cal, "--now", "2026-11-14 21:30"])
            self.assertEqual(code, 0, err)
            self.assertIn("NOTE: NSE event calendar: ", out)
            self.assertIn(word, out)
            self.assertNotIn("Next board meeting: none", out)  # an unread calendar says nothing about meetings

    def test_metrics_file_that_is_not_an_object_is_refused_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "metrics.json")
            with open(path, "w") as fh:
                fh.write("[1, 2]")
            code, _, err = run(["RELIANCE", "--offline", DIR, "--no-calendar", "--metrics", path,
                                "--now", "2026-11-14 21:30"])
        self.assertEqual(code, 2)
        self.assertIn("not a metrics file", err)
        self.assertNotIn("Traceback", err)

    def test_exit_codes_one_symbol_failing_and_all_failing(self):
        code, out, err = run(["RELIANCE", "ZZZZ", "--offline", DIR, "--no-calendar", "--now", "2026-11-14 21:30"])
        self.assertEqual(code, 1)  # some symbols failed
        self.assertIn("results: ZZZZ:", err)
        self.assertIn("Reliance Industries", out)
        code, _, _ = run(["ZZZZ", "YYYY", "--offline", DIR, "--no-calendar"])
        self.assertEqual(code, 2)  # none read


if __name__ == "__main__":
    unittest.main()
