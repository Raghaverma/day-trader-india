"""nse-alerts tests. Offline. Quotes are real Yahoo .NS chart files (1d, 5m) captured
after the close of Fri 13 Nov 2026; holiday-master.json is NSE's real
/api/holiday-master?type=trading answer; plan-RELIANCE.json is the plan nse-exit
writes for the worked example (stop 1,328.80, target 1,344.40, flat by 15:10)."""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import re
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, ROOT, FIX

alerts = load("nse-alerts", "alerts")
nc = alerts.nc

QDIR = os.path.join(FIX, "nse-alerts")
MASTER = os.path.join(QDIR, "holiday-master.json")
PLAN = os.path.join(QDIR, "plan-RELIANCE.json")
DT = datetime.datetime
GURU_NANAK = "Prakash Gurpurb Sri Guru Nanak Dev"


def ist(text):
    return alerts.parse_now(text)


def status(text, cfg=None):
    return nc.market_status(ist(text), cfg, nc.NSE_HOLIDAYS, nc.HOLIDAY_YEARS)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_doc():
    with open(PLAN, encoding="utf-8") as fh:
        return json.load(fh)


def plan_rules():
    return alerts.rules_from_plan(plan_doc())


def master_doc():
    with open(MASTER, encoding="utf-8") as fh:
        return json.load(fh)


def _no_network(req, timeout=None):
    raise urllib.error.URLError("no network in tests")


class TempDir(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.d, "state")
        self.cfg = os.path.join(self.d, "config.json")      # absent unless a test writes it
        # clock and holidays refresh a stale holiday list on their own: never let a test reach NSE
        self._saved_net = (alerts._urlopen, alerts._sleep)
        alerts._urlopen, alerts._sleep = _no_network, lambda s: None
        # a default path (no --rules / --state / --write) must land in a scratch trader folder, never in the
        # repo's working folder or ~/.day-trader-india
        self._home = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": os.path.join(self.d, "trader-home")})
        self._home.start()

    def tearDown(self):
        self._home.stop()
        alerts._urlopen, alerts._sleep = self._saved_net
        shutil.rmtree(self.d)

    def cal(self):
        return ["--state-dir", self.state_dir, "--config", self.cfg]


class Clock(TempDir):
    def test_now_is_ist(self):
        self.assertEqual(ist("2026-11-13 10:05"), DT(2026, 11, 13, 4, 35))
        self.assertEqual(ist("2026-11-13T04:35Z"), DT(2026, 11, 13, 4, 35))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_phases(self):
        self.assertEqual(status("2026-11-13 08:30")["phase"], "before-open")
        self.assertEqual(status("2026-11-13 09:05")["phase"], "pre-open")
        self.assertTrue(status("2026-11-13 09:15")["open"])
        self.assertTrue(status("2026-11-13 15:29")["open"])
        after = status("2026-11-13 15:30")
        self.assertEqual((after["phase"], after["next_open"]), ("after-close", "2026-11-16 09:15"))

    def test_weekend_next_open(self):
        st = status("2026-11-14 11:00")
        self.assertEqual((st["phase"], st["reason"], st["next_open"]), ("closed-day", "Saturday", "2026-11-16 09:15"))
        self.assertEqual(status("2026-11-15 23:59")["next_open"], "2026-11-16 09:15")

    def test_guru_nanak_jayanti(self):
        st = status("2026-11-24 10:00")
        self.assertFalse(st["open"])
        self.assertEqual(st["reason"], "NSE holiday: " + GURU_NANAK)
        self.assertEqual(st["next_open"], "2026-11-25 09:15")
        self.assertEqual(status("2026-11-23 16:00")["next_open"], "2026-11-25 09:15")

    def test_clock_on_saturday(self):
        code, text, _ = run(["clock", "--now", "2026-11-14 11:00"] + self.cal())
        self.assertEqual(code, 1)
        self.assertIn("IST 2026-11-14 11:00 (Saturday): NSE closed, Saturday.", text)
        self.assertIn("Next open: Mon 2026-11-16 09:15 IST.", text)
        self.assertIn("Upcoming NSE holidays: Tue 2026-11-24 %s; Fri 2026-12-25 Christmas." % GURU_NANAK, text)
        self.assertIn("NSE's 2027 holiday list is not known here yet", text)

    def test_clock_open_pre_open_and_holiday(self):
        code, text, _ = run(["clock", "--now", "2026-11-13 10:05"] + self.cal())
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN, normal market.", text)
        self.assertIn("Today's session: 09:15 to 15:30 IST", text)
        code, text, _ = run(["clock", "--now", "2026-11-16 09:05"] + self.cal())
        self.assertEqual(code, 1)
        self.assertIn("NSE PRE-OPEN, orders 09:00 to 09:08, the normal market opens at 09:15.", text)
        code, text, _ = run(["clock", "--now", "2026-11-24 10:00"] + self.cal())
        self.assertIn("NSE closed, NSE holiday: %s." % GURU_NANAK, text)
        self.assertIn("Next open: Wed 2026-11-25 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2026-11-13T04:35Z", "--json"] + self.cal())
        doc = json.loads(text)
        self.assertEqual((doc["now_ist"], doc["phase"], doc["known_years"]), ("2026-11-13 10:05", "open", [2026]))
        self.assertEqual(doc["upcoming_holidays"][0], {"date": "2026-11-24", "weekday": "Tuesday", "name": GURU_NANAK})
        self.assertEqual(doc["unknown_years"], [2027])

    def test_an_unknown_2027(self):
        code, text, _ = run(["clock", "--now", "2027-01-04 10:00"] + self.cal())
        self.assertEqual(code, 0)                                # a weekday counts as a trading day ...
        self.assertIn("No known NSE holidays on a weekday in the next 60 days.", text)
        self.assertIn("NSE's 2027 holiday list is not known here yet", text)    # ... and it says so
        self.assertFalse(status("2027-01-04 10:00")["holidays_known"])

    def test_a_special_session(self):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"special_sessions": {"2026-11-08": {"open": "18:15", "close": "19:15",
                                                           "label": "Muhurat trading"}}}, fh)
        code, text, _ = run(["clock", "--now", "2026-11-08 18:30"] + self.cal())
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN, special session.", text)
        self.assertIn("Today's session: 18:15 to 19:15 IST (Muhurat trading).", text)


