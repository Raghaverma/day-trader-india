import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

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

    def test_every_source_failing_is_a_plain_failure(self):
        # was exit 2 and a bare "Nothing new.": NSE and Google News both failed, nothing was checked.
        # Now exit 3 (2 stays "usage") and the first line says so.
        with tempfile.TemporaryDirectory() as empty:
            code, out, err = run(["--tickers", "RELIANCE", "--offline", empty, "--now", NOW,
                                  "--state", self.state])
        self.assertEqual(code, 3)
        self.assertIn("note: NSE filings for RELIANCE: offline: no file", out)
        self.assertIn("note: headlines for RELIANCE: offline: no file", out)
        self.assertIn("News check FAILED: could not reach NSE or Google News: nothing was checked.", out)
        self.assertNotIn("Nothing new", out)
        self.assertNotIn("Traceback", out + err)
        self.assertFalse(os.path.exists(self.state))

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


# ---------------------------------------------------------------- failures that must not look like "nothing new"

def gnews_empty():
    return b"<rss version='2.0'><channel><title>q</title></channel></rss>"


class Fake(news.Fetcher):
    """A Fetcher that answers by URL part: bytes to return, or an Exception to raise. No network."""

    def __init__(self, answers):
        super(Fake, self).__init__(offline=None, pause=0)
        self.answers, self.calls = answers, []

    def get(self, url, name, nse=False):
        self.calls.append(url)
        for key, val in self.answers.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise news.NewsError("could not reach %s: no route to host" % url.split("?")[0])


