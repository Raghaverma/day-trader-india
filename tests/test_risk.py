import copy
import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("nse-risk", "check")
nc = load("nse-risk", "nse_common")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-reliance.json")
SEC_PATH = os.path.join(FIX, "nse-scanner", "sec_list.csv.gz")
SEC = check.read_sec_list(SEC_PATH)
SEC_INFO = {"source": SEC_PATH, "date": "2026-11-13"}
NOW = "2026-11-13 10:05"
TODAY = "2026-11-13"


def at(text):
    return nc.parse_now(text)


def cfg(**over):
    with open(CFG_PATH, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def leg(o, which, **over):
    """A copy of order o with fields of one leg changed (None removes a field)."""
    o = copy.deepcopy(o)
    for k, v in over.items():
        if v is None:
            o[which].pop(k, None)
        else:
            o[which][k] = v
    return o


def kite_order(sym, qty, entry, stop, target, stop_limit=None):
    e = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY",
         "quantity": qty, "product": "MIS", "order_type": "LIMIT", "price": entry, "validity": "DAY",
         "tag": "dtindia"}
    s = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
         "quantity": qty, "product": "MIS", "order_type": "SL", "trigger_price": stop,
         "price": stop_limit if stop_limit is not None else stop, "validity": "DAY", "tag": "dtindia"}
    return {"symbol": sym, "entry": e, "stop": s, "target": {"price": target, "how": "alert"},
            "flat_by": "15:10", "plan": {"entry": entry, "stop": stop, "target": target}}


def sell(sym, qty, order_type="MARKET", price=None):
    e = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
         "quantity": qty, "product": "MIS", "order_type": order_type, "validity": "DAY", "tag": "dtindia"}
    if price is not None:
        e["price"] = price
    return {"symbol": sym, "entry": e, "stop": None, "flat_by": "15:10"}


def run_check(orders, c=None, now=NOW, **kw):
    kw.setdefault("sec", SEC)
    kw.setdefault("sec_info", SEC_INFO)
    return check.check(orders, c if c is not None else cfg(), now=at(now), **kw)


def placed(sym, side="buy", day=TODAY, max_loss=364.82, kind=None, status="placed", **extra):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T10:10:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    r.update(extra)
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])