class Holidays(TempDir):
    def test_refresh_parses_holiday_master(self):
        cache, years = alerts.refresh_holidays(master_doc(), self.state_dir, ist("2026-11-14 11:00"), "test")
        self.assertEqual(years, [2026])
        self.assertEqual(sorted(cache), ["fetched_at", "holidays", "source", "years"])
        self.assertEqual((cache["fetched_at"], cache["source"], cache["years"]), ("2026-11-14T05:30:00Z", "test", [2026]))
        self.assertEqual(cache["holidays"], nc.NSE_HOLIDAYS)
        self.assertEqual(cache["holidays"]["2026-11-08"], "Diwali Laxmi Pujan")     # NSE's trailing * dropped
        self.assertEqual(cache["holidays"]["2026-11-24"], GURU_NANAK)
        with open(os.path.join(self.state_dir, "nse-holidays.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), cache)
        hol, known = nc.load_holidays(state_dir=self.state_dir)
        self.assertEqual(hol["2026-11-24"], GURU_NANAK)
        self.assertIn(2026, known)

    def test_a_refreshed_2027_list_merges_and_is_known(self):
        doc = master_doc()
        doc["CM"] = [dict(r, tradingDate=r["tradingDate"].replace("2026", "2027")) for r in doc["CM"]][:3]
        alerts.refresh_holidays(master_doc(), self.state_dir, ist("2026-11-14 11:00"))
        cache, years = alerts.refresh_holidays(doc, self.state_dir, ist("2026-12-20 11:00"))
        self.assertEqual((years, cache["years"]), ([2027], [2026, 2027]))
        self.assertEqual(len(cache["holidays"]), 23)                 # 2026 kept, three 2027 dates added
        self.assertEqual(cache["holidays"]["2027-01-26"], "Republic Day")
        code, text, _ = run(["holidays", "--year", "2027", "--now", "2026-12-20 11:00"] + self.cal())
        self.assertEqual(code, 0)
        self.assertIn("NSE trading holidays 2027 (capital market, fetched 2026-12-20 from NSE's holiday-master):", text)
        self.assertIn("Tue 2027-01-26  Republic Day", text)
        code, text, _ = run(["clock", "--now", "2027-01-04 10:00"] + self.cal())
        self.assertNotIn("not known", text)
        self.assertIn("Tue 2027-01-26 Republic Day", text)

    def test_refresh_from_the_cli(self):
        code, text, err = run(["holidays", "--refresh", "--offline", MASTER, "--now", "2026-11-14 11:00"] + self.cal())
        self.assertEqual(code, 0, err)
        self.assertIn("Fetched NSE's capital-market holiday list for 2026: 20 date(s).", text)
        self.assertIn("Tue 2026-11-24  %s" % GURU_NANAK, text)
        self.assertIn("Sun 2026-11-08  Diwali Laxmi Pujan  (a weekend: closed anyway)", text)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, nc.HOLIDAY_CACHE)))

    def test_the_built_in_list_without_a_refresh(self):
        code, text, _ = run(["holidays", "--now", "2026-11-14 11:00"] + self.cal())
        self.assertEqual(code, 0)
        self.assertIn("NSE trading holidays 2026 (capital market, built in", text)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("  ") and " 2026-" in l]), 20)

    def test_an_unknown_2027_is_said_plainly(self):
        code, text, _ = run(["holidays", "--year", "2027"] + self.cal())
        self.assertEqual(code, 1)
        self.assertIn("NSE's 2027 holiday list is not known here yet", text)
        self.assertIn("every weekday of 2027 counts as a trading day", text)
        code, text, _ = run(["holidays", "--year", "2027", "--json"] + self.cal())
        self.assertEqual(json.loads(text)["known"], False)

    def test_extra_holidays_alone_do_not_make_a_year_known(self):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"extra_holidays": {"2027-01-26": "Republic Day"}}, fh)
        code, text, _ = run(["holidays", "--year", "2027"] + self.cal())
        self.assertEqual(code, 1)
        self.assertIn("not known here yet", text)
        self.assertIn("Tue 2027-01-26  Republic Day  (from extra_holidays)", text)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"extra_holidays": {"2027-01-26": "Republic Day"}, "holiday_years": [2027]}, fh)
        code, text, _ = run(["holidays", "--year", "2027"] + self.cal())
        self.assertEqual(code, 0)
        self.assertIn("in your config", text)
        code, text, _ = run(["clock", "--now", "2027-01-26 10:00"] + self.cal())
        self.assertIn("NSE closed, NSE holiday: Republic Day.", text)
        self.assertNotIn("not known", text)

    def test_a_broken_holiday_list(self):
        for bad in ({}, {"CM": []}, {"CM": [{"tradingDate": "soon"}]}, []):
            with self.assertRaises(alerts.AlertError):
                alerts.refresh_holidays(bad, self.state_dir, ist("2026-11-14 11:00"))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, nc.HOLIDAY_CACHE)))


class FakeNSE(object):
    """NSE's API answers 403 to a cold script; the home page sets cookies; then the
    API answers, gzip-compressed."""

    def __init__(self, first=403, then=200, body=None):
        self.calls, self.sleeps = [], []
        self.first, self.then = first, then
        with open(MASTER, "rb") as fh:
            self.body = body if body is not None else gzip.compress(fh.read())

    def urlopen(self, req, timeout=None):
        self.calls.append(("plain", req.full_url, req.get_header("User-agent"), req.get_header("Referer")))
        if self.first != 200:
            raise urllib.error.HTTPError(req.full_url, self.first, "x", {}, io.BytesIO(b""))
        return io.BytesIO(self.body)

    def build_opener(self):
        outer = self

        class Opener(object):
            def open(self, req, timeout=None):
                outer.calls.append(("cookies", req.full_url, req.get_header("User-agent"), req.get_header("Referer")))
                if req.full_url == alerts.NSE_HOME:
                    return io.BytesIO(b"<html></html>")
                if outer.then != 200:
                    raise urllib.error.HTTPError(req.full_url, outer.then, "x", {}, io.BytesIO(b""))
                return io.BytesIO(outer.body)
        return Opener()


