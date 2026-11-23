"""Kite Connect as the data source: nse_common.kite_answer against a fake Kite, and autopilot/kite.py."""

import datetime
import hashlib
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from _load import load

nc = load("nse-scanner", "nse_common")
scan = load("nse-scanner", "scan")
ticket = load("nse-broker", "ticket")

NOW = datetime.datetime(2026, 11, 23, 4, 12)        # 09:42 IST, Monday 23 Nov 2026 (a session)
MORNING = datetime.datetime(2026, 11, 23, 2, 30)    # 08:00 IST, before the open

INSTRUMENTS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
    "instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
    "519937,2031,M&M,MAHINDRA & MAHINDRA,0,,0,0.05,1,EQ,NSE,NSE\n"
    "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
    "999999,1,RELIANCE-BE,X,0,,0,0.1,1,EQ,NSE-BE,NSE\n")

SESSIONS = [datetime.date(2026, 11, d) for d in (13, 16, 17, 18, 19, 20, 23)]


class FakeKite:
    """Answers /instruments/NSE, /instruments/historical, /quote, /session/token, /user/profile."""

    def __init__(self, token="tok", deny=False):
        self.calls, self.token, self.deny = [], token, deny

    def ok(self, data):
        return 200, json.dumps({"status": "success", "data": data}).encode()

    def __call__(self, method, url, headers, data, timeout):
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((method, u.path, q, data))
        if u.path == "/session/token" and method == "POST":
            return self.ok({"access_token": "newtok", "user_id": "AB1234", "user_name": "Test"})
        if self.deny or headers.get("Authorization") not in ("token key1:tok", "token key1:newtok"):
            return 403, json.dumps({"status": "error", "message": "Incorrect api_key or access_token.",
                                    "error_type": "TokenException"}).encode()
        if u.path == "/instruments/NSE":
            return 200, INSTRUMENTS.encode()
        if u.path == "/user/profile":
            return self.ok({"user_name": "Test"})
        if u.path == "/quote":
            out = {}
            for i in q["i"]:
                out[i] = {"last_price": 1340.5, "last_trade_time": "2026-11-23 09:41:58", "volume": 123456,
                          "ohlc": {"open": 1330.0, "high": 1342.0, "low": 1328.0, "close": 1325.0}}
            return self.ok(out)
        if u.path.startswith("/instruments/historical/"):
            _, _, _, token, interval = u.path.split("/")
            a = datetime.datetime.strptime(q["from"][0], "%Y-%m-%d %H:%M:%S")
            b = datetime.datetime.strptime(q["to"][0], "%Y-%m-%d %H:%M:%S")
            candles = []
            for i, day in enumerate(SESSIONS):
                base = 1300 + i
                if interval == "day":
                    t = datetime.datetime.combine(day, datetime.time(0, 0))
                    if a <= t <= b:
                        candles.append([t.strftime("%Y-%m-%dT%H:%M:%S+0530"), base, base + 5, base - 5, base + 1, 1000])
                    continue
                step = {"minute": 1, "5minute": 5}[interval]
                t = datetime.datetime.combine(day, datetime.time(9, 15))
                end = datetime.datetime.combine(day, datetime.time(9, 41 if day == SESSIONS[-1] else 15, 30))
                while t <= end:
                    if a <= t <= b:
                        candles.append([t.strftime("%Y-%m-%dT%H:%M:%S+0530"), base, base + 1, base - 1, base + 0.5, 10])
                    t += datetime.timedelta(minutes=step)
            return self.ok({"candles": candles})
        return 404, json.dumps({"status": "error", "message": "no route"}).encode()


class KiteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.home, "NSE_TEST_MODE": "1"})
        self.env.start()
        os.environ.pop(nc.DATA_ENV, None)
        self.fake = FakeKite()
        nc._KITE.update(session=None, instruments=None, daily={}, said=False, http=self.fake,
                        sleep=lambda s: None, error=None, last={})

    def tearDown(self):
        nc._KITE.update(session=None, instruments=None, daily={}, http=None, said=False)
        self.env.stop()
        self.tmp.cleanup()

    def login(self, expires="2026-11-24T00:30:00Z", **extra):
        doc = {"api_key": "key1", "access_token": "tok", "user_id": "AB1234", "expires_at": expires}
        doc.update(extra)
        nc.write_json(os.path.join(self.home, "kite-session.json"), doc)

    def answer(self, url, now=NOW):
        with mock.patch.object(nc, "utc_now", return_value=now):
            return nc.kite_answer(url, now)