def skill_md(folder):
    """(frontmatter dict of simple keys, body lines) of a SKILL.md."""
    with open(os.path.join(ROOT, folder, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("---\n")
    head, body = text[4:].split("\n---\n", 1)
    meta, key = {}, None
    for line in head.splitlines():
        if line and not line.startswith(" ") and ":" in line:
            key, val = line.split(":", 1)
            meta[key] = val.strip()
        elif key:
            meta[key] += " " + line.strip()
    return meta, body.strip().splitlines(), text

class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        meta, body, text = skill_md("nse-risk")
        self.assertEqual(meta["name"], "nse-risk")
        self.assertGreater(len(meta["description"]), 200)
        self.assertIn("argument-hint", meta)
        self.assertGreater(len(body), 60)
        for want in ("${CLAUDE_SKILL_DIR}/check.py", "--sec-list", "Not financial advice", "**yes**", "NSE",
                     "₹", "No Band", "GSM", "BE/BZ", "IST", "nse-broker"):
            self.assertIn(want, text, want)
        self.assertNotIn("$", text.replace("${CLAUDE_SKILL_DIR}", ""))


class TheWorkedExample(unittest.TestCase):
    def test_reliance_passes_every_check(self):
        res = run_check([rel()])
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["reasons"], r["warnings"]), ([], []))
        self.assertEqual((r["kind"], r["entry_ref"], r["entry_ref_from"]), ("entry", 1334.0, "limit"))
        self.assertEqual((r["stop"], r["stop_limit"], r["target"]), (1328.8, 1322.1, 1344.4))
        self.assertEqual((r["risk_per_share"], r["max_loss_gross"], r["charges_at_stop"]), (5.2, 291.2, 73.62))
        self.assertEqual((r["max_loss"], r["charges_at_target"], r["reward_risk"]), (364.82, 73.87, 1.39))
        self.assertEqual((r["series"], r["band"], r["remarks"]), ("EQ", "No Band", "-"))
        self.assertEqual(r["value_inr"], 74704.0)
        self.assertEqual(res["cash_after"], 25296.0)
        self.assertEqual((res["date"], res["now_ist"], res["broker"]), (TODAY, "2026-11-13 10:05", "paper"))
        self.assertEqual(r["fingerprint"], nc.order_fingerprint(rel()))
        self.assertIn("max loss ₹364.82 with charges (₹291.20 + ₹73.62; limit ₹500), reward:risk 1.39 after "
                      "charges (min 1)", r["info"])

    def test_charges_decide_the_max_loss(self):
        res = run_check([rel()], cfg(max_loss_per_trade_inr=360))
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 56 shares x 5.20 risk a share = ₹291.20 at the stop, plus ₹73.62 charges = "
                          "₹364.82, over your ₹360 max loss per trade"])
        res = run_check([rel()], cfg(max_loss_per_trade_inr=360, include_charges=False))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual((res["results"][0]["max_loss"], res["results"][0]["reward_risk"]), (291.2, 2.0))

    def test_reward_risk_after_charges(self):
        o = rel(target={"price": 1339.6, "how": "alert"})
        res = run_check([o])
        self.assertIn("reward:risk: 0.66 after charges (target 1,339.60, entry 1,334.00, stop 1,328.80) is under "
                      "your minimum 1", reasons(res))
        self.assertEqual(verdicts(run_check([o], cfg(min_reward_risk=0.5))), ["PASS"])

    def test_the_fingerprint_is_nse_commons(self):
        o = rel()
        self.assertEqual(run_check([o])["results"][0]["fingerprint"], nc.order_fingerprint(o))
        self.assertEqual(nc.order_fingerprint(dict(o, why="x", plan={})), nc.order_fingerprint(o))
        self.assertNotEqual(nc.order_fingerprint(leg(o, "entry", quantity=57)), nc.order_fingerprint(o))
        self.assertNotEqual(nc.order_fingerprint(leg(o, "stop", trigger_price=1328.7)), nc.order_fingerprint(o))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--sec-list", SEC_PATH, "--now", NOW,
                                   "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("PASS  #1 BUY RELIANCE 56 shares, entry", text)
            self.assertIn("NSE RELIANCE: series EQ, No Band (an F&O stock: NSE sets a dynamic band), no surveillance",
                          text)
            self.assertIn("entry 1,334.00 (the limit price), stop 1,328.80 (SL limit 1,322.10), target 1,344.40: "
                          "risk 5.20 a share", text)
            self.assertIn("1 passed, 0 blocked at 2026-11-13 10:05 IST", text)
            self.assertIn("Not financial advice", text)
            self.assertIn("explicit yes", text)
            self.assertNotIn("$", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["fingerprint"], nc.order_fingerprint(rel()))
            self.assertEqual(rec["results"][0]["verdict"], "PASS")
            self.assertEqual(rec["sec_list"]["source"], SEC_PATH)