class HolidayNetwork(TempDir):
    def setUp(self):
        TempDir.setUp(self)
        self.saved = (alerts._urlopen, alerts._sleep, alerts._build_opener)

    def tearDown(self):
        alerts._urlopen, alerts._sleep, alerts._build_opener = self.saved
        TempDir.tearDown(self)

    def net(self, **kw):
        f = FakeNSE(**kw)
        alerts._urlopen, alerts._sleep, alerts._build_opener = f.urlopen, f.sleeps.append, f.build_opener
        return f

    def test_403_then_cookies_then_the_list(self):
        f = self.net()
        doc = alerts.fetch_holiday_master()
        self.assertEqual(len(doc["CM"]), 20)
        self.assertEqual([(c[0], c[1]) for c in f.calls], [("plain", alerts.HOLIDAY_URL), ("cookies", alerts.NSE_HOME),
                                                            ("cookies", alerts.HOLIDAY_URL)])
        self.assertTrue(all(c[2] == nc.UA and c[3] == "https://www.nseindia.com/" for c in f.calls))
        self.assertEqual(f.sleeps, [alerts.PAUSE])

    def test_a_straight_answer(self):
        f = self.net(first=200)
        code, text, err = run(["holidays", "--refresh", "--now", "2026-11-14 11:00"] + self.cal())
        self.assertEqual(code, 0, err)
        self.assertEqual(len(f.calls), 1)
        with open(os.path.join(self.state_dir, nc.HOLIDAY_CACHE), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["source"], alerts.HOLIDAY_URL)

    def test_failures_are_one_plain_line(self):
        self.net(first=403, then=503)
        code, _, err = run(["holidays", "--refresh"] + self.cal())
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "alerts.py: NSE answered HTTP 503 for its holiday list: try again in a few "
                                      "minutes, or add the dates to \"extra_holidays\" in trader/config.json")
        self.net(first=200, body=b"<html>Access Denied</html>")
        code, _, err = run(["holidays", "--refresh"] + self.cal())
        self.assertIn("other than JSON", err)
        self.assertNotIn("Traceback", err)


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), encoding="utf-8") as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE")
        self.assertEqual((q["symbol"], q["price"], q["prev_close"], q["open"]), ("RELIANCE", 1339.7, 1339.7, 1328.5))
        self.assertEqual((q["time"], q["currency"], q["source"]), ("2026-11-13T09:58:04Z", "INR", "yahoo"))
        with open(os.path.join(QDIR, "yahoo-TCS.json"), encoding="utf-8") as fh:
            q = alerts.parse_yahoo(fh.read(), "TCS")
        self.assertEqual((q["price"], q["prev_close"]), (2407.5, 2442.4))
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), encoding="utf-8") as fh:
            with self.assertRaises(alerts.AlertError) as cm:
                alerts.parse_yahoo(fh.read(), "TCS")
        self.assertIn("Yahoo sent RELIANCE.NS when asked for TCS.NS", str(cm.exception))

    def test_urls(self):
        self.assertEqual(alerts.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("M&M"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("TCS", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))           # Stooq is dropped

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "tcs.ns", "INFY", "HDFCBANK", "NOPE"], QDIR)
        self.assertEqual(sorted(q), ["HDFCBANK", "INFY", "RELIANCE", "TCS"])
        self.assertEqual((q["INFY"]["price"], q["HDFCBANK"]["price"]), (1141.5, 804.0))
        self.assertEqual(len(errors), 1)
        self.assertIn("yahoo-NOPE.json", errors[0])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-TCS.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-TCS.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["TCS"], d)
            self.assertEqual((q["TCS"]["price"], errors), (2407.5, []))
        finally:
            shutil.rmtree(d)

    def test_symbols(self):
        self.assertEqual(alerts.clean_symbols(["reliance", "NSE:TCS", "RELIANCE.NS", "m&m"]), ["RELIANCE", "TCS", "M&M"])
        for bad in ("^NSEI", "not a symbol"):
            with self.assertRaises(alerts.AlertError):
                alerts.clean_symbols([bad])


