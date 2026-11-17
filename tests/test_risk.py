import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

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
QTY = 37        # what the worst-exit rule lets RELIANCE 1334.00 / stop 1328.80 carry at a 500 cap (56 would lose 739.91)


def at(text):
    return nc.parse_now(text)


def cfg(**over):
    with open(CFG_PATH, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rel(qty=QTY, **over):
    """The fixture's RELIANCE order (orders-reliance.json is the 56-share one), resized to qty on both legs."""
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o["entry"]["quantity"] = qty
    o["stop"]["quantity"] = qty
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


def dated(o, now):
    """A copy of o planned for the IST day of `now` (the fixture's plan.date is 2026-11-13)."""
    o = copy.deepcopy(o)
    o["plan"]["date"] = nc.to_ist(at(now)).date().isoformat()
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


def utc_text(now, minutes=0):
    """An IST wall-clock text -> UTC iso, `minutes` earlier."""
    return nc.utc_iso(at(now) - datetime.timedelta(minutes=minutes))


def quote_doc(now, prices, minutes_ago=1):
    """A prices.json (nse-alerts) with every price struck `minutes_ago` before `now`."""
    t = utc_text(now, minutes_ago)
    return {"as_of": t, "source": "yahoo", "prices": dict(prices),
            "detail": {s: {"time": t, "fetched": t} for s in prices}}


def entry_prices(orders):
    """A quote at the plan's entry (or the limit) for every order that has one."""
    out = {}
    for o in orders:
        px = (o.get("plan") or {}).get("entry") or (o.get("entry") or {}).get("price")
        if o.get("symbol") and px:
            out[o["symbol"]] = px
    return out


def run_check(orders, c=None, now=NOW, quote="auto", **kw):
    """check.check at `now` against NSE's list. By default orders.json was written a minute ago and
    every order has a fresh quote at its entry price; quote=None leaves prices.json empty, and
    created_at=None / prices_doc=... / prices=... replace those defaults."""
    kw.setdefault("sec", SEC)
    kw.setdefault("sec_info", SEC_INFO)
    kw.setdefault("created_at", utc_text(now, 1))
    if quote == "auto" and "prices_doc" not in kw and "prices" not in kw:
        kw["prices_doc"] = quote_doc(now, entry_prices(orders))
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
        self.assertLess(len(meta["description"]), 600)
        self.assertIn("argument-hint", meta)
        self.assertGreater(len(body), 60)
        for want in ("${CLAUDE_SKILL_DIR}/check.py", "--sec-list", "Not financial advice", "**yes**", "NSE",
                     "₹", "No Band", "GSM", "BE/BZ", "IST", "nse-broker", "risk_pass_minutes", "loss_basis",
                     "order_max_age_minutes", "price_fresh_minutes", "alerts.py quote"):
            self.assertIn(want, text, want)
        self.assertNotIn("$", text.replace("${CLAUDE_SKILL_DIR}", ""))


class TheWorkedExample(unittest.TestCase):
    """RELIANCE 1334.00, stop 1328.80 (SL limit 1322.10): at the worst fill 56 shares would lose 739.91 against
    a 500 cap, 37 lose 492.48."""

    def test_reliance_passes_every_check(self):
        res = run_check([rel()])
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["reasons"], r["warnings"]), ([], []))
        self.assertEqual((r["kind"], r["entry_ref"], r["entry_ref_from"]), ("entry", 1334.0, "limit"))
        self.assertEqual((r["stop"], r["stop_limit"], r["target"]), (1328.8, 1322.1, 1344.4))
        # the loss is at the SL limit 1322.10 (11.90 a share), not at the 1328.80 trigger (5.20 a share)
        self.assertEqual((r["risk_per_share"], r["worst_exit"], r["loss_per_share"]), (5.2, 1322.1, 11.9))
        self.assertEqual((r["max_loss_gross"], r["charges_at_stop"], r["max_loss"]), (440.3, 52.18, 492.48))
        self.assertEqual((r["loss_basis"], r["charges_at_target"], r["reward_risk"]), ("limit", 52.71, 1.36))
        self.assertEqual((r["series"], r["band"], r["remarks"]), ("EQ", "No Band", "-"))
        self.assertEqual(r["value_inr"], 49358.0)
        self.assertEqual(res["cash_after"], 50642.0)
        self.assertEqual((res["date"], res["now_ist"], res["broker"]), (TODAY, "2026-11-13 10:05", "paper"))
        self.assertEqual(r["fingerprint"], nc.order_fingerprint(rel()))
        self.assertIn("max loss ₹492.48 at 1,322.10 with charges (37 x 11.90 = ₹440.30 + ₹52.18; limit ₹500), "
                      "reward:risk 1.36 after charges (min 1), 0.67 if the stop fills at 1,322.10", r["info"])
        self.assertEqual((r["reward_risk_worst"], r["quote"], r["quote_fresh"]), (0.67, 1334.0, True))

    def test_the_worst_exit_decides_the_max_loss(self):
        res = run_check([rel(56)])
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(r["reasons"],
                         ["loss: 56 shares x 11.90 a share to the SL limit 1,322.10 (the worst fill once the stop "
                          "triggers; trigger 1,328.80) = ₹666.40, plus ₹73.51 charges = ₹739.91, over your ₹500 max "
                          "loss per trade"])
        self.assertEqual((r["max_loss"], r["max_loss_gross"], r["charges_at_stop"]), (739.91, 666.4, 73.51))
        # nse_common sizes the same trade to 37 shares, and that is what passes
        self.assertEqual(nc.size_qty(1334.0, 1328.8, "RELIANCE", check.nc.DEFAULT_CONFIG), 37)
        self.assertEqual(verdicts(run_check([rel(37)])), ["PASS"])
        self.assertEqual(verdicts(run_check([rel(38)])), ["BLOCK"])             # 38 would lose 505.79

    def test_loss_basis_trigger_is_the_old_measure(self):
        res = run_check([rel(56)], cfg(loss_basis="trigger"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["loss_basis"], r["worst_exit"]), ("PASS", "trigger", 1328.8))
        self.assertEqual((r["max_loss_gross"], r["charges_at_stop"], r["max_loss"]), (291.2, 73.62, 364.82))
        res = run_check([rel(56)], cfg(loss_basis="trigger", max_loss_per_trade_inr=360))
        self.assertIn("to the trigger 1,328.80 (loss_basis \"trigger\") = ₹291.20, plus ₹73.62 charges = ₹364.82, "
                      "over your ₹360", reasons(res))

    def test_a_stop_leg_with_a_wider_limit_is_measured_at_its_own_limit(self):
        o = leg(rel(), "stop", price=1300.0)             # hand-edited: trigger 1328.80, but it may fill at 1300
        res = run_check([o])
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["worst_exit"], r["loss_per_share"]), ("BLOCK", 1300.0, 34.0))
        self.assertIn("to the stop leg's own SL limit 1,300.00 (wider than the usual 1,322.10; trigger 1,328.80) = "
                      "₹1,258", reasons(res))
        self.assertEqual(r["max_loss"], 1258 + r["charges_at_stop"])
        # a tighter limit than the usual one is not used to make the loss look smaller
        res = run_check([leg(rel(), "stop", price=1328.0)])
        self.assertEqual((res["results"][0]["worst_exit"], res["results"][0]["max_loss"]), (1322.1, 492.48))

    def test_charges_decide_the_max_loss(self):
        res = run_check([rel()], cfg(max_loss_per_trade_inr=480))
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 37 shares x 11.90 a share to the SL limit 1,322.10 (the worst fill once the stop "
                          "triggers; trigger 1,328.80) = ₹440.30, plus ₹52.18 charges = ₹492.48, over your ₹480 max "
                          "loss per trade"])
        res = run_check([rel()], cfg(max_loss_per_trade_inr=480, include_charges=False))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual((res["results"][0]["max_loss"], res["results"][0]["reward_risk"]), (440.3, 2.0))

    def test_reward_risk_after_charges(self):
        o = rel(target={"price": 1339.6, "how": "alert"})
        res = run_check([o])
        self.assertIn("reward:risk: 0.63 after charges (target 1,339.60, entry 1,334.00, stop 1,328.80) is under "
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
            orders = os.path.join(d, "orders.json")
            prices = os.path.join(d, "prices.json")
            nc.write_json(orders, {"created_at": utc_text(NOW, 1), "orders": [rel()]})
            nc.write_json(prices, quote_doc(NOW, {"RELIANCE": 1334.0}))
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = check.main(["--orders", orders, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", prices, "--state", none, "--sec-list", SEC_PATH, "--now", NOW,
                                   "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("PASS  #1 BUY RELIANCE 37 shares, entry", text)
            self.assertIn("NSE RELIANCE: series EQ, No Band (an F&O stock: NSE sets a dynamic band), no surveillance",
                          text)
            self.assertIn("entry 1,334.00 (the limit price), stop 1,328.80 (SL limit 1,322.10), target 1,344.40: "
                          "risk 5.20 a share", text)
            self.assertIn("max loss ₹492.48 at 1,322.10 with charges", text)
            self.assertIn("RELIANCE last 1,334.00 at 10:04 IST (1 min old)", text)
            self.assertIn("1 passed, 0 blocked at 2026-11-13 10:05 IST", text)
            self.assertIn("A PASS is good until 10:10 IST (risk_pass_minutes 5) and only today", text)
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
        # 45 shares: at the worst fill (SL limit 950.20) they lose 486.63 with charges; 50 would lose 540.70
        res = run_check([kite_order("SBIN", 45, 960.0, 955.0, 972.0, 950.2)])
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
        return run_check([dated(rel(), now)], cfg(**over), now=now)

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

    def test_an_unknown_holiday_year_blocks_new_entries(self):
        # was a warning that let entries through; now the year must be known (built in, cached from NSE, or in config)
        res = self.one("2027-01-05 10:05")
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("holidays: NSE's 2027 holiday list isn't loaded, so Tue 2027-01-05 can't be confirmed as a "
                      "trading day. Fix: nse-alerts `alerts.py holidays --refresh`", reasons(res))
        self.assertIn("\"extra_holidays\" and 2027 in \"holiday_years\"", reasons(res))
        # naming the year in the config (with its holidays) is the other fix
        res = self.one("2027-01-05 10:05", holiday_years=[2027], extra_holidays={"2027-01-26": "Republic Day"})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        # getting out is never blocked by it
        pf = {"cash": 0, "positions": [{"ticker": "RELIANCE", "shares": 37, "lots": []}]}
        res = run_check([sell("RELIANCE", 37)], cfg(), now="2027-01-05 10:05", portfolio=pf)
        self.assertEqual(verdicts(res), ["PASS"])
        # a holiday list that is known leaves 2026 alone
        self.assertNotIn("holidays:", reasons(self.one("2026-11-13 10:05")))

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
        self.assertIn("stop: the stop leg is 50 shares, the entry 37: they must match",
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
        self.assertEqual(res["results"][0]["max_loss"], 492.48)      # measured at the usual SL limit, 1322.10

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
        # these rules are about cash, the cap and the order size: 56 shares (₹74,704), with the loss cap raised
        # out of the way (at the worst fill 56 shares lose ₹739.91, which the loss rule blocks on its own)
        big = cfg(max_loss_per_trade_inr=1000)
        res = run_check([rel(56)], big, portfolio={"cash": 50000, "positions": []})
        self.assertIn("cash: needs ₹74,704 but only ₹50,000 cash; that would be margin", reasons(res))
        res = run_check([rel(56)], cfg(max_loss_per_trade_inr=1000, allow_margin=True, max_position_pct=200),
                        portfolio={"cash": 50000, "positions": []})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        res = run_check([rel(56)], cfg(max_loss_per_trade_inr=1000, max_position_pct=50, max_order_inr=100000))
        self.assertIn("position: RELIANCE would be ₹74,704, 74.7% of your ₹1,00,000 budget, over your 50% cap "
                      "(₹50,000)", reasons(res))
        res = run_check([rel(56)], cfg(max_loss_per_trade_inr=1000, max_order_inr=70000))
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
        self.assertIn("daily loss: ₹1,229.64 already at risk today plus ₹492.48 for this one (its max loss at the SL "
                      "limit) = ₹1,722.12, over your ₹1,500", why)
        yesterday = {"orders": [placed(s, day="2026-11-12") for s in ("TCS", "INFY", "SBIN")]}
        self.assertEqual(verdicts(run_check([rel()], state=yesterday)), ["PASS"])

    def test_a_real_loss_worse_than_planned_counts(self):
        st = {"orders": [placed("TCS", max_loss=364.82),
                         placed("TCS", side="sell", status="closed", kind="exit", max_loss=None, pnl_net=-1200.0)]}
        res = run_check([rel()], state=st)
        self.assertIn("daily loss: ₹1,200 already at risk today plus ₹492.48", reasons(res))
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
        mkt = leg(rel(30), "entry", order_type="MARKET", price=None)
        # (a ₹2 dearer entry leaves reward:risk at 0.81 against the plan's target: lowered here to isolate the entry)
        res = run_check([mkt], cfg(min_reward_risk=0.5), prices_doc=quote_doc(NOW, {"RELIANCE": 1336.0}, minutes_ago=5))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1336.0, "price", 7.2))
        self.assertEqual((r["verdict"], r["quote_fresh"]), ("PASS", True), r["reasons"])
        self.assertIn("RELIANCE last 1,336.00 is above the plan's entry 1,334.00: the risk is measured from 1,336.00",
                      " ".join(r["info"]))
        # the same freshness rule as every entry (price_fresh_minutes, 5): at 25 minutes old the quote is not used,
        # the order is measured from the plan's entry, and it is blocked for want of a fresh quote
        res = run_check([mkt], prices_doc=quote_doc(NOW, {"RELIANCE": 1336.0}, minutes_ago=25))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["verdict"]), (1334.0, "plan", "BLOCK"))
        self.assertIn("quote: the RELIANCE price in prices.json is from 2026-11-13 09:40 IST, 25 min old "
                      "(price_fresh_minutes is 5)", reasons(res))

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
        res = run_check([rel(), sell("RELIANCE", QTY)])
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
                self.assertEqual(check.main(argv + ["--config", CFG_PATH, "--no-write"]), 1)
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
            self.assertIn("window: no new entries before 09:30 IST (it is 09:20)", out["results"][0]["reasons"])
            self.assertFalse(os.path.exists(os.path.join(d, "trader")))