class SecurityList(unittest.TestCase):
    """series, price band and surveillance from NSE's sec_list.csv (13 Nov 2026)."""

    def test_the_fixture_reads(self):
        self.assertEqual(SEC["RELIANCE"], [{"series": "EQ", "name": "RELIANCE INDUSTRIES LIMITED",
                                            "band": "No Band", "remarks": "-"}])
        self.assertGreater(len(SEC), 3000)
        self.assertEqual(check.sec_lookup(SEC, "ABAN-BE", ["EQ"])["series"], "BE")
        self.assertEqual(check.sec_lookup(SEC, "ELECTCAST", ["EQ"])["series"], "EQ")   # EQ and W1 rows
        self.assertIsNone(check.sec_lookup(SEC, "NOTASTOCK", ["EQ"]))

    def test_plain_csv_and_gzip_both_read(self):
        with gzip.open(SEC_PATH, "rt", encoding="utf-8") as fh:
            text = fh.read()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sec_list.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.assertEqual(check.read_sec_list(p), SEC)
            with self.assertRaises(check.RiskError):
                bad = os.path.join(d, "bad.csv")
                with open(bad, "w") as fh:
                    fh.write("a,b\n1,2\n")
                check.read_sec_list(bad)

    def test_series_be_is_blocked(self):
        res = run_check([kite_order("ABAN", 20, 30.0, 29.5, 31.5, 29.35)], cfg(min_circuit_band_pct=0))
        self.assertEqual(res["results"][0]["reasons"],
                         ["series: ABAN trades in series BE (trade-for-trade: every trade settles by delivery, so no "
                          "intraday); you allow EQ"])
        res = run_check([kite_order("ABAN-BE", 20, 30.0, 29.5, 31.5, 29.35)], cfg(min_circuit_band_pct=0))
        self.assertIn("series: ABAN trades in series BE", reasons(res))
        res = run_check([kite_order("ABAN", 20, 30.0, 29.5, 31.5, 29.35)],
                        cfg(min_circuit_band_pct=0, allowed_series=["EQ", "BE"]))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_5_percent_band_is_blocked(self):
        res = run_check([kite_order("A2ZINFRA", 30, 20.0, 19.6, 21.0, 19.5)])
        self.assertEqual(res["results"][0]["reasons"],
                         ["band: A2ZINFRA has a 5% price band, under your 10% minimum: thin stocks can lock at the "
                          "limit and leave you unable to sell"])
        self.assertEqual(verdicts(run_check([kite_order("A2ZINFRA", 30, 20.0, 19.6, 21.0, 19.5)],
                                            cfg(min_circuit_band_pct=5))), ["PASS"])

    def test_gsm_is_blocked(self):
        # BLUECOAST: series EQ, 10% band, "GSM STAGE - 0"
        o = kite_order("BLUECOAST", 10, 60.0, 59.0, 62.5, 58.7)
        res = run_check([o])
        self.assertEqual(res["results"][0]["reasons"],
                         ["surveillance: BLUECOAST is under GSM STAGE - 0 (NSE's surveillance measures: tighter "
                          "limits, higher margins, sometimes trade-for-trade)"])
        res = run_check([o], cfg(block_surveillance=False))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("GSM STAGE - 0 (block_surveillance is off)", warnings(res))

    def test_a_no_band_fno_stock_is_allowed(self):
        for sym in ("RELIANCE", "SBIN", "M&M"):
            self.assertEqual(SEC[sym][0]["band"], "No Band")
        res = run_check([kite_order("SBIN", 50, 960.0, 955.0, 972.0, 950.2)])
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("No Band", " ".join(res["results"][0]["info"]))

    def test_unknown_symbol_and_no_list(self):
        res = run_check([kite_order("NOTASTOCK", 10, 100.0, 99.0, 103.0, 98.5)])
        self.assertIn("security: NOTASTOCK is not in NSE's security list (sec_list.csv of 2026-11-13)",
                      reasons(res))
        res = run_check([rel()], sec=None, sec_info={"error": "NSE's security list could not be fetched (offline)"})
        self.assertIn("security: NSE's security list isn't loaded", reasons(res))
        self.assertIn("entries are blocked until it loads", res["warnings"][0])

    def test_load_caches_for_the_day_and_falls_back(self):
        with gzip.open(SEC_PATH, "rt", encoding="utf-8") as fh:
            text = fh.read()
        calls = []

        def ok():
            calls.append(1)
            return text

        def down():
            raise OSError("network down")

        with tempfile.TemporaryDirectory() as d:
            rows, info = check.load_sec_list(None, d, "2026-11-13", fetch=ok)
            self.assertEqual((len(rows), info["source"], len(calls)), (len(SEC), check.SEC_LIST_URL, 1))
            self.assertTrue(os.path.exists(os.path.join(d, "sec_list-2026-11-13.csv")))
            rows, info = check.load_sec_list(None, d, "2026-11-13", fetch=ok)
            self.assertEqual((len(calls), info["date"]), (1, "2026-11-13"))       # from the cache
            rows, info = check.load_sec_list(None, d, "2026-11-16", fetch=down)
            self.assertEqual(len(rows), len(SEC))
            self.assertIn("using the copy from 2026-11-13", info["warning"])
        with tempfile.TemporaryDirectory() as d:
            rows, info = check.load_sec_list(None, d, "2026-11-16", fetch=down)
            self.assertIsNone(rows)
            self.assertIn("could not be fetched (network down)", info["error"])


