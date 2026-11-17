"""nse-entry tests. Offline, on real Yahoo charts in tests/fixtures/nse-entry/:

RELIANCE-2026-11-13-5m.json  RELIANCE.NS, Fri 13 Nov 2026, 75 five-minute bars 09:15-15:25 IST
                             (Yahoo v8, trimmed to the day; the 15:20 bar is null, the 09:15 bar
                             has volume 0, meta.regularMarketTime is 15:28:04 IST).
RELIANCE-2026-11-13-1m.json  the same day in 1-minute bars (13 nulls after 15:15).

The worked example (PORTING.md): opening range 09:15-09:30 from 1,324.30 to 1,333.40; the 10:00
bar closed at 1,334.00, the first close above the high, so it triggered at 10:05.
"""

import copy
import datetime
import decimal
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX, ROOT

entry = load("nse-entry", "entry")
nc = entry.nc

EDIR = os.path.join(FIX, "nse-entry")
R5 = os.path.join(EDIR, "RELIANCE-2026-11-13-5m.json")
R1 = os.path.join(EDIR, "RELIANCE-2026-11-13-1m.json")
OPEN_TS = 1794541500       # 2026-11-13 09:15 IST = 03:45 UTC
DT = datetime.datetime
D = datetime.date
FRI = D(2026, 11, 13)


def ist(text):
    return entry.parse_now(text)


