"""autopilot/accounts.py: more paper accounts that trade the same signals for real on paper, one per cap. The replay
runs the real scripts on the 13 November 2026 fixtures, so every account takes the README's RELIANCE trade."""

import json
import os
import unittest
from unittest import mock

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

    def test_an_accounts_own_filter(self):
        on = {"accounts": {"enabled": True, "caps": [5, 8], "filters": {"cap5": {"min_open_rvol": 1.5}}}}
        self.assertEqual(accounts.filter_of(on, 5), {"min_open_rvol": 1.5})
        self.assertIsNone(accounts.filter_of(on, 8))
        self.assertEqual(accounts.filter_of({"accounts": {"enabled": True, "filters": {"5": {"min_open_rvol": "2"}}}},
                                            5), {"min_open_rvol": 2.0})
        for bad in ({"min_open_rvol": 0}, {"min_open_rvol": "x"}, {}, "1.5"):
            self.assertIsNone(accounts.filter_of({"accounts": {"enabled": True, "filters": {"cap5": bad}}}, 5))
        # read with the extra accounts off too: the main account's own cap can carry the filter
        self.assertEqual(accounts.filter_of(dict(on, accounts=dict(on["accounts"], enabled=False)), 5),
                         {"min_open_rvol": 1.5})
        self.assertIsNone(accounts.filter_of({}, 5))
        self.assertEqual(accounts.label_of(5, {"min_open_rvol": 1.5}, True),
                         "5 trades a day, opening volume 1.5x or more only (main account)")
        f = {"min_open_rvol": 1.5}
        self.assertIsNone(accounts.filter_reason(f, 1.5))
        self.assertIsNone(accounts.filter_reason(f, 3.44))
        self.assertEqual(accounts.filter_reason(f, 1.11),
                         "opening volume 1.11x its 20-day median, under the filter's 1.5x")
        self.assertIn("not known", accounts.filter_reason(f, None))
        self.assertIsNone(accounts.filter_reason(None, 0.2))
        self.assertEqual(accounts.filter_words(f), "opening volume 1.5x or more")


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

    def with_filter(self):
        self.settings["accounts"]["filters"] = {"cap5": {"min_open_rvol": 1.5}}
        self.write_settings()

    def test_a_filter_keeps_out_a_signal_with_too_little_opening_volume(self):
        self.with_filter()
        with mock.patch.object(run_day.Day, "open_rvol", return_value=1.11):
            self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual(c["open_rvol"], 1.11)
        self.assertEqual(c["outcome"], "traded")                          # the main account has no filter
        self.assertEqual(c["accounts"]["cap5"], {"outcome": "filtered", "open_rvol": 1.11,
                                                 "reason": "opening volume 1.11x its 20-day median, under the "
                                                           "filter's 1.5x"})
        paper = os.path.join(self.d, "accounts", "cap5", "state", "paper.json")
        self.assertEqual((ac.read_json(paper) or {}).get("trades") or [], [])
        self.assertFalse(os.path.exists(self.acc("plan.json")))           # never sized, never asked
        rep = self.doc("reports", DAY + ".json")
        a = rep["accounts"][1]
        self.assertEqual((a["label"], a["filter"], a["orders"]),
                         ("5 trades a day, opening volume 1.5x or more only", {"min_open_rvol": 1.5}, 0))
        self.assertEqual(a["signals"], [{"symbol": "RELIANCE", "open_rvol": 1.11, "outcome": "filtered",
                                         "reason": c["accounts"]["cap5"]["reason"]}])
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("| 5 trades a day, opening volume 1.5x or more only | ₹2,500 | 0 | 0 | 0 | 0 | 0 |", md)
        self.assertIn("The 5-a-day account has a filter the others do not", md)
        self.assertIn("| RELIANCE | 1.11x | kept out |", md)
        self.assertIn("Let through 0 of 1 signals, kept out 1.", md)
        self.assertIn("5 trades a day, opening volume 1.5x or more only:", progress.account_lines(rep)[1])

    def test_a_filter_lets_a_signal_with_enough_opening_volume_through(self):
        self.with_filter()
        with mock.patch.object(run_day.Day, "open_rvol", return_value=2.4):
            self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual(c["accounts"]["cap5"]["outcome"], "traded")
        mine = self.doc("accounts", "cap5", "state", "paper.json")["trades"]
        self.assertEqual([(t["symbol"], t["qty"], t["exit_reason"], t["pnl_net"]) for t in mine],
                         [("RELIANCE", 37, "target", 328.4)])
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("| RELIANCE | 2.40x | let through | traded |", md)

    def main_has_the_filter(self):
        """The main account trades 5 a day with the filter; the extra account is 3 a day, every signal."""
        self.settings["accounts"] = {"enabled": True, "caps": [3, 5], "filters": {"cap5": {"min_open_rvol": 1.5}}}
        self.write_settings()
        self.set_cfg(max_trades_per_day=5, max_daily_loss_inr=2500)

    def test_the_main_accounts_own_filter_keeps_it_out_but_not_the_other_accounts(self):
        self.main_has_the_filter()
        with mock.patch.object(run_day.Day, "open_rvol", return_value=1.11):
            self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual(c["outcome"], "filtered")
        self.assertEqual(c["reason"], "opening volume 1.11x its 20-day median, under the filter's 1.5x")
        self.assertEqual(c["accounts"], {"cap3": c["accounts"]["cap3"]})          # cap5 is the main account itself
        self.assertEqual(c["accounts"]["cap3"]["outcome"], "traded")              # it still saw the signal
        self.assertFalse(os.path.exists(os.path.join(self.d, "accounts", "cap5")))
        self.assertEqual((ac.read_json(os.path.join(self.d, "state", "paper.json")) or {}).get("trades") or [], [])
        mine = self.doc("accounts", "cap3", "state", "paper.json")["trades"]
        self.assertEqual([(t["symbol"], t["exit_reason"], t["pnl_net"]) for t in mine], [("RELIANCE", "target", 328.4)])
        rep = self.doc("reports", DAY + ".json")
        self.assertEqual(rep["rules"]["filter"], {"min_open_rvol": 1.5})
        self.assertEqual([(a["name"], a["main"], a["label"]) for a in rep["accounts"]],
                         [("cap3", False, "3 trades a day"),
                          ("cap5", True, "5 trades a day, opening volume 1.5x or more only (main account)")])
        main = rep["accounts"][1]
        self.assertEqual([(s["symbol"], s["outcome"], s["replay_result"], s["replay_net"]) for s in main["signals"]],
                         [("RELIANCE", "filtered", "target", rep["shadows"]["rows"][0]["net"])])
        self.assertEqual(rep["shadows"]["rows"][0]["why"], "filtered")
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("Only stocks whose opening volume (09:20-09:30) is at least 1.5x its 20-day median.", md)
        self.assertIn("The main account (5 a day) has a filter", md)
        self.assertIn("| Stock | Opening volume | Filter | What happened | If taken (replayed) |", md)
        self.assertIn("| RELIANCE | 1.11x | kept out |  | target, +₹", md)
        self.assertIn("The 1 kept out that could be replayed would have made +₹", md)
        self.assertIn("1 kept out by the main account's opening-volume filter", md)
        self.assertIn("Long, kept out by the opening-volume filter (replayed)", md)

    def test_the_main_accounts_filter_lets_enough_volume_through(self):
        self.main_has_the_filter()
        with mock.patch.object(run_day.Day, "open_rvol", return_value=2.4):
            self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual((c["outcome"], c["accounts"]["cap3"]["outcome"]), ("traded", "traded"))
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("| RELIANCE | 2.40x | let through | traded |  |", md)

    def test_the_main_accounts_filter_holds_with_the_other_accounts_off(self):
        self.main_has_the_filter()
        self.settings["accounts"]["enabled"] = False
        self.write_settings()
        with mock.patch.object(run_day.Day, "open_rvol", return_value=1.11):
            self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual(c["outcome"], "filtered")
        self.assertNotIn("accounts", c)
        rep = self.doc("reports", DAY + ".json")
        self.assertEqual([(a["name"], a["main"]) for a in rep["accounts"]], [("cap5", True)])

    def test_a_filter_leaves_out_a_signal_whose_opening_volume_is_not_known(self):
        self.with_filter()
        self.replay()                                     # no market data in a test: not known
        c = self.state()["candidates"]["RELIANCE"]
        self.assertIsNone(c["open_rvol"])
        self.assertEqual(c["accounts"]["cap5"]["outcome"], "filtered")
        self.assertIn("not known", c["accounts"]["cap5"]["reason"])
        self.assertIn("| RELIANCE | not known | kept out |", text(os.path.join(self.d, "reports", DAY + ".md")))

    def test_off_by_default(self):
        self.settings.pop("accounts")
        self.write_settings()
        self.replay()
        self.assertFalse(os.path.exists(os.path.join(self.d, "accounts")))
        self.assertIsNone(self.doc("reports", DAY + ".json")["accounts"])


if __name__ == "__main__":
    unittest.main()