class Window(unittest.TestCase):
    def one(self, now, **over):
        return run_check([rel()], cfg(**over), now=now)

    def test_entry_window_in_ist(self):
        cases = [("2026-11-13 09:29", "window: no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-11-13 14:31", "window: no new entries after 14:30 IST (it is 14:31)"),
                 ("2026-11-13 09:05", "window: the NSE market is closed (Fri 2026-11-13 09:05 IST; the session is "
                                      "09:15 to 15:30 IST)"),
                 ("2026-11-13 15:35", "window: the NSE market is closed"),
                 ("2026-11-14 10:05", "window: the NSE market is closed today (Saturday, Sat 2026-11-14 10:05 IST)"),
                 ("2026-11-13 03:50Z", "window: no new entries before 09:30 IST (it is 09:20)")]
        for now, want in cases:
            self.assertIn(want, reasons(self.one(now)), now)
        for now in ("2026-11-13 09:30", "2026-11-13 14:30", "2026-11-13 04:35Z", "2026-11-13 10:05+05:30"):
            self.assertEqual(verdicts(self.one(now)), ["PASS"], now)

    def test_a_holiday_is_closed(self):
        res = self.one("2026-11-24 10:05")
        self.assertIn("window: the NSE market is closed today (NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev",
                      reasons(res))
        res = self.one("2026-11-25 10:05", extra_holidays={"2026-11-25": "a holiday in the config"})
        self.assertIn("NSE holiday: a holiday in the config", reasons(res))

    def test_an_unknown_holiday_year_is_a_warning_not_a_block(self):
        res = self.one("2027-01-05 10:05")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("NSE's holiday list for 2027 isn't known here yet", " ".join(res["warnings"]))

    def test_special_session(self):
        mu = {"2026-11-08": {"open": "18:15", "close": "19:15", "label": "Muhurat trading"}}
        res = self.one("2026-11-08 18:30", special_sessions=mu)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("special session today (Muhurat trading, 18:15-19:15 IST)", " ".join(res["warnings"]))
        self.assertIn("closed today (Sunday", reasons(self.one("2026-11-08 18:30")))

    def test_window_off_and_bad_times(self):
        self.assertEqual(verdicts(self.one("2026-11-14 10:05", no_entries_before=None, no_entries_after=None)),
                         ["PASS"])
        with self.assertRaises(check.RiskError):
            self.one(NOW, no_entries_before="9.30am")
        with self.assertRaises(check.RiskError):
            self.one(NOW, special_sessions={"2026-11-13": {"open": "6pm", "close": "19:15"}})

    def test_ist_not_the_machine_clock(self):
        self.assertEqual(nc.to_ist(at("2026-11-13 10:05")).strftime("%H:%M"), "10:05")
        self.assertEqual(at("2026-11-13 10:05").strftime("%H:%M"), "04:35")


class OrderFormat(unittest.TestCase):
    def test_product_exchange_variety(self):
        self.assertIn("product: the entry leg is CNC (delivery); this pack day-trades MIS (intraday) only",
                      reasons(run_check([leg(leg(rel(), "entry", product="CNC"), "stop", product="CNC")])))
        self.assertIn("exchange: the entry leg is on 'BSE'",
                      reasons(run_check([leg(rel(), "entry", exchange="BSE")])))
        self.assertIn("variety must be \"regular\"", reasons(run_check([leg(rel(), "entry", variety="amo")])))
        self.assertIn("whole number of shares", reasons(run_check([leg(rel(), "entry", quantity=56.5)])))
        self.assertIn("a LIMIT entry leg needs a price", reasons(run_check([leg(rel(), "entry", price=None)])))
        self.assertIn("validity must be DAY or IOC", reasons(run_check([leg(rel(), "entry", validity="TTL")])))
        self.assertIn("as a number", warnings(run_check([leg(rel(), "entry", quantity="56")])))

    def test_prices_on_the_tick(self):
        res = run_check([leg(rel(), "entry", price=1334.05)])
        self.assertIn("tick: the entry leg's price 1334.05 is not on the 0.10 tick", reasons(res))
        res = run_check([leg(rel(), "stop", trigger_price=1328.85)])
        self.assertIn("tick: the stop leg's trigger_price 1328.85 is not on the 0.10 tick", reasons(res))
        res = run_check([rel(target={"price": 1344.45})])
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("the target 1344.45 is not on the 0.10 tick", warnings(res))

    def test_the_stop_leg(self):
        self.assertIn("stop: the SL limit 1,330.00 is above its trigger 1,328.80",
                      reasons(run_check([leg(rel(), "stop", price=1330.0)])))
        self.assertIn("stop: the stop leg is 50 shares, the entry 56: they must match",
                      reasons(run_check([leg(rel(), "stop", quantity=50)])))
        self.assertIn("stop: the stop leg must be a SELL for a BUY entry",
                      reasons(run_check([leg(rel(), "stop", transaction_type="BUY")])))
        self.assertIn("stop: the stop leg must be an SL or SL-M order",
                      reasons(run_check([leg(rel(), "stop", order_type="LIMIT")])))
        res = run_check([leg(rel(), "stop", trigger_price=1335.0, price=1330.0)])
        self.assertIn("stop: the stop 1,335.00 must be below the entry 1,334.00 for a BUY", reasons(res))
        res = run_check([leg(rel(), "stop", order_type="SL-M", price=None)])
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("market protection", warnings(res))

    def test_a_stop_is_required(self):
        o = rel(stop=None, plan={"entry": 1334.0})
        res = run_check([o])
        self.assertIn("stop: no stop", reasons(res))
        res = run_check([o], cfg(require_stop=False, min_reward_risk=0))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))
        res = run_check([rel(stop=None)])
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 364.82)

    def test_fno_and_bad_symbols(self):
        fut = leg(leg(rel(symbol="RELIANCE26NOVFUT"), "entry", exchange="NFO", tradingsymbol="RELIANCE26NOVFUT"),
                  "stop", exchange="NFO", tradingsymbol="RELIANCE26NOVFUT")
        res = run_check([fut])
        self.assertIn("asset: RELIANCE26NOVFUT looks like an F&O contract on NFO; F&O is off", reasons(res))
        opt = kite_order("NIFTY26N1324000CE", 75, 120.0, 100.0, 160.0)
        self.assertIn("looks like an F&O contract", reasons(run_check([opt])))
        self.assertIn("not an NSE stock symbol", reasons(run_check([kite_order("^NSEI", 1, 100.0, 99.0, 103.0)])))
        self.assertIn("not an NSE stock symbol", reasons(run_check([kite_order("RELIANCE.BO", 1, 100.0, 99.0, 103.0)])))

    def test_no_sell_entries_unless_allowed(self):
        res = run_check([sell("RELIANCE", 56)])
        self.assertIn("short: selling more RELIANCE than you hold (0 shares); short selling is off", reasons(res))