class FakeNet(object):
    """Stands in for urllib: answers each Yahoo request with the next status code."""

    def __init__(self, codes):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), encoding="utf-8") as fh:
            self.body = fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_shared_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1339.7, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", nc.UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1339.7)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "RELIANCE"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "TCS", "INFY", "HDFCBANK"], QDIR)
        self.now = ist("2026-11-13 10:53")
        self.open = status("2026-11-13 10:53")

    def at(self, text, rules, state=None, quotes=None, cfg=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, status(text, cfg), state or {}, now)

    def test_a_target_rings_once_and_says_cancel_the_stop(self):
        rules = [{"id": "rel-t", "symbol": "RELIANCE", "type": "target", "price": 1339.0}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "RELIANCE is ₹1,339.70: at or above your target ₹1,339.00. "
                                              "Kite has no OCO for MIS: cancel the stop, then sell.")
        self.assertEqual(alerts.evaluate(rules, self.q, self.open, st, self.now)[0], [])
        plain = [{"id": "rel-a", "symbol": "RELIANCE", "type": "above", "price": 1339.0}]
        msg = alerts.evaluate(plain, self.q, self.open, {}, self.now)[0][0]["message"]
        self.assertIn("at or above your alert level ₹1,339.00", msg)
        self.assertNotIn("OCO", msg)

    def test_stop_and_rearm(self):
        rules = [{"id": "hdfc-stop", "symbol": "HDFCBANK", "type": "stop", "price": 805, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("HDFCBANK is ₹804.00: at or below your stop ₹805.00", rings[0]["message"])
        self.assertIn("SL sell should trigger", rings[0]["message"])
        self.q["HDFCBANK"]["price"] = 806.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("hdfc-stop", st["rung"])
        self.q["HDFCBANK"]["price"] = 803.5
        self.assertEqual(len(alerts.evaluate(rules, self.q, self.open, st, self.now)[0]), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "tcs-3", "symbol": "TCS", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        down = [{"id": "tcs-1", "symbol": "TCS", "type": "pct_move", "pct": 1, "direction": "down"}]
        rings, st = alerts.evaluate(down, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "TCS moved -1.43% to ₹2,407.50 (from ₹2,442.40).")
        self.assertEqual(alerts.evaluate(down, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "tcs-up", "symbol": "TCS", "type": "pct_move", "pct": 1, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])

    def test_opening_bell_at_09_15_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-11-13 09:05", rules)[0], [])       # pre-open
        self.assertEqual(self.at("2026-11-13 09:14", rules)[0], [])
        rings, st = self.at("2026-11-13 09:15", rules)
        self.assertEqual(rings[0]["message"], "Opening bell: NSE is open (2026-11-13 09:15 IST), normal market "
                                              "09:15 to 15:30.")
        self.assertEqual(self.at("2026-11-13 11:00", rules, st)[0], [])
        self.assertEqual(self.at("2026-11-14 09:20", rules, st)[0], [])   # Saturday
        self.assertEqual(self.at("2026-11-24 09:20", rules, st)[0], [])   # Guru Nanak Jayanti
        self.assertEqual(len(self.at("2026-11-16 09:16", rules, st)[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by_15_10(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1328.8, 1344.4))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["before_close"]), ("15:10", "flat by", True))
        self.assertTrue(all(r["date"] == "2026-11-13" for r in rules))
        rings, st = self.at("2026-11-13 10:53", rules)                   # the real quote, 1,339.70
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1328.7
        rings, st = self.at("2026-11-13 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is ₹1,328.70: at or below your stop ₹1,328.80 (plan stop: opening-range "
                      "midpoint)", rings[0]["message"])
        q["RELIANCE"]["price"] = 1344.4
        rings = self.at("2026-11-13 12:00", rules, st, q)[0]
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-target"])
        self.assertIn("at or above your target ₹1,344.40 (plan target: 2R (two times the risk)). Kite has no OCO "
                      "for MIS: cancel the stop, then sell.", rings[0]["message"])

    def test_flat_by_defaults(self):
        self.assertEqual(alerts.rules_from_plan(dict(plan_doc(), flat_by=None))[2]["at"], "15:10")
        self.assertEqual(alerts.rules_from_plan(dict(plan_doc(), flat_by=None), "15:00")[2]["at"], "15:00")
        self.assertEqual(alerts.rules_from_plan(plan_doc(), "15:00")[2]["at"], "15:10")   # the plan wins

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-11-13 15:09", rules)[0], [])
        rings, st = self.at("2026-11-13 15:10", rules)
        self.assertEqual(len(rings), 1)
        msg = rings[0]["message"]
        self.assertIn("Flat by: it is 15:10 IST (set for 15:10), the market closes at 15:30.", msg)
        self.assertIn("cancel its stop, then sell: nse-broker's ticket.py flatten --symbol RELIANCE", msg)
        self.assertIn("Zerodha squares off MIS at 15:25", msg)
        self.assertEqual(st["rung"]["reliance-flat-by"], "2026-11-13T09:40:00Z")
        self.assertEqual(self.at("2026-11-13 15:12", rules, st)[0], [])
        rings, _ = self.at("2026-11-13 15:45", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-16 15:11", rules, st)[0], [])   # plan rules: their day only

    def test_a_time_alert_on_a_special_session(self):
        cfg = {"special_sessions": {"2026-11-08": {"open": "18:15", "close": "19:15", "label": "Muhurat trading"}}}
        rule = [{"id": "flat", "type": "time", "at": "15:10", "label": "flat by", "before_close": True}]
        self.assertEqual(self.at("2026-11-08 18:54", rule, cfg=cfg)[0], [])
        rings, _ = self.at("2026-11-08 18:55", rule, cfg=cfg)
        self.assertIn("set for 18:55), the market closes at 19:15", rings[0]["message"])
        self.assertEqual(self.at("2026-11-08 18:55", rule)[0], [])        # no session configured: a Sunday
        self.assertEqual(self.at("2026-11-24 15:11", rule)[0], [])        # NSE holiday

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "nse-alerts", "alerts.example.json"), encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")], plan_rules())

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "TCS"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "3pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1328.8})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan(dict(plan_doc(), flat_by="3:10pm"))


class Cli(TempDir):
    def setUp(self):
        TempDir.setUp(self)
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now] + self.cal())

    def test_the_worked_example_from_the_plan(self):
        # The rules are added at 10:50. The fixture is the whole day's real bars, which include a 10:10 high above
        # the target and a 09:20 low below the stop: bars from before the rules existed must not ring them.
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.cfg, "--now", "2026-11-13 10:50"])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertIn("cancel the stop, then sell", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--state", self.state, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-11-13 09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-13 10:53")
        self.assertEqual(code, 1)
        self.assertIn("NSE OPEN (2026-11-13 10:53 IST, normal market).", text)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,339.70.", text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1339.7})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-11-13T09:58:04Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-11-13T05:23:00Z")

        code, text, _ = self.check("2026-11-13 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by: it is 15:10 IST", text)
        self.assertIn("not an order", text)
        self.assertIn("no OCO for MIS", text)
        self.assertEqual(self.check("2026-11-13 15:12")[0], 1)
        code, text, _ = self.check("2026-11-16 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-16 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state, "--config", self.cfg])
        with open(self.rules, encoding="utf-8") as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop", "reliance-plan-target"])
        with open(self.state, encoding="utf-8") as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_check_on_a_holiday(self):
        run(["add", "--rules", self.rules, "--state", self.state, "--opening-bell"])
        run(["add", "--rules", self.rules, "--state", self.state, "--at", "15:10", "--label", "flat by", "--symbol", "RELIANCE"])
        code, text, _ = self.check("2026-11-24 15:15")
        self.assertEqual(code, 1)
        self.assertIn("NSE closed (2026-11-24 15:15 IST, NSE holiday: %s)." % GURU_NANAK, text)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--state", self.state, "--at", "15:10", "--label", "flat by", "--before-close",
                             "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--state", self.state, "--symbol", "RELIANCE", "--target", "1344.40",
                             "--date", "2026-11-13"])
        self.assertEqual(code, 0)
        self.assertIn("cancel the stop, then sell", text)
        with open(self.rules, encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by",
                                    "symbol": "RELIANCE", "before_close": True})
        self.assertEqual(rules[1], {"id": "reliance-target-1344.40", "symbol": "RELIANCE", "type": "target",
                                    "price": 1344.4, "date": "2026-11-13"})
        code, _, err = run(["add", "--rules", self.rules, "--state", self.state, "--symbol", "RELIANCE", "--target", "1344.40",
                            "--date", "2026-11-13"])
        self.assertIn("already exists", err)
        self.assertEqual(run(["add", "--rules", self.rules, "--state", self.state, "--at", "3pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--state", self.state, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["add", "--rules", self.rules, "--state", self.state, "--symbol", "^NSEI", "--above", "24000"])
        self.assertIn("not an NSE symbol", err)

    def test_list_and_reset(self):
        run(["add", "--rules", self.rules, "--state", self.state, "--opening-bell"])
        self.check("2026-11-13 09:20")
        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("(rang 2026-11-13T03:50:00Z)", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "tcs", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-11-14 11:00"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   ₹1,339.70 (+0.00% vs prev close ₹1,339.70)  2026-11-13 15:28 IST  [yahoo]",
                      text)
        self.assertIn("TCS        ₹2,407.50 (-1.43% vs prev close ₹2,442.40)", text)
        self.assertIn("delayed", text)
        self.assertNotIn("$", text)
        run(["quote", "INFY", "--offline", QDIR, "--write", self.prices])
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc), ["as_of", "detail", "note", "prices", "source"])     # the original's shape
        self.assertEqual(sorted(doc["prices"]), ["INFY", "RELIANCE", "TCS"])
        self.assertEqual(doc["detail"]["TCS"], {"prev_close": 2442.4, "time": "2026-11-13T09:58:09Z",
                                                "source": "yahoo", "fetched": "2026-11-14T05:30:00Z"})

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump({"cash": 0, "positions": [{"symbol": "TCS", "quantity": 1}, {"tradingsymbol": "INFY"}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["INFY", "RELIANCE", "TCS"])
        code, _, err = run(["quote"])
        self.assertEqual(code, 1)
        self.assertIn("which symbols", err)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR] + self.cal())
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)
        self.assertEqual(run([])[0], 1)


