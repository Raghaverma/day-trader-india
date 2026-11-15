import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("nse-news", "news")
DIR = os.path.join(FIX, "nse-news")
NOW = "2026-11-14 21:30"  # IST, when the fixtures were captured (Saturday evening)
WATCH = {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy Services Limited"}


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def announcements(sym):
    return news.parse_announcements(raw("nse-announcements-%s.json" % sym), sym)


class Feeds(unittest.TestCase):
    def test_nse_announcements(self):
        items = announcements("RELIANCE")
        self.assertEqual(len(items), 19)
        first = items[0]
        self.assertEqual(first["id"], "nse:106859406")
        self.assertEqual(first["kind"], "filing")
        self.assertEqual(first["source"], "NSE")
        self.assertEqual(first["company"], "Reliance Industries Limited")
        self.assertEqual(first["title"],
                         "Reliance Industries Limited: Analysts/Institutional Investor Meet/Con. Call Updates")
        self.assertIn("Morgan Stanley 25th Annual Asia-Pacific Summit", first["summary"])
        # an_dt is IST: 18:25:01 IST is 12:55:01 UTC
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 13, 12, 55, 1))
        self.assertTrue(first["link"].endswith("_SE_13112026.pdf"))

    def test_google_news(self):
        items, skipped = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        self.assertEqual((len(items), skipped), (33, 5))  # 38 in the feed, 5 quote pages
        first = items[0]
        # " - DD News" comes off the title: the publisher is the source
        self.assertEqual(first["title"], "Reliance and Jio to invest Rs 10 lakh crore over seven years for "
                                         "India’s AI transformation: Mukesh Ambani")
        self.assertEqual(first["source"], "DD News")
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 13, 18, 48, 6))
        self.assertTrue(first["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertTrue(first["id"].startswith("gnews:"))
        with self.assertRaises(news.NewsError):
            news.parse_gnews(b"<rss><channel><item>", "X")
        # seen live: some items carry the publisher's URL instead of its name
        x = (b'<rss><channel><item><title>TCS And ASX Go-Live - https://www.fortuneindia.com</title><link>l</link>'
             b'<source url="https://www.fortuneindia.com">https://www.fortuneindia.com</source></item></channel></rss>')
        item = news.parse_gnews(x, "TCS")[0][0]
        self.assertEqual((item["source"], item["title"]), ("fortuneindia.com", "TCS And ASX Go-Live"))

    def test_quote_pages_are_not_news(self):
        for t in ("Reliance Industries Share Price - Live NSE: RELIANCE Stock Price & Chart",
                  "RELIANCE INDUSTRIES LTD Option Chain - Live RELIANCE INDUSTRIES LTD Option Chain Data, OI & Price",
                  "Reliance Industries Share Price Prediction for Tomorrow: 13 Nov", "TCS Share Price",
                  "Tata Consultancy Services (NSEI:TCS) - Stock Analysis",
                  "Reliance Industries Ltd (RLI.DU) Seasonal returns chart"):
            self.assertTrue(news.QUOTE_PAGE.search(t), t)
        for t in ("Reliance Industries shares jump 15% adding ₹2,37,767 cr market value",
                  "TCS share price rises 3% after order win"):
            self.assertFalse(news.QUOTE_PAGE.search(t), t)

    def test_times(self):
        self.assertEqual(news.parse_time("Fri, 13 Nov 2026 18:48:06 GMT"), datetime.datetime(2026, 11, 13, 18, 48, 6))
        self.assertEqual(news.parse_time("2026-11-13T18:25:00+05:30"), datetime.datetime(2026, 11, 13, 12, 55))
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.parse_nse_time("30-Oct-2026 19:40:11"), datetime.datetime(2026, 10, 30, 14, 10, 11))
        self.assertEqual(news.parse_nse_time("2026-10-30 19:40:11"), datetime.datetime(2026, 10, 30, 14, 10, 11))
        self.assertIsNone(news.parse_nse_time(""))

    def test_google_is_searched_by_company_name(self):
        url = news.gnews_query("Reliance Industries Limited", "RELIANCE", 72)
        self.assertIn("q=%22Reliance+Industries%22+when%3A3d", url)
        self.assertIn("hl=en-IN&gl=IN&ceid=IN:en", url)
        self.assertIn("when%3A1d", news.gnews_query("Tata Consultancy Services Limited", "TCS", 6))
        self.assertIn("q=M%26M+NSE+share", news.gnews_query(None, "M&M", 24))
        self.assertEqual(news.clean_name("Amber Enterprises (I) Ltd"), "Amber Enterprises (I)")
        self.assertEqual(news.clean_name("TD Power Systems Ltd."), "TD Power Systems")

    def test_names_come_from_nse(self):
        import gzip
        with gzip.open(os.path.join(FIX, "nse-scanner", "EQUITY_L.csv.gz"), "rt", encoding="utf-8") as fh:
            names = news.names_from_equity_l(fh.read())
        self.assertEqual(names["RELIANCE"], "Reliance Industries Limited")
        self.assertEqual(names["M&M"], "Mahindra & Mahindra Limited")
        self.assertEqual(names["TCS"], "Tata Consultancy Services Limited")
        # the announcements carry the name too, and the run caches it in the state
        state = {}
        fetch = news.Fetcher(offline=DIR)
        news.collect(fetch, {"TCS": None}, state, news.nc.parse_now(NOW), google=False)
        self.assertEqual(state["names"], {"TCS": "Tata Consultancy Services Limited"})


class Tagging(unittest.TestCase):
    def setUp(self):
        items = news.tag(announcements("RELIANCE") + announcements("TCS"), WATCH)
        self.by = {i["id"]: i for i in items}

    def test_real_filings(self):
        board = self.by["nse:106837394"]  # Outcome of Board Meeting, 30 Oct: Q2 results + NCDs up to Rs 15,000 cr
        self.assertEqual(board["tags"], ["results", "board meeting", "fund raising"])
        self.assertTrue(board["important"])
        rating = self.by["nse:106823937"]
        self.assertEqual(rating["tags"], ["credit rating"])
        self.assertTrue(rating["important"])
        meet = self.by["nse:106859406"]
        self.assertEqual(meet["tags"], ["investor meet"])
        self.assertFalse(meet["important"])
        tcs = self.by["nse:106818480"]  # completion of acquisition of BBY Services India LLP
        self.assertEqual(tcs["tags"], ["deal/merger"])
        self.assertTrue(tcs["important"])
        self.assertEqual(tcs["touches"], ["TCS"])
        ipo = self.by["nse:106827783"]  # Initial Public Offer of Jio Platforms Limited
        self.assertEqual(ipo["tags"], ["fund raising"])

    def test_lodr_boilerplate_is_not_regulatory_news(self):
        strike = self.by["nse:106833561"]  # "Disclosure under Regulation 30 of the SEBI (LODR) Regulations, 2015 ..."
        self.assertIn("Securities and Exchange Board of India", strike["summary"])
        self.assertNotIn("regulatory", strike["tags"])
        self.assertIn("deal/merger", strike["tags"])  # Other Restructuring: strike off of a subsidiary
        self.assertIn("regulatory", news.tag_text("SEBI bars promoters from the market"))

    def test_india_tags(self):
        cases = {
            "Q2 results: net profit jumps 20% on strong margins": ["results"],
            "Board meeting on 16 Nov to consider buyback": ["board meeting", "dividend/bonus/split/buyback"],
            "Ice-cream maker announces bonus, stock split; record date fixed": ["dividend/bonus/split/buyback"],
            "KEC bags ₹1,200 crore orders from Power Grid": ["order win"],
            "L&T wins mega contract in the Middle East": ["order win"],
            "Tata Motors completes demerger; scheme of arrangement effective": ["deal/merger"],
            "Nuvama sees 42% upside; target price ₹500": ["analyst/brokerage"],
            "Jefferies upgrades HDFC Bank to buy": ["analyst/brokerage"],
            "SEBI show-cause notice; NCLT admits insolvency plea": ["regulatory"],
            "CCI approves the deal": ["regulatory"],
            "Promoter pledges 5% stake with lenders": ["promoter/pledge/stake"],
            "Block deal: 2% equity changes hands": ["block deal"],
            "CRISIL reaffirms AAA credit rating": ["credit rating"],
            "CFO resigns; board appoints new MD": ["management change"],
            "Stock hits upper circuit; NSE moves it to ASM": ["circuit/halt"],
            "RBI policy: MPC holds repo rate as FIIs sell": ["macro"],
            "Union Budget 2027: what it means for Nifty": ["macro"],
        }
        for text, want in cases.items():
            got = news.tag_text(text)
            for t in want:
                self.assertIn(t, got, text)
        self.assertEqual(news.tag_text("commuters fed up with traffic pat the dog"), [])

    def test_touches_by_symbol_name_and_initials(self):
        items = [{"id": "x", "source": "Livemint", "feed": "google", "kind": "headline", "ticker": "TCS",
                  "title": "RIL and TCS lead Sensex gainers", "summary": "", "link": "l", "time": None},
                 {"id": "y", "source": "Livemint", "feed": "google", "kind": "headline", "ticker": "TCS",
                  "title": "Tata Consultancy Services wins deal from Reliance", "summary": "", "link": "m",
                  "time": None}]
        news.tag(items, WATCH)
        self.assertEqual(items[0]["touches"], ["RELIANCE", "TCS"])
        self.assertEqual(items[1]["touches"], ["RELIANCE", "TCS"])
        self.assertIn("macro", items[0]["tags"])  # Sensex

    def test_dedupe_merges_the_same_story(self):
        a, _ = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        b, _ = news.parse_gnews(raw("gnews-RELIANCE.xml"), "TCS")
        items = news.dedupe(news.tag(a + b, WATCH))
        self.assertEqual(len(items), 33)
        self.assertEqual(items[0]["touches"], ["RELIANCE", "TCS"])

    def test_nse_posts_one_filing_twice(self):
        # 30 Oct 19:40:11: two "Outcome of Board Meeting" rows, same text, two PDFs
        items = news.dedupe(news.tag(announcements("RELIANCE"), WATCH))
        self.assertEqual(len(items), 18)
        board = [i for i in items if i["id"] == "nse:106837394"][0]
        self.assertEqual(board["also"], ["nse:106837395"])
        state = {}
        news.remember(items, state, news.nc.parse_now(NOW))
        self.assertIn(news.fingerprint("nse:106837395"), state["seen"])


class Calendar(unittest.TestCase):
    def test_board_meetings_and_results_dates(self):
        events = news.parse_calendar(raw("event-calendar.json"))
        self.assertEqual(len(events), 18)
        up = news.upcoming(events, {"SANDHAR": None, "TTL": None, "RELIANCE": None}, news.nc.parse_now(NOW))
        self.assertEqual([(e["symbol"], e["date"], e["days"], e["results"]) for e in up],
                         [("SANDHAR", "2026-11-16", 2, True), ("TTL", "2026-11-16", 2, False)])
        lines = news.render_calendar(up)
        self.assertTrue(lines[0].startswith("SANDHAR    reports on Mon 16 Nov 2026 (in 2 days)"))
        self.assertIn("TTL        board meeting on Mon 16 Nov 2026 (in 2 days): Buyback", lines)
        # past meetings drop off
        self.assertEqual(news.upcoming(events, {"SANDHAR": None}, news.nc.parse_now("2026-11-17 09:00")), [])

    def test_calendar_in_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run(["--tickers", "SANDHAR", "--no-nse", "--no-google", "--offline", DIR,
                                  "--now", NOW, "--state", os.path.join(tmp, "news.json")])
        self.assertEqual(code, 1, err)
        self.assertIn("Coming up (NSE event calendar):", out)
        self.assertIn("SANDHAR    reports on Mon 16 Nov 2026 (in 2 days): board meeting for Financial Results", out)
        self.assertIn("Nothing new.", out)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        # last 72 hours: 2 NSE filings + 32 Reliance and 4 TCS headlines that name the company
        self.assertEqual(len(data["items"]), 38)
        self.assertEqual(sum(1 for i in data["items"] if i["feed"] == "nse"), 2)
        self.assertEqual((data["quote_pages_skipped"], data["off_topic_skipped"]), (13, 14))
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["items"][-1]["time_ist"], "2026-11-12 07:39")
        code, out, _ = run(["--tickers", "RELIANCE", "TCS"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_never_repeated_across_runs(self):
        # Friday 19:00 IST: the 18:25 filing is new; Saturday night only what came after shows
        friday = list(self.base)
        friday[friday.index("--now") + 1] = "2026-11-13 19:00"
        code, out, err = run(["--tickers", "RELIANCE", "--no-google", "--json"] + friday)
        self.assertEqual(code, 0, err)
        self.assertEqual([i["id"] for i in json.loads(out)["items"]], ["nse:106859406", "nse:106857662"])
        code, out, err = run(["--tickers", "RELIANCE", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        ids = [i["id"] for i in json.loads(out)["items"]]
        self.assertNotIn("nse:106859406", ids)
        self.assertEqual(len(ids), 32)
        code, out, _ = run(["--tickers", "RELIANCE", "--all", "--json", "--dry-run"] + self.base)
        shown = json.loads(out)["items"]
        self.assertEqual(len(shown), 34)
        self.assertFalse([i for i in shown if i["id"] == "nse:106859406"][0]["new"])

    def test_ist_lines_and_filing_text(self):
        code, out, err = run(["--tickers", "RELIANCE", "--no-google", "--hours", "744"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("  2026-11-13 18:25 IST  RELIANCE    NSE           Reliance Industries Limited: "
                      "Analysts/Institutional Investor Meet/Con. Call Updates", out)
        self.assertIn("! 2026-10-30 19:40 IST  RELIANCE    NSE           Reliance Industries Limited: "
                      "Outcome of Board Meeting", out)
        self.assertIn("tags: results, board meeting, fund raising", out)
        self.assertEqual(out.count("Outcome of Board Meeting"), 1)
        self.assertIn("RELIANCE   ₹1,339.70 +0.00% vs previous close ₹1,339.70 "
                      "(Fri 13 Nov 2026 15:28 IST)", out)
        self.assertIn("Checked 2026-11-14 21:30 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn("$", out)
        self.assertNotIn(chr(0x2014), out)

    def test_nse_window_is_asked_for_by_date(self):
        seen = []

        class Rec(news.Fetcher):
            def get(self, url, name, nse=False):
                seen.append((url, nse))
                return super(Rec, self).get(url, name, nse)

        news.collect(Rec(offline=DIR), {"RELIANCE": None}, {}, news.nc.parse_now(NOW), hours=72, google=False)
        self.assertEqual(seen, [(news.NSE_ANN % ("RELIANCE", "11-11-2026", "14-11-2026"), True)])

    def test_symbols_from_watchlist_plan_and_portfolio(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [{"symbol": "RELIANCE", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "nse-exit", "symbol": "TCS", "exchange": "NSE"}, fh)
        pf = os.path.join(self.tmp.name, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 50000, "positions": [{"ticker": "INFY.NS", "shares": 5}]}, fh)
        self.assertEqual(sorted(news.watch_from_files(pf, wl, plan)), ["INFY", "RELIANCE", "TCS"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run"] + args)
        self.assertEqual(code, 0, err)
        self.assertEqual({t for i in json.loads(out)["items"] for t in i["touches"]}, {"RELIANCE"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_symbols_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)
        code, _, err = run(["--tickers", "BAD SYMBOL!"] + self.base)
        self.assertEqual(code, 2)

    def test_every_source_failing_is_a_plain_note(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out, err = run(["--tickers", "RELIANCE", "--offline", empty, "--now", NOW,
                                  "--state", self.state])
        self.assertEqual(code, 2)
        self.assertIn("note: NSE filings for RELIANCE: offline: no file", out)
        self.assertIn("note: headlines for RELIANCE: offline: no file", out)
        self.assertNotIn("Traceback", out + err)

    def test_no_sec_user_agent_anymore(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            news.main(["--help"])
        self.assertNotIn("--user-agent", out.getvalue())
        self.assertNotIn("SEC", out.getvalue())
        self.assertFalse(hasattr(news, "sec_user_agent"))

    def test_moves(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        # five daily bars: the previous close is 12 Nov's bar, not chartPreviousClose (6 Nov, 1,320)
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1339.7, 1339.7, 0.0))
        self.assertEqual(news.nc.to_ist(m["time"]), datetime.datetime(2026, 11, 13, 15, 28, 4))
        friday = news.nc.parse_now("2026-11-13 15:30")
        self.assertEqual(news.render_moves([m], friday)[0],
                         "RELIANCE   ₹1,339.70 +0.00% vs previous close ₹1,339.70 at 15:28 IST")
        loud = dict(m, pct=-3.4)
        self.assertTrue(news.render_moves([loud], friday)[0].endswith("MOVING"))
        self.assertNotIn("MOVING", news.render_moves([dict(m, pct=1.2)], friday)[0])
        self.assertIn("MOVING", news.render_moves([dict(m, pct=1.2)], friday, move_pct=1.0)[0])
        with open(os.path.join(FIX, "nse-alerts", "yahoo-TCS.json"), "rb") as fh:
            one_day = news.parse_chart(fh.read(), "TCS")  # a 1d/5m chart: chartPreviousClose
        self.assertGreater(one_day["prev_close"], 0)
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no note
        code, out, _ = run(["--tickers", "TCS", "--dry-run", "--no-google"] + self.base)
        self.assertNotIn("vs previous close", out)
        self.assertNotIn("note:", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-10-01T00:00:00Z"}}
        now = news.nc.parse_now(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], "2026-11-14T16:00:00Z")


if __name__ == "__main__":
    unittest.main()