class OldOrders(unittest.TestCase):
    """An order is today's or it is not traded: orders.json's created_at and the plan's date are checked."""

    def test_the_13_nov_order_is_blocked_on_monday_16_nov(self):
        # the fixture order as budget wrote it on Friday 13 Nov at 10:05, checked on Monday at 10:05 with a fresh quote
        old = rel()
        res = run_check([old], now="2026-11-16 10:05", created_at="2026-11-13T04:35:00Z")
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        why = reasons(res)
        self.assertIn("stale: this order was planned for Fri 2026-11-13; it is Mon 2026-11-16 10:05 IST, 3 days "
                      "later. A plan from an earlier session is not today's trade: plan it again (nse-entry, "
                      "nse-exit, nse-budget)", why)
        self.assertIn("stale: orders.json was written 3 days 0 h ago (Fri 2026-11-13 10:05 IST), over your "
                      "120-minute limit (order_max_age_minutes)", why)
        self.assertEqual((r["plan_date"], r["order_age_minutes"]), ("2026-11-13", 4320.0))

    def test_yesterdays_plan_is_blocked_even_when_the_file_was_just_rewritten(self):
        res = run_check([rel()], now="2026-11-16 10:05")      # created_at is a minute ago; plan.date is Friday
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("stale: this order was planned for Fri 2026-11-13", reasons(res))
        self.assertNotIn("orders.json was written", reasons(res))
        # and a plan dated after today is as wrong
        res = run_check([dated(rel(), "2026-11-16 10:05")], now=NOW)
        self.assertIn("stale: this order is dated Mon 2026-11-16, after today (Fri 2026-11-13 10:05 IST)", reasons(res))
        res = run_check([rel(plan={"entry": 1334.0, "stop": 1328.8, "target": 1344.4, "date": "13/11"})])
        self.assertIn("stale: the plan's date '13/11' is not a date", reasons(res))

    def test_orders_json_older_than_order_max_age_minutes_is_blocked(self):
        res = run_check([rel()], created_at=utc_text(NOW, 121))                 # 08:04 IST
        self.assertIn("stale: orders.json was written 2 h 1 min ago (Fri 2026-11-13 08:04 IST), over your "
                      "120-minute limit (order_max_age_minutes): the price has moved since; size it again with "
                      "nse-budget", reasons(res))
        self.assertEqual(verdicts(run_check([rel()], created_at=utc_text(NOW, 119))), ["PASS"])
        res = run_check([rel()], cfg(order_max_age_minutes=30), created_at=utc_text(NOW, 31))
        self.assertIn("over your 30-minute limit", reasons(res))
        info = " ".join(run_check([rel()], created_at=utc_text(NOW, 7))["results"][0]["info"])
        self.assertIn("order written 7 min ago (Fri 2026-11-13 09:58 IST)", info)

    def test_each_order_keeps_its_own_created_at(self):
        # nse-budget --append stamps the file with the newest write, each order with its own
        old = dict(rel(), created_at=utc_text(NOW, 200))
        res = run_check([old], created_at=utc_text(NOW, 1))
        self.assertIn("orders.json was written 3 h 20 min ago", reasons(res))
        new = dict(rel(), created_at=utc_text(NOW, 3))
        self.assertEqual(verdicts(run_check([new], created_at=utc_text(NOW, 500))), ["PASS"])

    def test_an_order_with_no_date_at_all_cannot_be_told_from_an_old_one(self):
        o = rel(plan={"entry": 1334.0, "stop": 1328.8, "target": 1344.4})
        res = run_check([o], created_at=None)
        self.assertIn("stale: this order has no date (no plan.date, and orders.json has no created_at)", reasons(res))
        # a file time alone is enough, and so is a plan date alone
        self.assertEqual(verdicts(run_check([o])), ["PASS"])
        self.assertEqual(verdicts(run_check([rel()], created_at=None)), ["PASS"])
        res = run_check([rel()], created_at="yesterday")
        self.assertIn("stale: orders.json's created_at 'yesterday' is not a time", reasons(res))
        res = run_check([rel()], created_at=utc_text(NOW, -30))
        self.assertIn("30 min after the check clock", reasons(res))

    def test_an_exit_is_never_blocked_by_age(self):
        pf = {"cash": 0, "positions": [{"ticker": "RELIANCE", "shares": 37, "lots": []}]}
        res = run_check([sell("RELIANCE", 37)], now="2026-11-16 15:12", portfolio=pf, quote=None,
                        created_at="2026-11-13T04:35:00Z")
        self.assertEqual((verdicts(res), res["results"][0]["kind"]), (["PASS"], "exit"))