class Bars(unittest.TestCase):
    """Review item 10. The fixture is the day's real 5-minute RELIANCE bars: the last price (1,339.70) is
    below the 1,344.40 target and above the 1,328.80 stop, yet the 10:10 bar's high was 1,347.90 and the
    09:20 bar's low 1,324.30. A check that only compares the last price never rings either."""

    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE"], QDIR)
        self.target = [{"id": "t", "symbol": "RELIANCE", "type": "target", "price": 1344.4}]
        self.stop = [{"id": "s", "symbol": "RELIANCE", "type": "stop", "price": 1328.8}]

    def at(self, text, rules, state=None, price=None, quotes=None):
        q = json.loads(json.dumps(self.q if quotes is None else quotes))
        if price is not None:
            q["RELIANCE"]["price"] = price
        return alerts.evaluate(rules, q, status(text), state or {}, ist(text))

    def test_the_fixture_is_what_the_tests_say(self):
        bars = {b["t"]: b for b in self.q["RELIANCE"]["bars"]}
        self.assertEqual(self.q["RELIANCE"]["bar_seconds"], 300)
        self.assertEqual(len(bars), 74)                                     # the 15:20 hole is skipped
        self.assertAlmostEqual(bars["2026-11-13T04:40:00Z"]["high"], 1347.9)    # 10:10 IST
        self.assertAlmostEqual(bars["2026-11-13T03:50:00Z"]["low"], 1324.3)     # 09:20 IST
        self.assertEqual(sorted(bars["2026-11-13T04:40:00Z"]), ["high", "low", "t"])
        self.assertLess(self.q["RELIANCE"]["price"], 1344.4)

    def test_a_spike_through_the_target_between_checks_rings(self):
        rings, st = self.at("2026-11-13 10:00", self.target, price=1334.0)      # first check: last price only
        self.assertEqual(rings, [])
        self.assertEqual((st["quoted"]["RELIANCE"], st["armed"]["t"]), ("2026-11-13T04:30:00Z",) * 2)
        rings, st = self.at("2026-11-13 10:20", self.target, st)                # the last price is 1,339.70
        self.assertEqual(len(rings), 1)
        msg = rings[0]["message"]
        self.assertIn("RELIANCE traded at ₹1,347.90 in the 10:10 IST 5-minute bar (bar high, not the last price): "
                      "at or above your target ₹1,344.40.", msg)
        self.assertIn("The last price is ₹1,339.70", msg)
        self.assertIn("Kite has no OCO for MIS: cancel the stop, then sell.", msg)
        self.assertEqual((rings[0]["source"], rings[0]["bar_time"]), ("bar", "2026-11-13T04:40:00Z"))
        self.assertEqual(self.at("2026-11-13 10:40", self.target, st)[0], [])    # rings once
        self.assertIn("t", st["rung"])

    def test_a_spike_through_the_stop_between_checks_rings(self):
        rings, st = self.at("2026-11-13 09:16", self.stop)
        self.assertEqual(rings, [])
        rings, st = self.at("2026-11-13 09:31", self.stop, st)
        self.assertEqual(len(rings), 1)
        msg = rings[0]["message"]
        # the 09:15 bar (low 1,326.40) started before the rule did: the first bar that counts is 09:20
        self.assertIn("RELIANCE traded at ₹1,324.30 in the 09:20 IST 5-minute bar (bar low, not the last price): "
                      "at or below your stop ₹1,328.80.", msg)
        self.assertIn("SL sell should trigger", msg)
        self.assertEqual(self.at("2026-11-13 09:45", self.stop, st)[0], [])

    def test_the_last_price_still_rings_first_with_its_old_message(self):
        rings, _ = self.at("2026-11-13 10:53", self.target, price=1344.4)
        self.assertEqual(rings[0]["message"], "RELIANCE is ₹1,344.40: at or above your target ₹1,344.40. "
                                              "Kite has no OCO for MIS: cancel the stop, then sell.")
        self.assertNotIn("source", rings[0])

    def test_bars_from_before_the_rule_existed_never_ring(self):
        # the first check ever looks at the last price only
        self.assertEqual(self.at("2026-11-13 15:10", self.target + self.stop)[0], [])
        # armed at 10:50: the 10:10 spike and the 09:20 low are older than the rule
        state = {"armed": {"t": "2026-11-13T05:20:00Z", "s": "2026-11-13T05:20:00Z"},
                 "quoted": {"RELIANCE": "2026-11-13T03:46:00Z"}}
        self.assertEqual(self.at("2026-11-13 15:10", self.target + self.stop, state)[0], [])

    def test_a_bar_still_forming_is_never_used(self):
        state = {"armed": {"t": "2026-11-13T04:30:00Z"}, "quoted": {"RELIANCE": "2026-11-13T04:30:00Z"}}
        self.assertEqual(self.at("2026-11-13 10:12", self.target, state)[0], [])       # the 10:10 bar ends 10:15
        self.assertEqual(self.at("2026-11-13 10:14", self.target, state)[0], [])
        rings, _ = self.at("2026-11-13 10:15", self.target, state)
        self.assertIn("10:10 IST 5-minute bar", rings[0]["message"])

    def test_a_bar_the_previous_check_already_covered_is_not_looked_at_again(self):
        high = [{"id": "t", "symbol": "RELIANCE", "type": "target", "price": 1350.0}]      # only the 10:15 bar (1,350.50)
        armed = {"t": "2026-11-13T04:30:00Z"}
        seen = {"armed": armed, "quoted": {"RELIANCE": "2026-11-13T04:50:00Z"}}            # last quote 10:20: bar ended 10:20
        self.assertEqual(self.at("2026-11-13 10:40", high, seen)[0], [])
        missed = {"armed": armed, "quoted": {"RELIANCE": "2026-11-13T04:45:00Z"}}          # last quote 10:15: not yet seen
        rings, _ = self.at("2026-11-13 10:40", high, missed)
        self.assertIn("₹1,350.50 in the 10:15 IST 5-minute bar", rings[0]["message"])

    def test_a_failed_quote_does_not_move_the_window(self):
        state = {"armed": {"t": "2026-11-13T04:30:00Z"}, "quoted": {"RELIANCE": "2026-11-13T04:30:00Z"}}
        _, st = self.at("2026-11-13 10:20", [{"id": "t", "symbol": "RELIANCE", "type": "target", "price": 9999.0}],
                        state, quotes={})
        self.assertEqual(st["quoted"]["RELIANCE"], "2026-11-13T04:30:00Z")       # no quote, so no new "last looked"
        self.assertIsNotNone(self.at("2026-11-13 10:40", self.target, st)[0])

    def test_rearm_counts_only_bars_after_the_rearm(self):
        rule = [{"id": "t", "symbol": "RELIANCE", "type": "target", "price": 1350.0, "rearm": True}]
        _, st = self.at("2026-11-13 10:00", rule, price=1334.0)
        rings, st = self.at("2026-11-13 10:20", rule, st)               # the 10:15 bar's high
        self.assertEqual(len(rings), 1)
        rings, st = self.at("2026-11-13 10:40", rule, st)               # nothing reached 1,350 since: re-armed
        self.assertEqual((rings, st["rung"], st["armed"]["t"]), ([], {}, "2026-11-13T05:10:00Z"))
        self.assertEqual(self.at("2026-11-13 11:00", rule, st)[0], [])  # the old spike does not come back
        later = json.loads(json.dumps(self.q))
        later["RELIANCE"]["bars"].append({"t": "2026-11-13T05:35:00Z", "high": 1351.0, "low": 1339.0})   # 11:05 IST
        rings, _ = self.at("2026-11-13 11:15", rule, st, quotes=later)
        self.assertIn("₹1,351.00 in the 11:05 IST 5-minute bar", rings[0]["message"])

    def test_an_old_state_file_without_the_new_keys_still_works(self):
        rings, st = self.at("2026-11-13 10:53", self.target, {"rung": {}, "last_check": "2026-11-13T05:00:00Z"})
        self.assertEqual(rings, [])
        self.assertEqual(sorted(st), ["armed", "last_check", "quoted", "rung"])

    def test_parse_yahoo_keeps_the_bars_and_prices_json_does_not(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), encoding="utf-8") as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE")
        self.assertEqual((len(q["bars"]), q["bar_seconds"]), (74, 300))
        self.assertEqual(q["bars"][0]["t"], "2026-11-13T03:45:00Z")
        d = tempfile.mkdtemp()
        try:
            doc = alerts.write_prices(os.path.join(d, "prices.json"), {"RELIANCE": q}, ist("2026-11-13 10:00"))
            self.assertNotIn("bars", json.dumps(doc))
        finally:
            shutil.rmtree(d)