class IsolatedHome(unittest.TestCase):
    """Every test gets its own trader folder: the real trader/ is never read or written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "trader")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--state", self.state, "--now", NOW]

    def go(self, fake, args):
        with mock.patch.object(news, "Fetcher", lambda offline=None, pause=0.5: fake):
            return run(args + self.base)

    def saved(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)


class SourceFailures(IsolatedHome):
    def good(self, **over):
        answers = {"corporate-announcements": raw("nse-announcements-RELIANCE.json"),
                   "news.google.com": raw("gnews-RELIANCE.xml"),
                   "event-calendar": raw("event-calendar.json"),
                   "EQUITY_L": news.NewsError("HTTP 503 for EQUITY_L.csv")}
        answers.update(over)
        return answers

    def test_nse_and_google_both_down_is_exit_3_and_leaves_the_state_alone(self):
        down = {"corporate-announcements": news.NewsError("could not reach nseindia.com: timed out"),
                "news.google.com": news.NewsError("could not reach news.google.com: timed out")}
        code, out, err = self.go(Fake(down), ["--tickers", "RELIANCE", "TCS"])
        self.assertEqual(code, 3, err)
        self.assertTrue(out.startswith("News check FAILED: could not reach NSE or Google News: nothing was "
                                       "checked."), out)
        self.assertNotIn("Nothing new", out)
        self.assertIn("note: NSE filings for TCS: could not reach nseindia.com: timed out", out)
        self.assertFalse(os.path.exists(self.state))  # nothing remembered: the next run sees everything
        code, out, _ = self.go(Fake(down), ["--tickers", "RELIANCE", "--json"])
        doc = json.loads(out)
        self.assertEqual((code, doc["status"]), (3, "failed"))
        self.assertEqual(doc["failure"], "could not reach NSE or Google News: nothing was checked")
        self.assertEqual(doc["sources"]["nse"]["status"], "failed")

    def test_only_nse_asked_and_down_names_nse(self):
        down = {"corporate-announcements": news.NewsError("HTTP 403 for nseindia.com")}
        code, out, _ = self.go(Fake(down), ["--tickers", "RELIANCE", "--no-google"])
        self.assertEqual(code, 3)
        self.assertIn("could not reach NSE: nothing was checked", out)

    def test_html_block_pages_are_failures_not_empty_news(self):
        blocked = {"corporate-announcements": b"<html><body>Access Denied</body></html>",
                   "news.google.com": b"<html><head><title>Before you continue</title></head><body/></html>"}
        code, out, _ = self.go(Fake(blocked), ["--tickers", "RELIANCE"])
        self.assertEqual(code, 3)
        self.assertIn("is not JSON (NSE may be blocking the request)", out)
        self.assertIn("sent <html>, not the RSS feed", out)  # well-formed XML, but not Google's feed

    def test_nse_error_object_is_not_an_empty_list(self):
        with self.assertRaises(news.NewsError):
            news.parse_announcements(b'{"error": "Resource not found"}', "RELIANCE")
        with self.assertRaises(news.NewsError):
            news.parse_announcements(b"{}", "RELIANCE")
        self.assertEqual(news.parse_announcements(b'{"data": []}', "RELIANCE"), [])  # quiet is fine
        self.assertEqual(news.parse_announcements(b"[]", "RELIANCE"), [])

    def test_google_down_alone_is_named_and_its_items_are_not_marked_seen(self):
        answers = self.good(**{"news.google.com": news.NewsError("HTTP 429 for news.google.com/rss/search")})
        code, out, err = self.go(Fake(answers), ["--tickers", "RELIANCE", "--no-moves"])
        self.assertEqual(code, 0, err)
        self.assertIn("checked: NSE filings ok; Google News failed; NSE calendar ok.", out)
        self.assertNotIn("Nothing new", out)
        self.assertIn("note: headlines for RELIANCE: HTTP 429", out)
        saved = self.saved()
        self.assertEqual(len(saved["seen"]), 2)  # the two NSE filings inside 72 h, nothing from Google
        self.assertEqual(saved["checked"], {"RELIANCE": {"nse": "2026-11-14T16:00:00Z"}})
        # Google comes back: its headlines were never marked seen, so they show now
        code, out, err = self.go(Fake(self.good()), ["--tickers", "RELIANCE", "--no-moves", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["status"], "ok")
        self.assertEqual({i["feed"] for i in doc["items"]}, {"google"})
        self.assertEqual(len(doc["items"]), 32)

    def test_one_symbol_failing_on_nse_is_named_with_the_ones_that_worked(self):
        class PerSymbol(Fake):
            def get(self, url, name, nse=False):
                if "symbol=TCS" in url:
                    raise news.NewsError("HTTP 500 for corporate-announcements")
                return super(PerSymbol, self).get(url, name, nse)

        code, out, err = self.go(PerSymbol(self.good()), ["--tickers", "RELIANCE", "TCS", "--no-google",
                                                          "--no-moves", "--no-calendar"])
        self.assertEqual(code, 0, err)
        self.assertIn("checked: NSE filings failed for TCS (ok for RELIANCE).", out)
        marks = self.saved()["checked"]
        self.assertIn("nse", marks["RELIANCE"])
        self.assertNotIn("TCS", marks)  # TCS was not read: the next run reaches back to before this one

    def test_checked_line_is_quiet_when_everything_worked(self):
        answers = self.good(**{"chart": raw("chart-RELIANCE.json")})
        code, out, err = self.go(Fake(answers), ["--tickers", "RELIANCE"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("checked:", out)
        self.assertEqual(json.loads(self.go(Fake(answers), ["--tickers", "RELIANCE", "--json", "--all",
                                                           "--dry-run"])[1])["status"], "ok")

    def test_calendar_down_is_named_not_empty(self):
        answers = self.good(**{"event-calendar": news.NewsError("HTTP 403 for event-calendar")})
        code, out, _ = self.go(Fake(answers), ["--tickers", "SANDHAR", "--no-google", "--no-moves"])
        self.assertIn("NSE calendar failed", out)
        self.assertIn("note: NSE event calendar: HTTP 403", out)
        self.assertNotIn("Coming up", out)

    def test_calendar_that_lists_nothing_or_nothing_readable_is_not_no_meetings(self):
        with self.assertRaises(news.NewsError):
            news.parse_calendar(b'{"data": []}')
        with self.assertRaises(news.NewsError):
            news.parse_calendar(b'[{"symbol": "TCS", "date": "2026/11/16", "purpose": "Results"}]')
        with self.assertRaises(news.NewsError):
            news.parse_calendar(b'{"message": "Resource not found"}')

    def test_unreadable_portfolio_is_a_note_not_a_silent_skip(self):
        with open(os.path.join(self.home, "portfolio.json"), "w") as fh:
            fh.write("{ this is not json")
        problems = []
        self.assertEqual(news.watch_from_files(problems=problems), {})
        self.assertIn("portfolio.json", problems[0])
        answers = self.good(**{"chart": news.NewsError("no chart")})
        code, out, _ = self.go(Fake(answers), ["--tickers", "RELIANCE", "--no-google"])
        self.assertIn("note: could not read", out)
        self.assertIn("portfolio.json, so its symbols are not checked", out)


class StateFile(IsolatedHome):
    def test_a_broken_state_file_is_a_note_not_a_silent_reset(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as fh:
            fh.write("{ half a file")
        fake = Fake({"corporate-announcements": raw("nse-announcements-RELIANCE.json")})
        code, out, err = self.go(fake, ["--tickers", "RELIANCE", "--no-google", "--no-moves", "--no-calendar"])
        self.assertEqual(code, 0, err)
        self.assertIn("note: could not read %s" % self.state, out)
        self.assertIn("items shown before may show again", out)
        with open(self.state, encoding="utf-8") as fh:
            self.assertIn("seen", json.load(fh))  # rewritten whole
        with open(self.state, "w") as fh:
            fh.write("[1, 2]")
        code, out, _ = self.go(fake, ["--tickers", "RELIANCE", "--no-google", "--no-moves", "--no-calendar"])
        self.assertIn("note: could not read", out)


class Window(IsolatedHome):
    def answers(self):
        return {"corporate-announcements": raw("nse-announcements-TCS.json")}

    def test_default_window_reaches_back_to_the_last_full_check(self):
        # TCS's 9 Nov 17:12 IST filing is 124 h before NOW. Last read in full 130 h ago (a long
        # weekend and a holiday): it must show, where the old fixed 72 h window dropped it.
        os.makedirs(os.path.dirname(self.state))
        last = news.nc.utc_iso(news.nc.parse_now(NOW) - datetime.timedelta(hours=130))
        with open(self.state, "w") as fh:
            json.dump({"checked": {"TCS": {"nse": last}}}, fh)
        code, out, err = self.go(Fake(self.answers()), ["--tickers", "TCS", "--no-google", "--no-moves",
                                                        "--no-calendar", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual([i["id"] for i in doc["items"]], ["nse:106853516"])
        self.assertIn("looking back 131 hours (not 72)", " ".join(doc["notes"]))
        self.assertEqual(self.saved()["checked"]["TCS"]["nse"], news.nc.utc_iso(news.nc.parse_now(NOW)))

    def test_no_history_means_72_hours_and_an_explicit_window_is_never_widened(self):
        code, out, _ = self.go(Fake(self.answers()), ["--tickers", "TCS", "--no-google", "--no-moves",
                                                      "--no-calendar", "--json"])
        self.assertEqual((code, json.loads(out)["items"]), (1, []))
        last = news.nc.utc_iso(news.nc.parse_now(NOW) - datetime.timedelta(hours=130))
        with open(self.state, "w") as fh:
            json.dump({"checked": {"TCS": {"nse": last}}}, fh)
        code, out, _ = self.go(Fake(self.answers()), ["--tickers", "TCS", "--no-google", "--no-moves",
                                                      "--no-calendar", "--json", "--hours", "72", "--dry-run"])
        self.assertEqual((code, json.loads(out)["items"]), (1, []))

    def test_window_hours_is_capped_at_the_seen_marks_lifetime(self):
        now = news.nc.parse_now(NOW)
        old = news.nc.utc_iso(now - datetime.timedelta(days=60))
        state = {"checked": {"TCS": {"nse": old}}}
        self.assertEqual(news.window_hours(state, now, ["TCS"], ["nse"]), news.MAX_HOURS)
        self.assertEqual(news.window_hours({}, now, ["TCS"], ["nse"]), news.HOURS)
        recent = news.nc.utc_iso(now - datetime.timedelta(hours=5))
        self.assertEqual(news.window_hours({"checked": {"TCS": {"nse": recent}}}, now, ["TCS"], ["nse"]), 72)
        # a feed that was not asked for does not widen the window
        self.assertEqual(news.window_hours(state, now, ["TCS"], ["google"]), news.HOURS)


# ---------------------------------------------------------------- the EQUITY_L cache the scanner shares

def equity_l(n=1200, extra=("SANDHAR,Sandhar Technologies Limited,EQ",)):
    rows = ["SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE"]
    rows += ["%s,Company %d Limited,EQ,01-JAN-2000,1,1,INE%06d01,1" % ("SYM%d" % i, i, i) for i in range(n)]
    rows += list(extra)
    return "\n".join(rows) + "\n"


class EquityListCache(IsolatedHome):
    def setUp(self):
        super(EquityListCache, self).setUp()
        self.cache = os.path.join(self.home, "cache")
        self.now = news.nc.parse_now(NOW)
        self.today = os.path.join(self.cache, "EQUITY_L-2026-11-14.csv")

    def put(self, name, text):
        os.makedirs(self.cache, exist_ok=True)
        with open(os.path.join(self.cache, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_shape_check(self):
        self.assertIsNone(news.check_equity_l(equity_l()))
        self.assertIsNone(news.check_equity_l("﻿" + equity_l()))
        self.assertIn("empty", news.check_equity_l(""))
        self.assertIn("web page", news.check_equity_l("<!DOCTYPE html><html>Access Denied</html>"))
        self.assertIn("SERIES", news.check_equity_l("SYMBOL,NAME OF COMPANY\nA,B\n" * 5))
        self.assertIn("only 3 companies", news.check_equity_l(equity_l(0, ("A,A Ltd,EQ", "B,B Ltd,EQ", "C,C Ltd,EQ"))))

    def test_a_block_page_is_not_cached_and_the_old_good_cache_is_kept(self):
        self.put("EQUITY_L-2026-11-13.csv", equity_l())
        fake = Fake({"EQUITY_L": b"<!DOCTYPE html><html><body>Access Denied</body></html>"})
        names = news.equity_list(fake, self.now, self.cache)
        self.assertEqual(names["SANDHAR"], "Sandhar Technologies Limited")  # yesterday's names, still good
        self.assertFalse(os.path.exists(self.today))  # the scanner will not find a web page here
        self.assertTrue(os.path.exists(os.path.join(self.cache, "EQUITY_L-2026-11-13.csv")))
        self.assertFalse([f for f in os.listdir(self.cache) if f.endswith(".tmp")])

    def test_a_block_page_with_no_old_cache_raises_and_caches_nothing(self):
        fake = Fake({"EQUITY_L": b"<html>blocked</html>"})
        with self.assertRaises(news.NewsError) as cm:
            news.equity_list(fake, self.now, self.cache)
        self.assertIn("not usable", str(cm.exception))
        self.assertFalse(os.path.exists(self.cache) and os.listdir(self.cache))

    def test_good_download_is_cached_whole_and_replaces_older_days(self):
        self.put("EQUITY_L-2026-11-13.csv", equity_l(1100))
        names = news.equity_list(Fake({"EQUITY_L": equity_l().encode()}), self.now, self.cache)
        self.assertIn("SANDHAR", names)
        self.assertEqual(os.listdir(self.cache), ["EQUITY_L-2026-11-14.csv"])
        with open(self.today, encoding="utf-8") as fh:
            self.assertIsNone(news.check_equity_l(fh.read()))
        # the second call is served from the cache: no request at all
        fake = Fake({})
        self.assertIn("SANDHAR", news.equity_list(fake, self.now, self.cache))
        self.assertEqual(fake.calls, [])

    def test_a_poisoned_cache_for_today_is_deleted_and_fetched_again(self):
        self.put("EQUITY_L-2026-11-14.csv", "<html>Access Denied</html>")  # what an older version wrote
        fake = Fake({"EQUITY_L": equity_l().encode()})
        self.assertIn("SANDHAR", news.equity_list(fake, self.now, self.cache))
        self.assertEqual(len(fake.calls), 1)
        with open(self.today, encoding="utf-8") as fh:
            self.assertIsNone(news.check_equity_l(fh.read()))

    def test_a_cut_off_download_is_not_cached(self):
        cut = equity_l(1200).encode()[:20000]  # 1,200 rows cut to about 150
        with self.assertRaises(news.NewsError):
            news.equity_list(Fake({"EQUITY_L": cut}), self.now, self.cache)
        self.assertFalse(os.path.exists(self.today))


class Names(IsolatedHome):
    def test_hit_without_a_name_is_looked_up_in_the_equity_l_cache(self):
        os.makedirs(os.path.join(self.home, "cache"))
        with open(os.path.join(self.home, "cache", "EQUITY_L-2026-11-14.csv"), "w", encoding="utf-8") as fh:
            fh.write(equity_l())
        with open(os.path.join(self.home, "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "SANDHAR", "setups": ["orb"]}]}, fh)  # the scanner wrote no name
        watch = news.watch_from_files()
        self.assertEqual(watch, {"SANDHAR": None})
        fake = Fake({"corporate-announcements": b'{"data": []}', "news.google.com": gnews_empty(),
                     "EQUITY_L": news.NewsError("must come from the cache")})
        code, out, err = self.go(fake, ["--no-calendar", "--no-moves"])
        self.assertEqual(code, 1, err)
        google = [u for u in fake.calls if "news.google.com" in u][0]
        self.assertIn("q=%22Sandhar+Technologies%22", google)  # searched by company name
        self.assertFalse([u for u in fake.calls if "EQUITY_L" in u])  # the cache answered
        self.assertNotIn("no company name", out)

    def test_name_unknown_everywhere_falls_back_to_the_symbol(self):
        with open(os.path.join(self.home, "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "ZZNOPE"}]}, fh)
        fake = Fake({"corporate-announcements": b'{"data": []}', "news.google.com": gnews_empty(),
                     "EQUITY_L": equity_l().encode()})
        code, out, err = self.go(fake, ["--no-calendar", "--no-moves"])
        self.assertEqual(code, 1, err)
        google = [u for u in fake.calls if "news.google.com" in u][0]
        self.assertIn("q=ZZNOPE+NSE+share", google)
        self.assertIn("note: no company name for ZZNOPE: searched Google News by symbol", out)

    def test_a_name_in_the_watchlist_wins(self):
        with open(os.path.join(self.home, "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "SANDHAR", "name": "Sandhar Tech From The Scanner"}]}, fh)
        fake = Fake({"corporate-announcements": b'{"data": []}', "news.google.com": gnews_empty()})
        self.go(fake, ["--no-calendar", "--no-moves"])
        self.assertIn("Sandhar+Tech+From+The+Scanner", [u for u in fake.calls if "google" in u][0])
        self.assertFalse([u for u in fake.calls if "EQUITY_L" in u])


class Paths(IsolatedHome):
    def test_state_and_inputs_follow_the_trader_home_not_the_working_folder(self):
        os.makedirs(os.path.join(self.home, "state"))
        with open(os.path.join(self.home, "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "RELIANCE", "name": "Reliance Industries Limited"}]}, fh)
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        cwd = os.getcwd()
        os.chdir(elsewhere.name)
        try:
            code, out, err = run(["--offline", DIR, "--no-google", "--now", NOW, "--no-calendar", "--no-moves"])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, err)  # RELIANCE came from $NSE_TRADER_HOME/watchlist.json
        self.assertTrue(os.path.isfile(os.path.join(self.home, "state", "news.json")))
        self.assertFalse(os.path.exists(os.path.join(elsewhere.name, "trader")))
        self.assertEqual(news.nc.tpath("state", "news.json"), os.path.join(self.home, "state", "news.json"))
        # the "no symbols" message names the real paths
        os.remove(os.path.join(self.home, "watchlist.json"))
        code, _, err = run(["--offline", DIR, "--now", NOW])
        self.assertEqual(code, 2)
        self.assertIn(os.path.join(self.home, "watchlist.json"), err)


if __name__ == "__main__":
    unittest.main()
