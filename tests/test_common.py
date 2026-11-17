import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

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


class OrderTick(unittest.TestCase):
    def test_near_a_slab_edge_takes_the_coarser_tick(self):
        cfg = nc.load_config(None)
        # 997.97 is on the 0.05 slab but within 10% of 1000: use 0.10 so the price is valid either way
        self.assertEqual(nc.order_tick(997.97, "X", cfg), Decimal("0.10"))
        self.assertEqual(nc.tick_size(997.97), Decimal("0.05"))
        self.assertEqual(nc.order_tick(1334, "RELIANCE", cfg), Decimal("0.10"))
        self.assertEqual(nc.order_tick(150, "X", cfg), Decimal("0.01"))
        self.assertEqual(nc.order_tick(240, "X", cfg), Decimal("0.05"))      # 10% above is over 250

    def test_override_and_band_switch(self):
        self.assertEqual(nc.order_tick(997.97, "X", {"tick_sizes": {"X": 0.05}}), Decimal("0.05"))
        self.assertEqual(nc.order_tick(997.97, "X", {"tick_band_pct": 0}), Decimal("0.05"))

    def test_a_price_on_the_coarse_grid_is_on_every_finer_grid(self):
        cfg = nc.load_config(None)
        for price in (249.7, 998.3, 1003.2, 4890.4, 5120.0, 9800.5, 19999.0):
            tick = nc.order_tick(price, "X", cfg)
            q = nc.round_to_tick(price, tick)
            for finer in (Decimal("0.01"), Decimal("0.05"), Decimal("0.10"), Decimal("0.50"), Decimal("1.00")):
                if finer <= tick:
                    self.assertTrue(nc.on_tick(q, finer), (price, tick, finer))


class LossAtStop(unittest.TestCase):
    CFG = nc.load_config(None)

    def test_the_max_loss_is_measured_at_the_sl_limit(self):
        self.assertEqual(nc.stop_limit_price(1328.80, "RELIANCE", self.CFG), Decimal("1322.10"))
        self.assertEqual(nc.worst_exit_price(1328.80, "RELIANCE", self.CFG), Decimal("1322.10"))
        loss = nc.loss_at_stop(56, 1334, 1328.80, "RELIANCE", self.CFG)
        self.assertEqual(loss["per_share"], Decimal("11.90"))
        self.assertEqual(loss["gross"], Decimal("666.40"))
        self.assertEqual(loss["total"], loss["gross"] + loss["charges"])
        self.assertGreater(loss["total"], 500)

    def test_trigger_basis_is_the_old_behaviour(self):
        cfg = dict(self.CFG, loss_basis="trigger")
        self.assertEqual(nc.worst_exit_price(1328.80, "RELIANCE", cfg), Decimal("1328.80"))
        self.assertEqual(nc.loss_at_stop(56, 1334, 1328.80, "RELIANCE", cfg)["gross"], Decimal("291.20"))

    def test_shares_that_fit_the_cap(self):
        n = nc.max_loss_shares(500, 1334, 1328.80, "RELIANCE", self.CFG)
        self.assertEqual(n, 37)
        self.assertLessEqual(nc.loss_at_stop(n, 1334, 1328.80, "RELIANCE", self.CFG)["total"], 500)
        self.assertGreater(nc.loss_at_stop(n + 1, 1334, 1328.80, "RELIANCE", self.CFG)["total"], 500)

    def test_the_four_limits(self):
        limits = {l["name"]: l["shares"] for l in nc.size_limits(1334, 1328.80, "RELIANCE", self.CFG)}
        self.assertEqual(limits, {"max loss": 37, "position cap": 56, "max order": 59, "cash": 67})
        self.assertEqual(nc.size_qty(1334, 1328.80, "RELIANCE", self.CFG), 37)

    def test_a_losing_day_shrinks_the_account_base(self):
        pf = {"cash": 60000, "positions": []}
        total = nc.portfolio_total(pf)
        self.assertEqual(total, 60000)
        self.assertEqual(nc.account_base(self.CFG, total), 60000)
        self.assertEqual(nc.account_base(self.CFG, None), 100000)
        self.assertEqual(nc.account_base(self.CFG, 250000), 100000)
        small = {l["name"]: l["shares"] for l in nc.size_limits(1334, 1328.80, "RELIANCE", self.CFG,
                                                                  portfolio_total=total, cash=60000)}
        self.assertLess(small["position cap"], 56)

    def test_portfolio_total_values_holdings_at_price_else_cost(self):
        pf = {"cash": 50000, "positions": [{"ticker": "RELIANCE", "shares": 10,
                                            "lots": [{"shares": 10, "cost": 13300}]}]}
        self.assertEqual(nc.portfolio_total(pf, {"RELIANCE": 1334}), 63340)
        self.assertEqual(nc.portfolio_total(pf, {}), 63300)
        self.assertIsNone(nc.portfolio_total(None))


