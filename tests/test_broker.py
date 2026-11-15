import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("nse-broker", "ticket")
check = load("nse-risk", "check")
nc = load("nse-broker", "nse_common")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-reliance.json")
PLAN_PATH = os.path.join(FIX, "nse-budget", "plan-reliance.json")
SEC_PATH = os.path.join(FIX, "nse-scanner", "sec_list.csv.gz")
BARS_PATH = os.path.join(FIX, "nse-entry", "RELIANCE-2026-11-13-1m.json")
BARS = ticket.read_bars_file(BARS_PATH)
NOW = "2026-11-13 10:05"
TODAY = "2026-11-13"
ENTRY_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "quantity": 56, "product": "MIS", "order_type": "LIMIT", "price": 1334.0, "validity": "DAY",
              "tag": "dtindia"}
STOP_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
             "quantity": 56, "product": "MIS", "order_type": "SL", "price": 1322.1, "trigger_price": 1328.8,
             "validity": "DAY", "tag": "dtindia"}


def rel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def trade(fill_time, fill_price, stop, target, qty=10, flat_by="15:10", stop_limit=None):
    return {"id": "T", "date": TODAY, "symbol": "RELIANCE", "qty": qty, "status": "filled", "limit": fill_price,
            "ordered_at": fill_time, "fill_time": fill_time, "fill_price": fill_price, "target": target,
            "stop": {"type": "SL", "trigger": stop, "price": stop_limit or round(stop * 0.995, 1)},
            "flat_by": flat_by}


def at_ist(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")



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
        meta, body, text = skill_md("nse-broker")
        self.assertEqual(meta["name"], "nse-broker")
        self.assertGreater(len(meta["description"]), 200)
        self.assertIn("argument-hint", meta)
        self.assertGreater(len(body), 60)
        for want in ("${CLAUDE_SKILL_DIR}/ticket.py", "paper-fill", "paper-check", "Zerodha", "Kite", "₹",
                     "Not financial advice", "**yes**"):
            self.assertIn(want, text, want)
        self.assertNotIn("$", text.replace("${CLAUDE_SKILL_DIR}", "").replace("${KITE_API_KEY}", ""))


class ExplicitYes(unittest.TestCase):
    BATCH = ["RELIANCE", "TCS"]

    def yes(self, reply, order=None):
        return ticket.parse_yes(reply, order or rel(), self.BATCH)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 56 RELIANCE",
                  "yes 1334", "yes 1,334.00", "yes max loss 364.82", "yes target 1344.40", "yes ₹291.20"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "haan", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both", "yes TCS", "yes 57",
                  "yes 100 shares", "yes but make it 60", "yes?", "no", "not yes", "yes, wait",
                  "yes if it is under 1330", "yes, stop 1328.80"):
            self.assertFalse(self.yes(r), r)

    def test_a_flatten_yes(self):
        flat = {"symbol": "RELIANCE", "entry": dict(ENTRY_CALL, transaction_type="SELL", order_type="MARKET",
                                                    price=None), "stop": None}
        flat["entry"].pop("price")
        self.assertTrue(self.yes("yes sell 56 RELIANCE", flat))
        self.assertFalse(self.yes("yes sell 50", flat))


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_kite_place_order_call(self):
        self.assertEqual(ticket.tool_input(rel()["entry"]), ENTRY_CALL)
        self.assertEqual(ticket.tool_input(rel()["stop"]), STOP_CALL)
        self.assertEqual(set(ENTRY_CALL) | set(STOP_CALL), set(nc.KITE_FIELDS))
        self.assertEqual(ticket.tool_input(dict(ENTRY_CALL, quantity="56", price="1334")), ENTRY_CALL)

    def test_plain_english(self):
        parts = ticket.plain_parts(rel(), nc.DEFAULT_CONFIG)
        self.assertEqual(parts, [
            "BUY 56 RELIANCE, NSE, MIS (intraday), LIMIT 1,334.00, DAY",
            "then the stop: SELL 56 RELIANCE, MIS (intraday), SL trigger 1,328.80, limit 1,322.10, DAY, placed "
            "once the entry fills",
            "target 1,344.40: an alert, not an order (Kite has no OCO for MIS): when it rings, cancel the stop, "
            "then sell",
            "max loss about ₹364.82 with charges if the stop fills at 1,328.80 (₹291.20 + ₹73.62 charges; it can "
            "fill worse, or not at all, in a gap)"])
        self.assertEqual(ticket.max_loss(rel(), nc.DEFAULT_CONFIG), (364.82, 1328.8, 291.2, 73.62))
        self.assertEqual(ticket.max_loss(rel(), dict(nc.DEFAULT_CONFIG, include_charges=False))[0], 291.2)

    def test_manual_lines_are_what_you_type_into_kite(self):
        lines = ticket.manual_lines(rel())
        self.assertEqual(lines[0], "Search RELIANCE, pick NSE, press Buy. Product: Intraday (MIS). Order type: "
                                   "Limit. Qty: 56. Price: 1334.00. Validity: Day.")
        self.assertIn("Order type: SL. Qty: 56. Trigger price: 1328.80. Price: 1322.10. Validity: Day.", lines[1])
        self.assertTrue(lines[1].startswith("Once the buy has filled"))
        self.assertIn("cancel the SL order in Kite > Orders, then Sell 56 RELIANCE MIS", lines[2])