class FreshQuote(unittest.TestCase):
    """Every entry needs a fresh quote for its symbol, and the price must not already be through the stop."""

    def test_no_quote_blocks_with_the_command_to_run(self):
        res = run_check([rel()], quote=None)
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("quote: no RELIANCE price in prices.json. Every entry needs a fresh quote (under 5 minutes "
                      "old) to check the order against the market: run nse-alerts: alerts.py quote RELIANCE --write, "
                      "then check again", reasons(res))
        # a quote for another symbol does not do
        res = run_check([rel()], prices_doc=quote_doc(NOW, {"TCS": 3000.0}))
        self.assertIn("quote: no RELIANCE price", reasons(res))
        self.assertEqual((res["results"][0]["quote"], res["results"][0]["quote_fresh"]), (None, False))

    def test_the_command_names_a_prices_file_that_is_not_the_default(self):
        res = run_check([rel()], quote=None, prices_path="/tmp/x/prices.json")
        self.assertIn("alerts.py quote RELIANCE --write /tmp/x/prices.json", reasons(res))
        self.assertIn("no RELIANCE price in /tmp/x/prices.json", reasons(res))

    def test_a_stale_quote_blocks(self):
        res = run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": 1334.0}, minutes_ago=6))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["quote"], r["quote_fresh"]), ("BLOCK", 1334.0, False))
        self.assertIn("quote: the RELIANCE price in prices.json is from 2026-11-13 09:59 IST, 6 min old "
                      "(price_fresh_minutes is 5): run nse-alerts: alerts.py quote RELIANCE --write, then check "
                      "again", reasons(res))
        self.assertEqual(verdicts(run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": 1334.0}, minutes_ago=4))),
                         ["PASS"])
        res = run_check([rel()], cfg(price_fresh_minutes=10), prices_doc=quote_doc(NOW, {"RELIANCE": 1334.0}, 9))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_price_with_no_quote_time_is_never_fresh(self):
        res = run_check([rel()], prices_doc={"prices": {"RELIANCE": 1334.0}})
        self.assertIn("quote: the RELIANCE price in prices.json has no quote time, so its age can't be told",
                      reasons(res))
        # the time of the last trade, not the file's as_of, is the quote's age
        doc = quote_doc(NOW, {"RELIANCE": 1334.0})
        doc["detail"]["RELIANCE"]["time"] = utc_text(NOW, 20)
        self.assertIn("is from 2026-11-13 09:45 IST, 20 min old", reasons(run_check([rel()], prices_doc=doc)))
        # a quote stamped after the check clock is wrong, not fresh
        res = run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": 1334.0}, minutes_ago=-10))
        self.assertIn("is stamped 10:15 IST, after the check clock (10:05 IST)", reasons(res))

    def test_a_limit_entry_above_a_price_already_through_the_stop_is_blocked(self):
        # LIMIT 1334, stop trigger 1328.80, last 1320: it would fill near 1320 with the trigger above the last
        # price, which Kite rejects, so the trade would have no stop. The old check measured from the LIMIT and passed.
        res = run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": 1320.0}))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("quote: RELIANCE last 1,320.00 (10:04 IST) is already at or below the stop trigger 1,328.80: "
                      "the entry would fill under the stop, and Kite rejects a stop leg whose trigger is above the "
                      "last price, so the trade would have no stop. Skip it, or plan again from the current price",
                      reasons(res))
        for price, want in ((1328.8, "BLOCK"), (1328.7, "BLOCK"), (1328.9, "PASS"), (1334.0, "PASS")):
            got = verdicts(run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": price})))
            self.assertEqual(got, [want], price)

    def test_a_price_more_than_half_an_r_above_the_limit_only_warns(self):
        # R is 5.20 (entry to the trigger): 0.5R is 2.60
        res = run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": 1337.0}))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("RELIANCE last 1,337.00 is 3.00 above the entry 1,334.00, more than half the 5.20 risk a "
                      "share (0.5R): the order may not fill, or the plan is stale", warnings(res))
        for price in (1336.6, 1334.0, 1331.0):
            res = run_check([rel()], prices_doc=quote_doc(NOW, {"RELIANCE": price}))
            self.assertEqual((verdicts(res), warnings(res)), (["PASS"], ""), price)

    def test_a_market_entry_below_the_stop_is_blocked_too(self):
        mkt = leg(rel(30), "entry", order_type="MARKET", price=None)
        res = run_check([mkt], prices_doc=quote_doc(NOW, {"RELIANCE": 1320.0}))
        self.assertIn("quote: RELIANCE last 1,320.00 (10:04 IST) is already at or below the stop trigger", reasons(res))
        res = run_check([mkt], quote=None)
        self.assertIn("quote: no RELIANCE price in prices.json", reasons(res))

    def test_an_exit_needs_no_quote(self):
        pf = {"cash": 0, "positions": [{"ticker": "RELIANCE", "shares": 37, "lots": []}]}
        res = run_check([sell("RELIANCE", 37)], portfolio=pf, quote=None)
        self.assertEqual((verdicts(res), reasons(res)), (["PASS"], ""))

    def test_a_short_entry_is_blocked_by_a_price_above_its_stop(self):
        # allow_short: a SELL entry with its buy stop at 1339.20 while the last price is already 1340
        e = dict(rel()["entry"], transaction_type="SELL")
        s = dict(rel()["stop"], transaction_type="BUY", trigger_price=1339.2, price=1345.9)
        o = rel(entry=e, stop=s, plan={"entry": 1334.0, "stop": 1339.2, "target": 1323.6, "date": TODAY},
                target={"price": 1323.6, "how": "alert"})
        res = run_check([o], cfg(allow_short=True), prices_doc=quote_doc(NOW, {"RELIANCE": 1340.0}))
        self.assertIn("is already at or above the stop trigger 1,339.20", reasons(res))