class Pieces(KiteBase):
    def test_tokens_expire_at_six_ist(self):
        # logged in 08:00 IST on the 23rd -> 06:00 IST on the 24th; at 02:00 IST -> 06:00 the same morning
        self.assertEqual(nc.kite_expiry(datetime.datetime(2026, 11, 23, 2, 30)), datetime.datetime(2026, 11, 24, 0, 30))
        self.assertEqual(nc.kite_expiry(datetime.datetime(2026, 11, 22, 20, 30)), datetime.datetime(2026, 11, 23, 0, 30))

    def test_yahoo_urls_are_recognised(self):
        kind, syms, q = nc.yahoo_request(nc.yahoo_chart_url("M&M", "1d", "5m"))
        self.assertEqual((kind, syms, q["range"], q["interval"]), ("chart", ["M&M.NS"], "1d", "5m"))
        kind, syms, q = nc.yahoo_request(nc.yahoo_spark_url(["RELIANCE", "M&M"], "1d", "5m"))
        self.assertEqual((kind, syms), ("spark", ["RELIANCE.NS", "M&M.NS"]))
        self.assertIsNone(nc.yahoo_request("https://www.nseindia.com/api/x"))
        self.assertIsNone(nc.yahoo_request("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/x"))

    def test_symbols(self):
        self.assertEqual(nc.kite_symbol("RELIANCE.NS"), "RELIANCE")
        self.assertEqual(nc.kite_symbol("M%26M.NS"), "M&M")
        self.assertEqual(nc.kite_symbol("^NSEI"), "NIFTY 50")
        for other in ("^GSPC", "ES=F", "INR=X", "RELIANCE.BO", "^UNMAPPED"):
            self.assertIsNone(nc.kite_symbol(other), other)

    def test_windows(self):
        a, b, n = nc.kite_window({"range": "5d"}, NOW)
        self.assertEqual(n, 5)
        self.assertLessEqual(a, nc.to_ist(NOW) - datetime.timedelta(days=10))
        a, b, n = nc.kite_window({"range": "60d"}, NOW)
        self.assertEqual((n, (b - a).days), (None, 60))
        self.assertIsNone(nc.kite_window({"range": "max"}, NOW))
        a, b, n = nc.kite_window({"period1": "1794541500", "period2": "1794560000"}, NOW)
        self.assertEqual(a, datetime.datetime(2026, 11, 13, 9, 15))


