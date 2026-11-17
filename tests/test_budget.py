import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, ROOT, FIX

size = load("nse-budget", "size")
nc = load("nse-budget", "nse_common")
exitp = load("nse-exit", "exit")
tm = load("nse-timemachine", "timemachine")
check = load("nse-risk", "check")

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


def setUpModule():
    # every default path (config.json, plan.json, prices.json, orders.json...) is a temp folder, never the real trader/
    global _HOME, _ENV
    _HOME = tempfile.mkdtemp()
    _ENV = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": os.path.join(_HOME, "trader")})
    _ENV.start()


def tearDownModule():
    _ENV.stop()
    shutil.rmtree(_HOME)


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

    def test_description_is_short_and_keeps_its_triggers(self):
        meta, _, _ = skill_md("nse-budget")
        d = meta["description"][2:].strip()                      # drop the ">-" marker
        self.assertLess(len(d), 600, len(d))
        for phrase in ("how many shares", "size this trade", "how much should I buy", "I have ₹1 lakh",
                       "I only want to risk ₹500 a trade", "set my rules", "/nse-exit"):
            self.assertIn(phrase, d)

    def test_the_worked_example_in_the_doc_is_the_code(self):
        # recomputed from size.py, not guessed: every number in the SKILL.md example
        _, _, text = skill_md("nse-budget")
        s = size.size_trade(plan(), cfg(), today=TODAY)
        for want in ("buy 37 shares", "37 x 11.90 = ₹440.30 + ₹52.18 charges = ₹492.48", "38 would be ₹505.79",
                     "37 shares = ₹49,358", "₹384.80 − ₹52.71 charges = ₹332.09", "reward:risk 1.36 after charges",
                     "0.67 if the stop fills at its limit", "Break-even 1,335.50", "a move of 1.50",
                     "trigger 1,328.80, limit 1,322.10", "56 x 11.90 = ₹666.40 + ₹73.51 charges", "₹739.91",
                     "37 x 5.20 = ₹192.40 + ₹52.33 = ₹244.73", "size.py\" --init-config", "--write --append"):
            self.assertIn(want, text, want)
        self.assertEqual((s["qty"], s["max_loss_with_charges"], s["reward_risk"], s["break_even"]),
                         (37, 492.48, 1.36, 1335.5))
        self.assertEqual(nc.loss_at_stop(38, 1334.0, 1328.8, "RELIANCE", cfg())["total"], nc.dec("505.79"))


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
    ₹1,00,000 budget, ₹500 max loss with charges -> 37 shares, bound by the max loss measured at the stop
    leg's worst fill (the SL limit 1,322.10). The old code measured at the trigger and said 56."""

    def test_37_shares_bound_by_the_max_loss_at_the_worst_fill(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 37)
        self.assertEqual(s["bound_by"], "max loss")
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 37), ("position cap", 56), ("max order", 59), ("cash", 67)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max loss"])
        self.assertEqual((s["entry"], s["stop"], s["target"], s["tick"]), (1334.0, 1328.8, 1344.4, "0.10"))
        self.assertEqual((s["stop_limit"], s["worst_exit"], s["loss_basis"]), (1322.1, 1322.1, "limit"))
        self.assertEqual((s["risk_per_share"], s["risk_per_share_worst"]), (5.2, 11.9))
        self.assertEqual((s["cost"], s["max_loss"]), (49358.0, 440.3))                 # 37 x 11.90
        self.assertEqual((s["charges_at_stop"], s["max_loss_with_charges"]), (52.18, 492.48))
        self.assertEqual((s["charges_at_target"], s["gain_at_target"], s["net_at_target"]), (52.71, 384.8, 332.09))
        # reward:risk is the plan's R to the trigger, net of charges (what nse-exit prints and nse-risk gates on);
        # the worst-fill version is reported next to it
        self.assertEqual((s["reward_risk"], s["reward_risk_gross"], s["reward_risk_worst"]), (1.36, 2.0, 0.67))
        self.assertEqual((s["break_even"], s["break_even_move"]), (1335.5, 1.5))
        self.assertEqual((s["cash_buffer"], s["cash_available"]), (10000.0, 90000.0))
        self.assertIsNone(s["refusal"])

    def test_the_review_example_56_shares_would_lose_739_91_at_the_worst_fill(self):
        lo = nc.loss_at_stop(56, 1334.0, 1328.8, "RELIANCE", cfg())
        self.assertEqual((lo["exit_price"], lo["per_share"], lo["gross"], lo["charges"], lo["total"]),
                         (nc.dec("1322.1"), nc.dec("11.9"), nc.dec("666.4"), nc.dec("73.51"), nc.dec("739.91")))
        self.assertGreater(lo["total"], 500)
        self.assertEqual(nc.loss_at_stop(37, 1334.0, 1328.8, "RELIANCE", cfg())["total"], nc.dec("492.48"))
        self.assertGreater(nc.loss_at_stop(38, 1334.0, 1328.8, "RELIANCE", cfg())["total"], 500)
        # "loss_basis": "trigger" is the old way, and gives the old answer
        s = size.size_trade(plan(), cfg(loss_basis="trigger"), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["loss_basis"], s["worst_exit"]), (56, "position cap", "trigger", 1328.8))
        self.assertEqual(s["max_loss_with_charges"], 364.82)
        self.assertIn("at the stop trigger 1,328.80", s["limits"][0]["how"])

    def test_the_limit_texts_say_which_price_the_loss_is_measured_at(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["limits"][0]["how"], "₹500 incl. charges at the SL limit 1,322.10: 37 x 11.90 = "
                                                "₹440.30 + ₹52.18 charges = ₹492.48")
        refused = size.size_trade(plan(stop=800.0), cfg(), today=TODAY)
        self.assertIn("one share loses ₹539.07 at the SL limit 796.00", refused["refusal"])

    def test_charges_at_target_match_nse_common(self):
        self.assertEqual(nc.intraday_charges(37, 1334.0, 1344.4)["total"], 52.71)
        self.assertEqual(nc.intraday_charges(37, 1334.0, 1328.8)["total"], 52.33)         # at the trigger
        self.assertEqual(nc.intraday_charges(37, 1334.0, 1322.1)["total"], 52.18)         # at the worst fill

    def test_the_kite_shaped_order(self):
        o = size.build_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "RELIANCE",
            "entry": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                      "transaction_type": "BUY", "quantity": 37, "product": "MIS", "order_type": "LIMIT",
                      "price": 1334.0, "validity": "DAY", "tag": "dtindia"},
            "stop": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                     "transaction_type": "SELL", "quantity": 37, "product": "MIS", "order_type": "SL",
                     "trigger_price": 1328.8, "price": 1322.1, "validity": "DAY", "tag": "dtindia"},
            "target": {"price": 1344.4, "how": size.TARGET_HOW},
            "flat_by": "15:10",
            "plan": {"symbol": "RELIANCE", "entry": 1334.0, "stop": 1328.8, "target": 1344.4, "risk_per_share": 5.2,
                     "max_loss": 440.3, "charges_at_stop": 52.18, "max_loss_with_charges": 492.48,
                     "charges_at_target": 52.71, "reward_risk": 1.36, "reward_risk_worst": 0.67,
                     "break_even": 1335.5, "setup": "orb", "date": "2026-11-13", "entry_time": "10:05",
                     "flat_by": "15:10", "tick": "0.10", "loss_basis": "limit", "worst_exit": 1322.1,
                     "risk_per_share_worst": 11.9}})
        self.assertIn("no OCO for MIS", size.TARGET_HOW)
        self.assertIn("cancel the stop, then sell", size.TARGET_HOW)
        self.assertTrue(why.startswith("Opening-range breakout on RELIANCE: 37 shares, bound by your ₹500 max loss per "
                                       "trade"))
        self.assertIn("₹440.30 at the SL limit 1,322.10, ₹492.48 with charges", why)
        for leg in (o["entry"], o["stop"]):
            self.assertTrue(set(leg) <= set(nc.KITE_FIELDS))

    def test_the_risk_fixture_has_the_shape_it_writes(self):
        # orders-reliance.json is the old 56-share order (nse-risk and nse-broker tests share it, and it is not
        # ours to change): compare its SHAPE, not its numbers, to what size.py writes today
        with open(ORDERS, encoding="utf-8") as fh:
            fixture = json.load(fh)["orders"][0]
        mine = size.build_order(size.size_trade(plan(), cfg(), today=TODAY))

        def shape(x):
            if isinstance(x, dict):
                return {k: shape(v) for k, v in x.items()}
            return type(x).__name__

        for k in ("symbol", "entry", "stop", "target", "flat_by", "why"):
            self.assertEqual(shape(mine[k]), shape(fixture[k]), k)
        self.assertEqual(set(fixture["plan"]) - set(mine["plan"]), set())          # nothing the fixture has is gone
        for k in fixture["plan"]:
            self.assertEqual(shape(mine["plan"][k]), shape(fixture["plan"][k]), k)
        for k in ("entry", "stop", "target"):
            self.assertEqual(mine["plan"][k], fixture["plan"][k], k)
        self.assertEqual(mine["entry"]["price"], fixture["entry"]["price"])
        self.assertEqual(mine["stop"]["trigger_price"], fixture["stop"]["trigger_price"])
        self.assertEqual(mine["stop"]["price"], fixture["stop"]["price"])
        self.assertEqual((mine["entry"]["quantity"], fixture["entry"]["quantity"]), (37, 56))

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0, text)
            self.assertIn("Size: 37 shares of RELIANCE = ₹49,358 at 1,334.00.", text)
            self.assertIn("If the stop is hit and fills at the SL limit 1,322.10: 37 x 11.90 = ₹440.30, plus about "
                          "₹52.18 in charges = ₹492.48.", text)
            self.assertIn("minus about ₹52.71 in charges = ₹332.09. Reward:risk 1.36 after charges", text)
            self.assertIn("measured to the stop trigger 1,328.80", text)
            self.assertIn("0.67 if the stop fills at the SL limit 1,322.10", text)
            self.assertIn("Break-even: 1,335.50, a move of 1.50 a share", text)
            self.assertIn("max loss      ₹500 incl. charges at the SL limit 1,322.10: 37 x 11.90 = ₹440.30 + ₹52.18 "
                          "charges = ₹492.48", text)
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
            o = doc["orders"][0]
            self.assertEqual(o["plan"]["max_loss_with_charges"], 492.48)
            # nse-risk and nse-broker read these two: the IST day of the plan and the order's own age
            self.assertEqual((o["plan"]["symbol"], o["plan"]["date"]), ("RELIANCE", "2026-11-13"))
            self.assertEqual(o["created_at"], "2026-11-13T04:35:00Z")

    def test_cli_json(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 37)
        self.assertEqual(out["orders"][0]["entry"]["price"], 1334.0)
        self.assertEqual(out["orders"][0]["created_at"], "2026-11-13T04:35:00Z")