class ReadmeExample(unittest.TestCase):
    """The RELIANCE numbers the README quotes: 37 shares, worst fill 1,322.10."""
    CFG = nc.load_config(None)

    def test_the_numbers_in_the_readme(self):
        self.assertEqual(nc.size_qty(1334, 1328.80, "RELIANCE", self.CFG), 37)
        self.assertEqual(37 * Decimal("1334"), Decimal("49358"))
        loss = nc.loss_at_stop(37, 1334, 1328.80, "RELIANCE", self.CFG)
        self.assertEqual((loss["gross"], loss["charges"], loss["total"]),
                         (Decimal("440.30"), Decimal("52.18"), Decimal("492.48")))
        self.assertEqual(nc.max_loss_shares(500, 1334, 1328.80, "RELIANCE", self.CFG) + 1, 38)
        self.assertEqual(nc.loss_at_stop(38, 1334, 1328.80, "RELIANCE", self.CFG)["total"], Decimal("505.79"))
        self.assertEqual(nc.loss_at_stop(56, 1334, 1328.80, "RELIANCE", self.CFG)["total"], Decimal("739.91"))
        at_target = nc.intraday_charges(37, 1334, 1344.40, self.CFG)["total"]
        self.assertEqual(at_target, 52.71)
        self.assertEqual(round(37 * 10.40 - at_target, 2), 332.09)
        paper = nc.intraday_charges(37, 1334, 1344.30, self.CFG)["total"]
        self.assertEqual((paper, round(37 * 10.30, 2), round(37 * 10.30 - paper, 2)), (52.70, 381.10, 328.40))


class TraderHome(unittest.TestCase):
    def test_env_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(nc.trader_home(cwd=tmp, env={nc.HOME_ENV: tmp}), os.path.abspath(tmp))

    def test_finds_trader_in_a_parent_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(os.path.join(tmp, "trader"))
            nc.write_json(os.path.join(tmp, "trader", "config.json"), {})
            deep = os.path.join(tmp, "a", "b")
            os.makedirs(deep)
            self.assertEqual(nc.trader_home(cwd=deep, env={}), os.path.join(tmp, "trader"))

    def test_falls_back_to_trader_in_the_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            with mock.patch.dict(os.environ, {"HOME": tmp}):      # no ~/.day-trader-india either
                self.assertEqual(nc.trader_home(cwd=tmp, env={}), os.path.join(tmp, "trader"))

    def test_a_missing_config_is_refused_when_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "config.json")
            with self.assertRaises(nc.ConfigMissing) as cm:
                nc.load_config(p, require=True)
            self.assertIn(p, str(cm.exception))
            self.assertEqual(nc.load_config(p)["budget"], 100000)       # not required: defaults
            nc.write_json(p, {"budget": 5000})
            self.assertEqual(nc.load_config(p, require=True)["budget"], 5000)

    def test_test_mode_needs_the_env_var(self):
        self.assertFalse(nc.test_mode({}))
        self.assertFalse(nc.test_mode({nc.TEST_ENV: "0"}))
        self.assertTrue(nc.test_mode({nc.TEST_ENV: "1"}))