class BarsCli(Cli):
    def test_a_spike_between_two_check_runs(self):
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
             "--config", self.cfg, "--now", "2026-11-13 10:00"])
        with open(self.state, encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertEqual(set(st["armed"]), {"reliance-plan-stop", "reliance-plan-target", "reliance-flat-by"})
        self.assertEqual(st["armed"]["reliance-plan-stop"], "2026-11-13T04:30:00Z")
        code, text, _ = self.check("2026-11-13 10:05")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit.", text)
        code, text, _ = self.check("2026-11-13 10:40")         # the last price is 1,339.70: below the target
        self.assertEqual(code, 0)
        self.assertIn("ALERT RELIANCE traded at ₹1,347.90 in the 10:10 IST 5-minute bar (bar high, not the last "
                      "price)", text)
        self.assertNotIn("your stop", text)
        self.assertEqual(self.check("2026-11-13 10:45")[0], 1)    # once only
        with open(self.state, encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertEqual((st["quoted"]["RELIANCE"], "reliance-plan-target" in st["rung"]),
                         ("2026-11-13T05:15:00Z", True))

    def test_json_says_it_was_a_bar(self):
        run(["add", "--rules", self.rules, "--state", self.state, "--symbol", "RELIANCE", "--above", "1344.4", "--now", "2026-11-13 10:00"])
        self.check("2026-11-13 10:05")
        code, text, _ = run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                             "--offline", QDIR, "--now", "2026-11-13 10:40", "--json"] + self.cal())
        ring = json.loads(text)["rings"][0]
        self.assertEqual((code, ring["source"], ring["bar_time"], ring["bar_price"]),
                         (0, "bar", "2026-11-13T04:40:00Z", 1347.9))

    def test_reset_starts_counting_again_from_now(self):
        run(["add", "--rules", self.rules, "--state", self.state, "--symbol", "RELIANCE", "--target", "1344.4", "--now", "2026-11-13 10:00"])
        self.check("2026-11-13 10:05")
        self.assertEqual(self.check("2026-11-13 10:40")[0], 0)
        self.assertEqual(run(["reset", "reliance-target-1344.40", "--state", self.state, "--now", "2026-11-13 10:45"])[0], 0)
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["armed"]["reliance-target-1344.40"], "2026-11-13T05:15:00Z")
        self.assertEqual(self.check("2026-11-13 10:50")[0], 1)    # the 10:10 spike is not counted again


