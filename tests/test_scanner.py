"""nse-scanner tests. Offline, on real NSE and Yahoo files in tests/fixtures/nse-scanner/:

ind_nifty50list.csv, ind_nifty500list.csv, fo_mktlots.csv, EQUITY_L.csv.gz, sec_list.csv.gz
    NSE archives, as published for Fri 13 Nov 2026.
daily-spark-nifty50-2026-11-13.json   Yahoo spark, 3mo of daily closes, all 50 (one answer).
wall5m-nifty50-5d-2026-11-13.json.gz  {SYMBOL: v8 chart result}, 5 sessions of 5m bars to 13 Nov.
spark5m-2026-11-13.json               a real 20-symbol spark 5m answer (fetched 14 Nov, trimmed to
                                      13 Nov): Yahoo drops the made-up ZZZNOTREAL and some bars.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

scan = load("nse-scanner", "scan")
nc = scan.nc

SDIR = os.path.join(FIX, "nse-scanner")
WALL = os.path.join(SDIR, "wall5m-nifty50-5d-2026-11-13.json.gz")
DAILY = os.path.join(SDIR, "daily-spark-nifty50-2026-11-13.json")
SPARK5 = os.path.join(SDIR, "spark5m-2026-11-13.json")
SEC = os.path.join(SDIR, "sec_list.csv.gz")
DT = datetime.datetime
D = datetime.date
FRI = D(2026, 11, 13)
DEF = nc.load_config(None)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    fmt = "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2 else "%Y-%m-%d %H:%M"
    utc = nc.to_utc(DT.strptime(ist_text, fmt))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` (IST) on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


def cut(hhmm, day=FRI):
    return DT.combine(day, scan.parse_hhmm(hhmm))


class Lists(unittest.TestCase):
    """NSE's archive files -> universes, then sec_list's series, band and GSM screen."""

    @classmethod
    def setUpClass(cls):
        cls.sec = scan.parse_sec_list(scan.read_maybe_gz(SEC))

    def read(self, name):
        return scan.read_maybe_gz(os.path.join(SDIR, name))

    def test_nifty50_and_nifty500(self):
        n50 = scan.parse_index_list(self.read("ind_nifty50list.csv"))
        self.assertEqual(len(n50), 50)
        syms = [s for s, _ in n50]
        for s in ("RELIANCE", "M&M", "BAJAJ-AUTO", "TMPV"):
            self.assertIn(s, syms)
        self.assertEqual(dict(n50)["M&M"], "Mahindra & Mahindra Ltd.")
        n500 = scan.parse_index_list(self.read("ind_nifty500list.csv"))
        self.assertEqual(len(n500), 501)                    # 500 + NSE's DUMMYHEG placeholder

    def test_fno_skips_the_index_rows(self):
        fno = scan.parse_fo_mktlots(self.read("fo_mktlots.csv"))
        syms = [s for s, _ in fno]
        self.assertEqual(len(syms), 218)
        for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SYMBOL"):
            self.assertNotIn(idx, syms)
        self.assertEqual(syms[:3], ["ADANIENSOL", "ANANDRATHI", "ADANIPORTS"])
        self.assertIn("GVT&D", syms)                        # padded columns, & in the symbol
        self.assertIn("BAJAJ-AUTO", syms)
        # without the "Derivatives on Individual Securities" marker, NIFTY rows are still skipped
        text = "\n".join(ln for ln in self.read("fo_mktlots.csv").splitlines() if "Derivatives on" not in ln)
        self.assertEqual(len(scan.parse_fo_mktlots(text)), 218)

    def test_all_is_equity_l_series_eq(self):
        eq = scan.parse_equity_l(self.read("EQUITY_L.csv.gz"))
        self.assertEqual(len(eq), 2391)                     # of 2,635 rows: 210 BE and 34 BZ left out
        self.assertEqual(eq[0], ("20MICRONS", "20 Microns Limited"))
        self.assertNotIn("21STCENMGM", dict(eq))            # series BE

    def test_the_nifty500_screen(self):
        syms = [s for s, _ in scan.parse_index_list(self.read("ind_nifty500list.csv"))]
        kept, dropped = scan.screen(syms, self.sec, DEF)
        self.assertEqual(len(kept), 494)
        self.assertEqual(dropped, {"BAGMANE": "series RR", "BIRET": "series RR", "EMBASSY": "series RR",
                                   "HFCL": "series BE", "STLTECH": "series BE", "VISL": "5% band",
                                   "DUMMYHEG": "not in sec_list"})
        text = scan.drop_summary(len(syms), dropped, DEF)
        self.assertEqual(text, "sec_list.csv dropped 7 of 501: 3 in series RR (REIT), 2 in series BE "
                               "(trade-for-trade, no intraday), 1 in a 5% circuit band, 1 not in sec_list "
                               "(suspended or a placeholder).")

    def test_every_equity_screen_bands_and_gsm(self):
        syms = [s for s, _ in scan.parse_equity_l(self.read("EQUITY_L.csv.gz"))]
        kept, dropped = scan.screen(syms, self.sec, DEF)
        self.assertEqual(len(kept), 2244)
        self.assertEqual(scan.reason_counts(dropped), {"5% band": 121, "surveillance (GSM)": 26})
        self.assertEqual(dropped["BALKRISHNA"], "surveillance (GSM)")   # EQ, 20% band, GSM STAGE - 0
        self.assertIn("26 under GSM surveillance", scan.drop_summary(len(syms), dropped, DEF))

    def test_one_case_per_rule_and_the_config(self):
        syms = ["RELIANCE", "M&M", "BAJAJ-AUTO", "HFCL", "ANKITMETAL", "AQYLON", "BALKRISHNA", "BLUECOAST",
                "VISL", "ELECTCAST", "BAGMANE", "NOPE"]
        kept, dropped = scan.screen(syms, self.sec, DEF)
        self.assertEqual(kept, ["RELIANCE", "M&M", "BAJAJ-AUTO", "ELECTCAST"])  # No Band x3; EQ row of EQ + W1
        self.assertEqual(dropped, {"HFCL": "series BE", "ANKITMETAL": "series BZ", "AQYLON": "5% band",
                                   "BALKRISHNA": "surveillance (GSM)", "BLUECOAST": "surveillance (GSM)",
                                   "VISL": "5% band", "BAGMANE": "series RR", "NOPE": "not in sec_list"})
        loose = dict(DEF, min_circuit_band_pct=5, block_surveillance=False, allowed_series=["EQ", "BE"])
        kept, dropped = scan.screen(syms, self.sec, loose)
        self.assertEqual(sorted(dropped), ["ANKITMETAL", "BAGMANE", "NOPE"])  # BZ, RR, unknown
        self.assertIn("HFCL", kept)                         # BE allowed, 5% band allowed
        self.assertEqual(scan.band_pct("No Band"), None)
        self.assertEqual(scan.band_pct("20"), 20.0)
        self.assertIsNone(scan.surveillance("-\t"))
        self.assertEqual(scan.surveillance("GSM STAGE - II"), "GSM STAGE - II")

    def test_not_nse_files(self):
        for fn in (scan.parse_index_list, scan.parse_fo_mktlots, scan.parse_equity_l, scan.parse_sec_list):
            with self.assertRaises(scan.ScanError):
                fn("<html><body>Access Denied</body></html>\n<p>x</p>\n")


class Time(unittest.TestCase):
    def test_ist_clock(self):
        self.assertEqual(scan.parse_now("2026-11-13 10:05"), DT(2026, 11, 13, 4, 35))      # IST is UTC+05:30
        self.assertEqual(scan.parse_now("2026-11-13 10:05 IST"), DT(2026, 11, 13, 4, 35))
        self.assertEqual(scan.parse_now("2026-11-13T04:35:00Z"), DT(2026, 11, 13, 4, 35))
        self.assertEqual(nc.ist_from_epoch(1794541500), DT(2026, 11, 13, 9, 15))         # RELIANCE's first bar
        self.assertEqual(scan.hm(DT(2026, 11, 13, 10, 5)), "10:05")
        self.assertEqual(scan.day_label(FRI), "Fri 2026-11-13")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")


class Spark(unittest.TestCase):
    def test_a_real_daily_spark_answer(self):
        with open(DAILY, encoding="utf-8") as fh:
            data = scan.parse_spark(fh.read())
        self.assertEqual(len(data), 50)
        self.assertIn("M&M", data)                          # M&M.NS comes back bare
        self.assertIn("BAJAJ-AUTO", data)                   # a hyphen stays a hyphen
        r = data["RELIANCE"]
        self.assertEqual(len(r["t"]), 63)
        self.assertIsNone(r["h"])                           # spark is closes only
        self.assertEqual(r["c"][-1], 1339.7)
        self.assertEqual(r["meta"]["regularMarketVolume"], 13756247)
        self.assertEqual(r["meta"]["currency"], "INR")
        # read during Friday's session, Friday's bar is still in progress
        closes = scan.daily_closes(r, scan.parse_now("2026-11-13 15:29"))
        self.assertEqual(closes[-1][0], D(2026, 11, 12))
        closes = scan.daily_closes(r, scan.parse_now("2026-11-13 15:30"))
        self.assertEqual(closes[-1], (FRI, 1339.7))
        closes = scan.daily_closes(r, scan.parse_now("2026-11-14 11:00"))  # Saturday: Friday's bar is done
        self.assertEqual(closes[-1][0], FRI)

    def test_a_real_5m_spark_answer_drops_bars(self):
        data, _ = scan.load_offline(SPARK5)
        self.assertEqual(len(data), 18)                     # 20 asked: ZZZNOTREAL dropped, AXISBANK twice
        self.assertNotIn("ZZZNOTREAL", data)
        bars = scan.session_bars(data["ITC"], FRI, cut("15:30"))
        self.assertEqual(len(bars), 66)                     # Yahoo's spark lost 9 of ITC's 75 bars
        self.assertFalse(scan.usable(bars, FRI, cut("15:30"), 3))
        wall, _ = scan.load_offline(WALL)
        self.assertTrue(scan.usable(scan.session_bars(wall["ITC"], FRI, cut("15:30")), FRI, cut("15:30"), 3))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls_for_nse_symbols(self):
        self.assertEqual(scan.spark_url(["M&M", "bajaj-auto"], "3mo", "1d"),
                         "https://query1.finance.yahoo.com/v7/finance/spark?symbols=M%26M.NS,BAJAJ-AUTO.NS"
                         "&range=3mo&interval=1d")
        self.assertEqual(scan.chart_url("RELIANCE", "1d", "5m"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        self.assertEqual(scan._other_host(scan.chart_url("M&M", "1d", "5m")),
                         "https://query1.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=5m")


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(DAILY, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get(scan.spark_url(["RELIANCE"], "3mo", "1d"))
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com",
                                      "https://query1.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_the_other_host_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get(scan.chart_url("RELIANCE", "1d", "5m"))
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        with open(DAILY, encoding="utf-8") as fh:
            real = sorted(scan.parse_spark(fh.read()))
        syms = real[:19] + ["ZZZNOTREAL"] + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZZNOTREAL"], "no data from Yahoo")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-11-16 09:00"), 20, 10)
        self.assertEqual((counts["requested"], counts["fetched"]), (40, 19))

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get(scan.chart_url("NOPE", "1d", "5m"))
        self.assertEqual(len(self.calls), 1)

    def test_a_dropped_connection_is_a_plain_error(self):
        def fake(url, timeout=20):
            raise urllib.error.URLError("nodename nor servname provided")
        scan.http_get = fake
        with self.assertRaises(scan.ScanError) as cm:
            scan.yahoo_get(scan.chart_url("RELIANCE", "1d", "5m"))
        self.assertIn("nodename", str(cm.exception))


class Daily(unittest.TestCase):
    # Closes through Fri 13 Nov 2026 (a rally after Diwali), hand-checked on a few:
    #   BSE         3,937.70 > 3,909.10, the best close of the 20 before; up 16.1 % from 3,265 to 3,790
    #               over the 10 sessions before the last 5, then a 3.5 % range: breakout and flag
    #   RELIANCE    up 10.9 % (1,164.90 on 30 Oct is c[-16]... 1,300 on 2 Nov), last 5 in a 1.5 % range
    #   ULTRACEMCO  up 11.8 %, last 5 in a 2.9 % range, and under its 20-day high of 12,450
    EXPECT = {"RELIANCE": ["bull_flag"], "BAJFINANCE": ["breakout_20d", "bull_flag"],
              "BSE": ["breakout_20d", "bull_flag"], "LT": ["bull_flag"], "MARUTI": ["bull_flag"],
              "ETERNAL": ["bull_flag"], "INDIGO": ["bull_flag"], "APOLLOHOSP": ["bull_flag"],
              "TITAN": ["bull_flag"], "JSWSTEEL": ["bull_flag"], "ULTRACEMCO": ["bull_flag"],
              "TRENT": ["bull_flag"], "DRREDDY": ["bull_flag"], "ASIANPAINT": ["bull_flag"],
              "MAXHEALTH": ["breakout_20d", "bull_flag"], "TATACONSUM": ["bull_flag"]}

    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(DAILY)
        cls.syms = sorted(cls.data)

    def test_setups_before_mondays_open(self):
        self.assertEqual(len(self.syms), 50)
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-16 09:00"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, FRI)
        self.assertEqual(counts, {"requested": 50, "fetched": 50, "illiquid": 0, "short": 0, "scanned": 50,
                                  "hits": 16})
        self.assertEqual(hits[0], {"symbol": "RELIANCE", "setups": ["bull_flag"], "price": 1339.7,
                                   "close_date": "2026-11-13", "rsi": 70.8, "volume": 13756247})
        bse = scan.find_setups([c for _, c in scan.daily_closes(self.data["BSE"], scan.parse_now("2026-11-16 09:00"))])
        self.assertAlmostEqual(bse[1]["high20"], 3909.1)
        self.assertAlmostEqual(bse[1]["run_pct"], 16.08, places=2)

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-13 09:00"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-13 15:29"), 0, 0)
        self.assertEqual(before, during)
        self.assertEqual(before[2], D(2026, 11, 12))
        self.assertEqual(len(before[0]), 22)               # through Thursday: 19 breakouts, 7 flags
        after = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-13 15:30"), 0, 0)
        self.assertEqual(after[2], FRI)

    def test_a_holiday_or_weekend_bar_is_complete(self):
        s = {"t": [ts("2026-11-20 09:15"), ts("2026-11-23 09:15")], "c": [10.0, 11.0], "meta": {}}
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-11-23 15:29"))), 1)  # Monday in progress
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-11-23 15:30"))), 2)
        # 24 Nov is Prakash Gurpurb, an NSE holiday: no session to wait for
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-11-24 11:00"))), 2)

    def test_rupee_liquidity_filter(self):
        # last session's price x volume from the spark meta: TATACONSUM ₹74.6 cr, BAJAJFINSV ₹79.4 cr,
        # SBILIFE ₹94.5 cr are the thinnest Nifty 50 names that Friday
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-16 09:00"), 20, 100)
        self.assertEqual(counts["illiquid"], 3)
        self.assertNotIn("TATACONSUM", [h["symbol"] for h in hits])
        self.assertEqual(len(hits), 15)
        # the defaults (₹20, ₹10 cr) keep the whole Nifty 50
        _, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-16 09:00"), 20, 10)
        self.assertEqual(counts["illiquid"], 0)
        # price floor: ₹250 drops TATASTEEL (186.68), ONGC (225.00) and JIOFIN (227.50)
        _, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-16 09:00"), 250, 0)
        self.assertEqual(counts["illiquid"], 3)

    def test_average_traded_value_when_volumes_exist(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 31)], "c": [100.0] * 30,
             "v": [100000] * 29 + [10], "meta": {"regularMarketPrice": 100.0, "regularMarketVolume": 10}}
        bars = scan.daily_closes(s, scan.parse_now("2026-11-02 09:00"), with_volume=True)
        value, how = scan.traded_value(s, bars)
        self.assertEqual(how, "average of 20 sessions")
        self.assertAlmostEqual(value / scan.CRORE, (19 * 1e7 + 1000) / 20 / 1e7)
        value, how = scan.traded_value({"meta": {"regularMarketPrice": 100.0, "regularMarketVolume": 10 ** 6}},
                                       [(FRI, 100.0, None)])
        self.assertEqual((value / scan.CRORE, how), (10.0, "last session"))

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 11)], "c": [100.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-10-20 09:00"), 20, 10)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    RELIANCE = {"symbol": "RELIANCE", "setups": ["orb"], "price": 1334.0, "at": "10:05", "bar_start": "10:00",
                "bar_index": 9, "or_high": 1333.4, "or_low": 1324.3, "pct_over": 0.04}

    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=0, syms=None, or_minutes=15, until=datetime.time(14, 30), at=None):
        day, cutoff = scan.orb_window(scan.parse_now(now), at, or_minutes, until)
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_worked_example_reliance_at_1005(self):
        hits, counts, failed = self.scan_at("2026-11-13 10:05")
        self.assertEqual(len(self.syms), 50)
        self.assertEqual(self.errors, {})
        self.assertEqual(counts["scanned"], 50)
        self.assertEqual(len(hits), 31)
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual(rel, self.RELIANCE)               # range 1,324.30-1,333.40, the 10:00 bar closed 1,334.00
        self.assertEqual(hits[0]["symbol"], "BAJFINANCE")   # sorted by time, then symbol
        self.assertEqual([h["at"] for h in hits], sorted(h["at"] for h in hits))
        hits, _, _ = self.scan_at("2026-11-13 10:04", syms=["RELIANCE"])
        self.assertEqual(hits, [])                         # the 10:00 bar is not done until 10:05

    def test_34_of_the_nifty_50_by_the_close(self):
        # PORTING.md: "34 of the Nifty 50 closed a 5-minute bar above their 15-minute opening range"
        hits, counts, _ = self.scan_at("2026-11-13 16:00", at=datetime.time(15, 30), until=datetime.time(15, 30))
        self.assertEqual((len(hits), counts["scanned"], counts["unusable"]), (34, 50, 0))
        self.assertIn(self.RELIANCE, hits)
        self.assertEqual(hits[-1]["symbol"], "AXISBANK")    # the last one, at 10:50
        self.assertEqual(hits[-1]["at"], "10:50")
        # every hit came before the default 14:30 cutoff, so the 14:30 scan finds the same 34
        hits14, _, _ = self.scan_at("2026-11-13 14:30")
        self.assertEqual(hits14, hits)

    def test_yahoos_empty_closing_bars_are_not_halts(self):
        # every chart's 15:20 bar (and some 15:15 bars) came as nulls from Yahoo
        s = self.data["RELIANCE"]
        bars = scan.session_bars(s, FRI, cut("15:30"))
        self.assertEqual(len(bars), 74)
        self.assertNotIn(DT(2026, 11, 13, 15, 20), [b["start"] for b in bars])
        self.assertTrue(scan.usable(bars, FRI, cut("15:30"), 3))
        # a missing bar earlier in the day is a halt or no trades: unusable
        noon = [b for b in bars if b["start"] != DT(2026, 11, 13, 12, 0)]
        self.assertFalse(scan.usable(noon, FRI, cut("15:30"), 3))
        self.assertTrue(scan.usable(noon, FRI, cut("12:00"), 3))  # not yet due at noon

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-11-13 14:30", min_price=250)
        self.assertEqual(counts["illiquid"], 3)            # TATASTEEL, JIOFIN, ONGC under ₹250
        self.assertEqual(len(hits), 31)
        self.assertNotIn("ONGC", [h["symbol"] for h in hits])

    def test_in_progress_bar_and_the_live_tick(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5]
        s = bars_series("2026-11-13", closes, highs=[c + 0.2 for c in closes],
                        extra=[("2026-11-13 09:51:07", 103.0)])
        bars = scan.session_bars(s, FRI, cut("09:52"))
        self.assertEqual([scan.hm(b["start"]) for b in bars], ["09:15", "09:20", "09:25", "09:30", "09:35",
                                                                "09:40", "09:45"])
        self.assertEqual(nc.ist_from_epoch(s["t"][-1]), DT(2026, 11, 13, 9, 51, 7))  # off the grid: dropped
        self.assertEqual(len(scan.session_bars(s, FRI, cut("09:49"))), 6)            # 09:45 in progress
        hits, _, _ = scan.scan_orb({"X": s}, ["X"], FRI, cut("09:52"), 15)
        # range high 101.2 (the 09:20 bar's 101 + 0.2); the 09:35 bar closed 101.2, AT the high, not above
        self.assertEqual((hits[0]["at"], hits[0]["bar_start"], hits[0]["price"]), ("09:50", "09:45", 101.5))

    def test_stage_one_is_a_necessary_condition(self):
        for sym, s in self.data.items():
            bars = scan.session_bars(s, FRI, cut("14:30"))
            if scan.usable(bars, FRI, cut("14:30"), 3) and scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}  # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, FRI, cut("10:05"), 15, 0, 0, confirm)
        self.assertEqual(len(hits), 31)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 50)                    # stage 1 skipped some charts

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.NS" % sym)
        hits, counts, failed = scan.scan_orb(spark_like, ["RELIANCE", "TCS"], FRI, cut("10:05"), 15, 0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["RELIANCE"])

    def test_a_real_gappy_spark_stage_one(self):
        """Stage 1 on the real spark answer, stage 2 on the v8 wall: the same hits as the wall alone,
        although spark lost bars of ITC, INFY and ETERNAL, three of the breakouts."""
        spark, _ = scan.load_offline(SPARK5)
        syms = sorted(spark)
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, _ = scan.scan_orb(spark, syms, FRI, cut("14:30"), 15, 0, 0, confirm)
        wall, _, _ = scan.scan_orb(self.data, syms, FRI, cut("14:30"), 15)
        self.assertEqual(hits, wall)
        self.assertEqual((len(hits), counts["gappy"], counts["candidates"]), (13, 7, 16))
        for sym in ("ITC", "INFY", "ETERNAL"):
            self.assertIn(sym, [h["symbol"] for h in hits])
            self.assertFalse(scan.usable(scan.session_bars(spark[sym], FRI, cut("14:30")), FRI, cut("14:30"), 3))

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, FRI, cut("10:05"), 15, 0, 0, confirm, max_confirm=10)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (10, counts["candidates"] - 10))
        self.assertIn("RELIANCE", asked)                   # among the most rupees traded by 10:05
        all_hits, _, _ = scan.scan_orb(spark_like, self.syms, FRI, cut("10:05"), 15, 0, 0, confirm, max_confirm=0)
        self.assertEqual(len(all_hits), 31)
        for h in hits:
            self.assertIn(h, all_hits)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-11-13", [10, 10.2, 10.1, None, 10.5, 10.6, 10.7, 10.8, 10.9])
        bars = scan.session_bars(s, FRI, cut("10:00"))
        self.assertFalse(scan.usable(bars, FRI, cut("10:00"), 3))

    def test_a_30_minute_range_and_a_muhurat_session(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-11-13", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], FRI, cut("12:00"), 30)
        self.assertEqual(hits[0]["at"], "09:50")           # range 09:15 to 09:45 (high 101.4), 09:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)
        # Muhurat trading on Sun 8 Nov 2026, 18:15-19:15 IST, from config special_sessions
        cfg = dict(DEF, special_sessions={"2026-11-08": {"open": "18:15", "close": "19:15",
                                                         "label": "Muhurat trading"}})
        mu = bars_series("2026-11-08", closes[:12], highs=[c + 0.2 for c in closes[:12]], start="18:15")
        day, cutoff = scan.orb_window(scan.parse_now("2026-11-08 19:00"), None, 15, datetime.time(15, 30), cfg=cfg)
        self.assertEqual((day, cutoff), (D(2026, 11, 8), DT(2026, 11, 8, 19, 0)))
        hits, _, _ = scan.scan_orb({"X": mu}, ["X"], day, cutoff, 15, cfg=cfg)
        self.assertEqual((hits[0]["at"], hits[0]["bar_index"]), ("18:50", 6))
        # no_entries_after keeps its distance from the close: 14:30 (an hour early) is 18:15 here
        self.assertEqual(scan.shifted_until(day, datetime.time(14, 30), cfg), datetime.time(18, 15))
        with self.assertRaises(scan.ScanError) as cm:
            scan.orb_window(scan.parse_now("2026-11-08 19:00"), None, 15, datetime.time(14, 30), cfg=cfg)
        self.assertIn("after 18:15", str(cm.exception))
        with self.assertRaises(scan.ScanError) as cm:
            scan.orb_window(scan.parse_now("2026-11-08 19:00"), None, 15, datetime.time(14, 30))
        self.assertIn("No session on Sun 2026-11-08 (Sunday)", str(cm.exception))

    def test_window_messages(self):
        until = datetime.time(14, 30)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("No session on Sat 2026-11-14 (Saturday)", msg("2026-11-14 10:30"))
        self.assertIn("NSE holiday: Prakash Gurpurb", msg("2026-11-24 10:30"))
        self.assertIn("Diwali-Balipratipada", msg("2026-11-10 10:30"))
        self.assertIn("NSE opens at 09:15", msg("2026-11-13 08:30"))
        pre = msg("2026-11-13 09:05")
        self.assertIn("pre-open call auction (09:00-09:15, orders until 09:08)", pre)
        self.assertIn("Run orb again after 09:35", pre)
        self.assertIn("Run orb again after 09:35", msg("2026-11-13 09:32"))
        self.assertIn("expired", msg("2026-11-13 14:45"))
        self.assertIn("expired", msg("2026-11-13 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 15:30 IST", msg("2026-11-13 16:00"))
        self.assertIn("later than now", msg("2026-11-13 10:00", at=datetime.time(10, 30)))
        self.assertIn("after the 15:30 close", msg("2026-11-13 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-13 16:30"), datetime.time(10, 5), 15, until),
                         (FRI, DT(2026, 11, 13, 10, 5)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-13 09:35"), None, 15, until)[1],
                         DT(2026, 11, 13, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-11-13 10:15"), None, 7, until)


class FakeYahoo(object):
    """Answers spark and v8 URLs from the wall, the way Yahoo would (M%26M.NS and all)."""

    def __init__(self):
        self.bundle = json.loads(scan.read_maybe_gz(WALL))
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        path = url.split("finance.yahoo.com")[1]
        if path.startswith("/v7/finance/spark"):
            res = []
            for y in urllib.parse.parse_qs(path.split("?")[1])["symbols"][0].split(","):
                r = self.bundle.get(nc.nse_symbol(y))
                if r:
                    closes = r["indicators"]["quote"][0].get("close", [])
                    res.append({"symbol": y, "response": [dict(r, indicators={"quote": [{"close": closes}]})]})
            return json.dumps({"spark": {"result": res, "error": None}})
        sym = nc.nse_symbol(urllib.parse.unquote(path.split("/chart/")[1].split("?")[0]))
        return json.dumps({"chart": {"result": [self.bundle[sym]], "error": None}})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.cfg = os.path.join(self.t, "config.json")
        self.saved = (scan.http_get, scan.yahoo_get, scan.SLEEP, scan.today_ist)
        scan.SLEEP = lambda s: None
        scan.today_ist = lambda: D(2026, 11, 14)

    def tearDown(self):
        scan.http_get, scan.yahoo_get, scan.SLEEP, scan.today_ist = self.saved
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--lists", SDIR, "--out", uni, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Nifty 500 from NSE (ind_nifty500list.csv): 501 symbols.", text)
        self.assertIn("sec_list.csv dropped 7 of 501", text)
        self.assertIn("Kept 494 for intraday scans.", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 494)
        self.assertIn("M&M|Mahindra & Mahindra Ltd.", lines)
        self.assertNotIn("HFCL|HFCL Ltd.", lines)

        # daily over that universe with the Nifty 50 charts: partial, and said so
        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--lists", SDIR,
                             "--now", "2026-11-16 09:00", "--out", self.wl, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 50 of 494 charts (444 failed or had no data)", text)
        self.assertIn("(₹20+ and ₹10 cr+ traded a day)", text)
        self.assertNotIn("$", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["created_by"], doc["mode"], doc["universe"], doc["scanned"], doc["failed"]),
                         ("nse-scanner", "daily", 494, 50, 444))
        self.assertEqual(doc["hits"][0], {"symbol": "RELIANCE", "setups": ["bull_flag"], "price": 1339.7,
                                          "close_date": "2026-11-13", "rsi": 70.8, "volume": 13756247})
        self.assertEqual(doc["as_of"], "2026-11-13 close")
        self.assertEqual(doc["filters"], {"min_price": 20.0, "min_value_cr": 10.0})

        # the worked example's ORB scan, the Nifty 50 at 15:30
        code, text, _ = run(["orb", "--offline", WALL, "--lists", SDIR, "--universe", "nifty50", "--at", "15:30",
                             "--until", "15:30", "--out", self.wl, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Universe: Nifty 50, 50 symbols. sec_list.csv: none of 50 dropped", text)
        self.assertIn("34 of 50 charts closed above their opening-range high by 15:30 IST", text)
        self.assertIn("10:05  RELIANCE      ₹1,334.00  range 1,324.30 to 1,333.40  +0.04% over", text)
        self.assertIn("09:50  BAJAJ-AUTO   ₹10,919.00", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "mode", "exchange", "as_of", "universe", "scanned",
                                              "source")},
                         {"created_by": "nse-scanner", "mode": "orb", "exchange": "NSE",
                          "as_of": "2026-11-13 15:30 IST", "universe": 50, "scanned": 50, "source": "yahoo"})
        self.assertEqual(len(doc["hits"]), 34)
        self.assertIn(Orb.RELIANCE, doc["hits"])

        # the hits read back as a watchlist, with --now in IST
        code, text, _ = run(["orb", "--offline", WALL, "--lists", SDIR, "--now", "2026-11-13 10:05",
                             "--watchlist", self.wl, "--json", "--no-write", "--config", self.cfg])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (34, 34, 31))
        self.assertEqual(out["created_at"], "2026-11-13T04:35:00Z")

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the 13 Nov wall."""
        fake = FakeYahoo()
        scan.yahoo_get = fake
        code, text, _ = run(["orb", "--universe", "nifty50", "--lists", SDIR, "--now", "2026-11-13 10:05",
                             "--max-confirm", "10", "--no-write", "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("50 usable; 40 candidates, 10 confirmed", text)
        self.assertIn("PARTIAL: 30 more candidates were not checked", text)
        self.assertIn("of the 10 checked charts closed above their opening-range high by 10:05 IST", text)
        self.assertEqual(sum(1 for c in fake.calls if "/v8/" in c), 10)
        self.assertEqual(sum(1 for c in fake.calls if "/v7/" in c), 3)    # 50 symbols, 20 a request
        self.assertTrue(all("range=5d&interval=5m" in c for c in fake.calls))  # Friday, asked on Saturday
        self.assertIn("M%26M.NS", [c for c in fake.calls if "/v7/" in c][1])

    def test_lists_are_cached_for_the_day(self):
        cache = os.path.join(self.t, "cache")
        os.makedirs(cache)
        with open(os.path.join(cache, "sec_list-2026-11-13.csv"), "w") as fh:
            fh.write("stale")
        calls = []

        def get(url, timeout=20):
            calls.append(url)
            return scan.read_maybe_gz(os.path.join(SDIR, os.path.basename(url) + (".gz" if "sec_list" in url else "")))
        scan.http_get = get
        argv = ["universe", "nifty50", "--cache", cache, "--no-write", "--config", self.cfg]
        code, text, _ = run(argv)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
                                 "https://nsearchives.nseindia.com/content/equities/sec_list.csv"])
        self.assertEqual(sorted(os.listdir(cache)), ["ind_nifty50list-2026-11-14.csv", "sec_list-2026-11-14.csv"])

        def offline(url, timeout=20):
            raise AssertionError("the cache should have answered")
        scan.http_get = offline
        self.assertEqual(run(argv)[0], 0)
        scan.http_get = get
        run(argv + ["--refresh"])
        self.assertEqual(len(calls), 4)

    def test_nse_down_is_one_plain_line(self):
        def down(url, timeout=20):
            raise urllib.error.URLError("timed out")
        scan.http_get = down
        code, _, err = run(["daily", "--universe", "nifty50", "--cache", os.path.join(self.t, "cache"),
                            "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertEqual(err, "scan.py: could not reach nsearchives.nseindia.com for ind_nifty50list.csv: "
                              "timed out\n")

        def blocked(url, timeout=20):
            return "<html><body>Access Denied</body></html>"
        scan.http_get = blocked
        code, _, err = run(["universe", "--cache", os.path.join(self.t, "cache"), "--config", self.cfg])
        self.assertIn("sent a web page", err)
        self.assertNotIn("Traceback", err)

    def test_plain_messages_and_exit_codes(self):
        base = ["orb", "--offline", WALL, "--out", self.wl, "--config", self.cfg]
        code, _, err = run(base + ["--now", "2026-11-14 10:30"])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-11-14 (Saturday)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(base + ["--now", "2026-11-13 09:05"])
        self.assertIn("pre-open call auction", err)
        os.makedirs(self.t)
        with open(self.cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(base + ["--now", "2026-11-13 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        os.remove(self.cfg)
        code, _, err = run(["orb", "--offline", WALL, "--config", self.cfg])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(base + ["--now", "2026-11-13 09:50", "--symbols", "RELIANCE"])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 09:50 IST", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt"), "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("--universe is nifty50, nifty500, fno, all or a file", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here"), "--config", self.cfg])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)
        code, _, err = run(["daily", "--offline", DAILY, "--symbols", "RELIANCE,^NSEI", "--config", self.cfg])
        self.assertIn("not NSE symbols: ^NSEI", err)
        with open(self.cfg, "w") as fh:
            fh.write("{nope")
        code, _, err = run(["daily", "--offline", DAILY, "--config", self.cfg])
        self.assertIn("not valid JSON", err)

    def test_sec_list_screen_on_your_own_symbols(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--sec-list", SEC, "--symbols",
                             "RELIANCE,nse:m&m,BAJAJ-AUTO.NS,HFCL,BALKRISHNA,VISL", "--now", "2026-11-16 09:00",
                             "--json", "--no-write", "--config", self.cfg])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (3, 3))
        self.assertEqual(out["dropped_symbols"], {"HFCL": "series BE", "BALKRISHNA": "surveillance (GSM)",
                                                  "VISL": "5% band"})
        # offline without a sec_list it says the screen was skipped
        code, text, _ = run(["daily", "--offline", DAILY, "--symbols", "RELIANCE,HFCL", "--now",
                             "2026-11-16 09:00", "--no-write", "--config", self.cfg])
        self.assertIn("sec_list.csv not checked", text)
        os.makedirs(self.t)
        with open(self.cfg, "w") as fh:
            json.dump({"block_surveillance": False, "min_circuit_band_pct": 5}, fh)
        code, text, _ = run(["daily", "--offline", DAILY, "--sec-list", SEC, "--symbols", "RELIANCE,BALKRISHNA,VISL",
                             "--now", "2026-11-16 09:00", "--json", "--no-write", "--config", self.cfg])
        self.assertEqual(json.loads(text)["dropped_symbols"], {})

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan.read_maybe_gz(WALL))
        for sym in ("RELIANCE", "M&M", "BAJAJ-AUTO", "TCS"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump({"chart": {"result": [bundle[sym]], "error": None}}, fh)
        with open(os.path.join(folder, "GONE.json"), "w") as fh:
            json.dump({"chart": {"result": None, "error": {"description": "No data found, symbol may be delisted"}}}, fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-11-13 10:05", "--no-write", "--json",
                             "--config", self.cfg])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (5, 4, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["TCS", "BAJAJ-AUTO", "RELIANCE"])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nM&M|Mahindra & Mahindra Ltd.\nreliance.ns\n\nBAJAJ-AUTO\n")
        self.assertEqual(scan.symbols_from_file(txt), ["M&M", "RELIANCE", "BAJAJ-AUTO"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "m&m"}, {"symbol": "RELIANCE"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["M&M", "RELIANCE"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-16 09:00",
                             "--json", "--no-write", "--config", self.cfg])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (3, 3))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-11-16 09:00", "--json",
                             "--no-write", "--config", self.cfg])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-16 09:00", "--json", "--no-write",
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 16)
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-scanner", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-scanner\ndescription: >-\n"))
        fm = text.split("\n---\n", 1)[0]
        self.assertGreater(len(fm), 200)
        self.assertIn("argument-hint:", fm)
        self.assertGreater(len(text.split("\n---\n", 1)[1].splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("nse", "nifty", "₹", "${claude_skill_dir}", "not financial advice", "explicit **yes**",
                     "sec_list", "gsm", "09:15", "1,334.00", "34 of 50", "/nse-news", "/nse-entry",
                     "trader/watchlist.json", "trader/cache/"):
            self.assertIn(must, low)
        for never in ("\u2014", "$", "dollar", "nasdaq", "nyse"):
            self.assertNotIn(never, low.replace("${claude_skill_dir}", ""))

    def test_script_has_no_us_leftovers(self):
        with open(os.path.join(ROOT, "nse-scanner", "scan.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("\u2014", src)
        for never in ("nasdaq", "NYSE", "New York", "$%", "\"$", "dollar"):
            self.assertNotIn(never, src)


if __name__ == "__main__":
    unittest.main()