class AccountAndDay(unittest.TestCase):
    def test_cash_margin_and_position(self):
        res = run_check([rel()], portfolio={"cash": 50000, "positions": []})
        self.assertIn("cash: needs ₹74,704 but only ₹50,000 cash; that would be margin", reasons(res))
        res = run_check([rel()], cfg(allow_margin=True, max_position_pct=200), portfolio={"cash": 50000, "positions": []})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        res = run_check([rel()], cfg(max_position_pct=50, max_order_inr=100000))
        self.assertIn("position: RELIANCE would be ₹74,704, 74.7% of the ₹1,00,000 account, over your 50% cap",
                      reasons(res))
        res = run_check([rel()], cfg(max_order_inr=70000))
        self.assertIn("size: ₹74,704 is over your ₹70,000 max order", reasons(res))

    def test_blocked_list(self):
        self.assertIn("blocked: RELIANCE is on your blocked list", reasons(run_check([rel()], cfg(blocked=["reliance"]))))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("TCS"), placed("INFY")]}
        res = run_check([rel()], state=two, portfolio={"cash": 300000, "positions": []})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("today before this: 2 of 3 trades, ₹729.64 of ₹1,500 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("TCS"), placed("INFY"), placed("SBIN", max_loss=500)]}
        res = run_check([rel()], state=three, portfolio={"cash": 300000, "positions": []})
        why = reasons(res)
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: ₹1,229.64 already at risk today plus ₹364.82 for this one = ₹1,594.46, over your "
                      "₹1,500", why)
        yesterday = {"orders": [placed(s, day="2026-11-12") for s in ("TCS", "INFY", "SBIN")]}
        self.assertEqual(verdicts(run_check([rel()], state=yesterday)), ["PASS"])

    def test_a_real_loss_worse_than_planned_counts(self):
        st = {"orders": [placed("TCS", max_loss=364.82),
                         placed("TCS", side="sell", status="closed", kind="exit", max_loss=None, pnl_net=-1200.0)]}
        res = run_check([rel()], state=st)
        self.assertIn("daily loss: ₹1,200 already at risk today plus ₹364.82", reasons(res))
        self.assertIn("TCS lost ₹1,200 today, ₹835.18 more than its planned max loss", " ".join(res["warnings"]))
        self.assertEqual(res["today"]["realised"], {"TCS": -1200.0})

    def test_exits_stops_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("TCS"), placed("TCS", side="sell", kind="exit", max_loss=None),
                         placed("TCS", side="sell", kind="stop", leg="stop", max_loss=None),
                         placed("INFY", status="declined"), placed("SBIN", status="failed")]}
        res = run_check([rel()], state=st)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["orders_placed"]), (1, 3))

    def test_the_batch_counts_toward_the_day(self):
        orders = [kite_order(s, 5, 960.0, 955.0, 972.0, 950.2) for s in ("SBIN", "TCS", "INFY", "HDFCBANK")]
        orders[1] = kite_order("TCS", 2, 3000.0, 2990.0, 3030.0, 2975.0)
        res = run_check(orders)
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier in this batch", reasons(res, 3))

    def test_duplicates(self):
        res = run_check([rel()], state={"orders": [placed("RELIANCE")]})
        self.assertIn("duplicate: a BUY RELIANCE was already placed today", reasons(res))
        res = run_check([rel(), rel()], portfolio={"cash": 1000000, "positions": []},
                        c=cfg(max_position_pct=100))
        self.assertIn("duplicate: a second BUY RELIANCE in this batch", reasons(res, 1))
        st = {"orders": [placed("T%d" % i, max_loss=1) for i in range(10)]}
        res = run_check([rel()], state=st)
        self.assertIn("daily limit: 10 orders placed today", reasons(res))

    def test_a_fresh_higher_price_is_the_entry_for_a_market_order(self):
        mkt = leg(rel(), "entry", order_type="MARKET", price=None)
        res = run_check([mkt], prices={"RELIANCE": 1336.0}, price_times={"RELIANCE": at("2026-11-13 10:00")})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1336.0, "price", 7.2))
        res = run_check([mkt], prices={"RELIANCE": 1336.0}, price_times={"RELIANCE": at("2026-11-13 09:40")})
        self.assertEqual(res["results"][0]["entry_ref"], 1334.0)
        self.assertIn("older than 15 minutes", warnings(res))

    def test_live_broker_modes_warn(self):
        res = run_check([rel()], cfg(broker="kite"))
        self.assertIn("broker \"kite\": these orders are for your real Zerodha account, real money", res["warnings"][0])