class KiteMcpConfig(unittest.TestCase):
    def test_example_is_self_hosted_with_keys_from_env_vars(self):
        with open(os.path.join(ROOT, "nse-broker", "kite-mcp.example.json"), encoding="utf-8") as fh:
            server = json.load(fh)["mcpServers"]["kite"]
        self.assertTrue(server["command"].endswith("kite-mcp-server"))
        self.assertEqual(server["env"]["APP_MODE"], "stdio")
        self.assertEqual(server["env"]["KITE_API_KEY"], "${KITE_API_KEY}")
        self.assertEqual(server["env"]["KITE_API_SECRET"], "${KITE_API_SECRET}")
        self.assertNotIn("mcp.kite.trade", json.dumps(server))
        excluded = server["env"]["EXCLUDED_TOOLS"].split(",")
        for needed in ("place_order", "cancel_order", "get_orders", "get_positions"):
            self.assertNotIn(needed, excluded)

    def test_skill_md_covers_the_setup(self):
        with open(os.path.join(ROOT, "nse-broker", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        for want in ("mcp.kite.trade", "zerodha/kite-mcp-server", "Kite Connect", "static IP", "1 April 2026",
                     "Needs approval", "\"ask\"", "no OCO for MIS", "Not financial advice", "yes"):
            self.assertIn(want, text, want)


class PaperWalk(unittest.TestCase):
    """paper-check's bar walk on RELIANCE's real 1-minute bars, 13 Nov 2026."""

    def test_the_target_rings_at_10_13(self):
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 56), BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev, [("target", at_ist("2026-11-13 10:13"), 1344.4, None)])

    def test_the_stop_fills_at_its_trigger(self):
        ev = ticket.walk(trade("2026-11-13 10:08", 1339.8, 1338.5, 1350.0), BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev, [("stop", at_ist("2026-11-13 10:11"), 1338.5, None)])

    def test_a_gap_below_the_stop_fills_at_the_open(self):
        # the 10:17 bar opened at 1,348.70, under a 1,348.80 trigger
        ev = ticket.walk(trade("2026-11-13 10:17", 1349.0, 1348.8, 1360.0), BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev[0][:3], ("stop", at_ist("2026-11-13 10:17"), 1348.7))
        self.assertIn("gapped below the trigger", ev[0][3])

    def test_flat_by_closes_at_that_bars_close(self):
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1300.0, 1400.0), BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev[0][:3], ("flat_by", at_ist("2026-11-13 15:10"), 1340.9))
        self.assertEqual(ticket.walk(trade("2026-11-13 10:05", 1334.0, 1300.0, 1400.0), BARS,
                                     at_ist("2026-11-13 15:00")), [])

    def test_an_empty_flat_by_bar_closes_at_the_last_real_bar(self):
        # Yahoo has no 1-minute data for RELIANCE from 15:16 to 15:27 on 13 Nov
        self.assertEqual([b[0].strftime("%H:%M") for b in BARS if b[0].strftime("%H:%M") >= "15:14"],
                         ["15:14", "15:15", "15:28"])
        t = trade("2026-11-13 10:05", 1334.0, 1300.0, 1400.0, flat_by="15:20")
        ev = ticket.walk(t, BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev, [("flat_by", at_ist("2026-11-13 15:15"), 1340.7,
                               "the 15:20 bar has no data on Yahoo: closed at the 15:15 bar's close")])
        ev = ticket.walk(t, BARS, at_ist("2026-11-13 15:25"))        # the 15:28 bar isn't complete yet
        self.assertEqual(ev[0][:3], ("flat_by", at_ist("2026-11-13 15:15"), 1340.7))
        self.assertEqual(ticket.walk(t, BARS, at_ist("2026-11-13 15:20")), [])
        nulls = {"chart": {"result": [{"timestamp": [1794541500 + 21300 + 60 * i for i in range(3)],   # 15:10..15:12
                                       "indicators": {"quote": [{"open": [1340.1, None, None],
                                                                 "high": [1341.3, None, None],
                                                                 "low": [1340.1, None, None],
                                                                 "close": [1340.9, None, None],
                                                                 "volume": [1, None, None]}]}}]}}
        ev = ticket.walk(trade("2026-11-13 15:10", 1334.0, 1300.0, 1400.0, flat_by="15:11"),
                         ticket.parse_bars(nulls), at_ist("2026-11-13 15:30"))
        self.assertEqual(ev[0][:3], ("flat_by", at_ist("2026-11-13 15:10"), 1340.9))
        self.assertIn("the 15:11 bar has no data on Yahoo", ev[0][3])

    def test_only_bars_complete_by_now(self):
        self.assertEqual(ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4), BARS,
                                     at_ist("2026-11-13 10:13")), [])
        self.assertEqual(len(ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4), BARS,
                                         at_ist("2026-11-13 10:14"))), 1)
        self.assertEqual(ticket.last_price(BARS, at_ist("2026-11-13 10:05")), (at_ist("2026-11-13 10:04"), 1334.0))

    def test_a_resting_limit_fills_or_is_cancelled(self):
        t = trade("2026-11-13 10:20", None, 1325.0, 1350.0)
        t.update(status="open", limit=1336.0, fill_time=None)
        ev = ticket.walk(t, BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev[0][0], "fill")
        self.assertLessEqual(ev[0][2], 1336.0)
        t.update(limit=1330.0)
        ev = ticket.walk(t, BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual(ev[0][0], "cancel")
        self.assertIn("never filled by flat_by 15:10", ev[0][3])

    def test_a_bar_through_both_counts_as_the_stop(self):
        t0 = 1794541500 + 3600   # 10:15 IST
        doc = {"chart": {"result": [{"timestamp": [t0, t0 + 60],
                                     "indicators": {"quote": [{"open": [1334.0, 1334.0], "high": [1335.0, 1346.0],
                                                               "low": [1333.0, 1327.0], "close": [1334.5, 1340.0],
                                                               "volume": [1, 1]}]}}]}}
        ev = ticket.walk(trade("2026-11-13 10:15", 1334.0, 1328.8, 1344.4), ticket.parse_bars(doc),
                         at_ist("2026-11-13 10:30"))
        self.assertEqual(ev[0][:3], ("stop", at_ist("2026-11-13 10:16"), 1328.8))
        self.assertIn("touched the target too", ev[0][3])


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.paper = os.path.join(self.d, "state", "paper.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def set_cfg(self, **over):
        with open(self.cfg, encoding="utf-8") as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def paper_args(self, *extra, **kw):
        return self.args(*extra, **kw) + ["--paper", self.paper, "--portfolio", self.pf, "--bars", BARS_PATH]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--sec-list", SEC_PATH,
                               "--now", now, "--out", self.risk])

    def doc(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_paper_flow_to_the_target(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER (no broker, no money)", text)
        self.assertIn("READY: nse-risk PASS, waiting for your yes", text)
        self.assertIn("paper-fill", text)
        self.assertNotIn("$", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("ticket.py paper-fill --index 1", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)
        appr = self.doc(self.state)["orders"][0]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"], appr["broker"]),
                         ("yes", "entry", 364.82, TODAY, "paper"))
        self.assertEqual(appr["at"], "2026-11-13T10:05:00+05:30")

        code, text, err = run(self.paper_args("paper-fill", "--index", "1"))
        self.assertEqual(code, 0, err)
        self.assertIn("Paper: bought 56 RELIANCE at 1,334.00 (the 10:04 IST 1-minute close on Yahoo)", text)
        pf = self.doc(self.pf)
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"]), (25296.0, 56))
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)

        code, text, _ = run(["paper-check", "--config", self.cfg, "--state", self.state, "--paper", self.paper,
                             "--portfolio", self.pf, "--bars", BARS_PATH, "--now", "2026-11-13 10:10"])
        self.assertEqual(code, 0)
        self.assertIn("still open, 56 at 1,334.00; last 1,341.00 (10:09 IST)", text)
        code, text, _ = run(["paper-check", "--config", self.cfg, "--state", self.state, "--paper", self.paper,
                             "--portfolio", self.pf, "--bars", BARS_PATH, "--now", "2026-11-13 15:30"])
        self.assertIn("TARGET HIT at 1,344.40 (10:13 IST bar). Gross ₹582.40, charges ₹73.87, net ₹508.53.", text)
        self.assertIn("Paper P&L today (2026-11-13): ₹508.53 net of ₹73.87 charges over 1 trade.", text)
        paper = self.doc(self.paper)["trades"][0]
        self.assertEqual((paper["status"], paper["exit_reason"], paper["pnl_net"]), ("closed", "target", 508.53))
        pf = self.doc(self.pf)
        self.assertEqual((pf["cash"], pf["positions"]), (100508.53, []))
        st = self.doc(self.state)
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled", "placed", "filled",
                                                               "closed"])
        self.assertEqual(st["orders"][-1]["pnl_net"], 508.53)
        self.assertEqual(st["orders"][-1]["at"], "2026-11-13T10:13:00+05:30")
        code, text, _ = run(["log", "--state", self.state, "--now", "2026-11-13 15:30"])
        self.assertIn("1 placed today. Closed: 1, net ₹508.53 after charges.", text)
        # nse-risk now counts it: a second RELIANCE buy today is a duplicate
        res = check.check([rel()], nc.DEFAULT_CONFIG, state=st, now=nc.parse_now("2026-11-13 10:30"),
                          sec=check.read_sec_list(SEC_PATH))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 364.82))
        # paper-check again: nothing open
        code, text, _ = run(["paper-check", "--config", self.cfg, "--state", self.state, "--paper", self.paper,
                             "--portfolio", self.pf, "--bars", BARS_PATH, "--now", "2026-11-13 15:31"])
        self.assertNotIn("TARGET HIT", text)

    def test_a_limit_that_is_not_marketable_rests(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, _ = run(self.paper_args("paper-fill", "--index", "1", "--price", "1336.5"))
        self.assertEqual(code, 0)
        self.assertIn("isn't marketable (the latest price is 1,336.50", text)
        self.assertFalse(os.path.exists(self.pf))
        self.assertEqual(self.doc(self.paper)["trades"][0]["status"], "open")

    def test_paper_flatten(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.paper_args("paper-fill", "--index", "1"))
        late = "2026-11-13 15:05"
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("Flatten RELIANCE: SELL 56 RELIANCE, NSE, MIS (intraday), MARKET, DAY (flat by 15:10 IST)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancels the paper stop", text)
        doc = self.doc(self.orders)
        self.assertEqual(ticket.tool_input(doc["orders"][0]["entry"]),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                          "transaction_type": "SELL", "quantity": 56, "product": "MIS", "order_type": "MARKET",
                          "validity": "DAY", "tag": "dtindia"})
        self.assertIsNone(doc["orders"][0]["stop"])
        self.assertEqual(self.run_risk(now=late), 0)       # after the 14:30 entry cutoff: an exit still passes
        self.assertEqual(self.doc(self.risk)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("first cancel the open stop", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 56 RELIANCE", now=late))[0], 0)
        code, text, _ = run(self.paper_args("paper-fill", "--index", "1", now=late))
        self.assertEqual(code, 0)
        self.assertIn("Paper: cancelled the stop and sold 56 RELIANCE at", text)
        self.assertEqual(self.doc(self.pf)["positions"], [])
        self.assertEqual(self.doc(self.paper)["trades"][0]["exit_reason"], "sold")
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_manual_mode_prints_what_to_type(self):
        self.set_cfg(broker="manual")
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("MANUAL: you type it into Kite, REAL MONEY", text)
        self.assertIn("Type this into Kite (web or app), exactly:", text)
        self.assertIn("Product: Intraday (MIS). Order type: Limit. Qty: 56. Price: 1334.00.", text)
        self.assertIn("Order type: SL. Qty: 56. Trigger price: 1328.80. Price: 1322.10.", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertIn("Now type it into Kite, exactly:", text)
        code, _, err = run(self.paper_args("paper-fill", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("paper-fill is for \"broker\": \"paper\"", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "251113000101",
                                      "--filled-qty", "56", "--filled-price", "1334", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("Now the stop leg in Kite: Once the buy has filled", text)
        self.assertEqual(self.doc(self.pf)["cash"], 25296.0)

    def test_kite_mode_entry_then_stop_then_flatten(self):
        self.set_cfg(broker="kite")
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("KITE MCP, LIVE ACCOUNT, REAL MONEY", text)
        self.assertIn("Claude will call place_order (your self-hosted Kite MCP) with exactly:", text)
        self.assertIn("Once the entry fills (get_orders shows COMPLETE), Claude calls place_order for the stop:", text)
        self.assertIn("Allow once", text)
        self.assertNotIn('"why"', text)
        self.assertNotIn('"plan"', text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        first = text[text.index("{"):]
        self.assertEqual(json.loads(first[:first.index("}") + 1]), ENTRY_CALL)
        code, _, err = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("the stop leg follows the entry", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1",
                                      "--filled-qty", "56", "--filled-price", "1334.0", "--portfolio", self.pf,
                                      now="2026-11-13 10:06"))
        self.assertEqual(code, 0)
        self.assertIn('"order_type": "SL"', text)
        code, _, _ = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed", "--broker-id",
                                   "S1", now="2026-11-13 10:07"))
        self.assertEqual(code, 0)
        st = self.doc(self.state)
        self.assertEqual([(r["status"], r.get("leg")) for r in st["orders"]],
                         [("approved", None), ("placed", "entry"), ("filled", None), ("placed", "stop")])
        self.assertEqual(st["orders"][1]["max_loss"], 364.82)
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state,
                             "--now", "2026-11-13 15:10"])
        self.assertIn('cancel_order {"variety": "regular", "order_id": "S1"}', text)
        self.assertIn("market protection", text)
        self.assertEqual(self.doc(self.orders)["orders"][0]["cancel_first"]["order_id"], "S1")
        self.assertEqual(self.run_risk(now="2026-11-13 15:10"), 0)
        code, text, _ = run(self.args("show", now="2026-11-13 15:10"))
        self.assertIn("First Claude calls cancel_order for the open stop:", text)
        self.assertIn('{"variety": "regular", "order_id": "S1"}', text)
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--qty", "56", "--limit", "1339.97", "--orders",
                             self.orders, "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 15:10"])
        self.assertEqual(self.doc(self.orders)["orders"][0]["entry"]["price"], 1339.9)
        self.assertNotIn("market protection", text)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        doc = self.doc(self.orders)
        doc["orders"][0]["entry"]["quantity"] = 57
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("this exact order was not in the last risk check", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)

    def test_a_changed_target_is_not_ready(self):
        self.run_risk()
        doc = self.doc(self.orders)
        doc["orders"][0]["target"]["price"] = 1336.0          # same legs, worse reward:risk: the fingerprint covers the target
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: this exact order was not in the last risk check", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)

    def test_bars_for_another_symbol_are_refused(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        doc = self.doc(self.orders)
        doc["orders"][0]["symbol"] = "TCS"
        bars = os.path.join(self.d, "tcs.json")
        chart = ticket.read_chart_file(BARS_PATH)
        chart["chart"]["result"][0]["meta"]["symbol"] = "TCS.NS"
        with open(bars, "w") as fh:
            json.dump(chart, fh)
        a = type("A", (), {"bars": bars})()
        with self.assertRaises(ticket.TicketError) as e:
            ticket.bars_for("RELIANCE", a, {})
        self.assertIn("are for TCS, not RELIANCE", str(e.exception))

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-11-12 10:05")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("the risk check is from 2026-11-12, not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-11-13 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 09:20"))
        self.assertIn("BLOCKED by nse-risk: window: no new entries before 09:30 IST", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-11-13 09:20"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, _, err = run(self.paper_args("paper-fill", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("paper-fill only follows an approval", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_fill_sell_relieves_the_oldest_lot_and_logs_the_pnl(self):
        pf = {"cash": 0, "positions": [{"ticker": "RELIANCE", "shares": 56, "lots": [
            {"id": "RELIANCE-1", "date": "2026-11-13", "shares": 56, "cost": 74704.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "56", "--price", "1344.40",
                             "--reason", "target", "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("P&L ₹508.53 net of ₹73.87 charges", text)
        self.assertEqual(self.doc(self.pf), {"cash": 75286.4, "positions": []})
        closed = self.doc(self.state)["orders"][-1]
        self.assertEqual((closed["status"], closed["reason"], closed["pnl_net"]), ("closed", "target", 508.53))
        code, _, err = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "5", "--price", "1344",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["fill", "--symbol", "RELIANCE", "--side", "buy", "--qty", "5.5", "--price", "1344",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertIn("whole shares", err)


if __name__ == "__main__":
    unittest.main()
