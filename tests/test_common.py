import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal

from _load import load, FIX

nc = load("nse-scanner", "nse_common")


class Clock(unittest.TestCase):
    def test_now_is_ist_wall_clock(self):
        self.assertEqual(nc.parse_now("2026-11-13 10:05"), datetime.datetime(2026, 11, 13, 4, 35))
        self.assertEqual(nc.parse_now("2026-11-13T10:05:30"), datetime.datetime(2026, 11, 13, 4, 35, 30))
        self.assertEqual(nc.parse_now("2026-11-13 04:35Z"), datetime.datetime(2026, 11, 13, 4, 35))
        self.assertEqual(nc.parse_now("2026-11-13 10:05+05:30"), datetime.datetime(2026, 11, 13, 4, 35))
        self.assertEqual(nc.parse_now("2026-11-13"), datetime.datetime(2026, 11, 13, 3, 45))
        with self.assertRaises(ValueError):
            nc.parse_now("13/11/2026 10:05")
        with self.assertRaises(ValueError):
            nc.parse_now("2026-11-13 25:05")

    def test_epoch_to_ist(self):
        # RELIANCE's first 5-minute bar on 13 Nov 2026 is stamped 09:15 IST
        self.assertEqual(nc.ist_from_epoch(1794541500), datetime.datetime(2026, 11, 13, 9, 15))

    def test_holidays_and_weekends(self):
        self.assertEqual(nc.trading_day(datetime.date(2026, 11, 13)), (True, ""))
        self.assertEqual(nc.trading_day(datetime.date(2026, 11, 14)), (False, "Saturday"))
        self.assertEqual(nc.trading_day(datetime.date(2026, 11, 24)),
                         (False, "NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev"))
        self.assertEqual(nc.next_trading_day(datetime.date(2026, 11, 23)), datetime.date(2026, 11, 25))

    def test_builtin_list_matches_nse_holiday_master(self):
        with open(os.path.join(FIX, "nse-alerts", "holiday-master.json"), encoding="utf-8") as fh:
            hol, years = nc.parse_holiday_master(json.load(fh))
        self.assertEqual(years, [2026])
        self.assertEqual(hol, nc.NSE_HOLIDAYS)

    def test_special_session_opens_a_closed_day(self):
        cfg = {"special_sessions": {"2026-11-08": {"open": "18:15", "close": "19:15", "label": "Muhurat trading"}}}
        self.assertEqual(nc.trading_day(datetime.date(2026, 11, 8), cfg=cfg), (True, "Muhurat trading"))
        st = nc.market_status(nc.parse_now("2026-11-08 18:30"), cfg=cfg)
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"]), ("18:15", "19:15"))

    def test_market_status_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            hol, years = nc.load_holidays(state_dir=tmp)
        ph = lambda s: nc.market_status(nc.parse_now(s), holidays=hol, years=years)
        self.assertEqual(ph("2026-11-13 08:30")["phase"], "before-open")
        self.assertEqual(ph("2026-11-13 09:05")["phase"], "pre-open")
        self.assertEqual(ph("2026-11-13 09:15")["phase"], "open")
        self.assertEqual(ph("2026-11-13 15:29")["phase"], "open")
        self.assertEqual(ph("2026-11-13 15:30")["phase"], "after-close")
        st = ph("2026-11-13 16:00")
        self.assertEqual(st["next_open"], "2026-11-16 09:15")
        st = ph("2026-11-14 11:00")
        self.assertEqual((st["phase"], st["reason"]), ("closed-day", "Saturday"))
        self.assertEqual(ph("2026-11-23 16:00")["next_open"], "2026-11-25 09:15")
        self.assertFalse(ph("2027-01-04 10:00")["holidays_known"])

    def test_holiday_cache_and_extra_holidays(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc.write_json(os.path.join(tmp, nc.HOLIDAY_CACHE),
                          {"holidays": {"2027-01-26": "Republic Day"}, "years": [2027]})
            hol, years = nc.load_holidays({"extra_holidays": {"2027-03-22": "Holi"}}, state_dir=tmp)
        self.assertEqual(hol["2027-01-26"], "Republic Day")
        self.assertEqual(hol["2027-03-22"], "Holi")
        self.assertIn(2027, years)

    def test_one_extra_holiday_does_not_make_its_year_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            hol, years = nc.load_holidays({"extra_holidays": {"2028-01-26": "Republic Day"}}, state_dir=tmp)
            self.assertIn("2028-01-26", hol)
            self.assertNotIn(2028, years)
            _, years = nc.load_holidays({"holiday_years": [2028]}, state_dir=tmp)
            self.assertIn(2028, years)

    def test_bare_hhmm_is_today_ist(self):
        got = nc.to_ist(nc.parse_now("10:05"))
        self.assertEqual((got.hour, got.minute), (10, 5))
        self.assertEqual(got.date(), nc.to_ist(nc.utc_now()).date())


class Money(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(nc.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(nc.inr(56000), "₹56,000")
        self.assertEqual(nc.inr(56000, keep_zeros=True), "₹56,000.00")
        self.assertEqual(nc.inr(999), "₹999")
        self.assertEqual(nc.inr(1000), "₹1,000")
        self.assertEqual(nc.inr(10000000), "₹1,00,00,000")
        self.assertEqual(nc.inr(-500), "-₹500")
        self.assertEqual(nc.inr(5.2), "₹5.20")
        self.assertEqual(nc.inr(1334.04999, decimals=0), "₹1,334")
        self.assertEqual(nc.crore(3328610000000), "₹3,32,861 cr")
        self.assertEqual(nc.crore(72600000), "₹7.26 cr")
        self.assertEqual(nc.crore(25000000, decimals=0), "₹3 cr")

    def test_ticks(self):
        self.assertEqual(nc.tick_size(186.4), Decimal("0.01"))
        self.assertEqual(nc.tick_size(250), Decimal("0.05"))
        self.assertEqual(nc.tick_size(999.95), Decimal("0.05"))
        self.assertEqual(nc.tick_size(1334), Decimal("0.10"))
        self.assertEqual(nc.tick_size(8593), Decimal("0.50"))
        self.assertEqual(nc.tick_size(12889), Decimal("1.00"))
        self.assertEqual(nc.tick_size(25000), Decimal("5.00"))
        self.assertEqual(nc.tick_size(1334, "RELIANCE", {"tick_sizes": {"RELIANCE": 0.05}}), Decimal("0.05"))
        self.assertEqual(nc.round_to_tick(1328.85, Decimal("0.10"), "down"), Decimal("1328.80"))
        self.assertEqual(nc.round_to_tick(1344.35, Decimal("0.10"), "up"), Decimal("1344.40"))
        self.assertEqual(nc.round_to_tick(1344.34, Decimal("0.10")), Decimal("1344.30"))
        self.assertEqual(nc.round_to_tick(1324.2999999999998, Decimal("0.10"), "down"), Decimal("1324.30"))
        self.assertTrue(nc.on_tick(1334.0, Decimal("0.10")))
        self.assertFalse(nc.on_tick(1334.05, Decimal("0.10")))


class Symbols(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(nc.nse_symbol(" nse:reliance "), "RELIANCE")
        self.assertEqual(nc.nse_symbol("TCS.NS"), "TCS")
        self.assertEqual(nc.nse_symbol("^NSEI"), "^NSEI")
        for ok in ("RELIANCE", "M&M", "BAJAJ-AUTO", "360ONE", "M&MFIN"):
            self.assertTrue(nc.is_nse_symbol(ok), ok)
        for bad in ("", "reliance", "TCS.NS", "BRK/B", "A B"):
            self.assertFalse(nc.is_nse_symbol(bad), bad)
        self.assertEqual(nc.yahoo_symbol("m&m"), "M&M.NS")
        self.assertTrue(nc.yahoo_chart_url("M&M").endswith("/v8/finance/chart/M%26M.NS?range=1d&interval=5m"))
        self.assertIn("symbols=RELIANCE.NS,BAJAJ-AUTO.NS&", nc.yahoo_spark_url(["RELIANCE", "BAJAJ-AUTO"]))
        self.assertTrue(nc.yahoo_spark_url(["TCS"], host="query2").startswith("https://query2.finance.yahoo.com/"))


class Charges(unittest.TestCase):
    def test_round_trip(self):
        c = nc.intraday_charges(56, 1334.0, 1344.4)
        # buy 74,704.00, sell 75,286.40: brokerage is capped at ₹20 each side
        self.assertEqual(c["turnover"], 149990.4)
        self.assertEqual(c["brokerage"], 40.0)
        self.assertEqual(c["stt"], 18.82)        # 0.025% of 75,286.40
        self.assertEqual(c["exchange"], 4.6)     # 0.00307% of 1,49,990.40
        self.assertEqual(c["sebi"], 0.15)
        self.assertEqual(c["stamp"], 2.24)       # 0.003% of 74,704
        self.assertEqual(c["gst"], 8.06)         # 18% of 40 + 4.60 + 0.15 + 0.00
        self.assertEqual(c["total"], 73.87)

    def test_small_order_pays_percent_brokerage(self):
        c = nc.intraday_charges(10, 100, 101)
        self.assertEqual(c["brokerage"], 0.6)    # 0.03% of 1,000 + 0.03% of 1,010

    def test_override(self):
        c = nc.intraday_charges(56, 1334.0, 1344.4, {"charges": {"brokerage_cap": 0}})
        self.assertEqual(c["brokerage"], 0.0)


class Orders(unittest.TestCase):
    def test_fingerprint_ignores_tag_and_number_spelling(self):
        leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
               "quantity": 56, "product": "MIS", "order_type": "LIMIT", "price": 1334.0, "validity": "DAY"}
        stop = dict(leg, transaction_type="SELL", order_type="SL", trigger_price=1328.8, price=1322.2)
        a = nc.order_fingerprint({"entry": leg, "stop": stop})
        b = nc.order_fingerprint({"entry": dict(leg, price="1334", tag="x"), "stop": dict(stop, quantity=56.0)})
        self.assertEqual(a, b)
        self.assertEqual(a, nc.order_fingerprint({"entry": dict(leg, quantity="56"), "stop": stop}))
        self.assertNotEqual(a, nc.order_fingerprint({"entry": dict(leg, quantity=57), "stop": stop}))
        self.assertEqual(len(a), 16)

    def test_fingerprint_covers_target_and_flat_by(self):
        leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
               "quantity": 56, "product": "MIS", "order_type": "LIMIT", "price": 1334.0, "validity": "DAY"}
        base = {"entry": leg, "stop": None, "target": {"price": 1344.4}, "flat_by": "15:10"}
        fp = nc.order_fingerprint(base)
        self.assertEqual(fp, nc.order_fingerprint(dict(base, target={"price": "1344.40", "how": "alert"})))
        self.assertNotEqual(fp, nc.order_fingerprint(dict(base, target={"price": 1350.0})))
        self.assertNotEqual(fp, nc.order_fingerprint(dict(base, flat_by="15:24")))
        self.assertEqual(nc.order_target({"plan": {"target": 1344.4}}), 1344.4)


class Config(unittest.TestCase):
    def test_defaults_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "config.json")
            self.assertEqual(nc.load_config(p)["flat_by"], "15:10")
            nc.write_json(p, {"budget": 50000, "charges": {"brokerage_cap": 0}})
            cfg = nc.load_config(p)
        self.assertEqual(cfg["budget"], 50000)
        self.assertEqual(cfg["charges"], {"brokerage_cap": 0})
        self.assertEqual(cfg["max_loss_per_trade_inr"], 500)


if __name__ == "__main__":
    unittest.main()