class OrderTickNearAThousand(unittest.TestCase):
    """NSE sets a stock's tick from an earlier close, so a price within tick_band_pct of a slab edge uses the
    coarser tick (nse_common.order_tick). 997.95 is on the 0.05 grid of its own slab and off the 0.10 one."""

    def test_a_price_near_1000_must_be_on_the_coarser_tick(self):
        o = kite_order("SBIN", 10, 997.95, 990.0, 1010.0, 985.0)
        res = run_check([o])
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("tick: the entry leg's price 997.95 is not on the 0.10 tick (997.95 is within 10% of the "
                      "₹1,000 slab edge and NSE sets each stock's tick from an earlier close, not today's price, so "
                      "the coarser 0.10 tick is the one that is valid either way): use 997.90 or 998.00", reasons(res))
        self.assertEqual(verdicts(run_check([kite_order("SBIN", 10, 998.0, 990.0, 1010.0, 985.0)])), ["PASS"])
        # a stop or an SL limit on the finer grid is refused the same way
        res = run_check([kite_order("SBIN", 10, 998.0, 990.05, 1010.0, 985.0)])
        self.assertIn("tick: the stop leg's trigger_price 990.05 is not on the 0.10 tick", reasons(res))
        res = run_check([kite_order("SBIN", 10, 998.0, 990.0, 1010.0, 985.05)])
        self.assertIn("tick: the stop leg's price 985.05 is not on the 0.10 tick", reasons(res))

    def test_a_price_far_from_an_edge_keeps_its_own_tick(self):
        res = run_check([leg(rel(), "entry", price=1334.05)])
        self.assertIn("is not on the 0.10 tick (NSE's tick for a ₹1,334.05 price): use 1334.00 or 1334.10", reasons(res))
        o = kite_order("SBIN", 10, 500.05, 495.0, 510.0, 492.5)       # 0.05 slab, 10% from 500 is still 0.05
        self.assertEqual(verdicts(run_check([o])), ["PASS"])

    def test_a_config_tick_wins(self):
        res = run_check([kite_order("SBIN", 10, 997.95, 990.0, 1010.0, 985.0)], cfg(tick_sizes={"SBIN": 0.05}))
        self.assertNotIn("tick:", reasons(res))


