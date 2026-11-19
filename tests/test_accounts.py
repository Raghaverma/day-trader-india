"""autopilot/accounts.py: more paper accounts that trade the same signals for real on paper, one per cap. The replay
runs the real scripts on the 13 November 2026 fixtures, so every account takes the README's RELIANCE trade."""

import json
import os
import unittest

from _load import ROOT, FIX
from test_autopilot import HomeBase, CFG_PATH, DAY, text, utc, ac, nc, report, run_day

import accounts  # noqa: E402  (test_autopilot put autopilot/ on sys.path)
import progress  # noqa: E402


class Config(unittest.TestCase):
    cfg = nc.load_config(CFG_PATH)            # ₹1,00,000: ₹500 a trade, ₹1,500 a day, 3 trades

    def test_each_cap_gets_its_own_daily_limit_and_nothing_else_changes(self):
        c = accounts.config_for(dict(self.cfg, broker="paper", autopilot=True), 8)
        self.assertEqual((c["max_trades_per_day"], c["max_daily_loss_inr"]), (8, 4000.0))
        for k in ("budget", "max_loss_per_trade_inr", "cash_buffer_pct", "broker", "autopilot", "allow_fno"):
            self.assertEqual(c[k], dict(self.cfg, broker="paper", autopilot=True)[k])

    def test_caps(self):
        self.assertEqual(accounts.caps_of({"accounts": {"enabled": True, "caps": [5, "8", 5, 0, "x", 10]}}), [5, 8, 10])
        self.assertEqual(accounts.caps_of({"accounts": {"enabled": False, "caps": [5]}}), [])
        self.assertEqual(accounts.caps_of({}), [])


class InTheDay(HomeBase):
    def setUp(self):
        super(InTheDay, self).setUp()
        self.set_cfg(broker="paper")
        self.settings["accounts"] = {"enabled": True, "caps": [3, 5]}          # 3 is the main account itself
        self.write_settings()

    def acc(self, *parts):
        return os.path.join(self.d, "accounts", "cap5", *parts)

    def test_every_account_trades_the_signal_on_its_own_paper(self):
        self.replay()
        self.assertFalse(os.path.exists(os.path.join(self.d, "accounts", "cap3")))   # the main account is cap 3
        with open(self.acc("config.json")) as fh:
            cfg = json.load(fh)
        self.assertEqual((cfg["max_trades_per_day"], cfg["max_daily_loss_inr"], cfg["broker"], cfg["autopilot"]),
                         (5, 2500.0, "paper", True))
        self.assertTrue(os.path.islink(self.acc("cache")))

        main = self.doc("state", "paper.json")["trades"]
        mine = self.doc("accounts", "cap5", "state", "paper.json")["trades"]
        self.assertEqual([(t["symbol"], t["qty"], t["exit_reason"], t["pnl_net"]) for t in main],
                         [("RELIANCE", 37, "target", 328.4)])
        self.assertEqual([(t["symbol"], t["qty"], t["exit_reason"], t["pnl_net"]) for t in mine],
                         [("RELIANCE", 37, "target", 328.4)])               # its own order, fill and exit
        appr = [r for r in self.doc("accounts", "cap5", "state", "broker.json")["orders"]
                if r.get("status") == "approved"]
        self.assertEqual([r["approved_by"] for r in appr], ["autopilot"])
        self.assertEqual(self.doc("accounts", "cap5", "portfolio.json")["cash"], round(100000 + 328.4, 2))
        st = self.state()
        self.assertEqual(st["candidates"]["RELIANCE"]["accounts"]["cap5"]["outcome"], "traded")
        self.assertEqual(st["phases"]["premarket"]["accounts"], ["cap5"])

        rep = self.doc("reports", DAY + ".json")
        self.assertEqual([(a["name"], a["main"], a["closed"], a["net"]) for a in rep["accounts"]],
                         [("cap3", True, 1, 328.4), ("cap5", False, 1, 328.4)])
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("## The paper accounts: 3 and 5 trades a day", md)
        self.assertIn("| 3 trades a day (main account) | ₹1,500 | 1 | 1 | 0 | 1 | 0 | +₹328.40 |", md)
        self.assertIn("| 5 trades a day | ₹2,500 | 1 | 1 | 0 | 1 | 0 | +₹328.40 |", md)
        self.assertIn("The paper accounts, all days", md)
        card = self.doc("reports", "scorecard.json")["totals"]["accounts"]
        self.assertEqual(list(card), ["cap3", "cap5"])

        s = {"accounts": rep["accounts"]}
        lines = progress.account_lines(s)
        self.assertIn("5 trades a day:", lines[1])
        self.assertIn("1 of 5 trades used, 0 open, 1 closed (1 won, 0 lost), net +₹328", lines[1])

    def test_a_full_account_takes_no_more_and_asks_nobody(self):
        d = self.day("%s 08:45" % DAY)
        a = d.accounts(refresh=True)[0]
        os.makedirs(self.acc("state"), exist_ok=True)
        with open(self.acc("state", "broker.json"), "w") as fh:
            json.dump({"orders": [{"date": DAY, "status": "approved", "kind": "entry"}] * 5}, fh)
        calls = []
        a["runner"].run = lambda *x, **k: calls.append(x)
        out = d.account_trade(a, "RELIANCE", os.path.join(self.d, "plan.json"))
        self.assertEqual(out, {"outcome": "shadow", "reason": "the day's cap of 5 trades is used"})
        self.assertEqual(calls, [])

    def test_an_account_that_is_not_paper_is_left_out(self):
        d = self.day("%s 08:45" % DAY)
        d.accounts(refresh=True)
        with open(self.acc("config.json")) as fh:
            c = json.load(fh)
        c["broker"] = "kite"
        with open(self.acc("config.json"), "w") as fh:
            json.dump(c, fh)
        self.assertEqual(self.day("%s 10:05" % DAY).accounts(), [])

    def test_off_by_default(self):
        self.settings.pop("accounts")
        self.write_settings()
        self.replay()
        self.assertFalse(os.path.exists(os.path.join(self.d, "accounts")))
        self.assertIsNone(self.doc("reports", DAY + ".json")["accounts"])


if __name__ == "__main__":
    unittest.main()
