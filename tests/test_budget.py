import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("nse-budget", "size")
nc = load("nse-budget", "nse_common")

CFG = os.path.join(ROOT, "nse-budget", "config.example.json")
PLAN = os.path.join(FIX, "nse-budget", "plan-reliance.json")
ORDERS = os.path.join(FIX, "nse-risk", "orders-reliance.json")
NOW = "2026-11-13 10:05"
TODAY = "2026-11-13"


def cfg(**over):
    with open(CFG, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN, encoding="utf-8") as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-11-13T04:50:00Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": price},
            "detail": {"RELIANCE": {"prev_close": 1320.0, "time": quote_time, "source": "yahoo"}}}



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
        meta, body, text = skill_md("nse-budget")
        self.assertEqual(meta["name"], "nse-budget")
        self.assertTrue(meta["description"].startswith(">-"))
        self.assertGreater(len(meta["description"]), 200)
        self.assertIn("argument-hint", meta)
        self.assertGreater(len(body), 60)
        for want in ("${CLAUDE_SKILL_DIR}/size.py", "Not financial advice", "**yes**", "NSE", "₹", "Kite",
                     "no OCO for MIS", "nse-risk"):
            self.assertIn(want, text, want)
        for key in nc.DEFAULT_CONFIG:
            self.assertIn("`%s`" % key, text, key)
        self.assertNotIn("$", text.replace("${CLAUDE_SKILL_DIR}", ""))


class Config(unittest.TestCase):
    def test_config_example_is_nse_common_default_config(self):
        with open(CFG, encoding="utf-8") as fh:
            c = json.load(fh)
        self.assertEqual(c, nc.DEFAULT_CONFIG)
        self.assertFalse([k for k in c if k.endswith("_usd")])
        for k in ("pdt_guard", "fractional", "allow_options", "sec_user_agent", "paper"):
            self.assertNotIn(k, c)
        self.assertEqual((c["broker"], c["product"], c["flat_by"], c["stop_order_type"]),
                         ("paper", "MIS", "15:10", "SL"))

    def test_no_weights_mode(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "nse-budget", "targets.example.json")))
        self.assertFalse(hasattr(size, "parse_weights"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            size.main(["--plan", PLAN, "--weights", "RELIANCE=100"])


class TheWorkedExample(unittest.TestCase):
    """RELIANCE, 13 Nov 2026: entry 1,334.00, stop 1,328.80, target 1,344.40 (2R).
    ₹1,00,000 budget, ₹500 max loss with charges -> 56 shares, bound by the 75% position cap."""

    def test_56_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 56)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 79), ("position cap", 56), ("max order", 59), ("cash", 67)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["position cap"])
        self.assertEqual((s["entry"], s["stop"], s["target"], s["tick"]), (1334.0, 1328.8, 1344.4, "0.10"))
        self.assertEqual((s["risk_per_share"], s["cost"], s["max_loss"]), (5.2, 74704.0, 291.2))
        self.assertEqual((s["charges_at_stop"], s["max_loss_with_charges"]), (73.62, 364.82))
        self.assertEqual((s["charges_at_target"], s["gain_at_target"], s["net_at_target"]), (73.87, 582.4, 508.53))
        self.assertEqual((s["reward_risk"], s["reward_risk_gross"]), (1.39, 2.0))
        self.assertEqual((s["break_even"], s["break_even_move"]), (1335.4, 1.4))
        self.assertEqual((s["cash_buffer"], s["cash_available"]), (10000.0, 90000.0))
        self.assertIsNone(s["refusal"])

    def test_charges_at_target_match_nse_common(self):
        self.assertEqual(nc.intraday_charges(56, 1334.0, 1344.4)["total"], 73.87)
        self.assertEqual(nc.intraday_charges(56, 1334.0, 1328.8)["total"], 73.62)

    def test_the_kite_shaped_order(self):
        o = size.build_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "RELIANCE",
            "entry": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                      "transaction_type": "BUY", "quantity": 56, "product": "MIS", "order_type": "LIMIT",
                      "price": 1334.0, "validity": "DAY", "tag": "dtindia"},
            "stop": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                     "transaction_type": "SELL", "quantity": 56, "product": "MIS", "order_type": "SL",
                     "trigger_price": 1328.8, "price": 1322.1, "validity": "DAY", "tag": "dtindia"},
            "target": {"price": 1344.4, "how": size.TARGET_HOW},
            "flat_by": "15:10",
            "plan": {"entry": 1334.0, "stop": 1328.8, "target": 1344.4, "risk_per_share": 5.2, "max_loss": 291.2,
                     "charges_at_stop": 73.62, "max_loss_with_charges": 364.82, "charges_at_target": 73.87,
                     "reward_risk": 1.39, "break_even": 1335.4, "setup": "orb", "date": "2026-11-13",
                     "entry_time": "10:05", "flat_by": "15:10", "tick": "0.10"}})
        self.assertIn("no OCO for MIS", size.TARGET_HOW)
        self.assertIn("cancel the stop, then sell", size.TARGET_HOW)
        self.assertTrue(why.startswith("Opening-range breakout on RELIANCE: 56 shares, bound by your position cap"))
        self.assertIn("₹291.20 at the stop, ₹364.82 with charges", why)
        for leg in (o["entry"], o["stop"]):
            self.assertTrue(set(leg) <= set(nc.KITE_FIELDS))

    def test_the_risk_fixture_is_what_it_writes(self):
        with open(ORDERS, encoding="utf-8") as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.build_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0, text)
            self.assertIn("Size: 56 shares of RELIANCE = ₹74,704 at 1,334.00.", text)
            self.assertIn("At the stop: 56 x 5.20 = ₹291.20, plus about ₹73.62 in charges = ₹364.82.", text)
            self.assertIn("minus about ₹73.87 in charges = ₹508.53. Reward:risk 1.39 after charges", text)
            self.assertIn("Break-even: 1,335.40, a move of 1.40 a share", text)
            self.assertIn("position cap  75% of ₹1,00,000 = ₹75,000 / 1,334.00", text)
            self.assertIn("<- binds", text)
            self.assertIn("SL trigger 1,328.80, limit 1,322.10", text)
            self.assertIn("in a gap below 1,322.10 it may not fill", text)
            self.assertIn("no OCO for MIS", text)
            self.assertIn("Flat by 15:10 IST", text)
            self.assertIn("Not financial advice", text)
            self.assertIn("explicit yes", text)
            self.assertNotIn("$", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual((doc["created_by"], doc["status"], doc["broker"]), ("nse-budget", "proposed", "paper"))
            self.assertEqual(doc["created_at"], "2026-11-13T04:35:00Z")
            self.assertIn("you say yes to it in the chat", doc["note"])
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["plan"]["max_loss_with_charges"], 364.82)

    def test_cli_json(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 56)
        self.assertEqual(out["orders"][0]["entry"]["price"], 1334.0)


