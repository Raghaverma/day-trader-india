"""nse-news/dossier.py: everything public on one stock, offline. The NSE answers are trimmed copies of the
shapes NSE's /api/ sends; the year of daily bars is made up so every number can be checked by hand."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX

dossier = load("nse-news", "dossier")
nc = dossier.nc
TODAY = datetime.date(2026, 11, 13)
NOW = "2026-11-13 08:45"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def epoch(d):
    return int(datetime.datetime(d.year, d.month, d.day, 4, 0, tzinfo=datetime.timezone.utc).timestamp())


def chart(n=260, last_close=1400.0, last_volume=3000000, step=1.0):
    """n daily bars ending the day before TODAY, closes rising by `step` a day to `last_close`."""
    days = [TODAY - datetime.timedelta(days=n - i) for i in range(n)]
    closes = [last_close - step * (n - 1 - i) for i in range(n)]
    return {"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [epoch(d) for d in days],
                                  "indicators": {"quote": [{"close": closes, "open": closes,
                                                            "high": [c * 1.01 for c in closes],
                                                            "low": [c * 0.99 for c in closes],
                                                            "volume": [1000000] * (n - 1) + [last_volume]}]}}],
                      "error": None}}


BM = [{"bm_symbol": "RELIANCE", "bm_date": "13-Nov-2026", "bm_purpose": "Board Meeting Intimation",
       "bm_desc": "Board meeting to consider results", "sm_name": "Reliance Industries Limited", "sm_indusrty": "Refineries"},
      {"bm_symbol": "RELIANCE", "bm_date": "13-Nov-2026", "bm_purpose": "Financial Results",
       "bm_desc": "To consider the quarterly results", "sm_name": "Reliance Industries Limited", "sm_indusrty": "Refineries"},
      {"bm_symbol": "RELIANCE", "bm_date": "17-Jul-2026", "bm_purpose": "Financial Results", "bm_desc": "",
       "sm_name": "Reliance Industries Limited", "sm_indusrty": "Refineries"}]
CA = [{"exDate": "20-Nov-2026", "recDate": "20-Nov-2026", "subject": "Dividend - Rs 6 Per Share",
       "comp": "Reliance Industries Limited"},
      {"exDate": "14-Aug-2025", "recDate": "14-Aug-2025", "subject": "Dividend - Rs 5.5 Per Share",
       "comp": "Reliance Industries Limited"}]
SHP = [{"date": "30-SEP-2026", "pr_and_prgrp": "48.90", "public_val": "51.10", "name": "Reliance Industries Limited",
        "broadcastDate": "21-OCT-2026 16:25:15"},
       {"date": "30-JUN-2026", "pr_and_prgrp": "50.40", "public_val": "49.60", "name": "Reliance Industries Limited"},
       {"date": "30-JUN-2026", "pr_and_prgrp": "50.40", "public_val": "49.60", "name": "Reliance Industries Limited"}]
PLEDGE = {"data": [{"shp": "30-Sep-2026", "percPromoterShares": "  7.50", "percTotShares": " 3.67",
                    "percSharesPledged": "1.54", "percPromoterHolding": "48.90"}]}
PIT = {"data": [
    {"acqName": "A Director", "personCategory": "Director", "tdpTransactionType": "Sell", "acqMode": "Market Sale",
     "secAcq": "10000", "secVal": "14000000", "secType": "Equity Shares", "acqfromDt": "01-Nov-2026",
     "date": "03-Nov-2026 18:00"},
    {"acqName": "A Relative", "personCategory": "Immediate relative", "tdpTransactionType": "Sell",
     "acqMode": "Off Market", "secAcq": "5000", "secVal": "7000000", "secType": "Equity Shares",
     "acqfromDt": "02-Nov-2026", "date": "03-Nov-2026 18:00"},
    {"acqName": "Someone Else", "personCategory": "Other", "tdpTransactionType": "Buy", "acqMode": "Market Purchase",
     "secAcq": "100", "secVal": "140000", "secType": "Equity Shares", "acqfromDt": "05-Nov-2026",
     "date": "06-Nov-2026 18:00"},
    {"acqName": "Old Trade", "personCategory": "Promoters", "tdpTransactionType": "Buy", "acqMode": "Market Purchase",
     "secAcq": "100", "secVal": "99000000", "secType": "Equity Shares", "acqfromDt": "01-Jan-2026",
     "date": "02-Jan-2026 18:00"}]}
BAN = "Securities in Ban For Trade Date 13-NOV-2026:\n1,RELIANCE\n2,KAYNES\n"
BULK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
        "12-Nov-2026,RELIANCE,Reliance Industries,BIG FUND LTD,BUY,2000000,1390.50,-\n"
        "12-Nov-2026,TCS,Tata Consultancy,OTHER FUND,SELL,10,3000,-\n")
BLOCK = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"


def sec_list(extra=()):
    rows = ["Symbol,Series,Security Name,Band,Remarks", "RELIANCE,EQ,RELIANCE INDUSTRIES,No Band,-"]
    rows += ["S%04d,EQ,STOCK %d,20,-" % (i, i) for i in range(600)]
    rows += list(extra)
    return "\n".join(rows) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.off = os.path.join(self.d, "off")
        os.makedirs(self.off)
        files = {"chart1y-RELIANCE.json": json.dumps(chart()), "bm-RELIANCE.json": json.dumps(BM),
                 "ca-RELIANCE.json": json.dumps(CA), "shp-RELIANCE.json": json.dumps(SHP),
                 "pledge-RELIANCE.json": json.dumps(PLEDGE), "pit-RELIANCE.json": json.dumps(PIT),
                 "fo_secban.csv": BAN, "sec_list.csv": sec_list(), "bulk.csv": BULK, "block.csv": BLOCK}
        for name, body in files.items():
            with open(os.path.join(self.off, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        for name in ("nse-announcements-RELIANCE.json", "gnews-RELIANCE.xml"):
            shutil.copy(os.path.join(FIX, "nse-news", name), os.path.join(self.off, name))
        p = mock.patch.dict(os.environ, {"NSE_TEST_MODE": "1", "NSE_TRADER_HOME": self.d})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dossier.main(list(args) + ["--offline", self.off, "--now", NOW, "--no-results", "--pause", "0"])
        return code, out.getvalue(), err.getvalue()

    def build(self, **kw):
        f = dossier.news.Fetcher(offline=self.off, pause=0)
        shared = dossier.Shared(f, None)
        return dossier.build("RELIANCE", f, shared, nc.parse_now(NOW), results=False, **kw)


class Parsers(Base):
    def test_board_meetings_drop_the_intimation_on_the_meetings_own_date(self):
        bm = dossier.parse_board_meetings(json.dumps(BM), "RELIANCE", TODAY)
        self.assertEqual([(e["date"], e["purpose"], e["days"], e["results"]) for e in bm],
                         [("2026-11-13", "Financial Results", 0, True), ("2026-07-17", "Financial Results", -119, True)])
        self.assertEqual(bm[0]["industry"], "Refineries")

    def test_actions_shareholding_and_pledge(self):
        ca = dossier.parse_actions(json.dumps(CA), "RELIANCE", TODAY)
        self.assertEqual((ca[0]["ex_date"], ca[0]["days"]), ("2026-11-20", 7))
        sh = dossier.parse_shareholding(json.dumps(SHP), "RELIANCE")
        self.assertEqual([(s["quarter_end"], s["promoter_pct"]) for s in sh],
                         [("2026-09-30", 48.9), ("2026-06-30", 50.4)])          # the repeated quarter once
        pl = dossier.parse_pledge(json.dumps(PLEDGE), "RELIANCE")
        self.assertEqual((pl["as_of"], pl["promoter_pledged_pct"], pl["all_pledged_pct"]), ("2026-09-30", 7.5, 1.54))
        self.assertIsNone(dossier.parse_pledge(json.dumps({"data": []}), "RELIANCE"))   # no filing is not 0%

    def test_insiders_count_only_insiders_on_the_market_in_the_window(self):
        ins = dossier.parse_insiders(json.dumps(PIT), "RELIANCE", TODAY)
        self.assertEqual(len(ins["trades"]), 3)                          # January is outside 90 days
        self.assertEqual((ins["market_sold"], ins["market_bought"]), (14000000.0, 0.0))  # off-market, "Other" out
        self.assertEqual(ins["latest_filing"], "2026-11-05")

    def test_shared_files(self):
        self.assertEqual(dossier.parse_fo_ban(BAN), ("13-NOV-2026", {"RELIANCE", "KAYNES"}))
        with self.assertRaises(dossier.DossierError):
            dossier.parse_fo_ban("<html>blocked</html>")
        sl = dossier.parse_sec_list(sec_list())
        self.assertEqual(sl["RELIANCE"]["band"], "No Band")
        with self.assertRaises(dossier.DossierError):
            dossier.parse_sec_list("Symbol,Series,Security Name,Band\nA,EQ,A,5\n")    # cut off
        deals = dossier.parse_deal_file(BULK, "bulk")
        self.assertEqual((deals[0]["symbol"], deals[0]["side"], deals[0]["qty"], deals[0]["value"]),
                         ("RELIANCE", "BUY", 2000000.0, 2781000000.0))
        self.assertIn("dynamic", dossier.band_words("No Band"))
        self.assertEqual(dossier.band_words("5"), "circuit band 5%")

    def test_price_facts_by_hand(self):
        p = dossier.price_facts(dossier.parse_daily(json.dumps(chart()), "RELIANCE"))
        self.assertEqual((p["close"], p["change_1d_pct"]), (1400.0, round(100 * (1400 / 1399.0 - 1), 2)))
        self.assertEqual(p["change_1w_pct"], round(100 * (1400 / 1395.0 - 1), 2))
        self.assertEqual(p["high_52w"], round(1400 * 1.01, 2))
        self.assertEqual(p["sma20"], round(sum(1400 - i for i in range(20)) / 20, 2))
        self.assertEqual((p["avg_volume_20d"], p["volume_x"]), (1000000, 3.0))
        self.assertEqual(p["avg_range_20d_pct"], 2.0)


class Build(Base):
    def test_every_source_and_the_flags(self):
        doc = self.build()
        self.assertEqual(doc["failed"], {})
        self.assertEqual(doc["name"], "Reliance Industries Limited")
        fl = doc["flags"]
        self.assertTrue(fl[0].startswith("In NSE's F&O ban list for 13-NOV-2026"))
        self.assertIn("Board meeting TODAY (2026-11-13): Financial Results", fl)
        self.assertIn("Ex-date in 7 days (2026-11-20): Dividend - Rs 6 Per Share", fl)
        self.assertIn("Volume 3.0x its 20-day average in the last session", fl)
        self.assertTrue(any(f.startswith("Within 1.0% of its 52-week high") for f in fl))
        self.assertIn("Insiders sold ₹1.40 cr on the market in the last 90 days", fl)
        self.assertIn("Promoters have pledged 7.5% of their shares", fl)
        self.assertIn("Promoter holding down 1.50 points in the quarter to 2026-09-30 (48.90%)", fl)
        self.assertTrue(any(f.startswith("Bulk deal 2026-11-12: BIG FUND LTD bought 2,000,000 shares") for f in fl))
        self.assertEqual(doc["deals"][0]["client"], "BIG FUND LTD")
        self.assertFalse(any(d["symbol"] == "TCS" for d in doc["deals"]))

    def test_a_failed_source_is_named_never_empty(self):
        os.remove(os.path.join(self.off, "pit-RELIANCE.json"))
        os.remove(os.path.join(self.off, "fo_secban.csv"))
        doc = self.build()
        self.assertEqual(sorted(doc["failed"]), ["fo_ban", "insiders"])
        self.assertIsNone(doc["insiders"])
        self.assertEqual(doc["flags"][-1], "Could not read: NSE F&O ban list, NSE insider trades")
        text = dossier.render(doc)
        self.assertIn("## Insider trades, last 90 days (SEBI PIT disclosures on NSE)\n- Not available:", text)
        self.assertNotIn("F&O ban list for", text)

    def test_render_has_every_section(self):
        text = dossier.render(self.build())
        for h in ("# RELIANCE: Reliance Industries Limited", "## Flags", "## Price (Yahoo, daily)", "## Events",
                  "## Latest results", "## NSE filings, last 30 days", "## Headlines, last 7 days",
                  "## Who owns it", "## Insider trades", "## Bulk and block deals", "## Sources"):
            self.assertIn(h, text)
        self.assertIn("- F&O ban list for 13-NOV-2026: IN THE BAN.", text)
        self.assertIn("- 2026-09-30: promoters 48.90%, public 51.10%", text)

    def test_results_come_from_nse_analyst(self):
        class P(object):
            returncode, stderr = 0, ""
            stdout = json.dumps([{"result": {"basis": "consolidated", "rows": [
                {"quarter": "Q2 FY27", "end": "2026-09-30", "filed": "12-Nov-2026 21:32:55", "top_line": 1,
                 "net_profit": 2, "eps_diluted": 3}]}, "changes": ["Revenue up 10%."],
                "results_due": ["2026-12-31", "2027-02-14"], "notes": []}])
        f = dossier.news.Fetcher(offline=self.off, pause=0)
        doc = dossier.build("RELIANCE", f, dossier.Shared(f, None), nc.parse_now(NOW), results=True,
                            run=lambda argv, **kw: P())
        self.assertEqual((doc["results"]["quarter"], doc["results"]["changes"]), ("Q2 FY27", ["Revenue up 10%."]))
        self.assertIn("Results for Q2 FY27 filed 12-Nov-2026 21:32:55", doc["flags"])
        self.assertIn("- Revenue up 10%.", dossier.render(doc))


class Quoting(Base):
    def test_an_ampersand_never_ends_the_query(self):
        urls = []

        class F(object):
            offline = None

            def get(self, url, name, nse=False):
                urls.append(url)
                raise dossier.news.NewsError("offline")

        dossier.build("M&M", F(), dossier.Shared(F(), None), nc.parse_now(NOW), results=False)
        nse = [u for u in urls if "nseindia.com/api/" in u]
        self.assertEqual(len(nse), 6)
        for u in nse:
            self.assertIn("symbol=M%26M", u)


class Cli(Base):
    def test_out_writes_each_stock_and_the_index(self):
        out = os.path.join(self.d, "stocks")
        code, so, _ = self.run_main("RELIANCE", "--out", out)
        self.assertEqual(code, 0)
        self.assertIn("1 of 1 stock files", so)
        idx = read(os.path.join(out, "index.json"))
        self.assertEqual(list(idx["stocks"]), ["RELIANCE"])
        self.assertTrue(idx["stocks"]["RELIANCE"]["fo_ban"])
        self.assertEqual(idx["stocks"]["RELIANCE"]["next_board_meeting"]["date"], "2026-11-13")
        self.assertTrue(os.path.isfile(os.path.join(out, "RELIANCE.md")))
        self.assertTrue(os.path.isfile(os.path.join(out, "RELIANCE.json")))

    def test_out_of_time_is_listed(self):
        out = os.path.join(self.d, "stocks")
        code, _, _ = self.run_main("RELIANCE", "TCS", "--out", out, "--budget-s", "-1")
        idx = read(os.path.join(out, "index.json"))
        self.assertEqual(idx["stocks"]["TCS"]["failed"], ["not built: out of time"])
        self.assertEqual(idx["stocks"]["RELIANCE"]["failed"], ["not built: out of time"])

    def test_usage(self):
        self.assertEqual(self.run_main()[0], 2)
        code, _, err = self.run_main("not a symbol!")
        self.assertEqual(code, 2)
        self.assertIn("not an NSE symbol", err)

    def test_text_and_json(self):
        code, so, _ = self.run_main("RELIANCE")
        self.assertEqual(code, 0)
        self.assertIn("## Flags", so)
        code, so, _ = self.run_main("RELIANCE", "--json")
        doc = json.loads(so)
        self.assertEqual(doc["stocks"][0]["symbol"], "RELIANCE")


if __name__ == "__main__":
    unittest.main()