class HolidayRefresh(HolidayNetwork):
    """Review item 25: alerts.py shares nse_common's holiday code and refreshes a stale list on its own."""

    def master_2027(self):
        doc = master_doc()
        doc["CM"] = [dict(r, tradingDate=r["tradingDate"].replace("2026", "2027")) for r in doc["CM"]]
        return gzip.compress(json.dumps(doc).encode("utf-8"))

    def cache_at(self, text):
        alerts.refresh_holidays(master_doc(), self.state_dir, ist(text))

    def test_the_functions_are_thin_wrappers_over_nse_common(self):
        with mock.patch.object(nc, "fetch_holiday_master", side_effect=nc.HolidayError("NSE answered HTTP 500 "
                                                                                    "for its holiday list")) as m:
            with self.assertRaises(alerts.AlertError) as cm:
                alerts.fetch_holiday_master()
        self.assertEqual(m.call_count, 1)
        self.assertIn("NSE answered HTTP 500 for its holiday list", str(cm.exception))
        with mock.patch.object(nc, "update_holiday_cache", return_value=({"years": [2026]}, [2026])) as m:
            self.assertEqual(alerts.refresh_holidays({"CM": []}, "/x", ist("2026-11-14 11:00"), "src"),
                             ({"years": [2026]}, [2026]))
        self.assertEqual(m.call_args[0][1:], ("/x", ist("2026-11-14 11:00"), "src"))
        with mock.patch.object(nc, "update_holiday_cache", side_effect=nc.HolidayError("bad list")):
            with self.assertRaises(alerts.AlertError):
                alerts.refresh_holidays({}, "/x", ist("2026-11-14 11:00"))

    def test_if_stale_is_silent_when_the_list_is_fresh(self):
        self.cache_at("2026-11-14 11:00")
        f = self.net(first=200)
        code, text, err = run(["holidays", "--refresh", "--if-stale", "--now", "2026-11-20 10:00"] + self.cal())
        self.assertEqual((code, text, err, f.calls), (0, "", "", []))

    def test_if_stale_refreshes_an_old_cache_in_one_line(self):
        self.cache_at("2026-09-01 10:00")
        f = self.net(first=200)
        code, text, err = run(["holidays", "--refresh", "--if-stale", "--now", "2026-11-14 11:00"] + self.cal())
        self.assertEqual((code, err, len(f.calls)), (0, "", 1))
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("Refreshed NSE's holiday list (the holiday cache is older than 30 days)", text)
        with open(os.path.join(self.state_dir, nc.HOLIDAY_CACHE), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["fetched_at"], "2026-11-14T05:30:00Z")

    def test_if_stale_exits_0_when_the_network_fails(self):
        self.cache_at("2026-09-01 10:00")
        self.net(first=403, then=503)
        code, text, err = run(["holidays", "--refresh", "--if-stale", "--now", "2026-11-14 11:00"] + self.cal())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("Could not refresh NSE's holiday list (the holiday cache is older than 30 days): NSE answered "
                      "HTTP 503", text)
        code, text, _ = run(["holidays", "--refresh", "--if-stale", "--now", "2027-01-04 11:00"] + self.cal())
        self.assertEqual(code, 0)                           # 2027 is unknown: stale, the fetch fails again, still 0

    def test_if_stale_in_december_when_next_years_list_is_missing(self):
        self.cache_at("2026-12-01 10:00")
        f = self.net(first=200, body=self.master_2027())
        code, text, _ = run(["holidays", "--refresh", "--if-stale", "--now", "2026-12-10 11:00"] + self.cal())
        self.assertEqual((code, len(f.calls)), (0, 1))
        self.assertIn("NSE's 2027 holiday list is not loaded yet", text)
        self.assertEqual(nc.load_holidays(state_dir=self.state_dir)[1], {2026, 2027})

    def test_clock_refreshes_an_unknown_year_on_its_own(self):
        f = self.net(first=200, body=self.master_2027())
        code, text, err = run(["clock", "--now", "2027-01-04 10:00"] + self.cal())
        self.assertEqual((code, err, len(f.calls)), (0, "", 1))
        self.assertIn("Holiday list: refreshed NSE's holiday list (NSE's 2027 holiday list is not loaded).", text)
        self.assertNotIn("not known here yet", text)
        self.assertIn("Tue 2027-01-26 Republic Day", text)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, nc.HOLIDAY_CACHE)))

    def test_clock_and_holidays_survive_a_failed_refresh(self):
        code, text, err = run(["clock", "--now", "2027-01-04 10:00"] + self.cal())        # _no_network from TempDir
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Holiday list: could not refresh NSE's holiday list (NSE's 2027 holiday list is not loaded)", text)
        self.assertIn("NSE's 2027 holiday list is not known here yet", text)
        code, text, err = run(["holidays", "--year", "2027", "--now", "2027-01-04 10:00"] + self.cal())
        self.assertEqual((code, err), (1, ""))
        self.assertIn("could not refresh NSE's holiday list", text)
        self.assertNotIn("Traceback", text + err)

    def test_holidays_refreshes_an_unknown_year_and_shows_it(self):
        self.net(first=200, body=self.master_2027())
        code, text, _ = run(["holidays", "--year", "2027", "--now", "2027-01-04 10:00"] + self.cal())
        self.assertEqual(code, 0)
        self.assertIn("NSE trading holidays 2027 (capital market, fetched 2027-01-04 from NSE's holiday-master):", text)

    def test_a_fresh_known_year_does_not_touch_the_network(self):
        f = self.net(first=200)
        for argv in (["clock", "--now", "2026-11-14 11:00"], ["holidays", "--now", "2026-11-14 11:00"]):
            code, text, _ = run(argv + self.cal())
            self.assertNotIn("Holiday list:", text)
        self.assertEqual(f.calls, [])