class Exits(unittest.TestCase):
    PF = {"cash": 25296.0, "positions": [{"ticker": "RELIANCE", "shares": 56,
                                          "lots": [{"date": TODAY, "shares": 56, "cost": 74704.0}]}]}

    def test_getting_out_is_never_blocked_by_a_day_limit(self):
        full = {"orders": [placed("RELIANCE"), placed("TCS"), placed("INFY")] +
                          [placed("T%d" % i, max_loss=1) for i in range(7)] +
                          [placed("RELIANCE", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["RELIANCE"], max_order_inr=100, max_daily_loss_inr=50)
        res = run_check([sell("RELIANCE", 56)], c, now="2026-11-13 15:12", portfolio=self.PF, state=full)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        gsm = {"cash": 0, "positions": [{"ticker": "BLUECOAST", "shares": 10, "lots": []}]}
        res = run_check([sell("BLUECOAST", 10)], cfg(), portfolio=gsm)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("exit, not blocked: surveillance: BLUECOAST is under GSM STAGE - 0", warnings(res))
        res = run_check([sell("RELIANCE", 56)], cfg(), now="2026-11-14 12:00", portfolio=self.PF)
        self.assertEqual(verdicts(res), ["PASS"])

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        res = run_check([leg(sell("RELIANCE", 56), "entry", product="CNC")], portfolio=self.PF)
        self.assertIn("product:", reasons(res))
        res = run_check([sell("RELIANCE", 60)], portfolio=self.PF)
        self.assertIn("short: selling more RELIANCE than you hold (56 shares)", reasons(res))
        res = run_check([sell("RELIANCE", 56, "LIMIT")], portfolio=self.PF)
        self.assertIn("needs a price", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = run_check([rel(), sell("RELIANCE", 56)])
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class Cli(unittest.TestCase):
    def test_errors_are_one_line(self):
        for argv, word in ((["--orders", "/nonexistent.json"], "no orders file"),
                           (["--orders", ORDERS_PATH, "--now", "noon"], "--now wants"),
                           (["--orders", ORDERS_PATH, "--today", "friday"], "--today wants"),
                           (["--orders", ORDERS_PATH, "--sec-list", "/nonexistent.csv"], "can't read the security list")):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(argv + ["--no-write"]), 1)
            self.assertIn(word, err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_blocked_exit_code_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices",
                                   none, "--state", none, "--sec-list", SEC_PATH, "--now", "2026-11-13 09:20",
                                   "--no-write", "--json"])
            self.assertEqual(code, 1)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["results"][0]["verdict"], "BLOCK")
            self.assertFalse(os.path.exists(os.path.join(d, "trader")))


if __name__ == "__main__":
    unittest.main()