class Answers(KiteBase):
    def test_no_login_means_yahoo_and_no_kite_calls(self):
        self.assertIsNone(self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m")))
        self.assertEqual(self.fake.calls, [])

    def test_expired_login_means_yahoo(self):
        self.login(expires="2026-11-23T00:30:00Z")
        self.assertIsNone(self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m")))
        self.assertEqual(self.fake.calls, [])

    def test_env_switch_forces_yahoo(self):
        self.login()
        with mock.patch.dict(os.environ, {nc.DATA_ENV: "yahoo"}):
            self.assertIsNone(self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m")))

    def test_world_symbols_never_touch_kite(self):
        self.login()
        self.assertIsNone(self.answer(nc.yahoo_chart_url("^GSPC", "1y", "1d").replace("%5EGSPC.NS", "%5EGSPC")))
        self.assertIsNone(self.answer("https://query2.finance.yahoo.com/v8/finance/chart/ES%3DF?range=5d&interval=1d"))
        self.assertEqual(self.fake.calls, [])

    def test_intraday_chart_in_yahoo_shape(self):
        self.login()
        text = self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m"))
        doc = json.loads(text)
        r = doc["chart"]["result"][0]
        m = r["meta"]
        self.assertEqual(m["symbol"], "RELIANCE.NS")
        self.assertEqual(m["dataSource"], "kite")
        self.assertEqual(m["dataGranularity"], "5m")
        self.assertEqual(m["regularMarketPrice"], 1340.5)                       # the live quote, not the bar
        self.assertEqual(nc.ist_from_epoch(m["regularMarketTime"]), datetime.datetime(2026, 11, 23, 9, 41, 58))
        self.assertEqual(m["previousClose"], 1325.0)
        self.assertEqual(m["chartPreviousClose"], 1306.0)                       # Friday's daily close
        stamps = [nc.ist_from_epoch(t) for t in r["timestamp"]]
        self.assertEqual(stamps[0], datetime.datetime(2026, 11, 23, 9, 15))    # today's session only (range 1d)
        self.assertTrue(all(t.date() == datetime.date(2026, 11, 23) for t in stamps))
        series = scan.parse_chart(text, "RELIANCE")                             # the scanner reads it
        self.assertEqual(series["c"][0], 1306.5)

    def test_paper_broker_reads_kite_bars(self):
        self.login()
        bars = ticket.parse_bars(json.loads(self.answer(nc.yahoo_chart_url("RELIANCE", "5d", "1m"))))
        days = sorted({b[0].date() for b in bars})
        self.assertEqual(len(days), 5)                                          # range 5d = five sessions
        self.assertEqual(bars[-1][0], datetime.datetime(2026, 11, 23, 9, 41))

    def test_spark_batch(self):
        self.login()
        text = self.answer(nc.yahoo_spark_url(["RELIANCE", "M&M"], "1d", "5m"))
        got = scan.parse_spark(text, {"RELIANCE.NS": "RELIANCE", "M&M.NS": "M&M"})
        self.assertEqual(sorted(got), ["M&M", "RELIANCE"])
        quotes = [c for c in self.fake.calls if c[1] == "/quote"]
        self.assertEqual(len(quotes), 1)                                        # one quote call for the batch
        self.assertEqual(sorted(quotes[0][2]["i"]), ["NSE:M&M", "NSE:RELIANCE"])

    def test_spark_with_an_unknown_symbol_goes_to_yahoo_whole(self):
        self.login()
        self.assertIsNone(self.answer(nc.yahoo_spark_url(["RELIANCE", "NOSUCHCO"], "1d", "5m")))

    def test_daily_bars_end_with_todays_live_bar(self):
        self.login()
        r = json.loads(self.answer(nc.yahoo_chart_url("RELIANCE", "1mo", "1d")))["chart"]["result"][0]
        stamps = [nc.ist_from_epoch(t) for t in r["timestamp"]]
        self.assertEqual(stamps[-1], datetime.datetime(2026, 11, 23, 9, 15))
        q = r["indicators"]["quote"][0]
        self.assertEqual((q["open"][-1], q["close"][-1]), (1330.0, 1340.5))
        self.assertEqual(stamps[-2], datetime.datetime(2026, 11, 20, 9, 15))    # Yahoo-style 09:15 stamps
        # the daily candles are fetched once a day per symbol, then come from the cache
        before = len([c for c in self.fake.calls if "/day" in c[1]])
        nc._KITE["daily"] = {}
        self.answer(nc.yahoo_chart_url("RELIANCE", "3mo", "1d"))
        self.assertEqual(len([c for c in self.fake.calls if "/day" in c[1]]), before)

    def test_before_the_open_range_1d_is_the_last_session_and_no_quote(self):
        self.login()
        r = json.loads(self.answer(nc.yahoo_chart_url("NIFTY", "1d", "5m").replace("NIFTY.NS", "%5ENSEI"),
                                   now=MORNING))["chart"]["result"][0]
        self.assertEqual(r["meta"]["symbol"], "^NSEI")
        self.assertEqual(nc.ist_from_epoch(r["timestamp"][0]).date(), datetime.date(2026, 11, 20))
        self.assertEqual(r["meta"]["previousClose"], 1305.0)                    # Thursday's close
        self.assertFalse([c for c in self.fake.calls if c[1] == "/quote"])

    def test_rejected_token_marks_the_session_dead(self):
        self.login()
        self.fake.deny = True
        with mock.patch("sys.stderr"):
            self.assertIsNone(self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m")))
        doc = nc.read_json(os.path.join(self.home, "kite-session.json"))
        self.assertIn("Incorrect api_key", doc["dead"])
        n = len(self.fake.calls)
        self.assertIsNone(self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m")))
        self.assertEqual(len(self.fake.calls), n)                               # no second try today

    def test_rate_limit_retries(self):
        self.login()
        real, seen = self.fake, {"n": 0}

        def flaky(method, url, headers, data, timeout):
            if "/quote" in url and seen["n"] < 1:
                seen["n"] += 1
                return 429, b'{"status":"error","message":"Too many requests"}'
            return real(method, url, headers, data, timeout)
        nc._KITE["http"] = flaky
        self.assertIsNotNone(self.answer(nc.yahoo_chart_url("RELIANCE", "1d", "5m")))


class LoginCommand(KiteBase):
    def setUp(self):
        super().setUp()
        self.kite = load("autopilot", "kite")
        self.kite.nc._KITE.update(http=self.fake, sleep=lambda s: None, session=None)

    def tearDown(self):
        self.kite.nc._KITE.update(http=None, session=None)
        super().tearDown()

    def test_checksum(self):
        self.assertEqual(self.kite.checksum("k", "r", "s"), hashlib.sha256(b"krs").hexdigest())

    def test_login_with_a_request_token_saves_a_session(self):
        nc.write_json(os.path.join(self.home, "kite.json"), {"api_key": "key1", "port": 5005})
        with mock.patch.object(self.kite.nc, "keychain_get", return_value="secret1"), \
                mock.patch.object(self.kite.nc, "utc_now", return_value=NOW), \
                mock.patch("builtins.print"):
            self.kite.main(["login", "--request-token", "rt1"])
        post = [c for c in self.fake.calls if c[0] == "POST"][0]
        form = urllib.parse.parse_qs(post[3].decode())
        self.assertEqual(form["checksum"], [hashlib.sha256(b"key1rt1secret1").hexdigest()])
        doc = nc.read_json(os.path.join(self.home, "kite-session.json"))
        self.assertEqual((doc["user_id"], doc["expires_at"]), ("AB1234", "2026-11-24T00:30:00Z"))
        self.assertEqual(oct(os.stat(os.path.join(self.home, "kite-session.json")).st_mode & 0o777), "0o600")

    def test_status(self):
        st = self.kite.status(self.home, offline=True, now_utc=NOW)
        self.assertEqual((st["source"], st["why"]), ("yahoo", "no Kite login yet"))
        self.login()
        self.assertEqual(self.kite.status(self.home, offline=True, now_utc=NOW)["source"], "kite")
        st = self.kite.status(self.home, offline=True, now_utc=datetime.datetime(2026, 11, 24, 1, 0))
        self.assertIn("expired", st["why"])

    def test_no_secret_no_login(self):
        nc.write_json(os.path.join(self.home, "kite.json"), {"api_key": "key1"})
        with mock.patch.object(self.kite.nc, "keychain_get", return_value=None), mock.patch("sys.stderr"):
            self.assertEqual(self.kite.main(["login", "--request-token", "x"]), 1)


class AutopilotSource(KiteBase):
    def setUp(self):
        super().setUp()
        self.ac = load("autopilot", "autopilot_common")
        self.report = load("autopilot", "report")
        self.ac.nc._KITE.update(session=None)

    def test_data_source(self):
        self.assertEqual(self.ac.data_source(self.home, NOW), "yahoo: no Kite login")
        self.login()
        self.assertEqual(self.ac.data_source(self.home, NOW), "kite")
        self.assertEqual(self.ac.data_source(self.home, datetime.datetime(2026, 11, 24, 1, 0)),
                         "yahoo: no Kite login today")
        self.login(dead="Incorrect api_key or access_token.")
        self.assertIn("rejected", self.ac.data_source(self.home, NOW))

    def test_report_words(self):
        self.assertEqual(self.report.data_words("kite"), "Kite")
        self.assertEqual(self.report.data_words(None), "Yahoo")
        self.assertEqual(self.report.data_words("yahoo: no Kite login today"), "Yahoo (no Kite login today)")
        self.assertEqual(self.report.data_words("kite", "yahoo: Kite rejected the login (x)"),
                         "Kite at the open, Yahoo (Kite rejected the login (x)) by the close")

    def test_kite_cli_home_flag(self):
        kite = load("autopilot", "kite")
        other = os.path.join(self.home, "elsewhere")
        with mock.patch("builtins.print") as out:
            kite.main(["--home", other, "status", "--offline"])
        self.assertIn(other, " ".join(str(c) for c in out.call_args_list))


if __name__ == "__main__":
    unittest.main()