class NearAThousand(unittest.TestCase):
    """item 11: a stop at 997.97 on the 0.05 grid is rejected when the stock's tick is still 0.10 (NSE sets it
    from an earlier close). Every order price goes on nc.order_tick: the coarsest tick within 10%."""

    def test_the_stop_limit_and_target_are_valid_under_both_ticks(self):
        p = plan(symbol="ABC", entry=1005.0, stop=997.97, target=1020.03)
        s = size.size_trade(p, cfg(), today=TODAY)
        self.assertEqual((s["tick"], s["stop"], s["stop_limit"], s["target"]), ("0.10", 997.9, 992.9, 1020.0))
        o = size.build_order(s)
        for price in (o["entry"]["price"], o["stop"]["trigger_price"], o["stop"]["price"], o["target"]["price"]):
            self.assertEqual(nc.dec(price) % nc.dec("0.10"), 0, price)       # on the 0.10 grid ...
            self.assertEqual(nc.dec(price) % nc.dec("0.05"), 0, price)       # ... and so on the 0.05 grid too
        # the old per-slab tick would have put 997.97 on 997.95: off the 0.10 grid
        self.assertEqual(nc.round_to_tick(nc.dec("997.97"), nc.tick_size(nc.dec("997.97")), "down"), nc.dec("997.95"))
        self.assertEqual(nc.dec("997.95") % nc.dec("0.10"), nc.dec("0.05"))
        # a stock whose price is under the edge but whose limit is over it: same rule
        s = size.size_trade(plan(symbol="ABC", entry=999.95, stop=994.9, target=1010.0), cfg(), today=TODAY)
        self.assertEqual((s["tick"], s["entry"], s["stop"]), ("0.10", 1000.0, 994.9))     # 999.95 -> the 0.10 grid
        # a per-symbol tick in the config still wins
        s = size.size_trade(p, cfg(tick_sizes={"ABC": 0.05}), today=TODAY)
        self.assertEqual((s["tick"], s["stop"]), ("0.05", 997.95))