class TraderHome(TempDir):
    """Review item 8: every default path is in the trader folder, and the config used is named."""

    def test_defaults_follow_the_trader_home_not_the_cwd(self):
        home = os.path.join(self.d, "home")
        os.makedirs(home)
        elsewhere = os.path.join(self.d, "elsewhere")
        os.makedirs(elsewhere)
        old = os.getcwd()
        try:
            os.chdir(elsewhere)
            with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": home}):
                self.assertEqual(run(["add", "--symbol", "RELIANCE", "--above", "1300"])[0], 0)
                self.assertTrue(os.path.isfile(os.path.join(home, "alerts.json")))
                self.assertTrue(os.path.isfile(os.path.join(home, "state", "alerts.json")))   # the rule's armed stamp
                code, text, _ = run(["check", "--offline", QDIR, "--now", "2026-11-13 10:53"])
                self.assertEqual(code, 0)
                self.assertTrue(os.path.isfile(os.path.join(home, "prices.json")))
                self.assertIn("ALERT RELIANCE is ₹1,339.70", text)
                self.assertEqual(run(["quote", "TCS", "--offline", QDIR, "--write"])[0], 0)
                with open(os.path.join(home, "prices.json"), encoding="utf-8") as fh:
                    doc = json.load(fh)
                self.assertEqual(sorted(doc["prices"]), ["RELIANCE", "TCS"])
                self.assertIn("fetched", doc["detail"]["TCS"])
                self.assertIn("time", doc["detail"]["TCS"])
                self.assertEqual(os.listdir(elsewhere), [])             # nothing was written in the cwd
        finally:
            os.chdir(old)

    def test_it_says_which_config_it_used(self):
        home = os.path.join(self.d, "home")
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": home}):
            code, text, _ = run(["clock", "--now", "2026-11-13 10:05"])
            self.assertIn("Config: none at %s, so the built-in defaults are in use." % os.path.join(home, "config.json"),
                          text)
            with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as fh:
                json.dump({"flat_by": "15:00"}, fh)
            code, text, _ = run(["clock", "--now", "2026-11-13 10:05"])
            self.assertIn("Config: %s." % os.path.join(home, "config.json"), text)
            self.assertEqual(json.loads(run(["clock", "--now", "2026-11-13 10:05", "--json"])[1])["config"],
                             os.path.join(home, "config.json"))
            code, text, _ = run(["holidays", "--now", "2026-11-13 10:05"])
            self.assertIn("Config: %s." % os.path.join(home, "config.json"), text)
            self.assertEqual(run(["add", "--opening-bell"])[0], 0)
            code, text, _ = run(["check", "--offline", QDIR, "--now", "2026-11-13 09:20"])
            self.assertIn("Config: %s." % os.path.join(home, "config.json"), text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-alerts", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-alerts\ndescription: >-\n"))
        head = text.split("---")[1]
        desc = " ".join(head.split("description: >-")[1].split("argument-hint:")[0].split())
        self.assertGreater(len(desc), 200)
        self.assertLess(len(desc), 600)                   # review item 27: long descriptions
        for phrase in ("alert me when reliance hits", "watch my stop", "is the nse open", "nse holidays"):
            self.assertIn(phrase, desc.lower())           # the trigger phrases a user says survive the trim
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("not financial advice", "**yes**", "${claude_skill_dir}", "cancel the stop, then sell",
                     "holidays --refresh", "09:15", "15:10", "guru nanak", "nse-holidays.json", "kite", ".ns",
                     "holidays --refresh --if-stale", "alerts.py quote symbol --write", "nse-risk",
                     "more than 5 minutes old", "5-minute bar", "$nse_trader_home", "built-in defaults"):
            self.assertIn(must, low)
        for never in ("—", "stooq", "nyse", "new york", "dollar"):
            self.assertNotIn(never, low)
        # no leftover names from the US edition (/trader-exit, trader-risk): "day-trader-india" is this repo
        self.assertIsNone(re.search(r"(?<!day-)trader-", low))


if __name__ == "__main__":
    unittest.main()
