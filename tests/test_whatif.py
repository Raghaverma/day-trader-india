"""autopilot/whatif.py and the deep scan in the day: why only N trades, the whole principal, and the stock files
before the open. The replay runs the real scripts on the 13 November 2026 fixtures (the README's RELIANCE trade)."""

import os
import unittest
from unittest import mock

from _load import ROOT, FIX
from test_autopilot import HomeBase, CFG_PATH, DAY, text, utc, sig, ac, nc, report, run_day, sizes, mail, ClaudeStub, \
    MailStub

import whatif  # noqa: E402  (test_autopilot put autopilot/ on sys.path)


class Limits(unittest.TestCase):
    cfg = nc.load_config(CFG_PATH)            # ₹1,00,000: ₹500 a trade, ₹1,500 a day, 3 trades, 10% kept back

    def four(self, exit_=1344.3, result="target", exit_at=None):
        return [sig(symbol=s, at="10:%02d" % (5 + i), exit=exit_, result=result,
                    exit_at=exit_at or "10:%02d" % (6 + i)) for i, s in enumerate(("A", "B", "C", "D"))]

    def test_each_limit_lifted_in_turn(self):
        sigs = self.four()
        steps = [sizes.simulate(sigs, self.cfg, 100000, margin=m, overrides=o) for _, o, m in whatif.ladder_steps()]
        self.assertEqual([len(s["trades"]) for s in steps], [3, 3, 4, 4])
        self.assertEqual(steps[0]["skipped"], {"trade cap": 1})
        self.assertEqual(steps[1]["skipped"], {"daily loss limit": 1})     # ₹1,500 / ₹500 = 3 entries, even winners
        self.assertEqual(steps[2]["skipped"], {})

    def test_the_cash_limit_and_unlimited_cash(self):
        sigs = self.four(exit_at="14:00")                                  # all four open at once
        cfg = dict(self.cfg, max_loss_per_trade_inr=5000, max_order_inr=10 ** 7)
        lifted = {"max_trades_per_day": 0, "max_daily_loss_inr": 0}
        cash = sizes.simulate(sigs, cfg, 100000, overrides=lifted)
        free = sizes.simulate(sigs, cfg, 100000, margin=1000.0, overrides=lifted)
        self.assertLess(len(cash["trades"]), 4)
        self.assertIn("no size fits (cash or caps)", cash["skipped"])
        self.assertEqual(len(free["trades"]), 4)
        self.assertGreater(whatif.peak_open(free["trades"]), 100000)        # needed more than the account had

    def test_all_in_takes_the_whole_principal_and_margin_multiplies_it(self):
        one = sizes.simulate([sig()], self.cfg, 100000, overrides=whatif.all_in_overrides(100000))
        five = sizes.simulate([sig()], self.cfg, 100000, margin=5, overrides=whatif.all_in_overrides(100000, 5))
        self.assertEqual(one["trades"][0]["qty"], 100000 // 1334)          # 74 shares: no buffer, no risk sizing
        self.assertEqual(five["trades"][0]["qty"], 500000 // 1334)         # 374 shares
        self.assertGreater(five["totals"]["net"], 4.5 * one["totals"]["net"])
        self.assertEqual(round(five["totals"]["return_pct"], 2), round(100 * five["totals"]["net"] / 100000, 2))

    def test_all_in_waits_while_the_money_is_in_a_trade(self):
        sigs = [sig(symbol="A", at="10:05", exit_at="14:00"), sig(symbol="B", at="10:06", exit_at="10:30"),
                sig(symbol="C", at="14:05", exit_at="14:30")]
        s = sizes.simulate(sigs, self.cfg, 100000, overrides=whatif.all_in_overrides(100000))
        self.assertEqual([t["symbol"] for t in s["trades"]], ["A", "C"])   # no crumbs of B with the change
        self.assertEqual(s["skipped"], {"no size fits (cash or caps)": 1})

    def test_peak_open(self):
        tr = [{"at": "10:00", "exit_at": "11:00", "fill": 1, "order_value": 50.0},
              {"at": "10:30", "exit_at": "12:00", "fill": 1, "order_value": 70.0},
              {"at": "11:00", "exit_at": "11:30", "fill": 1, "order_value": 40.0},
              {"at": "10:10", "exit_at": None, "fill": None, "order_value": 999.0}]
        self.assertEqual(whatif.peak_open(tr), 120.0)                       # an exit frees money before a new entry

    def test_what_it_trades_comes_from_the_config(self):
        t = whatif.instruments(self.cfg, {"universe": "nifty50", "shorts": "shadow"})
        for words in ("Shares only, in NSE's cash market: series EQ, the Nifty 50 stocks", "intraday (MIS, flat by "
                      "15:10)", "long only", "no futures or options", "no margin", "never traded"):
            self.assertIn(words, t)
        self.assertIn("futures and options allowed", whatif.instruments(dict(self.cfg, allow_fno=True), {}))

    def test_limits_in_numbers(self):
        rep = {"trades": [{"symbol": "A", "qty": 10, "fill": 1000.0}, {"symbol": "B", "qty": 20, "fill": None}],
               "plans": {"B": {"entry": 500.0}},
               "shadows": {"by_why": {"shadow": {"n": 5, "net": 1234.5}}}}
        L = whatif.limits(rep, self.cfg)
        self.assertEqual((L["cap"], L["entries_by_loss_limit"], L["usable_cash"]), (3, 3, 90000.0))
        self.assertEqual((L["entries"], L["unfilled"], L["avg_order"], L["orders_that_fit"]), (2, ["B"], 10000.0, 9))
        self.assertEqual((L["capped"], L["capped_net"]), (5, 1234.5))


class TradeCaps(unittest.TestCase):
    cfg = nc.load_config(CFG_PATH)            # ₹1,00,000: ₹500 a trade, ₹1,500 a day, 3 trades

    def sigs(self, n=10, exit_=1344.3, result="target"):
        """n signals, each closed a minute after it fired, so the cash never binds."""
        return [sig(symbol="S%02d" % i, at="%02d:%02d" % (10 + i // 6, (i % 6) * 10), exit=exit_, result=result,
                    exit_at="%02d:%02d" % (10 + i // 6, (i % 6) * 10 + 1)) for i in range(n)]

    def test_each_cap_gets_a_daily_limit_to_match(self):
        self.assertEqual(whatif.cap_overrides(self.cfg, 100000, 8),
                         {"max_trades_per_day": 8, "max_daily_loss_inr": 4000.0})
        self.assertEqual(whatif.cap_overrides(self.cfg, 1000000, 10)["max_daily_loss_inr"], 50000.0)  # ₹5,000 a trade

    def test_three_five_eight_and_ten(self):
        rows = whatif.trade_caps(self.sigs(), self.cfg, (3, 5, 8, 10), budget=100000)
        self.assertEqual([r["key"] for r in rows], ["cap3", "cap5", "cap8", "cap10"])
        self.assertEqual([r["totals"]["trades"] for r in rows], [3, 5, 8, 10])
        self.assertEqual([r["skipped"] for r in rows], [{"trade cap": 7}, {"trade cap": 5}, {"trade cap": 2}, {}])
        one = rows[0]["totals"]["net"] / 3
        self.assertAlmostEqual(rows[3]["totals"]["net"], 10 * one, delta=1)
        self.assertEqual(rows[1]["daily_loss"], 2500.0)

    def test_losers_hit_the_scaled_daily_limit_never_the_old_one(self):
        rows = whatif.trade_caps(self.sigs(exit_=1328.8, result="stop"), self.cfg, (3, 10), budget=100000)
        self.assertEqual([r["totals"]["trades"] for r in rows], [3, 10])          # ₹5,000 a day lets ten stops in
        self.assertNotIn("daily loss limit", rows[1]["skipped"])

    def test_a_larger_budget_scales_the_max_loss(self):
        rows = whatif.trade_caps(self.sigs(3), self.cfg, (3,), budget=1000000)
        self.assertEqual((rows[0]["budget"], rows[0]["daily_loss"]), (1000000.0, 15000.0))
        self.assertGreater(rows[0]["trades"][0]["qty"], 37)


class InTheDay(HomeBase):
    def setUp(self):
        super(InTheDay, self).setUp()
        self.settings["offline"]["dossier"] = ["--offline", os.path.join(FIX, "nse-dossier"), "--pause", "0"]
        self.settings["dossier"] = {"enabled": True, "results": False}
        self.settings["whatif"] = {"enabled": True, "margin": 5}
        self.settings["trade_caps"] = {"enabled": True, "caps": [3, 5, 8, 10], "budget": 1000000}
        self.write_settings()

    def test_the_deep_scan_feeds_the_veto_and_the_report(self):
        self.replay()
        pre = self.state()["phases"]["premarket"]
        self.assertEqual((pre["dossier"]["code"], pre["dossier"]["stocks"], pre["dossier"]["of"]), (0, 1, 1))
        brief = dict(self.claude.calls)["premarket"]
        self.assertEqual(brief["deep_scan"], "ok")
        self.assertTrue(brief["stock_flags"]["RELIANCE"][0].startswith("In NSE's F&O ban list"))
        self.assertTrue(os.path.isfile(os.path.join(self.d, "autopilot", DAY, "stocks", "RELIANCE.md")))

        rep = self.doc("reports", DAY + ".json")
        self.assertEqual(rep["stocks"]["picked"][0]["symbol"], "RELIANCE")
        self.assertEqual(rep["stocks"]["picked"][0]["why"], "traded")
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("## Stock files (the deep scan before the open)", md)
        self.assertIn("RELIANCE (traded, Reliance Industries Limited): In NSE's F&O ban list", md)
        self.assertIn("Deep scan: 1 of 1 stock files", md)
        stocks = os.path.join(self.d, "reports", DAY + ".stocks.md")
        self.assertIn("# RELIANCE: Reliance Industries Limited", text(stocks))
        self.assertEqual(self.mail.sent[0][1]["stocks"], stocks)            # attached to the email
        msg = mail.build_message(dict(self.settings, mail={"from": "a", "to": ["b"]}), rep, self.mail.sent[0][1])
        self.assertIn(DAY + ".stocks.md", [p.get_filename() for p in msg.iter_attachments()])

    def test_why_only_n_trades_and_the_whole_principal(self):
        self.replay()
        rep = self.doc("reports", DAY + ".json")
        wi = rep["whatif"]
        live = self.doc("state", "paper.json")["trades"][0]
        self.assertEqual(wi["ladder"][0]["totals"]["net"], live["pnl_net"])     # "as run" is the paper account
        self.assertEqual(wi["all_in"][0]["totals"]["net"], live["pnl_net"])
        self.assertEqual(wi["all_in"][1]["trades"][0]["qty"], 100000 // 1334)
        self.assertEqual(wi["all_in"][2]["trades"][0]["qty"], 500000 // 1334)
        self.assertEqual(wi["limits"]["entries_by_loss_limit"], 3)
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("What it trades: Shares only, in NSE's cash market", md)
        self.assertIn("no futures or options", md)
        self.assertIn("## Why only 1 trade", md)
        self.assertIn("Daily loss limit: ₹1,500. Each entry keeps its whole max loss (₹500) reserved", md)
        self.assertIn("## If every trade used the whole ₹1 lakh", md)
        self.assertIn("| Whole ₹1 lakh with 5x intraday margin (₹5 lakh of stock) |", md)
        self.assertIn("At 5x margin a 20% fall in one position loses the whole ₹1 lakh", md)
        card = self.doc("reports", "scorecard.json")["totals"]["whatif"]
        self.assertEqual(list(card), ["ladder0", "ladder1", "ladder2", "ladder3", "all_in_1x", "all_in_margin"])
        self.assertEqual((card["ladder0"]["days"], card["ladder0"]["net"]), (1, live["pnl_net"]))
        self.assertIn("Why so few trades, and the whole principal, all days", md)

    def test_three_five_eight_and_ten_trades_a_day_in_the_report(self):
        self.replay()
        rep = self.doc("reports", DAY + ".json")
        tc = rep["caps"]
        self.assertEqual((tc["budget"], tc["live_cap"]), (1000000.0, 3))
        self.assertEqual([(r["cap"], r["daily_loss"]) for r in tc["rows"]],
                         [(3, 15000.0), (5, 25000.0), (8, 40000.0), (10, 50000.0)])
        ten = sizes.simulate(sizes.from_report(rep), ac.load_cfg(self.d), 1000000)
        self.assertEqual(tc["rows"][0]["totals"]["net"], ten["totals"]["net"])   # 3 a day = the ₹10 lakh sizing
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("## 3, 5, 8 and 10 trades a day at ₹10 lakh", md)
        self.assertIn("The paper account itself trades 3 a day.", md)
        self.assertIn("| 10 | ₹50,000 | 1 |", md)
        self.assertIn("Trades a day, all days", md)
        card = self.doc("reports", "scorecard.json")["totals"]["caps"]
        self.assertEqual(list(card), ["cap3", "cap5", "cap8", "cap10"])
        self.assertEqual(card["cap10"]["days"], 1)

    def test_the_deep_scan_never_runs_into_the_session(self):
        d = self.day("%s 09:09" % DAY)
        out = d.dossier(["RELIANCE"])
        self.assertEqual(out, {"skipped": "too close to the open (it stops at 09:10)"})
        with mock.patch.object(d.runner, "run", side_effect=AssertionError("ran")):
            self.day("%s 09:09" % DAY).dossier(["RELIANCE"])

    def test_the_budget_shrinks_to_fit_before_the_open(self):
        calls = []

        class R(object):
            def run(self, step, script, args, **kw):
                calls.append((step, args, kw))
                return ac.Result(0, "", "", None, 1.0)

            def note(self, *a, **k):
                pass

        d = run_day.Day(self.d, utc("%s 09:00" % DAY), ac.load_settings(self.d), ac.load_cfg(self.d), runner=R(),
                        claude=ClaudeStub(), send_mail=MailStub())
        d.dossier(["RELIANCE"])
        step, args, kw = calls[0]
        self.assertEqual(step, "dossier")
        self.assertEqual(args[args.index("--budget-s") + 1], "600")        # 10 minutes to 09:10, not 900 s
        self.assertEqual(kw["timeout"], 840)
        self.assertIn("--no-results", args)

    def test_off_by_default(self):
        self.settings.pop("dossier")
        self.settings.pop("whatif")
        self.settings.pop("trade_caps")
        self.write_settings()
        self.replay()
        rep = self.doc("reports", DAY + ".json")
        self.assertNotIn("whatif", rep)
        self.assertNotIn("caps", rep)
        self.assertIsNone(rep["stocks"])
        self.assertNotIn("dossier", self.state()["phases"]["premarket"])
        self.assertIn("instruments", rep)                                    # what it trades is always said


if __name__ == "__main__":
    unittest.main()