def doc(path=R5):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="RELIANCE"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day, open_hhmm="09:15"):
    """The same session moved to another date (and open time): IST has no daylight saving."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((nc.to_utc(DT.combine(day, nc.parse_hhmm(open_hhmm))) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 1,333.40 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        for k, cap in (("close", 1333.0), ("high", 1333.3), ("open", 1333.0)):
            if q[k][i] is not None:
                q[k][i] = min(q[k][i], cap)
    return d


def live_cut(d, last_bar, tick_at, tick_price=1342.0):
    """The file as Yahoo served it live: bars up to `last_bar` (IST HH:MM, still in progress)
    and the live tick at `tick_at` (IST HH:MM:SS), which is also meta.regularMarketTime."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    end = int((nc.to_utc(DT.combine(D(2026, 11, 13), nc.parse_hhmm(last_bar))) - DT(1970, 1, 1)).total_seconds())
    keep = [i for i, t in enumerate(r["timestamp"]) if t <= end]
    r["timestamp"] = [r["timestamp"][i] for i in keep]
    q = r["indicators"]["quote"][0]
    for k in q:
        q[k] = [q[k][i] for i in keep]
    tick = int((nc.to_utc(DT.strptime("2026-11-13 " + tick_at, "%Y-%m-%d %H:%M:%S")) - DT(1970, 1, 1)).total_seconds())
    r["timestamp"].append(tick)
    for k in q:
        q[k].append(0 if k == "volume" else tick_price)
    r["meta"]["regularMarketTime"] = tick
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheWorkedExample(unittest.TestCase):
    def test_triggers_at_1005_on_the_1000_bar(self):
        r = at("2026-11-13 10:05")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1333.4)
        self.assertEqual(r["opening_range"]["low"], 1324.3)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "10:05")
        self.assertEqual(t["bar_start"], "10:00")
        self.assertEqual(t["bar_index"], 9)                # 09:15 is bar 0
        self.assertEqual(t["price"], 1334.0)
        self.assertEqual(t["close"], 1334.0)
        self.assertEqual(t["pct_over"], 0.04)
        self.assertEqual(r["level"], 1333.4)
        self.assertEqual((r["exchange"], r["tick"]), ("NSE", "0.10"))
        text = "\n".join(entry.describe(r))
        self.assertIn("TRIGGERED 10:05 at ₹1,334.00: the 10:00 bar (bar 9) closed above the range high "
                      "₹1,333.40 (+0.04%).", text)
        self.assertIn("This is the moment to get in, if you take the trade. Nothing was placed.", text)
        self.assertIn("/nse-exit", text)
        self.assertNotIn("$", text)

    def test_the_rest_of_the_example(self):
        # the opening-range-midpoint stop, 1,328.85, rounds DOWN to the ₹0.10 tick: 1,328.80
        r = at("2026-11-13 10:05")
        mid = (decimal.Decimal(str(r["opening_range"]["high"])) + decimal.Decimal(str(r["opening_range"]["low"]))) / 2
        self.assertEqual(mid, decimal.Decimal("1328.85"))
        self.assertEqual(nc.round_to_tick(mid, nc.tick_size(r["trigger"]["price"]), "down"), decimal.Decimal("1328.80"))
        # later that day it traded up to 1,350.50 and closed at 1,339.70
        bars = chart()["bars"]
        self.assertEqual(round(max(b["h"] for b in bars if b["t"] >= OPEN_TS + 10 * 300), 2), 1350.5)
        self.assertEqual(round(bars[-1]["c"], 2), 1339.7)

    def test_at_1004_the_1000_bar_is_still_forming(self):
        r = at("2026-11-13 10:04")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:55")
        self.assertEqual(r["last"]["close"], 1331.6)
        self.assertEqual(r["forming"], {"bar_start": "10:00", "closes": "10:05", "price": 1334.0})
        self.assertIn("₹1.80 under the range high ₹1,333.40", r["reason"])
        self.assertEqual(at("2026-11-13 10:04:59")["status"], "waiting")
        text = "\n".join(entry.describe(r))
        self.assertIn("The 10:00 bar is still forming (₹1,334.00 so far, closes 10:05)", text)

    def test_before_the_open_pre_open_and_while_the_range_forms(self):
        r = at("2026-11-13 08:30")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "before-open"))
        self.assertIn("opens at 09:15 IST (pre-open from 09:00)", r["reason"])
        r = at("2026-11-13 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("pre-open call auction (09:00-09:15 IST, orders until 09:08)", r["reason"])
        self.assertTrue(entry.describe(r)[1].startswith("PRE-OPEN  "))
        r = at("2026-11-13 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-11-13 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        d = live_cut(doc(), "10:40", "10:44:45")
        c = chart(d)
        self.assertEqual(nc.ist_from_epoch(c["bars"][-1]["t"]), DT(2026, 11, 13, 10, 44, 45))
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))     # read as of the file's own time
        self.assertEqual(r["now"], "2026-11-13 10:44:45")
        self.assertEqual(r["_bars"][-1]["slot"], 16)               # the 10:35 bar
        self.assertEqual(r["forming"]["bar_start"], "10:40")
        # 10:45: closed by the clock, but Yahoo's last trade (10:44:45) is before its close
        r = at("2026-11-13 10:45", d)
        self.assertEqual(r["_bars"][-1]["slot"], 16)
        self.assertIn("Yahoo's last trade is 10:44:45", r["lag_note"])
        # a minute later with no newer trade it counts; the 10:44:45 tick never becomes a bar
        r = at("2026-11-13 10:46", d)
        self.assertEqual(r["_bars"][-1]["slot"], 17)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-11-13 15:45")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:05")
        text = "\n".join(entry.describe(r))
        self.assertIn("340 minutes ago", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        c = chart()
        self.assertEqual(c["bars"][0]["v"], 0)                  # Yahoo's 09:15 bar: volume 0
        r = at("2026-11-13 14:30", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 410269)  # (686,067 + 134,471) / 2, the 0 left out
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"], t["volume_x"]), ("10:20", 12, 1343.6, 1.27))
        r = at("2026-11-13 10:06", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("₹1,334.00 but on 0.33x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-11-13 10:30", setup="level", level=1340)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("10:10", 10, 1341.0))
        # a level under the open: the first eligible close is the 09:25 bar's, at 09:30 (no_entries_before)
        r = at("2026-11-13 10:30", setup="level", level=1300)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("09:30", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-11-13 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_1430(self):
        d = no_breakout()
        self.assertEqual(at("2026-11-13 10:45", d)["status"], "waiting")
        self.assertEqual(at("2026-11-13 14:29", d)["status"], "waiting")
        r = at("2026-11-13 14:30", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("by 14:30", r["reason"])
        self.assertEqual(at("2026-11-13 16:00", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = live_cut(no_breakout(), "10:40", "10:44:40")
        cfg = dict(nc.DEFAULT_CONFIG, no_entries_before="09:30", no_entries_after="10:45")
        r = at("2026-11-13 10:45:10", d, cfg=cfg)                # the 10:40 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:44:40", r["lag_note"])
        self.assertEqual(at("2026-11-13 10:46", d, cfg=cfg)["status"], "expired")

    def test_config_window(self):
        cfg = dict(nc.DEFAULT_CONFIG, no_entries_after="10:00")
        self.assertEqual(at("2026-11-13 10:05", cfg=cfg)["status"], "expired")
        cfg = dict(nc.DEFAULT_CONFIG, no_entries_before="10:10")
        r = at("2026-11-13 10:30", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "10:10")          # the 10:05 bar, first to close at or after 10:10
        r = at("2026-11-13 10:06", cfg=cfg)
        self.assertIn("no entries before 10:10", r["reason"])

    def test_bad_inputs_and_ist_clock(self):
        with self.assertRaises(entry.EntryError):
            at("2026-11-13 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-11-13 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-11-13 10:05"), DT(2026, 11, 13, 4, 35))
        self.assertEqual(entry.parse_now("2026-11-13T04:35:00Z"), DT(2026, 11, 13, 4, 35))
        self.assertEqual(entry.parse_now("2026-11-13 10:05 IST"), DT(2026, 11, 13, 4, 35))
        self.assertEqual(entry.parse_now("2026-11-13 10:05+05:30"), DT(2026, 11, 13, 4, 35))
        real = entry.utc_now
        entry.utc_now = lambda: DT(2026, 11, 13, 6, 0)            # 11:30 IST
        try:
            self.assertEqual(entry.parse_now("10:05"), DT(2026, 11, 13, 4, 35))
            self.assertEqual(entry.parse_now("9:30"), DT(2026, 11, 13, 4, 0))
        finally:
            entry.utc_now = real

    def test_or_minutes_30(self):
        r = at("2026-11-13 10:30", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1333.9)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("10:05", 9))


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(R1)), "RELIANCE")

    def test_the_same_day_on_one_minute_bars(self):
        r = entry.evaluate(self.c(), ist("2026-11-13 10:30"), interval="1m")
        self.assertEqual(r["opening_range"], {"high": 1333.4, "low": 1324.3, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 87636})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:46", "09:45", 30, 1333.7))
        r = entry.evaluate(self.c(), ist("2026-11-13 09:45:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:45")

    def test_null_bars_and_volume(self):
        r = entry.evaluate(self.c(), ist("2026-11-13 10:30"), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["volume_x"]),
                         ("10:16", 60, 3.53))
        r = entry.evaluate(self.c(), ist("2026-11-13 15:35"), interval="1m")
        slots = [b["slot"] for b in r["_bars"]]
        self.assertEqual(r["bars_closed"], 362)                  # 375 minus the 13 null bars after 15:15
        self.assertNotIn(361, slots)                              # 15:16 came as nulls
        self.assertEqual(slots[-1], 373)                          # the 15:28 bar

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "RELIANCE", "--offline", R1])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_ist_has_no_daylight_saving(self):
        for day in (D(2026, 11, 16), D(2026, 3, 9), D(2026, 7, 1), D(2026, 12, 31)):
            d = shift_to(doc(), day)
            r = entry.evaluate(chart(d), ist("%s 10:05" % day))
            self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["bar_index"]), ("triggered", "10:05", 9))
            self.assertEqual(entry.evaluate(chart(d), ist("%s 10:04" % day))["status"], "waiting")
        self.assertEqual(ist("2026-12-31 10:05"), DT(2026, 12, 31, 4, 35))

    def test_weekend_and_nse_holidays(self):
        r = at("2026-11-14 10:30")
        self.assertEqual((r["status"], r["phase"], r["reason"]), ("no-data", "closed",
                                                                  "the NSE is closed today (Saturday)"))
        self.assertEqual(r["next_open"], "2026-11-16 09:15")
        r = at("2026-11-24 10:30")
        self.assertEqual(r["reason"], "the NSE is closed today (NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev)")
        self.assertEqual(r["next_open"], "2026-11-25 09:15")
        r = at("2026-11-10 10:30")
        self.assertIn("Diwali-Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11 09:15")
        r = at("2026-11-13 10:30", cfg=dict(nc.DEFAULT_CONFIG, extra_holidays={"2026-11-13": "test closure"}),
               holidays=dict(nc.NSE_HOLIDAYS, **{"2026-11-13": "test closure"}), years={2026})
        self.assertIn("test closure", r["reason"])

    def test_a_year_without_a_holiday_list(self):
        d = shift_to(doc(), D(2027, 1, 5))
        r = entry.evaluate(chart(d), ist("2027-01-05 10:05"))
        self.assertEqual(r["status"], "triggered")               # weekdays are trading days
        self.assertFalse(r["calendar_known"])
        self.assertIn("NSE's 2027 holiday list is not loaded", "\n".join(entry.describe(r)))
        self.assertTrue(at("2026-11-13 10:05")["calendar_known"])

    def test_muhurat_trading_from_config(self):
        cfg = dict(nc.DEFAULT_CONFIG, special_sessions={"2026-11-08": {"open": "18:15", "close": "19:15",
                                                                       "label": "Muhurat trading"}})
        d = shift_to(doc(), D(2026, 11, 8), "18:15")
        r = entry.evaluate(chart(d), ist("2026-11-08 18:45"))
        self.assertEqual((r["status"], r["reason"]), ("no-data", "the NSE is closed today (Sunday)"))
        # the default window (09:30-14:30) kept at its distance from the open and close leaves nothing
        r = entry.evaluate(chart(d), ist("2026-11-08 18:45"), cfg=cfg)
        self.assertEqual((r["opens"], r["closes"], r["no_entries_before"], r["no_entries_after"]),
                         ("18:15", "19:15", "18:30", "18:15"))
        self.assertEqual(r["status"], "expired")
        self.assertIn("(Muhurat trading, 18:15-19:15)", r["reason"])
        cfg["no_entries_after"] = "15:30"
        r = entry.evaluate(chart(d), ist("2026-11-08 19:10"), cfg=cfg)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("19:05", 9))
        self.assertIn("Special session today: Muhurat trading, 18:15-19:15 IST", "\n".join(entry.describe(r)))
        r = entry.evaluate(chart(d), ist("2026-11-08 18:10"), cfg=cfg)
        self.assertEqual(r["phase"], "before-open")

    def test_empty_data(self):
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-11-13 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-16 10:15")                                # Yahoo sent Friday's bars only
        self.assertEqual(r["status"], "no-data")

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(R5) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes, **kw):
        seq = list(codes)
        body = kw.get("body", self.body)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", FRI, today=FRI)
        self.assertEqual(len(c["bars"]), 74)                      # 75 slots, the 15:20 bar null
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d"
                                      "&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("RELIANCE", "5m", FRI, today=D(2026, 11, 14))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])
        self.assertIn("range=5d", self.calls[0])                 # Friday's bars, asked on Saturday

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", FRI, today=FRI)
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_symbols_with_an_ampersand(self):
        m = doc()
        m["chart"]["result"][0]["meta"]["symbol"] = "M&M.NS"
        self.answers(200, body=json.dumps(m))
        c = entry.get_chart("m&m", "5m", FRI, today=FRI)
        self.assertEqual(c["symbol"], "M&M")
        self.assertEqual(self.calls[0], "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d"
                                        "&interval=5m")

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn("Traceback", err)

    def test_no_network_is_one_plain_error(self):
        def fake(url, timeout=20):
            raise urllib.error.URLError("nodename nor servname provided")
        entry._open_url = fake
        code, _, err = run(["check", "RELIANCE", "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("could not connect", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", FRI, today=FRI)
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = D(2026, 11, 16)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(FRI, today, "5m"), "5d")
        self.assertEqual(entry.chart_range(D(2026, 11, 2), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(D(2026, 9, 25), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(D(2026, 11, 2), today, "1m")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(D(2026, 8, 1), today, "5m")


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1,335.00 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 1335.00, max(b["h"], 1335.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 1335.0, "h": 1335.0, "l": 1335.0, "c": 1335.0, "v": 0})
        return {"symbol": "RELIANCE", "meta": {}, "bars": bars, "prev_close": 1339.7, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def times(self, calls):
        return [nc.to_ist(c).strftime("%H:%M:%S") for c in calls]

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-11-13 09:57", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual((fired[0][0]["trigger"]["time"], fired[0][0]["trigger"]["price"]), ("10:05", 1334.0))
        # it looked at 09:57 (the 09:55 bar spiking to 1,335 in progress) and at 10:00:02 (it closed 1,331.60)
        self.assertEqual(self.times(calls), ["09:57:00", "10:00:02", "10:05:02"])
        text = "\n".join(lines)
        self.assertIn("09:55 bar  closed ₹1,331.60  ₹1.80 under ₹1,333.40", text)
        self.assertIn("10:00 bar  closed ₹1,334.00  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=ist("2026-11-13 10:05:30"),
                                                                 calls=calls), "2026-11-13 10:02", every=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.times(calls), ["10:02:00", "10:05:02", "10:05:32"])  # 10:05:02: Yahoo at 10:04:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:05")

    def test_from_the_pre_open_it_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-11-13 09:00", every=30)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "09:00 IST  pre-open call auction: watching from the first bar's close at 09:20.")
        text = "\n".join(lines)
        self.assertIn("09:25 bar  closed ₹1,329.40  opening range set: high ₹1,333.40, low ₹1,324.30", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 10)    # 09:15 ... 10:00, one line each
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-13 08:00", every=30)
        self.assertIn("not open yet", lines[0])

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-11-13 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.times(calls)[:2], ["09:31:00", "09:49:30"])   # straight after waking
        text = "\n".join(lines)
        for start in ("09:30", "09:35", "09:40", "09:45"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:05")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-13 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(nc.to_ist(clock.now()).strftime("%H:%M"), "14:30")
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-13 10:30", every=300,
                                                   until=DT(2026, 11, 13, 11, 0))
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-14 10:00")
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "2026-11-14: the NSE is closed today (Saturday). Nothing to watch.")
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-24 10:00")
        self.assertIn("Prakash Gurpurb", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-11-13 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:05 at ₹1,334.00", text)
        self.assertIn("/nse-exit", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "nse-entry", "exchange": "NSE", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
                "date": "2026-11-13", "status": "triggered", "time": "10:05", "bar_start": "10:00", "bar_index": 9,
                "price": 1334.0, "level": 1333.4, "interval": "5m",
                "opening_range": {"high": 1333.4, "low": 1324.3, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-11-13T04:35:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)
        # the same moment given in UTC
        code, text2, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13T04:35:00Z"])
        # the Wrote line names the real file in the trader folder (not a path relative to wherever you stand)
        wrote = "Wrote %s (status triggered).\n" % os.path.join(os.getcwd(), "trader", "entry.json")
        self.assertIn(wrote, text)
        self.assertEqual(text2, text.replace(wrote, ""))

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:04", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:04", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_closed_market_paths(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-14 11:00"])
        self.assertEqual(code, 1)
        self.assertIn("MARKET CLOSED  the NSE is closed today (Saturday).", text)
        self.assertIn("Next open: 2026-11-16 09:15 IST.", text)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-24 11:00"])
        self.assertIn("NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev", text)
        self.assertIn("Next open: 2026-11-25 09:15 IST.", text)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 09:05"])
        self.assertIn("PRE-OPEN  pre-open call auction", text)

    def test_holidays_from_config_and_the_nse_alerts_cache(self):
        os.makedirs(os.path.join("trader", "state"))
        nc.write_json(os.path.join("trader", "state", nc.HOLIDAY_CACHE),
                      {"holidays": {"2027-01-26": "Republic Day"}, "years": [2027]})
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"extra_holidays": {"2027-03-22": "Holi"}}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2027-01-26 10:30"])
        self.assertIn("NSE holiday: Republic Day", text)
        self.assertIn("Next open: 2027-01-27 09:15 IST.", text)
        self.assertNotIn("holiday list is not loaded", text)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2027-03-22 10:30"])
        self.assertIn("NSE holiday: Holi", text)

    def test_offline_gz_folder_symbols_and_mismatch(self):
        os.mkdir("charts")
        with open(R5, "rb") as src, gzip.open(os.path.join("charts", "RELIANCE.json.gz"), "wb") as dst:
            dst.write(src.read())
        for sym in ("M&M", "BAJAJ-AUTO"):
            m = doc()
            m["chart"]["result"][0]["meta"]["symbol"] = sym + ".NS"
            with open(os.path.join("charts", sym + ".json"), "w") as fh:
                json.dump(m, fh)
        code, text, _ = run(["check", "reliance", "--offline", "charts", "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 0)
        code, text, _ = run(["check", "nse:m&m", "--offline", "charts", "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("M&M  Fri 2026-11-13 10:05 IST"))
        code, text, _ = run(["check", "BAJAJ-AUTO.NS", "--offline", "charts", "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "TCS", "--offline", R5])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE, not TCS", err)
        code, _, err = run(["check", "^NSEI", "--offline", R5])
        self.assertIn("not an NSE symbol", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", R5, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", R5, "--level", "1300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", R5, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "2:30 pm"}, fh)
        code, _, err = run(["check", "RELIANCE", "--offline", R5])
        self.assertIn("no_entries_after wants HH:MM", err)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", R5])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", R5, "--now", "2026-11-13 09:30", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("10:00 bar  closed ₹1,334.00  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual((e["bar_index"], e["time"], e["price"]), (9, "10:05", 1334.0))
        code, text, _ = run(["watch", "RELIANCE", "--offline", R5, "--now", "2026-11-13 09:30", "--until", "09:50",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))
        code, text, _ = run(["watch", "RELIANCE", "--offline", R5, "--now", "2026-11-14 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("closed today (Saturday)", text)

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(R5, os.path.join("charts", "RELIANCE.json"))
        shutil.copy(R1, os.path.join("charts", "INFY.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"], "or_high": 1333.4, "or_low": 1324.3},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "INFY", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-13 10:05"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE    triggered  10:05  ₹1,334.00    ₹1,333.40"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertIn("INFY        error: the chart file holds RELIANCE, not INFY", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-13 10:04", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /nse-scanner first", err)


def hole_at(d, hhmm):
    """The same chart with one 5-minute bar left out the way Yahoo sometimes does (a null bar)."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    want = int((nc.to_utc(DT.combine(FRI, nc.parse_hhmm(hhmm))) - DT(1970, 1, 1)).total_seconds())
    i = r["timestamp"].index(want)
    for k, arr in r["indicators"]["quote"][0].items():
        arr[i] = None
    return d


class IncompleteRange(unittest.TestCase):
    """Review item 13: an opening range built from the bars Yahoo happened to send."""

    def test_the_control_with_the_whole_range(self):
        r = at("2026-11-13 10:05")
        self.assertEqual((r["status"], r["opening_range"]["bars"], r["opening_range"]["complete"]),
                         ("triggered", 3, True))

    def test_a_missing_range_bar_waits_instead_of_triggering(self):
        # the 09:15 bar holds the real range high (1,333.40). Without it the "range" is 09:20 and 09:25:
        # high 1,330.10, and the 09:30 bar (close 1,331.60) used to trigger at 09:35 off that wrong high
        d = hole_at(doc(), "09:15")
        for now in ("2026-11-13 09:35", "2026-11-13 10:05", "2026-11-13 12:00"):
            r = at(now, d)
            self.assertEqual((r["status"], r["phase"], r["trigger"]), ("waiting", "open", None), now)
            self.assertEqual(r["reason"], "opening range incomplete: 2 of 3 bars")
            self.assertEqual((r["opening_range"]["bars"], r["opening_range"]["of"], r["opening_range"]["complete"]),
                             (2, 3, False))
            self.assertIsNone(r["level"])
        text = "\n".join(entry.describe(at("2026-11-13 10:05", d)))
        self.assertIn("WAITING  opening range incomplete: 2 of 3 bars.", text)
        self.assertNotIn("TRIGGERED", text)
        self.assertIn("high ₹1,330.10, low ₹1,324.30 (2 of 3 bars, incomplete).", text)       # not "still forming"

    def test_every_position_in_the_window(self):
        for hhmm in ("09:15", "09:20", "09:25"):
            r = at("2026-11-13 10:05", hole_at(doc(), hhmm))
            self.assertEqual((r["status"], r["reason"]), ("waiting", "opening range incomplete: 2 of 3 bars"), hhmm)

    def test_it_expires_at_the_cutoff_when_the_range_never_completes(self):
        d = hole_at(doc(), "09:20")
        r = at("2026-11-13 14:29", d)
        self.assertEqual(r["status"], "waiting")
        r = at("2026-11-13 14:30", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertEqual(r["reason"], "opening range incomplete: 2 of 3 bars; no entry by 14:30, the last time for a "
                                      "new entry")
        self.assertIsNone(r["trigger"])

    def test_only_one_range_bar_and_none(self):
        d = hole_at(hole_at(doc(), "09:20"), "09:25")
        r = at("2026-11-13 10:05", d)
        self.assertEqual((r["status"], r["reason"]), ("waiting", "opening range incomplete: 1 of 3 bars"))
        d = hole_at(d, "09:15")
        self.assertEqual(at("2026-11-13 10:05", d)["status"], "no-data")       # no range bar at all

    def test_the_last_range_bar_still_settling(self):
        d = live_cut(doc(), "09:25", "09:29:50")             # Yahoo's last trade is before the 09:25 bar's close
        r = at("2026-11-13 09:30:05", d)
        self.assertEqual((r["status"], r["reason"]), ("waiting", "opening range incomplete: 2 of 3 bars"))
        self.assertIn("Yahoo's last trade is 09:29:50", r["lag_note"])
        self.assertEqual(at("2026-11-13 10:05")["status"], "triggered")        # the whole file: fine

    def test_the_level_setup_does_not_need_the_range(self):
        r = at("2026-11-13 10:30", hole_at(doc(), "09:15"), setup="level", level=1340)
        self.assertEqual((r["status"], r["trigger"]["time"]), ("triggered", "10:10"))

    def test_a_bar_counts_at_its_close_and_not_a_second_before(self):
        self.assertEqual(at("2026-11-13 10:04:59")["status"], "waiting")
        self.assertEqual(at("2026-11-13 10:05:00")["status"], "triggered")
        c1 = entry.parse_chart(json.dumps(doc(R1)), "RELIANCE")
        self.assertEqual(entry.evaluate(c1, ist("2026-11-13 09:45:59"), interval="1m")["status"], "waiting")
        self.assertEqual(entry.evaluate(c1, ist("2026-11-13 09:46:00"), interval="1m")["status"], "triggered")


class TriggeredAt(unittest.TestCase):
    """Review item 14: nse-exit refuses an old trigger by its time stamp."""

    def test_triggered_at_is_the_trigger_bars_close_in_utc(self):
        r = at("2026-11-13 10:05")
        self.assertEqual(r["triggered_at"], "2026-11-13T04:35:00Z")           # 10:05 IST
        self.assertEqual(r["trigger"]["triggered_at"], "2026-11-13T04:35:00Z")
        self.assertEqual(r["late_by_minutes"], 0)
        e = entry.entry_doc(r, ist("2026-11-13 10:05"))
        self.assertEqual((e["triggered_at"], e["late_by_minutes"], e["time"], e["price"]),
                         ("2026-11-13T04:35:00Z", 0, "10:05", 1334.0))
        self.assertEqual(entry.entry_doc(at("2026-11-13 10:04"), ist("2026-11-13 10:04"))["triggered_at"], None)

    def test_the_old_fields_are_all_still_there(self):
        e = entry.entry_doc(at("2026-11-13 10:05"), ist("2026-11-13 10:05"))
        for k in ("created_by", "exchange", "symbol", "setup", "side", "date", "status", "time", "bar_start",
                  "bar_index", "price", "level", "interval", "opening_range", "source", "checked_at", "reason"):
            self.assertIn(k, e)

    def test_late_by_minutes_is_in_the_json_and_in_the_text(self):
        for now, late in (("2026-11-13 10:05", 0), ("2026-11-13 10:09", 0), ("2026-11-13 10:10", 0),
                          ("2026-11-13 10:11", 6), ("2026-11-13 10:30", 25), ("2026-11-13 15:45", 340)):
            r = at(now)
            self.assertEqual((r["late_by_minutes"], r["minutes_since"]), (late, int((ist(now) - ist("2026-11-13 10:05"))
                                                                                  .total_seconds() // 60)), now)
            text = "\n".join(entry.describe(r))
            if late:
                self.assertIn("That was %d minutes ago (late_by_minutes %d)" % (late, late), text)
                self.assertIn("A late entry is a different trade", text)
                self.assertNotIn("This is the moment to get in", text)
            else:
                self.assertNotIn("late", text.lower())
                self.assertIn("This is the moment to get in", text)
        # the same numbers through the CLI's --json and --write
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "e.json")
            code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:30", "--json",
                                 "--write", out])
            j = json.loads(text)
            self.assertEqual((code, j["late_by_minutes"], j["triggered_at"]), (0, 25, "2026-11-13T04:35:00Z"))
            with open(out, encoding="utf-8") as fh:
                e = json.load(fh)
            self.assertEqual((e["late_by_minutes"], e["triggered_at"], e["checked_at"]),
                             (25, "2026-11-13T04:35:00Z", "2026-11-13T05:00:00Z"))
        finally:
            shutil.rmtree(d)


class OrderTick(unittest.TestCase):
    """Review item 11: the tick written to entry.json is the order tick."""

    def test_a_level_near_a_slab_edge_gets_the_coarser_tick(self):
        # 997.97 is on the 0.05 slab, but NSE sets a stock's tick from an earlier close: the stop and the
        # entry must sit on the 0.10 grid or the stock's own tick may reject them
        r = at("2026-11-13 10:30", setup="level", level=997.97)
        self.assertEqual(r["tick"], "0.10")
        self.assertEqual(at("2026-11-13 10:30", setup="level", level=1340)["tick"], "0.10")
        self.assertEqual(at("2026-11-13 10:30", setup="level", level=240)["tick"], "0.05")      # 0.01 slab, 0.05 at +10%
        self.assertEqual(at("2026-11-13 10:30", setup="level", level=200)["tick"], "0.01")
        r = at("2026-11-13 10:30", setup="level", level=1300, cfg=dict(nc.DEFAULT_CONFIG, tick_sizes={"RELIANCE": 0.5}))
        self.assertEqual(r["tick"], "0.5")


class WatchlistLevel(unittest.TestCase):
    """Review item 18, the entry end: it reads "level" and says so when a hit has none."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("charts")
        shutil.copy(R5, os.path.join("charts", "RELIANCE.json"))
        shutil.copy(R5, os.path.join("charts", "TCS.json"))
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, hits):
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": hits}, fh)

    def test_each_hit_uses_its_own_level(self):
        self.write([{"symbol": "RELIANCE", "setups": ["breakout"], "level": 1340.0, "name": "Reliance Industries Limited"}])
        code, text, _ = run(["watchlist", "--setup", "level", "--offline", "charts", "--now", "2026-11-13 10:30"])
        self.assertEqual(code, 0)
        row = text.splitlines()[1]
        self.assertTrue(row.startswith("RELIANCE    triggered  10:10  ₹1,341.00"), row)   # the 10:05 bar closed 1,341
        self.assertIn("₹1,340.00", row)

    def test_a_hit_without_a_level_says_what_to_do(self):
        self.write([{"symbol": "RELIANCE", "setups": ["oversold"]}, {"symbol": "TCS", "setups": ["breakout"],
                                                                   "level": "n/a"}])
        code, text, _ = run(["watchlist", "--setup", "level", "--offline", "charts", "--now", "2026-11-13 10:30"])
        self.assertEqual(code, 1)
        lines = text.splitlines()
        self.assertIn("RELIANCE    error: no level for RELIANCE: its hit in", lines[1])
        self.assertIn("has no \"level\" (nse-scanner writes the prior 20-day closing high on 20-day-breakout hits "
                      "only; this hit is oversold). Pass --level PRICE, or watch it with --setup orb", lines[1])
        self.assertIn("error: no level for TCS", lines[2])                        # a non-number is no level
        self.assertNotIn("needs a level", text)

    def test_a_given_level_beats_the_hits(self):
        self.write([{"symbol": "RELIANCE", "setups": ["breakout"], "level": 1340.0}])
        code, text, _ = run(["watchlist", "--setup", "level", "--level", "1347", "--offline", "charts", "--now",
                             "2026-11-13 10:30"])
        self.assertIn("₹1,347.00", text.splitlines()[1])

    def test_hit_level_reads_only_prices(self):
        self.assertEqual(entry.hit_level({"level": 1340}), 1340.0)
        for bad in ({}, {"level": None}, {"level": "1340"}, {"level": 0}, {"level": -5}, {"level": True}):
            self.assertIsNone(entry.hit_level(bad), bad)


class HolidayRefresh(unittest.TestCase):
    """Review item 25: entry.py refreshes a stale holiday list before it decides whether the NSE is open."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.saved = (entry.HOLIDAY_FETCH, entry._open_url, entry._sleep)
        entry._sleep = lambda s: None
        self.fetched = []
        with open(os.path.join(FIX, "nse-alerts", "holiday-master.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        m["CM"] = [dict(r, tradingDate=r["tradingDate"].replace("2026", "2027")) for r in m["CM"]]
        self.master_2027 = m
        shifted = json.dumps(shift_to(doc(), D(2027, 1, 5)))
        entry._open_url = lambda url, timeout=20: shifted

    def tearDown(self):
        entry.HOLIDAY_FETCH, entry._open_url, entry._sleep = self.saved
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def fake_nse(self, doc_or_error):
        def fetch():
            self.fetched.append(1)
            if isinstance(doc_or_error, Exception):
                raise doc_or_error
            return doc_or_error
        entry.HOLIDAY_FETCH = fetch

    def test_an_unknown_year_is_refreshed_and_then_known(self):
        self.fake_nse(self.master_2027)
        code, text, err = run(["check", "RELIANCE", "--now", "2027-01-26 10:30"])      # live path: Yahoo is faked
        self.assertEqual(err, "")
        self.assertEqual(len(self.fetched), 1)
        self.assertIn("NSE holiday: Republic Day", text)                            # known only because of the refresh
        self.assertIn("Holiday list: refreshed NSE's holiday list (NSE's 2027 holiday list is not loaded).", text)
        self.assertNotIn("holiday list is not loaded (nse-alerts", text)
        self.assertTrue(os.path.isfile(os.path.join("trader", "state", nc.HOLIDAY_CACHE)))
        run(["check", "RELIANCE", "--now", "2027-01-26 10:30"])
        self.assertEqual(len(self.fetched), 1)                                        # now cached and fresh: no refetch

    def test_a_failed_refresh_never_stops_the_check(self):
        self.fake_nse(nc.HolidayError("could not reach NSE: no network"))
        code, text, err = run(["check", "RELIANCE", "--now", "2027-01-05 10:05"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("TRIGGERED 10:05", text)
        self.assertIn("Holiday list: could not refresh NSE's holiday list (NSE's 2027 holiday list is not loaded): "
                      "could not reach NSE: no network.", text)
        self.assertIn("NSE's 2027 holiday list is not loaded", text)                 # and the old plain warning stays
        self.fake_nse(RuntimeError("boom"))                                           # even an unexpected error
        code, text, err = run(["check", "RELIANCE", "--now", "2027-01-05 10:05"])
        self.assertEqual((code, err), (0, ""))

    def test_offline_replays_never_touch_the_network(self):
        self.fake_nse(AssertionError("must not fetch"))
        code, text, err = run(["check", "RELIANCE", "--offline", R5, "--now", "2027-01-05 10:05"])
        self.assertEqual(self.fetched, [])
        self.assertNotIn("Holiday list:", text)
        code, text, err = run(["watchlist", "--offline", os.path.join(FIX, "nse-entry"), "--watchlist",
                               os.path.join(self.d, "none.json")])
        self.assertEqual(self.fetched, [])

    def test_a_known_fresh_year_does_not_fetch(self):
        self.fake_nse(self.master_2027)
        run(["check", "RELIANCE", "--now", "2026-11-13 10:05"])
        self.assertEqual(self.fetched, [])

    def test_watch_and_watchlist_refresh_too(self):
        self.fake_nse(self.master_2027)
        code, text, _ = run(["watch", "RELIANCE", "--now", "2027-01-05 09:30", "--every", "30"])
        self.assertEqual((code, len(self.fetched)), (0, 1))
        self.assertIn("Holiday list: refreshed", text)
        os.mkdir("trader") if not os.path.isdir("trader") else None
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "RELIANCE"}]}, fh)
        os.remove(os.path.join("trader", "state", nc.HOLIDAY_CACHE))
        self.fetched.clear()
        run(["watchlist", "--now", "2027-01-05 10:05"])
        self.assertEqual(len(self.fetched), 1)


class TraderHome(unittest.TestCase):
    """Review item 8: the default files are in the trader folder, and the config used is named."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        self.elsewhere = os.path.join(self.d, "elsewhere")
        os.mkdir(self.elsewhere)
        os.chdir(self.elsewhere)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_entry_json_goes_to_the_trader_home_not_the_cwd(self):
        home = os.path.join(self.d, "home")
        with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": home}):
            code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05", "--write"])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.isfile(os.path.join(home, "entry.json")))
            self.assertIn("Wrote %s (status triggered)." % os.path.join(home, "entry.json"), text)
            self.assertEqual(os.listdir(self.elsewhere), [])
            code, text, _ = run(["watch", "RELIANCE", "--offline", R5, "--now", "2026-11-13 09:30"])
            self.assertIn("Wrote %s." % os.path.join(home, "entry.json"), text)
            code, _, err = run(["watchlist", "--offline", EDIR])
            self.assertIn("no watchlist at %s" % os.path.join(home, "watchlist.json"), err)     # the real path
            self.assertEqual(os.listdir(self.elsewhere), [])

    def test_it_says_which_config_it_used(self):
        home = os.path.join(self.d, "home")
        os.mkdir(home)
        cfgfile = os.path.join(home, "config.json")
        with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": home}):
            code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05"])
            self.assertIn("Config: none at %s, so the built-in defaults are in use." % cfgfile, text)
            with open(cfgfile, "w") as fh:
                json.dump({"no_entries_after": "14:00"}, fh)
            code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05"])
            self.assertIn("Config: %s." % cfgfile, text)
            code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05", "--json"])
            self.assertIn("Config: %s." % cfgfile, json.loads(text)["notes"])
            code, text, _ = run(["watch", "RELIANCE", "--offline", R5, "--now", "2026-11-13 09:30"])
            self.assertIn("Config: %s." % cfgfile, text)

    def test_the_nearest_trader_folder_above_the_cwd_is_used(self):
        os.makedirs(os.path.join(self.d, "trader"))
        with open(os.path.join(self.d, "trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", R5, "--now", "2026-11-13 10:05"])   # cwd is d/elsewhere
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)                                              # d/trader/config.json was read


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-entry\ndescription: >-\n"))
        fm, body = text.split("\n---\n", 1)
        self.assertGreater(len(fm), 200)
        self.assertIn("argument-hint:", fm)
        self.assertGreater(len(body.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("nse", "nifty", "₹", "${claude_skill_dir}", "bar by bar", "can lag", "/nse-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "10:05", "₹1,334.00", "bar 9",
                     "not financial advice", "explicit **yes**", "09:15", "pre-open", "14:30", "holiday",
                     "opening range incomplete: 2 of 3 bars", "triggered_at", "late_by_minutes", "order_tick",
                     "watchlist --setup level", "hits[].level", "nse_trader_home", "built-in defaults",
                     "--allow-late"):
            self.assertIn(must, low)
        desc = " ".join(fm.split("description: >-")[1].split("argument-hint:")[0].split())
        self.assertLess(len(desc), 600)                       # review item 27
        for phrase in ("tell me when to get in", "watch reliance", "has it broken out", "check my watchlist"):
            self.assertIn(phrase, desc.lower())
        for never in ("\u2014", "$", "dollar", "nyse", "new york", "tick " + "by tick", "real " + "time",
                      "pro" + "fit"):
            self.assertNotIn(never, low.replace("${claude_skill_dir}", ""))

    def test_script_has_no_us_leftovers(self):
        with open(os.path.join(ROOT, "nse-entry", "entry.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("\u2014", src)
        for never in ("NYSE", "New York", "$%", "\"$", "dollar", "eastern"):
            self.assertNotIn(never, src)


if __name__ == "__main__":
    unittest.main()