class AccountBase(unittest.TestCase):
    """The position cap and the cash buffer use nse_common.account_base, the same base nse-budget sizes with."""

    def test_a_big_account_is_capped_on_the_budget(self):
        # 58 shares = ₹77,372: over 75% of the ₹1,00,000 budget. The old base was the account's ₹3,00,000 (cap 2,25,000)
        # while nse-budget used the budget, so budget never proposed this and risk would have passed it.
        pf = {"cash": 300000, "positions": []}
        c = cfg(max_order_inr=100000, max_loss_per_trade_inr=2000)
        res = run_check([rel(58)], c, portfolio=pf)
        self.assertEqual(res["account_base"], 100000.0)
        self.assertIn("position: RELIANCE would be ₹77,372, 77.4% of your ₹1,00,000 budget (the account holds "
                      "₹3,00,000), over your 75% cap (₹75,000)", reasons(res))
        self.assertEqual(verdicts(run_check([rel(56)], c, portfolio=pf)), ["PASS"])

    def test_a_losing_day_shrinks_the_base_to_the_account(self):
        pf = {"cash": 60000, "positions": []}
        res = run_check([rel()], portfolio=pf)
        self.assertEqual(res["account_base"], 60000.0)
        self.assertIn("position: RELIANCE would be ₹49,358, 82.3% of the ₹60,000 account (under your ₹1,00,000 "
                      "budget), over your 75% cap (₹45,000)", reasons(res))
        # nse-budget sizes the same account to this many shares, and risk passes exactly that
        c = dict(nc.DEFAULT_CONFIG)
        lims = {l["name"]: l for l in nc.size_limits(1334.0, 1328.8, "RELIANCE", c, cash=60000, portfolio_total=60000)}
        self.assertEqual((lims["position cap"]["inr"], lims["position cap"]["shares"]), (45000.0, 33))
        self.assertEqual(nc.size_qty(1334.0, 1328.8, "RELIANCE", c, cash=60000, portfolio_total=60000), 33)
        res = run_check([rel(33)], portfolio=pf)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual(verdicts(run_check([rel(34)], portfolio=pf)), ["BLOCK"])

    def test_the_cash_buffer_is_a_share_of_the_same_base(self):
        # ₹60,000 account: the 10% buffer is ₹6,000. 33 shares leave 15,978; 40 shares (53,360) leave 6,640 ...
        pf = {"cash": 60000, "positions": []}
        c = cfg(max_position_pct=100, max_loss_per_trade_inr=900)
        self.assertEqual(verdicts(run_check([rel(40)], c, portfolio=pf)), ["PASS"])
        res = run_check([rel(44)], c, portfolio=pf)                     # 44 x 1334 = 58,696 leaves 1,304
        self.assertIn("cash: leaves ₹1,304 cash, under your 10% buffer of ₹6,000 (of the ₹60,000 account (under "
                      "your ₹1,00,000 budget))", reasons(res))

    def test_no_portfolio_means_the_budget(self):
        res = run_check([rel()])
        self.assertEqual(res["account_base"], 100000.0)