class HolidayRefresh(unittest.TestCase):
    DOC = {"CM": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"},
                  {"tradingDate": "25-Dec-2027", "description": "Christmas"}]}

    def test_an_unknown_year_is_stale_and_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.datetime(2027, 1, 4, 5, 0)
            today = datetime.date(2027, 1, 4)
            stale, why = nc.holidays_stale(tmp, today, {}, now)
            self.assertTrue(stale)
            self.assertIn("2027", why)
            hol, years, note = nc.ensure_holidays({}, tmp, today, now, fetch=lambda: self.DOC)
            self.assertIn(2027, years)
            self.assertEqual(hol["2027-01-26"], "Republic Day")
            self.assertIn("refreshed", note)
            self.assertFalse(nc.holidays_stale(tmp, today, {}, now)[0])

    def test_a_failed_refresh_never_raises(self):
        def boom():
            raise nc.HolidayError("NSE answered HTTP 403")
        with tempfile.TemporaryDirectory() as tmp:
            hol, years, note = nc.ensure_holidays({}, tmp, datetime.date(2027, 1, 4),
                                                  datetime.datetime(2027, 1, 4, 5, 0), fetch=boom)
            self.assertNotIn(2027, years)
            self.assertIn("could not refresh", note)

    def test_an_old_cache_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc.update_holiday_cache(self.DOC, tmp, datetime.datetime(2027, 1, 1, 0, 0))
            today = datetime.date(2027, 3, 20)
            self.assertFalse(nc.holidays_stale(tmp, datetime.date(2027, 1, 10), {}, datetime.datetime(2027, 1, 10))[0])
            stale, why = nc.holidays_stale(tmp, today, {}, datetime.datetime(2027, 3, 20))
            self.assertTrue(stale)
            self.assertIn("older", why)

    def test_december_wants_next_years_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale, why = nc.holidays_stale(tmp, datetime.date(2026, 12, 10), {}, datetime.datetime(2026, 12, 10))
            self.assertTrue(stale)
            self.assertIn("2027", why)

    def test_fetch_retries_with_cookies_after_a_403(self):
        calls = []

        def fake_get(url, timeout, headers, opener):
            calls.append((url, opener is not None))
            if url == nc.HOLIDAY_URL and not any(o for _, o in calls):
                return 403, ""
            if url == nc.HOLIDAY_URL:
                return 200, json.dumps(self.DOC)
            return 200, "<html></html>"
        doc = nc.fetch_holiday_master(get=fake_get)
        self.assertEqual(doc, self.DOC)
        self.assertEqual([u for u, _ in calls], [nc.HOLIDAY_URL, nc.NSE_HOME_URL, nc.HOLIDAY_URL])


class FreshPrice(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 13, 4, 35)

    def doc(self, t):
        return {"prices": {"RELIANCE": 1334.0}, "detail": {"RELIANCE": {"time": t}}}

    def test_fresh_stale_future_and_missing(self):
        p, t, ok = nc.fresh_price(self.doc("2026-11-13T04:33:00Z"), "RELIANCE", self.NOW)
        self.assertEqual((p, ok), (1334.0, True))
        self.assertFalse(nc.fresh_price(self.doc("2026-11-13T04:20:00Z"), "RELIANCE", self.NOW)[2])
        self.assertTrue(nc.fresh_price(self.doc("2026-11-13T04:20:00Z"), "RELIANCE", self.NOW, minutes=30)[2])
        self.assertFalse(nc.fresh_price(self.doc("2026-11-13T05:30:00Z"), "RELIANCE", self.NOW)[2])
        self.assertEqual(nc.fresh_price(self.doc("2026-11-13T04:33:00Z"), "TCS", self.NOW), (None, None, False))
        self.assertEqual(nc.fresh_price(None, "TCS", self.NOW), (None, None, False))


if __name__ == "__main__":
    unittest.main()