class IndiaRules(unittest.TestCase):
    def test_charges_count_against_the_max_loss(self):
        big = dict(budget=1000000, max_position_pct=100, max_order_inr=1000000)
        s = size.size_trade(plan(), cfg(**big), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (79, "max loss"))
        self.assertEqual(s["max_loss_with_charges"], 495.27)          # 80 shares would be ₹500.95
        self.assertLessEqual(s["max_loss_with_charges"], 500)
        self.assertGreater(80 * 5.2 + nc.intraday_charges(80, 1334.0, 1328.8)["total"], 500)
        s = size.size_trade(plan(), cfg(include_charges=False, **big), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"]), (96, "max loss", 499.2))
        self.assertEqual(s["reward_risk"], 2.0)

    def test_prices_round_to_the_nse_tick(self):
        s = size.size_trade(plan(stop=1328.85, entry=1334.04, target=1344.36), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1334.0, 1328.8, 1344.4))
        o = size.build_order(s)
        self.assertEqual((o["stop"]["trigger_price"], o["stop"]["price"]), (1328.8, 1322.1))
        report = size.plan_report(s, o)
        self.assertIn("The plan's stop 1,328.85 is not on the 0.10 tick: rounded down to 1,328.80.", report)
        # a ₹186 stock trades in paise, an ₹8,593 stock in 50 paise
        s = size.size_trade(plan(symbol="ITC", entry=186.437, stop=184.996, target=189.333), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"], s["tick"]), (186.44, 184.99, 189.33, "0.01"))
        s = size.size_trade(plan(symbol="BAJAJ-AUTO", entry=8593.2, stop=8550.3, target=8680.1),
                            cfg(max_loss_per_trade_inr=1000), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"], s["tick"]), (8593.0, 8550.0, 8680.0, "0.50"))
        self.assertEqual(s["stop_limit"], 8507.0)                      # 8550 x 0.995 = 8507.25, down to 0.50

    def test_the_sl_limit_buffer_rounds_down(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["stop_limit"], 1322.1)                      # 1328.80 x 0.995 = 1322.156
        self.assertEqual(size.size_trade(plan(), cfg(stop_limit_buffer_pct=1), today=TODAY)["stop_limit"], 1315.5)
        self.assertEqual(size.size_trade(plan(), cfg(stop_limit_buffer_pct=0), today=TODAY)["stop_limit"], 1328.8)
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(stop_limit_buffer_pct=25), today=TODAY)

    def test_sl_m_has_no_price_and_warns(self):
        s = size.size_trade(plan(), cfg(stop_order_type="SL-M"), today=TODAY)
        o = size.build_order(s)
        self.assertEqual(o["stop"]["order_type"], "SL-M")
        self.assertNotIn("price", o["stop"])
        self.assertEqual(o["stop"]["trigger_price"], 1328.8)
        self.assertIn("market protection", size.plan_report(s, o))
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(stop_order_type="STOP"), today=TODAY)

    def test_reward_risk_after_charges(self):
        thin = plan(target=1339.6)                                     # 1.08R before charges
        s = size.size_trade(thin, cfg(), today=TODAY)
        self.assertEqual((s["reward_risk_gross"], s["reward_risk"]), (1.08, 0.66))
        self.assertIn("reward:risk 0.66 after charges", s["refusal"])
        self.assertIsNone(size.build_order(s))
        s = size.size_trade(thin, cfg(include_charges=False), today=TODAY)
        self.assertIsNone(s["refusal"])

    def test_a_plan_for_another_day_or_exchange(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-11-16")
        self.assertIn("today is 2026-11-16 in India (IST)", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(exchange="BSE"), cfg(), today=TODAY)
        self.assertIn("NSE cash market only", str(e.exception))

    def test_entry_time_is_ist(self):
        self.assertEqual(size.plan_time("10:05"), "10:05")
        self.assertEqual(size.plan_time("2:45 PM"), "14:45")
        self.assertEqual(size.plan_time("10:05 AM"), "10:05")


class OrderTypes(unittest.TestCase):
    def test_a_limit_at_another_price(self):
        s = size.size_trade(plan(), cfg(), limit=1333.5, today=TODAY)
        self.assertEqual((s["entry"], s["risk_per_share"], s["entry_from"]), (1333.5, 4.7, "limit"))
        self.assertEqual(size.build_order(s)["entry"]["price"], 1333.5)
        s = size.size_trade(plan(), cfg(), limit=1333.47, today=TODAY)
        self.assertEqual(s["entry"], 1333.5)

    def test_market_only_when_asked(self):
        s = size.size_trade(plan(), cfg(), market=True, today=TODAY)
        o = size.build_order(s)
        self.assertEqual(o["entry"]["order_type"], "MARKET")
        self.assertNotIn("price", o["entry"])
        self.assertIn("market protection", size.plan_report(s, o))
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--market", "--limit", "1334"])
        self.assertEqual(code, 1)
        self.assertIn("pick one", err)

    def test_a_fresh_higher_price_means_a_bigger_risk_for_a_market_order(self):
        s = size.size_trade(plan(), cfg(), market=True, price=1336.03, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1336.0, "price", 7.2))
        lower = size.size_trade(plan(), cfg(), market=True, price=1330.0, today=TODAY)
        self.assertEqual(lower["entry"], 1334.0)
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), market=True, price=1345.0, today=TODAY)
        self.assertIn("already happened", str(e.exception))

    def test_prices_file(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1339.7), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            out = json.loads(run(base + ["--now", "2026-11-13 10:25", "--market"])[1])
            self.assertEqual((out["sizing"]["entry"], out["sizing"]["entry_from"]), (1339.7, "price"))
            self.assertIn("above the plan's entry", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-11-13 11:30", "--market"])[1])
            self.assertEqual(out["sizing"]["entry"], 1334.0)
            self.assertIn("not used", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-11-13 10:25"])[1])
            self.assertEqual((out["sizing"]["entry"], out["sizing"]["order_type"]), (1334.0, "LIMIT"))
            self.assertIn("rests until the price comes back", out["notes"][0])


class Refusals(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=800.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹500 max loss per trade", s["refusal"])
        self.assertIsNone(size.build_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(stop=800.0), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=50000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (7, "cash", 10000.0))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=200), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss_with_charges"]), (30, "max loss", 198.43))
        self.assertLessEqual(s["max_loss_with_charges"], 200)

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 20000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (7, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=1335.0), "not below the entry"),
                        (plan(target=1330.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) intraday trades only"),
                        (plan(symbol=""), "no symbol"),
                        (plan(symbol="^NSEI"), "not an NSE stock symbol")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["reliance"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(budget=0), today=TODAY)
        self.assertIn("no budget", str(e.exception))

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="IDEA", entry=4.0, stop=3.0, target=6.0)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=3, include_charges=False), today=TODAY)
        self.assertEqual(s["qty"], 3)
        self.assertIn("under your ₹500 minimum order", s["refusal"])
        self.assertIsNone(size.build_order(s))

    def test_errors_are_one_line(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertIn("--now wants", err)
        self.assertNotIn("Traceback", err)
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "config.json")
            with open(bad, "w") as fh:
                fh.write("{not json")
            code, _, err = run(["--plan", PLAN, "--config", bad, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