class DailyLossAtTheWorstExit(unittest.TestCase):
    def test_the_day_total_uses_the_same_worst_exit_number(self):
        # ₹1,100 already at risk + this order's ₹492.48 at the SL limit = ₹1,592.48 > ₹1,500.
        # Measured at the trigger it would have been ₹1,100 + ₹244.73 and passed.
        st = {"orders": [placed("TCS", max_loss=1100)]}
        res = run_check([rel()], state=st)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(r["max_loss"], 492.48)
        self.assertIn("daily loss: ₹1,100 already at risk today plus ₹492.48 for this one (its max loss at the SL "
                      "limit) = ₹1,592.48, over your ₹1,500 daily loss limit", reasons(res))
        res = run_check([rel()], cfg(loss_basis="trigger"), state=st)
        self.assertEqual((verdicts(res), res["results"][0]["max_loss"]), (["PASS"], 244.73))
        # the batch adds each passed order at the same number
        a, b = kite_order("SBIN", 45, 960.0, 955.0, 972.0, 950.2), kite_order("TCS", 12, 3000.0, 2990.0, 3030.0, 2975.0)
        res = run_check([a, b], cfg(max_daily_loss_inr=600))
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("₹486.63 already at risk today plus", reasons(res, 1))


class RiskJsonStampsEveryResult(unittest.TestCase):
    """nse-broker lets a PASS stand for risk_pass_minutes from its own checked_at, so every result carries it."""

    def test_each_result_has_its_symbol_and_checked_at(self):
        orders = [rel(), kite_order("SBIN", 45, 960.0, 955.0, 972.0, 950.2), sell("INFY", 5)]
        res = run_check(orders)
        self.assertEqual(res["checked_at"], "2026-11-13T04:35:00Z")
        self.assertEqual([r["symbol"] for r in res["results"]], ["RELIANCE", "SBIN", "INFY"])
        for r in res["results"]:
            self.assertEqual(r["checked_at"], "2026-11-13T04:35:00Z")
            self.assertRegex(r["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((res["pass_minutes"], res["pass_until_ist"]), (5.0, "2026-11-13 10:10"))
        res = run_check([rel()], cfg(risk_pass_minutes=3))
        self.assertEqual((res["pass_minutes"], res["pass_until_ist"]), (3.0, "2026-11-13 10:08"))


class HomeCase(unittest.TestCase):
    """main() the way a real run goes: every default path inside the trader folder NSE_TRADER_HOME names."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        env = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.d})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(setattr, check, "fetch_holidays", check.fetch_holidays)

        def offline():
            raise check.nc.HolidayError("offline in tests")

        check.fetch_holidays = offline
        self.elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.elsewhere, True)

    def put(self, name, doc):
        nc.write_json(os.path.join(self.d, name), doc)

    def stock(self, now=NOW, order=None):
        shutil.copy(CFG_PATH, os.path.join(self.d, "config.json"))
        self.put("orders.json", {"created_at": utc_text(now, 1), "orders": [order or rel()]})
        self.put("prices.json", quote_doc(now, {"RELIANCE": 1334.0}))

    def run_main(self, *extra, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.elsewhere)            # the trader folder is found by NSE_TRADER_HOME, not by where we are
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = check.main(["--sec-list", SEC_PATH, "--now", now] + list(extra))
        finally:
            os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()

    def risk(self):
        return nc.read_json(os.path.join(self.d, "state", "risk.json"))


class DefaultPaths(HomeCase):
    def test_a_run_reads_and_writes_the_trader_home_not_the_working_folder(self):
        self.stock()
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""), out)
        rec = self.risk()
        self.assertEqual((rec["date"], rec["checked_at"]), (TODAY, "2026-11-13T04:35:00Z"))
        self.assertEqual(rec["orders_file"], os.path.join(self.d, "orders.json"))
        r = rec["results"][0]
        self.assertEqual((r["symbol"], r["checked_at"], r["verdict"]), ("RELIANCE", "2026-11-13T04:35:00Z", "PASS"))
        self.assertEqual(r["fingerprint"], nc.order_fingerprint(rel()))
        self.assertEqual(os.listdir(self.elsewhere), [])               # nothing written where Claude Code started

    def test_no_write_leaves_the_folder_alone(self):
        self.stock()
        code, _, _ = self.run_main("--no-write")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.d, "state")))

    def test_a_stale_file_blocks_through_main_and_is_recorded_as_a_block(self):
        self.stock()
        code, out, _ = self.run_main(now="2026-11-16 10:05")
        self.assertEqual(code, 1)
        self.assertIn("stale: this order was planned for Fri 2026-11-13", out)
        self.assertEqual(self.risk()["results"][0]["verdict"], "BLOCK")

    def test_a_missing_quote_blocks_through_main_and_names_the_prices_file(self):
        self.stock()
        os.remove(os.path.join(self.d, "prices.json"))
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("quote: no RELIANCE price in %s" % os.path.join(self.d, "prices.json"), out)
        self.assertIn("run nse-alerts: alerts.py quote RELIANCE --write", out)

    def test_an_entry_at_the_wrong_time_is_blocked_by_the_window_at_check_time(self):
        self.stock(now="2026-11-13 09:20")
        code, out, _ = self.run_main(now="2026-11-13 09:20")
        self.assertEqual(code, 1)
        self.assertIn("window: no new entries before 09:30 IST (it is 09:20)", out)


class MissingConfig(HomeCase):
    def test_no_config_exits_2_and_writes_nothing(self):
        self.put("orders.json", {"created_at": utc_text(NOW, 1), "orders": [rel()]})
        self.put("prices.json", quote_doc(NOW, {"RELIANCE": 1334.0}))
        code, out, err = self.run_main()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("check.py: no config.json at %s" % os.path.join(self.d, "config.json"), err)
        self.assertIn("the tools will not guess them", err)
        self.assertIn("NSE_TRADER_HOME", err)
        self.assertEqual(sorted(os.listdir(self.d)), ["orders.json", "prices.json"])      # no state/risk.json

    def test_a_config_path_that_does_not_exist_is_the_same(self):
        self.stock()
        gone = os.path.join(self.d, "elsewhere.json")
        code, out, err = self.run_main("--config", gone)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no config.json at " + gone, err)
        self.assertFalse(os.path.exists(os.path.join(self.d, "state")))

    def test_a_bad_now_does_not_hide_a_missing_config(self):
        code, _, err = self.run_main("--orders", ORDERS_PATH, now="noon")
        self.assertEqual(code, 2)
        self.assertIn("no config.json", err)

    def test_a_broken_config_is_an_error_not_a_default(self):
        self.stock()
        with open(os.path.join(self.d, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("is not a JSON object", err)


class UnknownHolidayYear(HomeCase):
    """NSE publishes next year's holidays around December. Until check.py has them, no new entry goes in."""

    NOW27 = "2027-01-05 10:05"

    def stock27(self):
        self.stock(self.NOW27, dated(rel(), self.NOW27))
        self.put("prices.json", quote_doc(self.NOW27, {"RELIANCE": 1334.0}))

    def test_a_failed_refresh_blocks_the_entry_and_says_how_to_fix_it(self):
        self.stock27()
        code, out, _ = self.run_main(now=self.NOW27)
        self.assertEqual(code, 1)
        self.assertIn("WARNING: could not refresh NSE's holiday list (NSE's 2027 holiday list is not loaded): could "
                      "not reach NSE", out.replace("offline in tests", "could not reach NSE"))
        self.assertIn("holidays: NSE's 2027 holiday list isn't loaded, so Tue 2027-01-05 can't be confirmed as a "
                      "trading day", out)
        self.assertIn("alerts.py holidays --refresh", out)
        self.assertEqual(self.risk()["results"][0]["verdict"], "BLOCK")

    def test_a_good_refresh_unblocks_it_and_is_cached(self):
        self.stock27()
        calls = []
        doc = {"CM": [{"tradingDate": "26-Jan-2027", "weekDay": "Tuesday", "description": "Republic Day"},
                      {"tradingDate": "26-Mar-2027", "weekDay": "Friday", "description": "Holi"}]}
        check.fetch_holidays = lambda: calls.append(1) or doc
        code, out, _ = self.run_main(now=self.NOW27)
        self.assertEqual((code, len(calls)), (0, 1), out)
        self.assertIn("NOTE: refreshed NSE's holiday list (NSE's 2027 holiday list is not loaded)", out)
        cache = nc.read_json(os.path.join(self.d, "state", "nse-holidays.json"))
        self.assertEqual(cache["years"], [2026, 2027] if 2026 in cache["years"] else [2027])
        self.assertEqual(cache["holidays"]["2027-01-26"], "Republic Day")
        self.assertEqual(self.risk()["results"][0]["verdict"], "PASS")
        # the cache is read next time: no second fetch
        code, _, _ = self.run_main(now=self.NOW27)
        self.assertEqual((code, len(calls)), (0, 1))

    def test_a_refreshed_list_still_closes_its_holidays(self):
        self.stock("2027-01-26 10:05", dated(rel(), "2027-01-26 10:05"))
        check.fetch_holidays = lambda: {"CM": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"}]}
        code, out, _ = self.run_main(now="2027-01-26 10:05")
        self.assertEqual(code, 1)
        self.assertIn("window: the NSE market is closed today (NSE holiday: Republic Day", out)
        self.assertNotIn("holidays: NSE's 2027", out)

    def test_pure_check_blocks_an_entry_and_lets_an_exit_through(self):
        res = run_check([dated(rel(), self.NOW27)], now=self.NOW27)
        self.assertIn("holidays: NSE's 2027 holiday list isn't loaded", reasons(res))
        res = run_check([dated(rel(), self.NOW27)], cfg(holiday_years=[2027]), now=self.NOW27)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))

if __name__ == "__main__":
    unittest.main()