class SetYourRules(unittest.TestCase):
    """items 8: nse-budget is the one place a config gets created; without one it stops and writes nothing."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.home = os.path.join(self.d, "trader")
        self.env = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.home})
        self.env.start()
        self.cfg_path = os.path.join(self.home, "config.json")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.d)

    def test_missing_config_exits_2_and_writes_nothing(self):
        out = os.path.join(self.home, "orders.json")
        code, text, err = run(["--plan", PLAN, "--now", NOW, "--portfolio", os.path.join(self.d, "none"), "--write"])
        self.assertEqual(code, 2)
        self.assertEqual(text, "")
        self.assertIn("no config.json at %s" % self.cfg_path, err)         # the real path, not "trader/config.json"
        self.assertIn("--init-config", err)
        self.assertIn("the tools will not guess them", err)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(self.cfg_path))
        self.assertFalse(os.path.exists(self.home))                       # not even the folder
        self.assertNotIn("Traceback", err)
        # the same with --json and with an explicit --config that is not there
        self.assertEqual(run(["--plan", PLAN, "--now", NOW, "--json"])[0], 2)
        self.assertEqual(run(["--plan", PLAN, "--now", NOW, "--config", os.path.join(self.d, "x.json")])[0], 2)

    def test_init_config_writes_the_defaults_plus_your_numbers(self):
        code, text, err = run(["--init-config", "--budget", "50000", "--max-loss", "300", "--max-daily-loss", "900"])
        self.assertEqual(code, 0, err)
        with open(self.cfg_path, encoding="utf-8") as fh:
            c = json.load(fh)
        want = dict(nc.DEFAULT_CONFIG, budget=50000, max_loss_per_trade_inr=300, max_daily_loss_inr=900, broker="paper")
        self.assertEqual(c, want)
        self.assertEqual((c["cash_buffer_pct"], c["max_position_pct"], c["stop_limit_buffer_pct"]), (10, 75, 0.5))
        self.assertIsInstance(c["budget"], int)
        self.assertIn("Wrote %s" % self.cfg_path, text)
        flat = " ".join(text.split())
        self.assertIn("budget ₹50,000 budget (yours)", flat)
        self.assertIn("max_loss_per_trade_inr ₹300 max loss per trade (charges included) (yours)", flat)
        self.assertIn("(default)", text)
        self.assertIn("paper", text)
        # and it is a working config: the next run sizes with it (300 cap, not 500)
        code, text, err = run(["--plan", PLAN, "--now", NOW, "--portfolio", os.path.join(self.d, "none"), "--json"])
        self.assertEqual(code, 0, err)
        s = json.loads(text)["sizing"]
        self.assertEqual((s["budget"], s["max_loss_per_trade_inr"], s["qty"]), (50000.0, 300.0, 22))

    def test_init_config_never_overwrites(self):
        self.assertEqual(run(["--init-config", "--budget", "50000"])[0], 0)
        before = open(self.cfg_path, encoding="utf-8").read()
        code, text, err = run(["--init-config", "--budget", "999999"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertIn("never overwrites", err)
        self.assertEqual(open(self.cfg_path, encoding="utf-8").read(), before)      # untouched
        self.assertEqual(json.loads(before)["budget"], 50000)
        # an existing config the user wrote by hand is just as safe, even an empty one
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertEqual(run(["--init-config"])[0], 1)
        self.assertEqual(open(self.cfg_path, encoding="utf-8").read(), "{}")

    def test_init_config_refuses_numbers_that_make_no_sense(self):
        cases = (("--max-loss", "100000", "not under the budget"),        # the default budget is 1,00,000
                 ("--max-daily-loss", "100", "under the max loss per trade"),
                 ("--budget", "-5", "must be above 0"),
                 ("--max-loss", "0", "must be above 0"),
                 ("--budget", "nan", "wants a number"),
                 ("--cash-buffer-pct", "100", "between 0 and 100"),
                 ("--max-trades", "2.5", "whole number"))
        for flag, value, why in cases:
            code, text, err = run(["--init-config", flag, value])
            self.assertEqual(code, 1, flag)
            self.assertIn(why, err, flag)
            self.assertIn("nothing written", err, flag)
            self.assertFalse(os.path.exists(self.cfg_path), flag)
            self.assertNotIn("Traceback", err)

    def test_rule_flags_only_go_with_init_config(self):
        self.assertEqual(run(["--init-config", "--budget", "50000"])[0], 0)
        code, _, err = run(["--plan", PLAN, "--now", NOW, "--max-loss", "100"])
        self.assertEqual(code, 1)
        self.assertIn("only goes with --init-config", err)

    def test_the_default_paths_follow_the_trader_home_not_the_cwd(self):
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            self.assertEqual(run(["--init-config"])[0], 0)
            self.assertTrue(os.path.exists(os.path.join(self.home, "config.json")))
            self.assertFalse(os.path.exists(os.path.join(self.d, "trader", "trader")))
        finally:
            os.chdir(cwd)


class OneOrderPerSymbol(unittest.TestCase):
    """item 22: --append adds this symbol's order to orders.json and keeps the others."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "orders.json")
        self.tcs = os.path.join(self.d, "plan-tcs.json")
        with open(self.tcs, "w", encoding="utf-8") as fh:
            json.dump(plan(symbol="TCS", entry=3500.0, stop=3480.0, target=3540.0), fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def size(self, plan_path, now=NOW, *more):
        code, text, err = run(["--plan", plan_path, "--config", CFG, "--portfolio", os.path.join(self.d, "none"),
                               "--now", now, "--write", self.out] + list(more))
        self.assertEqual(code, 0, text + err)
        with open(self.out, encoding="utf-8") as fh:
            return json.load(fh), text

    def test_append_keeps_the_other_symbol(self):
        doc, _ = self.size(PLAN)
        self.assertEqual([o["symbol"] for o in doc["orders"]], ["RELIANCE"])
        doc, text = self.size(self.tcs, "2026-11-13 10:20", "--append")
        self.assertEqual([o["symbol"] for o in doc["orders"]], ["RELIANCE", "TCS"])
        self.assertIn("2 orders in the file", text)
        rel, tcs = doc["orders"]
        self.assertEqual(rel["entry"]["quantity"], 37)                                # untouched
        self.assertEqual((tcs["entry"]["tradingsymbol"], tcs["plan"]["symbol"], tcs["plan"]["date"]),
                         ("TCS", "TCS", "2026-11-13"))
        # the file's created_at is the newest write; each order keeps its own
        self.assertEqual(doc["created_at"], "2026-11-13T04:50:00Z")
        self.assertEqual((rel["created_at"], tcs["created_at"]), ("2026-11-13T04:35:00Z", "2026-11-13T04:50:00Z"))

    def test_append_replaces_an_order_for_the_same_symbol(self):
        self.size(PLAN)
        self.size(self.tcs, "2026-11-13 10:20", "--append")
        doc, text = self.size(PLAN, "2026-11-13 10:40", "--append", "--limit", "1333.50")
        self.assertEqual(sorted(o["symbol"] for o in doc["orders"]), ["RELIANCE", "TCS"])        # still two
        rel = [o for o in doc["orders"] if o["symbol"] == "RELIANCE"][0]
        self.assertEqual(rel["entry"]["price"], 1333.5)                                           # the new one
        self.assertIn("Replaced the RELIANCE order", text)
        tcs = [o for o in doc["orders"] if o["symbol"] == "TCS"][0]
        self.assertEqual(tcs["created_at"], "2026-11-13T04:50:00Z")

    def test_without_append_it_overwrites_as_before(self):
        self.size(PLAN)
        doc, _ = self.size(self.tcs, "2026-11-13 10:20")
        self.assertEqual([o["symbol"] for o in doc["orders"]], ["TCS"])

    def test_append_drops_yesterdays_order_and_keeps_an_old_orders_age(self):
        self.size(PLAN)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        # an order from an older nse-budget: no created_at of its own (the file's was its age), and one from yesterday
        legacy = copy.deepcopy(doc["orders"][0])
        legacy.pop("created_at")
        legacy["symbol"] = legacy["plan"]["symbol"] = "INFY"
        yesterday = copy.deepcopy(legacy)
        yesterday["symbol"] = yesterday["plan"]["symbol"] = "HDFCBANK"
        yesterday["plan"]["date"] = "2026-11-12"
        doc["orders"] += [legacy, yesterday]
        with open(self.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        doc, text = self.size(self.tcs, "2026-11-13 12:00", "--append")
        self.assertEqual([o["symbol"] for o in doc["orders"]], ["RELIANCE", "INFY", "TCS"])
        self.assertIn("Dropped the HDFCBANK order for 2026-11-12", text)
        infy = doc["orders"][1]
        # the file's created_at is now 12:00; INFY must not look 12:00-fresh: it keeps the old file's time
        self.assertEqual((doc["created_at"], infy["created_at"]), ("2026-11-13T06:30:00Z", "2026-11-13T04:35:00Z"))

    def test_a_refused_order_leaves_the_file_alone(self):
        self.size(PLAN)
        before = open(self.out, encoding="utf-8").read()
        thin = os.path.join(self.d, "thin.json")
        with open(thin, "w", encoding="utf-8") as fh:
            json.dump(plan(symbol="TCS", entry=3500.0, stop=3480.0, target=3501.0), fh)
        code, text, _ = run(["--plan", thin, "--config", CFG, "--portfolio", os.path.join(self.d, "none"),
                             "--now", NOW, "--write", self.out, "--append"])
        self.assertEqual(code, 1)
        self.assertEqual(open(self.out, encoding="utf-8").read(), before)

    def test_append_refuses_a_broken_orders_file(self):
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(self.d, "none"),
                            "--now", NOW, "--write", self.out, "--append"])
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        self.assertEqual(open(self.out, encoding="utf-8").read(), "{not json")           # not clobbered


