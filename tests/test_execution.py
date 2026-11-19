"""1.11.0: the paper broker's entry expiry (entry_ttl_minutes) and fill rule (paper_fill), a cancelled entry freeing
its slot, waiting entries holding their cash, the simulator agreeing, and Claude's vetoes by category."""

import json
import os
import unittest

from _load import load
from test_autopilot import HomeBase, ClaudeStub, CFG_PATH, DAY, text, sig, ac, nc, run_day, sizes
import test_broker as tb

ticket = tb.ticket
size = load("nse-budget", "size")
accounts = __import__("accounts")


def open_order(ordered, limit):
    t = tb.trade(ordered, limit, 1330.0, 1360.0)
    t.update(status="open", fill_time=None, fill_price=None)
    return t


class EntryExpiryAndFillRule(unittest.TestCase):
    """A LIMIT buy at 1,342.00 from 10:15 on RELIANCE's real 13 Nov bars: the 10:21 bar touches 1,342.00 (low
    1,342.00) and the 10:38 bar is the first to trade below it (low 1,341.60)."""
    end = tb.at_ist("2026-11-13 15:30")

    def first(self, cfg):
        return ticket.walk(open_order("2026-11-13 10:15", 1342.0), tb.BARS, self.end, cfg)[0]

    def test_a_touch_fills_by_default(self):
        self.assertEqual(self.first({})[:3], ("fill", tb.at_ist("2026-11-13 10:21"), 1342.0))

    def test_through_needs_a_trade_below_the_limit(self):
        self.assertEqual(self.first({"paper_fill": "through"})[:3], ("fill", tb.at_ist("2026-11-13 10:38"), 1342.0))

    def test_an_order_that_has_not_filled_in_time_is_cancelled(self):
        ev = self.first({"entry_ttl_minutes": 5})
        self.assertEqual(ev[:2], ("cancel", tb.at_ist("2026-11-13 10:20")))
        self.assertIn("did not fill within 5 minutes", ev[3])
        self.assertEqual(self.first({"entry_ttl_minutes": 15})[0], "fill")          # 10:21 is inside 10:15-10:30
        self.assertEqual(self.first({"entry_ttl_minutes": 15, "paper_fill": "through"})[0], "cancel")

    def test_expiry_with_no_bar_yet(self):
        bars = [b for b in tb.BARS if b[0] < tb.at_ist("2026-11-13 10:18")]       # Yahoo has nothing after 10:17
        ev = ticket.walk(open_order("2026-11-13 10:15", 1342.0), bars, tb.at_ist("2026-11-13 10:25"),
                         {"entry_ttl_minutes": 5})
        self.assertEqual(ev[0][:2], ("cancel", tb.at_ist("2026-11-13 10:20")))
        self.assertEqual(ticket.walk(open_order("2026-11-13 10:15", 1342.0), bars, tb.at_ist("2026-11-13 10:20"),
                                     {"entry_ttl_minutes": 5}), [])           # not yet: wait for the 10:20 bar


class WaitingCashAndFreedSlots(unittest.TestCase):
    cfg = nc.load_config(CFG_PATH)            # ₹1,00,000: ₹500 a trade, ₹1,500 a day, 3 trades

    def test_budget_holds_back_the_cash_of_waiting_entries(self):
        d = __import__("tempfile").mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        p = os.path.join(d, "paper.json")
        with open(p, "w") as fh:
            json.dump({"trades": [{"status": "open", "date": DAY, "qty": 30, "limit": 1000.0},
                                  {"status": "cancelled", "date": DAY, "qty": 99, "limit": 1000.0}]}, fh)
        self.assertEqual(size.pending_entries(p, DAY), 30000.0)
        self.assertEqual(size.pending_entries(os.path.join(d, "none.json"), DAY), 0.0)

    def test_the_simulator_frees_an_expired_orders_slot(self):
        cfg = dict(self.cfg, max_trades_per_day=1)
        sigs = [sig(symbol="A", at="10:05", fill=None, exit=None, exit_at=None, result="never filled"),
                sig(symbol="B", at="10:30", exit_at="10:40")]
        self.assertEqual([t["symbol"] for t in sizes.simulate(sigs, cfg, 100000)["trades"]], ["A"])
        out = sizes.simulate(sigs, dict(cfg, entry_ttl_minutes=15), 100000)
        self.assertEqual([t["symbol"] for t in out["trades"]], ["A", "B"])
        self.assertEqual(out["totals"]["trades"], 1)

    def test_the_simulator_holds_cash_for_a_waiting_order(self):
        cfg = dict(self.cfg, max_loss_per_trade_inr=5000, max_order_inr=10 ** 7, max_trades_per_day=10)
        sigs = [sig(symbol="A", at="10:05", fill=None, exit=None, exit_at=None, result="never filled"),
                sig(symbol="B", at="10:06", exit_at="10:40")]
        out = sizes.simulate(sigs, cfg, 100000)
        a, b = out["trades"]
        self.assertLessEqual(a["order_value"] + b["order_value"], 90000 + 1)   # B sized on what A left

    def test_the_autopilot_frees_the_slot_too(self):
        d = __import__("tempfile").mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        os.makedirs(os.path.join(d, "state"))
        rows = [{"date": DAY, "status": "approved", "kind": "entry", "fingerprint": f} for f in ("a", "b", "c")]
        rows.append({"date": DAY, "status": "cancelled", "side": "buy", "fingerprint": "b"})
        with open(os.path.join(d, "state", "broker.json"), "w") as fh:
            json.dump({"orders": rows}, fh)
        self.assertIsNone(accounts.cap_reason(d, {"max_trades_per_day": 3}, DAY))
        self.assertIsNotNone(accounts.cap_reason(d, {"max_trades_per_day": 2}, DAY))


class VetoCategories(HomeBase):
    def test_a_veto_is_replayed_under_its_category(self):
        self.claude = ClaudeStub(vetoes=[{"symbol": "RELIANCE", "reason": "results today", "category": "RESULTS"}])
        self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual((c["outcome"], c["veto_category"]), ("vetoed", "RESULTS"))
        rep = self.doc("reports", DAY + ".json")
        self.assertEqual(rep["shadows"]["rows"][0]["veto_category"], "RESULTS")
        card = self.doc("reports", "scorecard.json")["totals"]["vetoes"]
        self.assertEqual((card["RESULTS"]["n"], card["RESULTS"]["net"]), (1, 328.4))
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("RELIANCE (RESULTS: results today)", md)
        self.assertIn("Claude's vetoes by category, all days", md)
        self.assertIn("| RESULTS | 1 | 1 | 1 | 1 | +₹328.40 |", md)


if __name__ == "__main__":
    unittest.main()