SEC_PATH = os.path.join(FIX, "nse-scanner", "sec_list.csv.gz")


class OneShareCount(unittest.TestCase):
    """item 19: nse-exit's estimate, nse-timemachine's estimate and nse-budget's order are the same
    arithmetic (nse_common.size_qty / size_limits), on the same account files. And what nse-budget proposes
    has to pass nse-risk."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.home = os.path.join(self.d, "trader")
        os.makedirs(self.home)
        self.env = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.home})
        self.env.start()
        self.cfg_path = os.path.join(self.home, "config.json")
        self.pf_path = os.path.join(self.home, "portfolio.json")
        self.plan_path = os.path.join(self.home, "plan.json")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def three(self, config=None, portfolio=None, stop=None, symbol_flags=None):
        """(exit's quantity, timemachine's estimate, size.py's shares) for the RELIANCE example."""
        self.write(self.cfg_path, dict(nc.DEFAULT_CONFIG, **(config or {})))
        if portfolio is not None:
            self.write(self.pf_path, portfolio)
        elif os.path.exists(self.pf_path):
            os.remove(self.pf_path)
        argv = ["plan", "--symbol", "RELIANCE", "--entry", "1334", "--or-high", "1333.40", "--or-low", "1324.30",
                "--time", "10:05", "--entry-bar", "9", "--date", TODAY, "--now", "2026-11-13 10:06",
                "--config", self.cfg_path, "--write", self.plan_path, "--json"]
        if stop:
            argv += ["--stop", stop]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(exitp.main(argv), 0)
        with open(self.plan_path, encoding="utf-8") as fh:
            written = json.load(fh)
        exit_qty = written["quantity"]
        # the timemachine reads the same plan, without the quantity exit wrote, so it has to work it out itself
        tm_plan = tm.normalize_plan({k: v for k, v in written.items() if k != "quantity"})
        cash, total, _ = tm.account_files(self.cfg_path)
        tm_qty = tm.estimate_qty(tm_plan, nc.load_config(self.cfg_path), cash, total)
        code, text, err = run(["--plan", self.plan_path, "--config", self.cfg_path, "--portfolio", self.pf_path,
                               "--now", NOW, "--json"])
        self.assertIn(code, (0, 1), err)
        size_qty = json.loads(text)["sizing"]["qty"]
        return exit_qty, tm_qty, size_qty

    def test_the_three_agree_on_the_example(self):
        self.assertEqual(self.three(), (37, 37, 37))

    def test_the_three_agree_when_each_limit_decides(self):
        cases = (
            ("max loss at the worst fill", dict(), None, None, 37),
            ("the old trigger basis", dict(loss_basis="trigger"), None, None, 56),
            ("max loss 200", dict(max_loss_per_trade_inr=200), None, None, 15),
            ("max order", dict(max_order_inr=40000), None, None, 29),
            ("position cap", dict(max_position_pct=40), None, None, 29),
            ("a smaller real account", dict(), {"cash": 20000, "positions": []}, None, 11),
            ("cash after holdings", dict(), {"cash": 50000, "positions": [
                {"ticker": "TCS", "shares": 10, "lots": [{"shares": 10, "cost": 35000}]}]}, None, 31),
            ("a stop so far that nothing fits", dict(), None, "price:800", 0),
            ("a wide stop", dict(), None, "price:1300", 11),        # 11 x 40.50 + charges = 493.9; 12 would be 538
        )
        for name, config, portfolio, stop, want in cases:
            got = self.three(config, portfolio, stop)
            self.assertEqual(got, (want, want, want), name)

    def test_what_nse_budget_proposes_passes_nse_risk(self):
        self.write(self.cfg_path, nc.DEFAULT_CONFIG)
        code, text, err = run(["--plan", PLAN, "--config", self.cfg_path, "--portfolio", self.pf_path, "--now", NOW,
                               "--json"])
        self.assertEqual(code, 0, err)
        order = json.loads(text)["orders"][0]
        self.assertEqual(order["entry"]["quantity"], 37)
        sec = check.read_sec_list(SEC_PATH)
        now = nc.parse_now(NOW)
        quote_time = nc.utc_iso(now - __import__("datetime").timedelta(minutes=1))
        quote = {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": 1334.0},
                 "detail": {"RELIANCE": {"time": quote_time, "fetched": quote_time}}}

        def verdict(o, **kw):
            res = check.check([o], dict(nc.DEFAULT_CONFIG), now=now, sec=sec, sec_info={"date": TODAY},
                              created_at=quote_time, prices_doc=quote, **kw)
            return res["results"][0]["verdict"], " | ".join(res["results"][0]["reasons"]), res["results"][0]

        v, why, r = verdict(order)
        self.assertEqual(v, "PASS", why)
        self.assertEqual(r["max_loss"], 492.48)                  # check.py and size.py measure the same worst fill
        self.assertLessEqual(r["max_loss"], 500)
        # the review's order: 56 shares (what the position cap and the trigger-basis maths allowed) must BLOCK
        big = copy.deepcopy(order)
        big["entry"]["quantity"] = big["stop"]["quantity"] = 56
        v, why, r = verdict(big)
        self.assertEqual(v, "BLOCK")
        self.assertIn("739.91", why)
        self.assertEqual(r["max_loss"], 739.91)
        # a real account of 20,000: both skills size the position cap on the same account_base
        pf = {"cash": 20000, "positions": []}
        self.write(self.pf_path, pf)
        code, text, err = run(["--plan", PLAN, "--config", self.cfg_path, "--portfolio", self.pf_path, "--now", NOW,
                               "--json"])
        small = json.loads(text)["orders"][0]
        self.assertEqual(small["entry"]["quantity"], 11)
        v, why, _ = verdict(small, portfolio=pf)
        self.assertEqual(v, "PASS", why)
        more = copy.deepcopy(small)
        more["entry"]["quantity"] = more["stop"]["quantity"] = 12
        v, why, _ = verdict(more, portfolio=pf)
        self.assertEqual(v, "BLOCK")
        self.assertIn("position", why)


class IndiaRules(unittest.TestCase):
    def test_charges_count_against_the_max_loss(self):
        big = dict(budget=1000000, max_position_pct=100, max_order_inr=1000000)
        s = size.size_trade(plan(), cfg(**big), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (37, "max loss"))
        self.assertEqual(s["max_loss_with_charges"], 492.48)          # 38 shares would be ₹505.79 at the worst fill
        self.assertLessEqual(s["max_loss_with_charges"], 500)
        self.assertGreater(38 * 11.9 + nc.intraday_charges(38, 1334.0, 1322.1)["total"], 500)
        s = size.size_trade(plan(), cfg(include_charges=False, **big), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"]), (42, "max loss", 499.8))      # 42 x 11.90
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
        self.assertEqual((s["reward_risk_gross"], s["reward_risk"], s["reward_risk_worst"]), (1.08, 0.63, 0.31))
        self.assertIn("reward:risk 0.63 after charges (to the stop trigger 1,328.80", s["refusal"])
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
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=40000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(max_position_pct=40), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (29, "position cap"))
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (7, "cash", 10000.0))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=200), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss_with_charges"]), (15, "max loss", 199.65))
        self.assertLessEqual(s["max_loss_with_charges"], 200)

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 20000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            # the account is ₹20,000, under the ₹1,00,000 budget: nc.account_base shrinks the base, so the 75%
            # position cap is ₹15,000 (11 shares), not ₹75,000. (The old code kept the budget as the base and said
            # 7 shares bound by cash; nse-risk used the real account, so risk blocked what budget proposed.)
            s = out["sizing"]
            self.assertEqual((s["qty"], s["bound_by"], s["account_base"], s["portfolio_total"]),
                             (11, "position cap", 20000.0, 20000.0))
            self.assertIn("account ₹20,000", s["limits"][1]["how"])
            self.assertEqual(s["cash_available"], 18000.0)                 # 20,000 less 10% of 20,000

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
        self.assertEqual(s["qty"], 2)                                  # ₹1.02 to the worst fill 2.98, not ₹1.00
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
