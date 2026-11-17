import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

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


class TestMode(unittest.TestCase):
    """--now and --today only work with NSE_TEST_MODE=1: every test class that pins the clock sets it."""

    def setUp(self):
        p = mock.patch.dict(os.environ, {"NSE_TEST_MODE": "1"})
        p.start()
        self.addCleanup(p.stop)
        super().setUp()



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
        self.assertLess(len(meta["description"]), 600)
        for phrase in ("place the order", "Zerodha", "flatten", "paper trade", "connect Kite"):
            self.assertIn(phrase, meta["description"], phrase)
        self.assertIn("argument-hint", meta)
        self.assertGreater(len(body), 60)
        for want in ("${CLAUDE_SKILL_DIR}/ticket.py", "paper-fill", "paper-check", "Zerodha", "Kite", "₹",
                     "Not financial advice", "**yes**"):
            self.assertIn(want, text, want)
        self.assertNotIn("$", text.replace("${CLAUDE_SKILL_DIR}", "").replace("${KITE_API_KEY}", ""))

    def test_skill_md_says_truthfully_what_is_enforced_where(self):
        _, _, text = skill_md("nse-broker")
        flat = " ".join(text.split())
        for want in ("What is enforced where", "only exists when this pack is installed as a plugin",
                     "Hooks do not ship with copied skill folders", "the approval gate is not installed",
                     "The hook cannot tell a `/loop`", "your own hands are the gate",
                     "NSE_TEST_MODE=1", "within the last few minutes", "Call `get_positions` first",
                     "--positions positions.json", "reconcile", "STOP TRIGGERED BUT NOT FILLED",
                     "next bar's open", "move-stop", "journal", "status", "PARTIAL FILL"):
            self.assertIn(want, flat, want)



class ExplicitYes(unittest.TestCase):
    BATCH = ["RELIANCE", "TCS"]

    def yes(self, reply, order=None, batch=None):
        return ticket.parse_yes(reply, order or rel(), self.BATCH if batch is None else batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 56 RELIANCE",
                  "yes please", "yes, go ahead", "yes go ahead!", "yes buy it", "yes reliance", "yes 56 shares",
                  "yes, place the order", "yes confirm"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "y", "sure", "ok", "okay", "haan", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both", "yes TCS", "yes 57",
                  "yes 100 shares", "yes but make it 60", "yes?", "no", "not yes", "yes, wait",
                  "yes if it is under 1330", "yes, stop 1328.80",
                  # numbers other than this order's share count are not a yes (the old parser took any amount
                  # that appeared in the ticket: a price is not a quantity)
                  "yes 1334", "yes 1,334.00", "yes max loss 364.82", "yes target 1344.40", "yes ₹291.20"):
            self.assertFalse(self.yes(r), r)

    def test_a_yes_for_another_stock_is_not_a_yes_for_this_ticket(self):
        # the review's bugs: orders.json held ONE order, so the old "names another stock" check saw nothing
        for r in ("yes for TCS", "yes buy INFY", "yes TCS", "yes buy HDFCBANK", "yes sell INFY", "yes 56 INFY",
                  "yes RELIANCE and TCS", "yes buy 100 TCS", "yes BAJAJ-AUTO", "yes M&M"):
            self.assertFalse(self.yes(r, batch=["RELIANCE"]), r)
            self.assertFalse(self.yes(r, batch=[]), r)
        ok, why = ticket.parse_yes("yes for TCS", rel(), ["RELIANCE"])
        self.assertIn("TCS", why)
        self.assertIn("RELIANCE", why)

    def test_a_bare_y_ok_sure_do_not_count(self):
        for r in ("y", "Y", "ok", "OK", "sure", "k", "yup", "ya"):
            self.assertFalse(self.yes(r), r)

    def test_the_verb_must_match_the_ticket(self):
        self.assertTrue(self.yes("yes buy 56 RELIANCE"))
        self.assertFalse(self.yes("yes sell 56 RELIANCE"))

    def test_a_flatten_yes(self):
        flat = {"symbol": "RELIANCE", "entry": dict(ENTRY_CALL, transaction_type="SELL", order_type="MARKET",
                                                    price=None), "stop": None}
        flat["entry"].pop("price")
        self.assertTrue(self.yes("yes sell 56 RELIANCE", flat))
        self.assertTrue(self.yes("yes sell it", flat))
        self.assertFalse(self.yes("yes buy 56 RELIANCE", flat))
        self.assertFalse(self.yes("yes sell 50", flat))
        self.assertFalse(self.yes("yes sell 56 TCS", flat))


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
            "max loss about ₹739.91 with charges if the stop fills at its SL limit 1,322.10 (trigger 1,328.80) "
            "(₹666.40 + ₹73.51 charges; it can fill worse, or not at all, in a gap)"])
        # the worst fill is the SL LIMIT, not the trigger: 56 x 11.90 = 666.40 (it was 56 x 5.20 = 291.20)
        self.assertEqual(ticket.max_loss(rel(), nc.DEFAULT_CONFIG), (739.91, 1328.8, 666.4, 73.51))
        self.assertEqual(ticket.max_loss(rel(), dict(nc.DEFAULT_CONFIG, include_charges=False))[0], 666.4)
        self.assertEqual(ticket.max_loss(rel(), dict(nc.DEFAULT_CONFIG, loss_basis="trigger")),
                         (364.82, 1328.8, 291.2, 73.62))

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
        for needed in ("place_order", "cancel_order", "modify_order", "get_orders", "get_positions"):
            self.assertNotIn(needed, excluded)           # modify_order: move-stop and a partial exit use it
        self.assertIn("place_gtt_order", excluded)

    def test_skill_md_covers_the_setup(self):
        with open(os.path.join(ROOT, "nse-broker", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        for want in ("mcp.kite.trade", "zerodha/kite-mcp-server", "Kite Connect", "static IP", "1 April 2026",
                     "Needs approval", "\"ask\"", "no OCO for MIS", "Not financial advice", "yes"):
            self.assertIn(want, text, want)


class PaperWalk(unittest.TestCase):
    """paper-check's bar walk on RELIANCE's real 1-minute bars, 13 Nov 2026."""

    def test_the_target_rings_at_10_13_and_fills_at_the_next_open(self):
        # the 10:13 bar's high (1,346.00) reaches the 1,344.40 target. On Kite the target is only an alert: you
        # cancel the stop and sell at market, so the fill is the 10:14 bar's open (1,344.30), NOT the target
        # (this test used to expect ("target", 10:13, 1344.4, None): a fill at the target on the touching bar)
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 56), BARS, at_ist("2026-11-13 15:30"))
        self.assertEqual([e[:3] for e in ev], [("target", at_ist("2026-11-13 10:14"), 1344.3)])
        self.assertIn("filled at the next bar's open", ev[0][3])

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


def bar_doc(rows, symbol="RELIANCE.NS"):
    """A Yahoo v8 1-minute chart doc from [("10:15", open, high, low, close), ...] (IST, 13 Nov 2026)."""
    ts, cols = [], {"open": [], "high": [], "low": [], "close": [], "volume": []}
    for hhmm, o, h, l, c in rows:
        hh, mm = hhmm.split(":")
        ts.append(1794541500 + 60 * (int(hh) * 60 + int(mm) - (9 * 60 + 15)))
        for k, v in zip(("open", "high", "low", "close"), (o, h, l, c)):
            cols[k].append(v)
        cols["volume"].append(1)
    return {"chart": {"result": [{"meta": {"symbol": symbol}, "timestamp": ts, "indicators": {"quote": [cols]}}]}}


def quiet(o, h=None, l=None, c=None):
    """a bar that sits at one price unless told otherwise"""
    return (o, h if h is not None else o, l if l is not None else o, c if c is not None else o)


class FlowBase(TestMode):
    """The temp trader folder and the helpers every flow test uses."""

    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.paper = os.path.join(self.d, "state", "paper.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.prompt = os.path.join(self.d, "state", "last_prompt.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)
        super().tearDown()

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

    def check_args(self, now, bars=BARS_PATH):
        return ["paper-check", "--config", self.cfg, "--state", self.state, "--paper", self.paper,
                "--portfolio", self.pf, "--bars", bars, "--now", now]

    def run_risk(self, now=NOW):
        """A hand-written nse-risk result for whatever is in orders.json (PASS for every order), so these tests do
        not move when the risk rules do. The real nse-risk is run once, in RealRiskCheck. Returns 0 like check.main."""
        self.write_risk(now)
        return 0

    def write_risk(self, now=NOW, verdict="PASS", checked_at=None, date=None):
        doc = self.doc(self.orders)
        when = nc.parse_now(now)
        results = []
        for o in doc["orders"]:
            e = o["entry"]
            results.append({"fingerprint": nc.order_fingerprint(o), "symbol": o.get("symbol"), "verdict": verdict,
                            "reasons": [] if verdict == "PASS" else ["window: no new entries before 09:30 IST"],
                            "kind": "entry" if e["transaction_type"] == "BUY" else "exit",
                            "max_loss": ticket.max_loss(o, nc.DEFAULT_CONFIG)[0], "target": ticket.target_of(o),
                            "checked_at": checked_at or nc.utc_iso(when)})
        risk = {"date": date or nc.to_ist(when).date().isoformat(), "checked_at": nc.utc_iso(when),
                "results": results}
        os.makedirs(os.path.dirname(self.risk), exist_ok=True)
        with open(self.risk, "w", encoding="utf-8") as fh:
            json.dump(risk, fh)
        return risk

    def say(self, text, at=NOW):
        """The user's message, as the UserPromptSubmit hook would have saved it."""
        ticket.record_user_prompt(text, self.prompt, nc.parse_now(at))

    def doc(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def edit_orders(self, fn):
        doc = self.doc(self.orders)
        fn(doc)
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def positions(self, **qty):
        path = os.path.join(self.d, "positions.json")
        rows = [{"tradingsymbol": k, "exchange": "NSE", "product": "MIS", "quantity": v, "average_price": 1334.0}
                for k, v in qty.items()]
        with open(path, "w") as fh:
            json.dump({"net": rows, "day": rows}, fh)
        return path

    def approve_paper(self, now=NOW, reply="yes"):
        self.write_risk(now)
        code, text, err = run(self.args("approve", "--index", "1", "--reply", reply, now=now))
        self.assertEqual(code, 0, text + err)

    def go_kite(self, now=NOW, reply="yes"):
        """kite mode with the hooks 'installed': the user's message on record, risk PASS, approved."""
        self.set_cfg(broker="kite")
        self.write_risk(now)
        self.say(reply, now)
        code, text, err = run(self.args("approve", "--index", "1", "--reply", reply, now=now))
        self.assertEqual(code, 0, text + err)
        return text

    def gate(self, tool, call, now="2026-11-13 10:06"):
        return ticket.gate_check(tool, call, self.state, self.cfg, nc.parse_now(now))



class Flow(FlowBase):
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
        self.assertIn("order: plan date 2026-11-13, written under a minute ago (10:05 IST)", text)
        self.assertIn("paper-fill", text)
        self.assertNotIn("$", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("ticket.py paper-fill --index 1", text)
        self.assertIn("reply_verified=false", text)        # paper mode, no hook on record: accepted, but marked
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)
        appr = self.doc(self.state)["orders"][0]
        # the max loss is now measured at the SL limit, the worst fill: 56 x 11.90 + 73.51 charges (was 364.82)
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"], appr["broker"]),
                         ("yes", "entry", 739.91, TODAY, "paper"))
        self.assertIs(appr["reply_verified"], False)
        self.assertNotIn("legs", appr)                     # no gate legs in paper mode
        self.assertEqual(appr["at"], "2026-11-13T10:05:00+05:30")

        code, text, err = run(self.paper_args("paper-fill", "--index", "1"))
        self.assertEqual(code, 0, err)
        self.assertIn("Paper: bought 56 RELIANCE at 1,334.00 (the 10:04 IST 1-minute close on Yahoo)", text)
        pf = self.doc(self.pf)
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"]), (25296.0, 56))
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)

        code, text, _ = run(self.check_args("2026-11-13 10:10"))
        self.assertEqual(code, 0)
        self.assertIn("still open, 56 at 1,334.00; last 1,341.00 (10:09 IST)", text)
        code, text, _ = run(self.check_args("2026-11-13 15:30"))
        # the 10:13 bar reaches 1,344.40; the exit is the 10:14 bar's open, 1,344.30 (an alert, then a market sell)
        self.assertIn("TARGET HIT at 1,344.30 (10:14 IST bar). Gross ₹576.80, charges ₹73.87, net ₹502.93.", text)
        self.assertIn("Paper P&L today (2026-11-13): ₹502.93 net of ₹73.87 charges over 1 trade.", text)
        paper = self.doc(self.paper)["trades"][0]
        self.assertEqual((paper["status"], paper["exit_reason"], paper["pnl_net"]), ("closed", "target", 502.93))
        pf = self.doc(self.pf)
        self.assertEqual((pf["cash"], pf["positions"]), (100502.93, []))
        st = self.doc(self.state)
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled", "placed", "filled",
                                                               "closed"])
        self.assertEqual(st["orders"][-1]["pnl_net"], 502.93)
        self.assertEqual(st["orders"][-1]["at"], "2026-11-13T10:14:00+05:30")
        code, text, _ = run(["log", "--state", self.state, "--now", "2026-11-13 15:30"])
        self.assertIn("1 placed today. Closed: 1, net ₹502.93 after charges.", text)
        # nse-risk now counts it: a second RELIANCE buy today is a duplicate
        res = check.check([rel()], nc.DEFAULT_CONFIG, state=st, now=nc.parse_now("2026-11-13 10:30"),
                          sec=check.read_sec_list(SEC_PATH))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        # paper-check again: nothing open
        code, text, _ = run(self.check_args("2026-11-13 15:31"))
        self.assertNotIn("TARGET HIT", text)

    def test_a_limit_that_is_not_marketable_rests(self):
        self.approve_paper()
        code, text, _ = run(self.paper_args("paper-fill", "--index", "1", "--price", "1336.5"))
        self.assertEqual(code, 0)
        self.assertIn("isn't marketable (the latest price is 1,336.50", text)
        self.assertFalse(os.path.exists(self.pf))
        self.assertEqual(self.doc(self.paper)["trades"][0]["status"], "open")

    def test_paper_flatten(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))
        late = "2026-11-13 15:05"
        flat = ["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", late]
        code, text, _ = run(flat)
        self.assertEqual(code, 0)
        self.assertIn("Flatten RELIANCE: SELL 56 RELIANCE, NSE, MIS (intraday), MARKET, DAY (flat by 15:10 IST)", text)
        self.assertIn("it replaced this symbol's earlier proposal", text)
        self.assertIn("cancels the paper stop", text)
        doc = self.doc(self.orders)
        self.assertEqual(ticket.tool_input(doc["orders"][0]["entry"]),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                          "transaction_type": "SELL", "quantity": 56, "product": "MIS", "order_type": "MARKET",
                          "validity": "DAY", "tag": "dtindia"})
        self.assertIsNone(doc["orders"][0]["stop"])
        self.assertEqual(doc["orders"][0]["created_at"], "2026-11-13T09:35:00Z")
        self.write_risk(now=late)                  # after the 14:30 entry cutoff: an exit still shows READY
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
        code, _, err = run(flat)
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
        self.write_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("KITE MCP, LIVE ACCOUNT, REAL MONEY", text)
        self.assertIn("Claude will call place_order (your self-hosted Kite MCP) with exactly:", text)
        self.assertIn("Once the entry fills (get_orders shows COMPLETE), Claude calls place_order for the stop:", text)
        self.assertIn("Allow once", text)
        self.assertNotIn('"why"', text)
        self.assertNotIn('"plan"', text)
        self.say("yes")
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        first = text[text.index("{"):]
        self.assertEqual(json.loads(first[:first.index("}") + 1]), ENTRY_CALL)
        self.assertIn("The order gate lets exactly this call through, once", text)
        appr = self.doc(self.state)["orders"][0]
        self.assertIs(appr["reply_verified"], True)
        self.assertEqual([(l["role"], l["tool"], l["status"]) for l in appr["legs"]],
                         [("entry", "place_order", "approved")])
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
        self.assertEqual(st["orders"][1]["max_loss"], 739.91)
        # flatten outside paper mode needs the broker's number
        flat = ["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 15:10"]
        code, _, err = run(flat)
        self.assertEqual(code, 1)
        self.assertIn("get_positions", err)
        code, text, _ = run(flat + ["--positions", self.positions(RELIANCE=56)])
        self.assertIn('cancel_order {"variety": "regular", "order_id": "S1"}', text)
        self.assertIn("market protection", text)
        self.assertEqual(self.doc(self.orders)["orders"][0]["cancel_first"]["order_id"], "S1")
        self.write_risk(now="2026-11-13 15:10")
        code, text, _ = run(self.args("show", now="2026-11-13 15:10"))
        self.assertIn("First Claude calls cancel_order for the open stop:", text)
        self.assertIn('{"variety": "regular", "order_id": "S1"}', text)
        code, text, _ = run(flat + ["--qty", "56", "--limit", "1339.97"])
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
        self.write_risk(now="2026-11-12 10:05")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("the risk check is from 2026-11-12, not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.write_risk(now="2026-11-13 09:20", verdict="BLOCK")
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

    def test_a_missing_config_is_an_error_not_a_default(self):
        os.remove(self.cfg)
        code, _, err = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("no config.json at", err)
        self.assertIn("will not guess", err)


class ClockGuards(FlowBase):
    """Item 1/2/5c: a PASS expires, the entry window is looked at again, yesterday's orders are not today's."""

    def test_the_test_switches_are_refused_outside_test_mode(self):
        with mock.patch.dict(os.environ, {"NSE_TEST_MODE": ""}):
            self.write_risk()
            for argv in (self.args("show"), ["log", "--state", self.state, "--today", "2026-11-13"],
                         self.check_args("2026-11-13 10:10")):
                code, _, err = run(argv)
                self.assertEqual(code, 1)
                self.assertIn("NSE_TEST_MODE=1", err)
        # with the switch on, --today is honoured (that is the whole point of the switch)
        code, text, _ = run(self.args("show", "--today", "2026-11-13"))
        self.assertEqual(code, 0)
        with mock.patch.dict(os.environ, {"NSE_TEST_MODE": "0"}):
            code, _, err = run(self.args("show", "--today", "2026-11-13"))
            self.assertEqual(code, 1)
            self.assertIn("test switches", err)

    def test_a_pass_expires_after_risk_pass_minutes(self):
        self.write_risk("2026-11-13 10:05")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:09"))   # 4 minutes
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:11"))   # 6 minutes
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: the nse-risk PASS is 6 min old (10:05 IST), over the 5-minute limit", text)
        self.assertIn("Run nse-risk again", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-11-13 10:11"))
        self.assertEqual(code, 1)
        self.assertIn("over the 5-minute limit", err)
        self.assertFalse(os.path.exists(self.state))
        # the limit is the config's
        self.set_cfg(risk_pass_minutes=10)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:11"))
        self.assertEqual(code, 0)

    def test_the_result_checked_at_wins_over_the_files(self):
        risk = self.write_risk("2026-11-13 10:05")
        risk["checked_at"] = "2026-11-13T04:35:00Z"
        del risk["results"][0]["checked_at"]
        with open(self.risk, "w") as fh:
            json.dump(risk, fh)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:12"))     # falls back to the file's
        self.assertIn("over the 5-minute limit", text)
        risk["results"][0]["checked_at"] = "2026-11-13T04:40:00Z"                       # 10:10 IST: its own time wins
        with open(self.risk, "w") as fh:
            json.dump(risk, fh)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:12"))
        self.assertIn("READY", text)
        self.assertEqual(code, 0)

    def test_a_pass_with_no_time_or_a_future_time_is_not_a_pass(self):
        risk = self.write_risk("2026-11-13 10:05")
        del risk["results"][0]["checked_at"]
        del risk["checked_at"]
        with open(self.risk, "w") as fh:
            json.dump(risk, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("has no time on it", text)
        # check.py --now can stamp a check in the future: that must not dodge the expiry
        self.write_risk("2026-11-13 11:30", checked_at="2026-11-13T06:00:00Z")      # 11:30 IST, clock says 10:05
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("in the future (11:30 IST)", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("in the future", err)
        # a PASS 30 seconds ahead of the clock is clock skew, not a trick
        self.write_risk("2026-11-13 10:05", checked_at="2026-11-13T04:35:30Z")
        self.assertEqual(run(self.args("show", "--index", "1"))[0], 0)

    def test_the_pass_must_be_from_today(self):
        # a PASS stamped yesterday night but filed under today's date
        self.write_risk("2026-11-13 10:05", checked_at="2026-11-12T10:30:00Z")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:05"))
        self.assertEqual(code, 1)
        self.assertIn("not today", text)

    def test_the_entry_window_is_checked_again_at_show_and_approve(self):
        for now, why in (("2026-11-13 09:20", "no new entries before 09:30 IST (it is 09:20)"),
                         ("2026-11-13 14:40", "no new entries after 14:30 IST (it is 14:40)"),
                         ("2026-11-13 15:20", "flat_by 15:10 IST has come (it is 15:20)")):
            self.write_risk(now)           # the PASS itself is fresh: only the clock is wrong
            code, text, _ = run(self.args("show", "--index", "1", now=now))
            self.assertEqual(code, 1, now)
            self.assertIn("NOT READY: ", text)
            self.assertIn(why, text)
            code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now=now))
            self.assertEqual(code, 1)
            self.assertIn(why, err)
        self.assertFalse(os.path.exists(self.state))

    def test_flat_by_stops_new_entries_even_with_a_late_window(self):
        self.set_cfg(no_entries_after="15:25")
        self.write_risk("2026-11-13 15:12")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 15:12"))
        self.assertEqual(code, 1)
        self.assertIn("flat_by 15:10 IST has come", text)

    def test_a_weekend_and_a_holiday_are_closed(self):
        # 2026-11-14 is a Saturday; 2026-11-10 is Diwali Balipratipada (NSE holiday)
        for day, why in (("2026-11-14", "closed today (Saturday)"), ("2026-11-10", "NSE holiday")):
            self.edit_orders(lambda d: d["orders"][0]["plan"].update(date=day))
            self.write_risk(day + " 10:05")
            code, text, _ = run(self.args("show", "--index", "1", now=day + " 10:05"))
            self.assertEqual(code, 1, day)
            self.assertIn(why, text)

    def test_an_unknown_holiday_list_is_not_a_trading_day(self):
        day = "2027-03-02"                      # a Tuesday; NSE's 2027 list is not loaded
        self.edit_orders(lambda d: (d["orders"][0]["plan"].update(date=day), d.update(created_at=day + "T04:35:00Z")))
        self.write_risk(day + " 10:05")
        code, text, _ = run(self.args("show", "--index", "1", now=day + " 10:05"))
        self.assertEqual(code, 1)
        self.assertIn("NSE's 2027 holiday list isn't loaded", text)
        self.set_cfg(holiday_years=[2027])
        self.assertEqual(run(self.args("show", "--index", "1", now=day + " 10:05"))[0], 0)

    def test_paper_fill_looks_at_the_clock_again(self):
        self.approve_paper()
        code, text, _ = run(self.paper_args("paper-fill", "--index", "1", now="2026-11-13 15:20"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.paper))
        self.assertFalse(os.path.exists(self.pf))
        code, text, err = run(self.paper_args("paper-fill", "--index", "1", now="2026-11-13 15:20"))
        self.assertIn("not filling order 1", err)
        self.assertIn("flat_by 15:10 IST has come", err)
        self.assertIn("no new entries after 14:30", err)
        self.assertIn("Nothing was bought", err)
        # a yes that is older than the approval window is not a yes any more
        code, _, err = run(self.paper_args("paper-fill", "--index", "1", now="2026-11-13 10:20"))
        self.assertEqual(code, 1)
        self.assertIn("your yes was 15 min ago", err)
        self.assertEqual(run(self.paper_args("paper-fill", "--index", "1", now="2026-11-13 10:08"))[0], 0)

    def test_exits_are_never_blocked_by_the_window(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))
        late = "2026-11-13 15:20"
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf, "--config",
             self.cfg, "--state", self.state, "--now", late])
        self.write_risk(late)
        code, text, _ = run(self.args("show", "--index", "1", now=late))
        self.assertEqual(code, 0, text)
        self.assertIn("READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", now=late))[0], 0)
        self.assertEqual(run(self.paper_args("paper-fill", "--index", "1", now=late))[0], 0)

    def test_yesterdays_order_and_an_old_orders_file_are_not_ready(self):
        self.write_risk()
        self.edit_orders(lambda d: d["orders"][0]["plan"].update(date="2026-11-12"))
        self.write_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("planned for 2026-11-12, not today (2026-11-13)", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertIn("planned for 2026-11-12", err)
        self.edit_orders(lambda d: d["orders"][0]["plan"].update(date="2026-11-13"))
        self.edit_orders(lambda d: d.update(created_at="2026-11-13T02:00:00Z"))         # 07:30 IST: 155 minutes old
        self.write_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("orders.json was written 155 min ago (07:30 IST), over the 120-minute limit", text)
        self.set_cfg(order_max_age_minutes=200)
        self.assertEqual(run(self.args("show", "--index", "1"))[0], 0)

    def test_the_ticket_prints_the_orders_age_and_plan_date(self):
        self.edit_orders(lambda d: d.update(created_at="2026-11-13T04:20:00Z"))
        self.write_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("order: plan date 2026-11-13, written 15 min ago (09:50 IST)", text)
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        out = json.loads(text)
        self.assertEqual(out["plan_date"], "2026-11-13")
        self.assertEqual(out["order_age"], ["plan date 2026-11-13", "written 15 min ago (09:50 IST)"])

    def test_decided_orders_keep_their_label_after_the_pass_expires(self):
        self.approve_paper()
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 11:00"))
        self.assertIn("APPROVED by you", text)


class ApproveReply(FlowBase):
    """Item 5b: approve --reply must be what the user last typed."""

    def test_the_reply_must_equal_the_users_last_message(self):
        self.set_cfg(broker="kite")
        self.write_risk()
        self.say("yes please")
        code, text, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("is not what the user last typed", text)
        self.assertIn("never write a yes yourself", text)
        self.assertFalse(os.path.exists(self.state))
        code, text, err = run(self.args("approve", "--index", "1", "--reply", "  Yes   PLEASE "))   # whitespace and case
        self.assertEqual(code, 0, text + err)
        self.assertIs(self.doc(self.state)["orders"][0]["reply_verified"], True)

    def test_a_fabricated_yes_is_refused_in_every_mode(self):
        self.write_risk()
        self.say("what is the max loss?")
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("is not what the user last typed", text)

    def test_kite_mode_without_the_hook_refuses(self):
        self.set_cfg(broker="kite")
        self.write_risk()
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("the approval gate is not installed: install this as a plugin, hooks do not ship with copied "
                      "skill folders", text)
        self.assertFalse(os.path.exists(self.state))

    def test_kite_mode_with_a_stale_message_refuses(self):
        self.set_cfg(broker="kite")
        self.write_risk()
        self.say("yes", at="2026-11-13 09:50")             # 15 minutes before the approval
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("recorded 15 min ago, over 10 minutes", text)

    def test_paper_and_manual_accept_without_a_record_but_say_so(self):
        for mode in ("paper", "manual"):
            self.set_cfg(broker=mode)
            if os.path.exists(self.state):
                os.remove(self.state)
            self.write_risk()
            code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
            self.assertEqual(code, 0, mode)
            rec = self.doc(self.state)["orders"][0]
            self.assertIs(rec["reply_verified"], False)
            self.assertIn("reply_verified=false", text)

    def test_a_corrupt_prompt_file_is_a_missing_one(self):
        self.set_cfg(broker="kite")
        self.write_risk()
        os.makedirs(os.path.dirname(self.prompt), exist_ok=True)
        with open(self.prompt, "w") as fh:
            fh.write("{not json")
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not installed", text)

    def test_record_user_prompt_never_raises(self):
        self.assertTrue(ticket.record_user_prompt("yes", self.prompt, nc.parse_now(NOW)))
        self.assertEqual(self.doc(self.prompt), {"text": "yes", "at": "2026-11-13T04:35:00Z"})
        self.assertFalse(ticket.record_user_prompt("yes", os.path.join(self.d, "state", "last_prompt.json", "x")))
        self.assertFalse(ticket.record_user_prompt(None, "/dev/null/x"))


class Gate(FlowBase):
    """Item 5a: gate_check, the logic behind hooks/gate.py."""

    def test_the_exact_entry_call_is_allowed_once(self):
        self.go_kite()
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL)
        self.assertTrue(ok, why)
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL)
        self.assertFalse(ok)
        self.assertIn("already sent once", why)
        leg = self.doc(self.state)["orders"][0]["legs"][0]
        self.assertEqual((leg["status"], leg["used_at"]), ("used", "2026-11-13T04:36:00Z"))

    def test_no_approval_means_no_call(self):
        self.set_cfg(broker="kite")
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL)
        self.assertFalse(ok)
        self.assertIn("does not exist", why)
        self.write_risk()
        self.say("yes")
        run(self.args("decline", "--index", "1"))       # a state file with a decline in it, but no approval
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL)
        self.assertFalse(ok)
        self.assertIn("no approved place_order on record", why)

    def test_a_mismatch_names_the_field(self):
        self.go_kite()
        for field, value in (("quantity", 57), ("price", 1335.0), ("tradingsymbol", "TCS"),
                             ("transaction_type", "SELL"), ("order_type", "MARKET"), ("product", "CNC"),
                             ("trigger_price", 1330.0), ("variety", "amo"), ("exchange", "BSE"),
                             ("validity", "IOC")):
            ok, why = self.gate("mcp__kite__place_order", dict(ENTRY_CALL, **{field: value}))
            self.assertFalse(ok, field)
            self.assertIn("differs from the approved ticket", why)
            self.assertIn(field + ":", why)
        ok, why = self.gate("mcp__kite__place_order", dict(ENTRY_CALL, iceberg_legs=3))
        self.assertFalse(ok)
        self.assertIn("iceberg_legs", why)
        # none of that used the leg: the right call still goes
        self.assertTrue(self.gate("mcp__kite__place_order", ENTRY_CALL)[0])

    def test_numbers_are_compared_as_numbers_and_the_tag_is_ignored(self):
        self.go_kite()
        call = dict(ENTRY_CALL, quantity="56", price="1334", tag="anything", trigger_price=0)
        self.assertTrue(self.gate("mcp__kite__place_order", call)[0])
        self.assertTrue(self.gate("mcp__other_server__place_order", dict(ENTRY_CALL, price=1334.0))[0] is False)

    def test_an_expired_approval_is_refused(self):
        self.go_kite()
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL, now="2026-11-13 10:11")      # 6 minutes after
        self.assertFalse(ok)
        self.assertIn("expired at 2026-11-13T04:40:00Z", why)
        self.assertEqual(self.doc(self.state)["orders"][0]["legs"][0]["status"], "approved")

    def test_an_entry_is_refused_at_flat_by(self):
        self.set_cfg(no_entries_after="15:25", order_max_age_minutes=500)
        self.go_kite(now="2026-11-13 15:08")                # 2 minutes before flat_by; the leg lasts 5
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL, now="2026-11-13 15:10")
        self.assertFalse(ok)
        self.assertIn("flat_by 15:10 IST has come", why)

    def test_gtt_orders_and_unknown_tools_are_refused(self):
        self.go_kite()
        for tool in ("mcp__kite__place_gtt_order", "mcp__kite__modify_gtt_order", "mcp__kite__delete_gtt_order"):
            ok, why = self.gate(tool, ENTRY_CALL)
            self.assertFalse(ok)
            self.assertIn("GTT", why)
        ok, why = self.gate("mcp__kite__get_positions", {})
        self.assertFalse(ok)
        self.assertIn("not an order tool", why)
        self.assertFalse(self.gate("mcp__kite__place_order", None)[0])
        self.assertFalse(self.gate("mcp__kite__place_order", "x")[0])

    def test_it_fails_closed_on_bad_state_and_no_config(self):
        self.go_kite()
        with open(self.state, "w") as fh:
            fh.write("{broken")
        ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL)
        self.assertFalse(ok)
        self.assertIn("unreadable", why)
        with open(self.state, "w") as fh:
            json.dump({"orders": "nope"}, fh)
        self.assertFalse(self.gate("mcp__kite__place_order", ENTRY_CALL)[0])
        with open(self.state, "w") as fh:
            json.dump(["x"], fh)
        self.assertFalse(self.gate("mcp__kite__place_order", ENTRY_CALL)[0])
        os.remove(self.cfg)
        ok, why = ticket.gate_check("mcp__kite__place_order", ENTRY_CALL, self.state, self.cfg,
                                    nc.parse_now(NOW))
        self.assertFalse(ok)
        self.assertIn("no config", why)

    def test_a_bug_inside_the_gate_is_a_deny(self):
        self.go_kite()
        with mock.patch.object(ticket, "parse_utc", side_effect=RuntimeError("boom")):
            ok, why = self.gate("mcp__kite__place_order", ENTRY_CALL)
        self.assertFalse(ok)
        self.assertIn("failed closed", why)
        self.assertIn("boom", why)

    def test_a_leg_of_a_wrong_shape_is_ignored(self):
        self.go_kite()
        st = self.doc(self.state)
        st["orders"][0]["legs"].insert(0, "junk")
        st["orders"][0]["legs"].insert(0, {"tool": "place_order", "args": "junk", "status": "approved"})
        with open(self.state, "w") as fh:
            json.dump(st, fh)
        self.assertTrue(self.gate("mcp__kite__place_order", ENTRY_CALL)[0])

    def test_two_calls_at_once_cannot_both_use_a_leg(self):
        self.go_kite()
        import threading
        res = []
        ts = [threading.Thread(target=lambda: res.append(self.gate("mcp__kite__place_order", ENTRY_CALL)[0]))
              for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(res), [False] * 5 + [True])

    def test_the_stop_leg_exists_only_after_the_fill_and_uses_the_filled_quantity(self):
        self.go_kite()
        self.assertFalse(self.gate("mcp__kite__place_order", STOP_CALL)[0])           # no stop leg yet
        self.assertTrue(self.gate("mcp__kite__place_order", ENTRY_CALL)[0])
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1", "--filled-qty", "56",
                      "--filled-price", "1334", "--portfolio", self.pf, now="2026-11-13 10:06"))
        leg = [l for l in self.doc(self.state)["orders"][0]["legs"] if l["role"] == "stop"][0]
        self.assertEqual(leg["args"]["quantity"], 56)
        self.assertEqual(leg["expires_at"], "2026-11-13T05:06:00Z")                       # 30 minutes
        self.assertFalse(self.gate("mcp__kite__place_order", dict(STOP_CALL, quantity=40))[0])
        ok, why = self.gate("mcp__kite__place_order", STOP_CALL)
        self.assertTrue(ok, why)
        self.assertFalse(self.gate("mcp__kite__place_order", STOP_CALL)[0])

    def test_manual_mode_records_the_legs_too(self):
        self.set_cfg(broker="manual")
        self.write_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual([l["role"] for l in self.doc(self.state)["orders"][0]["legs"]], ["entry"])


class PartialFills(FlowBase):
    """Item 4: the stop, the flatten quantity and portfolio.json follow the shares that FILLED."""

    def test_kite_partial_fill_sizes_the_stop_to_the_filled_shares(self):
        self.go_kite()
        self.assertTrue(self.gate("mcp__kite__place_order", ENTRY_CALL)[0])
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1",
                                      "--filled-qty", "30", "--filled-price", "1334.0", "--portfolio", self.pf,
                                      now="2026-11-13 10:06"))
        self.assertEqual(code, 0)
        self.assertIn("PARTIAL FILL: 30 of 56 shares", text)
        self.assertIn('"quantity": 30', text)
        self.assertNotIn('"quantity": 56', text.split("Now the stop leg")[1].split("Then:")[0])
        self.assertIn("The other 26 shares of the entry may still be working", text)
        self.assertIn('cancel_order {"variety": "regular", "order_id": "E1"}', text)
        pf = self.doc(self.pf)
        self.assertEqual((pf["positions"][0]["shares"], pf["cash"]), (30, 100000 - 30 * 1334.0))
        # the gate: the stop for 30 goes, the planned 56 does not; the rest of the entry can be cancelled once
        self.assertFalse(self.gate("mcp__kite__place_order", STOP_CALL)[0])
        self.assertTrue(self.gate("mcp__kite__place_order", dict(STOP_CALL, quantity=30))[0])
        self.assertTrue(self.gate("mcp__kite__cancel_order", {"variety": "regular", "order_id": "E1"})[0])
        self.assertFalse(self.gate("mcp__kite__cancel_order", {"variety": "regular", "order_id": "E1"})[0])
        # recording the stop leg records 30 shares, not 56
        code, text, _ = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed",
                                      "--broker-id", "S1", now="2026-11-13 10:07"))
        self.assertIn("(30 shares)", text)
        stop = self.doc(self.state)["orders"][-1]
        self.assertEqual((stop["qty"], stop["trigger"], stop["limit"]), (30, 1328.8, 1322.1))
        self.assertEqual([r for r in self.doc(self.state)["orders"] if r["status"] == "filled"][0]["qty"], 30)
        # flatten takes the broker's 30, and says portfolio.json agrees
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 15:00",
                             "--positions", self.positions(RELIANCE=30)])
        self.assertIn("SELL 30 RELIANCE", text)
        self.assertEqual(self.doc(self.orders)["orders"][0]["entry"]["quantity"], 30)

    def test_more_shares_than_ordered_is_refused(self):
        self.go_kite()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "60",
                                     "--filled-price", "1334", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("more than the 56 shares", err)
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "30",
                                     "--portfolio", self.pf))
        self.assertIn("give both", err)
        self.assertFalse(os.path.exists(self.pf))

    def test_manual_partial_fill_prints_the_stop_for_the_filled_shares(self):
        self.set_cfg(broker="manual")
        self.write_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "30",
                                      "--filled-price", "1334", "--portfolio", self.pf))
        self.assertIn("Order type: SL. Qty: 30. Trigger price: 1328.80. Price: 1322.10.", text)

    def test_a_late_fill_recorded_with_fill_gets_its_stop_leg(self):
        self.go_kite()
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1"))
        code, text, _ = run(["fill", "--symbol", "RELIANCE", "--side", "buy", "--qty", "40", "--price", "1334",
                             "--portfolio", self.pf, "--config", self.cfg, "--state", self.state, "--now",
                             "2026-11-13 10:07"])
        self.assertEqual(code, 0)
        self.assertIn("The stop leg for the 40 shares that filled", text)
        self.assertTrue(self.gate("mcp__kite__place_order", dict(STOP_CALL, quantity=40), "2026-11-13 10:08")[0])

    def test_paper_partial_fill_follows_the_filled_shares(self):
        self.approve_paper()
        code, text, err = run(self.paper_args("paper-fill", "--index", "1", "--filled-qty", "30"))
        self.assertEqual(code, 0, err)
        self.assertIn("bought 30 RELIANCE", text)
        trade = self.doc(self.paper)["trades"][0]
        self.assertEqual(trade["qty"], 30)
        self.assertEqual(self.doc(self.pf)["positions"][0]["shares"], 30)
        placed = [r for r in self.doc(self.state)["orders"] if r["status"] == "placed" and r.get("leg") == "entry"][0]
        self.assertEqual((placed["qty"], placed["planned_qty"]), (30, 56))
        code, text, _ = run(self.check_args("2026-11-13 15:30"))
        self.assertIn("TARGET HIT at 1,344.30", text)
        self.assertIn("Gross ₹309,", text)                 # 30 x 10.30, not 56 x
        self.assertEqual(self.doc(self.pf)["positions"], [])
        code, _, err = run(self.paper_args("paper-fill", "--index", "1", "--filled-qty", "60"))
        self.assertEqual(code, 1)


class FlattenAndPositions(FlowBase):
    """Items 9 and 22: the quantity comes from the broker; only this symbol's order is replaced."""

    def flat(self, *extra, now="2026-11-13 15:00"):
        return ["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                "--config", self.cfg, "--state", self.state, "--now", now] + list(extra)

    def hold(self, shares=56):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 25296.0, "positions": [{"ticker": "RELIANCE", "shares": shares, "lots": [
                {"id": "RELIANCE-1", "date": TODAY, "shares": shares, "cost": round(shares * 1334.0, 2)}]}]}, fh)

    def test_outside_paper_mode_flatten_needs_the_brokers_number(self):
        self.hold()
        for mode in ("kite", "manual"):
            self.set_cfg(broker=mode)
            code, _, err = run(self.flat())
            self.assertEqual(code, 1)
            self.assertIn("get_positions", err)
            self.assertIn("--positions FILE", err)
            self.assertIn("leaves you short", err)
        self.assertEqual(self.doc(self.orders)["orders"][0]["entry"]["transaction_type"], "BUY")   # nothing written

    def test_a_stop_that_already_filled_at_zerodha_is_not_sold_again(self):
        # portfolio.json still says 56 (nobody recorded the stop fill); get_positions says 0: the old flatten
        # would have written SELL 56 and left the account short 56
        self.set_cfg(broker="kite")
        self.hold(56)
        code, _, err = run(self.flat("--positions", self.positions(RELIANCE=0)))
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)
        self.assertIn("shows no RELIANCE shares", err)
        code, _, err = run(self.flat("--positions", self.positions(TCS=10)))
        self.assertIn("nothing to flatten", err)

    def test_the_brokers_number_wins_and_the_disagreement_is_said(self):
        self.set_cfg(broker="kite")
        self.hold(56)
        code, text, _ = run(self.flat("--positions", self.positions(RELIANCE=30)))
        self.assertEqual(code, 0)
        self.assertIn("SELL 30 RELIANCE", text)
        self.assertIn("portfolio.json says 56 RELIANCE, but the broker (get_positions", text)
        self.assertIn("using the broker's 30", text)
        self.assertEqual(self.doc(self.orders)["orders"][0]["entry"]["quantity"], 30)
        code, text, _ = run(self.flat("--qty", "12"))
        self.assertIn("SELL 12 RELIANCE", text)
        self.assertIn("using the broker's 12", text)

    def test_positions_rows_of_other_products_and_exchanges_are_ignored(self):
        doc = {"data": {"net": [
            {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "CNC", "quantity": 100},
            {"tradingsymbol": "RELIANCE", "exchange": "BSE", "product": "MIS", "quantity": 7},
            {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 12},
            {"tradingsymbol": "TCS", "exchange": "NSE", "product": "MIS", "quantity": -5}], "day": []}}
        rows = ticket.broker_positions(doc)
        self.assertEqual(rows["RELIANCE"][0], 12)
        self.assertEqual(rows["TCS"][0], -5)
        with self.assertRaises(ticket.TicketError):
            ticket.broker_positions({"status": "error"})

    def test_a_short_position_is_refused(self):
        self.set_cfg(broker="kite")
        code, _, err = run(self.flat("--positions", self.positions(RELIANCE=-10)))
        self.assertEqual(code, 1)
        self.assertIn("SHORT position of 10 RELIANCE", err)

    def test_flatten_replaces_only_that_symbols_order(self):
        def two(d):
            o2 = json.loads(json.dumps(d["orders"][0]))
            o2["symbol"] = "TCS"
            for leg in ("entry", "stop"):
                o2[leg]["tradingsymbol"] = "TCS"
            d["orders"].append(o2)
            d["created_at"] = "2026-11-13T03:00:00Z"
        self.edit_orders(two)
        self.hold()
        code, text, _ = run(self.flat())
        self.assertEqual(code, 0, text)
        orders = self.doc(self.orders)["orders"]
        self.assertEqual([(o["symbol"], o["entry"]["transaction_type"]) for o in orders],
                         [("RELIANCE", "SELL"), ("TCS", "BUY")])        # replaced in place, the other kept
        self.assertIn("the other 1 order kept", text)
        # the kept order keeps its own age: the file's new created_at must not make it look fresh
        self.assertEqual(orders[1]["created_at"], "2026-11-13T03:00:00Z")
        self.assertEqual(orders[0]["created_at"], "2026-11-13T09:30:00Z")
        self.assertEqual(self.doc(self.orders)["created_at"], "2026-11-13T09:30:00Z")
        # a symbol with no order of its own is added at the end
        self.hold()
        with open(self.pf) as fh:
            pf = json.load(fh)
        pf["positions"].append({"ticker": "TCS", "shares": 3, "lots": [{"id": "TCS-1", "date": TODAY,
                                                                          "shares": 3, "cost": 9000.0}]})
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["flatten", "--symbol", "TCS", "--orders", self.orders, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 15:00"])
        orders = self.doc(self.orders)["orders"]
        self.assertEqual([(o["symbol"], o["entry"]["transaction_type"]) for o in orders],
                         [("RELIANCE", "SELL"), ("TCS", "SELL")])

    def test_kite_approve_needs_the_stops_order_id_before_it_cancels(self):
        self.set_cfg(broker="kite")
        self.hold()
        run(self.flat("--positions", self.positions(RELIANCE=56)))
        order = self.doc(self.orders)["orders"][0]
        self.assertIsNone(order["cancel_first"]["order_id"])
        self.write_risk("2026-11-13 15:00")
        self.say("yes sell 56 RELIANCE", "2026-11-13 15:00")
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes sell 56 RELIANCE",
                                     now="2026-11-13 15:00"))
        self.assertEqual(code, 1)
        self.assertIn("--stop-id ID", err)
        code, text, _ = run(self.flat("--positions", self.positions(RELIANCE=56), "--stop-id", "S9"))
        self.assertIn("cancel_order", text)
        self.write_risk("2026-11-13 15:00")
        code, text, err = run(self.args("approve", "--index", "1", "--reply", "yes sell 56 RELIANCE",
                                        now="2026-11-13 15:00"))
        self.assertEqual(code, 0, err)
        roles = [(l["role"], l["tool"], l.get("after")) for l in self.doc(self.state)["orders"][0]["legs"]]
        fp = self.doc(self.state)["orders"][0]["fingerprint"]
        self.assertEqual(roles, [("cancel", "cancel_order", None), ("flatten", "place_order", fp + ":cancel")])
        sell = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
                "quantity": 56, "product": "MIS", "order_type": "MARKET", "validity": "DAY"}
        now = "2026-11-13 15:01"
        ok, why = self.gate("mcp__kite__place_order", sell, now)
        self.assertFalse(ok)                                            # the sell may not go before the cancel
        self.assertIn("must wait", why)
        self.assertFalse(self.gate("mcp__kite__cancel_order", {"variety": "regular", "order_id": "S1"}, now)[0])
        self.assertTrue(self.gate("mcp__kite__cancel_order", {"variety": "regular", "order_id": "S9"}, now)[0])
        self.assertTrue(self.gate("mcp__kite__place_order", sell, now)[0])
        self.assertFalse(self.gate("mcp__kite__place_order", sell, now)[0])

    def test_reconcile_shows_and_applies(self):
        self.hold(56)
        pos = self.positions(RELIANCE=30, TCS=4)
        code, text, _ = run(["reconcile", "--positions", pos, "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE: the broker shows 30, portfolio.json shows 56.", text)
        self.assertIn("TCS: the broker shows 4, portfolio.json shows 0.", text)
        self.assertIn("Nothing changed", text)
        self.assertEqual(self.doc(self.pf)["positions"][0]["shares"], 56)
        code, text, _ = run(["reconcile", "--positions", pos, "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--now", NOW, "--apply", "--cash", "50000"])
        self.assertEqual(code, 0, text)
        pf = self.doc(self.pf)
        held = {p["ticker"]: (p["shares"], sum(l["shares"] for l in p["lots"])) for p in pf["positions"]}
        self.assertEqual(held, {"RELIANCE": (30, 30), "TCS": (4, 4)})
        self.assertEqual(pf["cash"], 50000.0)
        self.assertEqual(self.doc(self.state)["orders"][-1]["kind"], "reconcile")
        code, text, _ = run(["reconcile", "--positions", pos, "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("matches the broker", text)
        # the broker holds none of it any more
        code, text, _ = run(["reconcile", "--positions", self.positions(), "--portfolio", self.pf, "--config",
                             self.cfg, "--state", self.state, "--now", NOW, "--apply"])
        self.assertEqual(self.doc(self.pf)["positions"], [])


class PaperRealism(FlowBase):
    """Item 12 and 16: the paper broker is no kinder than Kite, and it re-reads the last bar."""

    def test_a_target_filled_above_the_target_when_the_next_bar_opens_there(self):
        rows = [("10:06", *quiet(1334.0)), ("10:07", 1338.0, 1346.0, 1337.0, 1345.0), ("10:08", *quiet(1347.0)),
                ("10:09", *quiet(1347.0))]
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10), ticket.parse_bars(bar_doc(rows)),
                         at_ist("2026-11-13 10:20"))
        self.assertEqual([e[:3] for e in ev], [("target", at_ist("2026-11-13 10:08"), 1347.0)])

    def test_a_target_filled_below_the_target_when_it_pulls_back(self):
        rows = [("10:06", *quiet(1334.0)), ("10:07", 1338.0, 1346.0, 1337.0, 1345.0), ("10:08", *quiet(1341.0))]
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10), ticket.parse_bars(bar_doc(rows)),
                         at_ist("2026-11-13 10:20"))
        self.assertEqual(ev[0][2], 1341.0)         # the old model booked 1,344.40 here: ₹3.40 a share too kind

    def test_no_next_bar_yet_fills_one_tick_under_the_target(self):
        rows = [("10:06", *quiet(1334.0)), ("10:07", 1338.0, 1346.0, 1337.0, 1345.0)]
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10), ticket.parse_bars(bar_doc(rows)),
                         at_ist("2026-11-13 10:20"), nc.DEFAULT_CONFIG)
        self.assertEqual(ev[0][:3], ("target", at_ist("2026-11-13 10:08"), 1344.3))
        self.assertIn("next bar is not on Yahoo yet", ev[0][3])
        self.assertIn("minus one tick", ev[0][3])
        # the next bar is still in progress at 10:08:30: not complete, so not used either
        rows.append(("10:08", *quiet(1350.0)))
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10), ticket.parse_bars(bar_doc(rows)),
                         datetime.datetime(2026, 11, 13, 10, 8, 30), nc.DEFAULT_CONFIG)
        self.assertEqual(ev[0][2], 1344.3)

    def test_a_gap_through_the_sl_limit_does_not_fill_and_is_said_loudly(self):
        # trigger 1,328.80, limit 1,322.10; the 10:07 bar OPENS at 1,320
        rows = [("10:06", *quiet(1334.0)), ("10:07", 1320.0, 1321.0, 1318.0, 1319.0),
                ("10:08", 1319.0, 1321.5, 1318.5, 1320.0), ("10:09", 1320.0, 1323.0, 1319.5, 1322.5)]
        t = trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10, stop_limit=1322.1)
        bars = ticket.parse_bars(bar_doc(rows))
        ev = ticket.walk(t, bars, at_ist("2026-11-13 10:08"))
        self.assertEqual([e[:3] for e in ev], [("stop_gap", at_ist("2026-11-13 10:07"), None)])
        self.assertIn("STOP TRIGGERED BUT NOT FILLED", ev[0][3])
        self.assertIn("STILL OPEN", ev[0][3])
        self.assertIn("under the SL limit 1,322.10", ev[0][3])
        # later: the high reaches the limit on 10:09 and it fills AT THE LIMIT (not at the trigger, not the open)
        t["stop_gap"] = "2026-11-13 10:07"
        ev = ticket.walk(t, bars, at_ist("2026-11-13 10:20"))
        self.assertEqual([e[:3] for e in ev], [("stop", at_ist("2026-11-13 10:09"), 1322.1)])
        self.assertIn("gapped through its SL limit", ev[0][3])

    def test_a_stop_that_never_fills_is_flattened_at_flat_by(self):
        rows = [("10:06", 1320.0, 1321.0, 1318.0, 1319.0), ("10:07", 1319.0, 1320.0, 1317.0, 1318.0),
                ("10:08", 1318.0, 1320.5, 1316.0, 1317.0), ("10:09", 1317.0, 1319.0, 1316.0, 1316.5)]
        t = trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10, flat_by="10:09", stop_limit=1322.1)
        ev = ticket.walk(t, ticket.parse_bars(bar_doc(rows)), at_ist("2026-11-13 10:30"))
        self.assertEqual([e[0] for e in ev], ["stop_gap", "flat_by"])
        self.assertEqual(ev[1][2], 1316.5)
        self.assertIn("never filled", ev[1][3])

    def test_a_bar_that_opens_between_the_limit_and_the_trigger_still_fills_at_its_open(self):
        rows = [("10:06", 1325.0, 1326.0, 1324.0, 1325.0)]          # trigger 1,328.8, limit 1,322.1: open 1,325
        ev = ticket.walk(trade("2026-11-13 10:05", 1334.0, 1328.8, 1344.4, 10, stop_limit=1322.1),
                         ticket.parse_bars(bar_doc(rows)), at_ist("2026-11-13 10:20"))
        self.assertEqual(ev[0][:3], ("stop", at_ist("2026-11-13 10:06"), 1325.0))

    def test_paper_check_reports_the_gap_and_closes_at_the_limit_later(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))
        first = os.path.join(self.d, "b1.json")
        rows = [("10:05", *quiet(1334.0)), ("10:06", 1320.0, 1321.0, 1318.0, 1319.0),
                ("10:07", 1319.0, 1321.0, 1318.5, 1320.0)]
        with open(first, "w") as fh:
            json.dump(bar_doc(rows), fh)
        code, text, _ = run(self.check_args("2026-11-13 10:10", first))
        self.assertIn("!!! STOP TRIGGERED BUT NOT FILLED", text)
        self.assertIn("(TRIGGERED, NOT FILLED since 2026-11-13 10:06: no working stop)".replace("2026-11-13 ", ""), text)
        trade_ = self.doc(self.paper)["trades"][0]
        self.assertEqual((trade_["status"], trade_["stop_gap"]), ("filled", "2026-11-13 10:06"))
        self.assertEqual(self.doc(self.pf)["positions"][0]["shares"], 56)            # still held
        second = os.path.join(self.d, "b2.json")
        with open(second, "w") as fh:
            json.dump(bar_doc(rows + [("10:08", 1320.0, 1323.0, 1319.0, 1322.0)]), fh)
        code, text, _ = run(self.check_args("2026-11-13 10:15", second))
        self.assertIn("STOPPED OUT at 1,322.10 (10:08 IST bar)", text)
        self.assertEqual(self.doc(self.paper)["trades"][0]["exit_price"], 1322.1)

    def test_checked_to_stays_at_the_start_of_the_last_bar_so_a_revised_bar_is_read_again(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))
        rows = [("10:05", *quiet(1334.0)), ("10:06", *quiet(1335.0)), ("10:07", 1335.0, 1340.0, 1334.0, 1338.0)]
        first = os.path.join(self.d, "b1.json")
        with open(first, "w") as fh:
            json.dump(bar_doc(rows), fh)
        code, text, _ = run(self.check_args("2026-11-13 10:08", first))
        self.assertNotIn("TARGET HIT", text)
        self.assertEqual(self.doc(self.paper)["trades"][0]["checked_to"], "2026-11-13 10:07")     # not 10:08
        # Yahoo finishes the 10:07 bar later: its high is 1,346 after all, and a 10:08 bar follows
        revised = [("10:05", *quiet(1334.0)), ("10:06", *quiet(1335.0)), ("10:07", 1335.0, 1346.0, 1334.0, 1345.0),
                   ("10:08", *quiet(1344.8))]
        second = os.path.join(self.d, "b2.json")
        with open(second, "w") as fh:
            json.dump(bar_doc(revised), fh)
        code, text, _ = run(self.check_args("2026-11-13 10:10", second))
        self.assertIn("TARGET HIT at 1,344.80 (10:08 IST bar)", text)

    def test_a_bar_that_is_not_complete_is_never_acted_on(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))
        rows = [("10:05", *quiet(1334.0)), ("10:06", 1334.0, 1346.0, 1333.0, 1345.0)]       # 10:06 ends at 10:07
        bars = os.path.join(self.d, "b.json")
        with open(bars, "w") as fh:
            json.dump(bar_doc(rows), fh)
        code, text, _ = run(self.check_args("2026-11-13 10:06", bars))                        # 10:06:00: in progress
        self.assertNotIn("TARGET HIT", text)
        self.assertEqual(self.doc(self.paper)["trades"][0]["status"], "filled")
        self.assertEqual(self.doc(self.paper)["trades"][0]["checked_to"], "2026-11-13 10:05")

    def test_a_resting_limit_that_filled_is_not_stopped_on_its_own_fill_bar(self):
        t = trade("2026-11-13 10:20", None, 1325.0, 1360.0)
        t.update(status="open", limit=1336.0, fill_time=None)
        rows = [("10:20", 1340.0, 1341.0, 1339.0, 1340.0), ("10:21", 1337.0, 1338.0, 1320.0, 1322.0)]
        # 10:21: opens 1,337, trades down through the 1,336 limit and on through the 1,325 stop in ONE bar
        bars = ticket.parse_bars(bar_doc(rows))
        ev = ticket.walk(t, bars, at_ist("2026-11-13 10:30"))
        self.assertEqual([e[0] for e in ev], ["fill"])          # the fill bar is not also read as a stop bar
        t.update(status="filled", fill_time="2026-11-13 10:21", fill_price=1336.0, fill_bar="2026-11-13 10:21",
                 checked_to="2026-11-13 10:21")
        self.assertEqual(ticket.walk(t, bars, at_ist("2026-11-13 10:30")), [])    # and not on the re-read either


class StatusAndMoveStop(FlowBase):
    """Items 21 and 23."""

    def status(self, now, quiet=False):
        return ticket.status_report(self.state, self.pf, self.risk, self.cfg, nc.parse_now(now), quiet=quiet)

    def test_a_clean_account_prints_nothing_when_quiet(self):
        self.assertEqual(self.status(NOW, quiet=True), "")
        self.assertIn("nothing needs attention", self.status(NOW))
        code, text, _ = run(["status", "--quiet", "--config", self.cfg, "--state", self.state, "--risk", self.risk,
                             "--portfolio", self.pf, "--now", NOW])
        self.assertEqual((code, text), (0, ""))

    def test_an_approval_nobody_placed(self):
        self.go_kite()
        self.assertEqual(self.status("2026-11-13 10:07", quiet=True), "")             # 2 minutes: not yet
        text = self.status("2026-11-13 10:09", quiet=True)                              # 4 minutes
        self.assertIn("RELIANCE: you said yes at 10:05 IST", text)
        self.assertIn("nothing has been recorded since", text)
        code, text, _ = run(["status", "--config", self.cfg, "--state", self.state, "--risk", self.risk,
                             "--portfolio", self.pf, "--now", "2026-11-13 10:09"])
        self.assertEqual(code, 0)                                                      # exit 0 always
        self.assertIn("1 thing needs attention", text)
        code, text, _ = run(["status", "--json", "--config", self.cfg, "--state", self.state, "--risk", self.risk,
                             "--portfolio", self.pf, "--now", "2026-11-13 10:09"])
        out = json.loads(text)
        self.assertEqual((code, out["ok"], out["findings"][0]["kind"]), (0, False, "unplaced"))

    def test_an_entry_filled_with_no_stop_recorded(self):
        self.go_kite()
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1", "--filled-qty", "56",
                      "--filled-price", "1334", "--portfolio", self.pf, now="2026-11-13 10:06"))
        text = self.status("2026-11-13 10:07", quiet=True)          # the process "crashed" before the stop leg
        self.assertIn("RELIANCE: the entry filled", text)
        self.assertIn("56 shares have no stop at the broker", text)
        run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed", "--broker-id", "S1",
                      now="2026-11-13 10:08"))
        self.assertEqual(self.status("2026-11-13 10:09", quiet=True), "")

    def test_a_position_in_the_portfolio_with_no_stop_on_record(self):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 1000, "positions": [{"ticker": "TCS", "shares": 3, "lots": []}]}, fh)
        text = self.status(NOW, quiet=True)
        self.assertIn("TCS: portfolio.json holds 3 shares and no stop is on record today", text)

    def test_a_stop_that_covers_fewer_shares(self):
        self.go_kite()
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1", "--filled-qty", "30",
                      "--filled-price", "1334", "--portfolio", self.pf, now="2026-11-13 10:06"))
        run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed", "--broker-id", "S1",
                      now="2026-11-13 10:07"))
        self.assertEqual(self.status("2026-11-13 10:08", quiet=True), "")
        with open(self.pf) as fh:
            pf = json.load(fh)
        pf["positions"][0]["shares"] = 56                  # the rest of the entry filled later
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        self.assertIn("the stop covers 30 of the 56 shares", self.status("2026-11-13 10:08", quiet=True))

    def test_a_stale_pass(self):
        self.write_risk("2026-11-13 10:05")
        self.assertEqual(self.status("2026-11-13 10:08", quiet=True), "")
        self.assertIn("the nse-risk PASS from 10:05 IST is 7 min old", self.status("2026-11-13 10:12", quiet=True))
        self.write_risk("2026-11-12 10:05")                # yesterday's file is not a warning today
        self.assertEqual(self.status("2026-11-13 10:12", quiet=True), "")

    def test_status_never_raises(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            fh.write("{broken")
        self.assertIn("could not be checked", self.status(NOW))
        self.assertIsInstance(ticket.status_report("/nonexistent/x", "/nonexistent/y", "/nonexistent/z",
                                                   "/nonexistent/c", nc.parse_now(NOW), quiet=True), str)

    # ------------------------------------------------------------------ move-stop

    def held_paper(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))

    def move(self, *extra, now="2026-11-13 10:20"):
        return ["move-stop", "--symbol", "RELIANCE", "--config", self.cfg, "--state", self.state, "--paper", self.paper,
                "--portfolio", self.pf, "--now", now, "--prompt-file", self.prompt] + list(extra)

    def test_breakeven_is_the_lowest_tick_that_pays_back_the_charges(self):
        be = ticket.breakeven_price(56, 1334.0, "RELIANCE", nc.DEFAULT_CONFIG)
        self.assertEqual(be, 1335.4)
        net = lambda p: 56 * (p - 1334.0) - nc.intraday_charges(56, 1334.0, p, nc.DEFAULT_CONFIG)["total"]
        self.assertGreaterEqual(net(1335.4), 0)
        self.assertLess(net(1335.3), 0)

    def test_paper_move_to_breakeven(self):
        self.held_paper()
        code, text, _ = run(self.move("--to", "breakeven", "--price", "1342"))
        self.assertEqual(code, 0, text)
        self.assertIn("MOVE STOP RELIANCE:", text)
        self.assertIn("from trigger 1,328.80 / limit 1,322.10 to trigger 1,335.40 / limit 1,328.70 on 56 shares", text)
        self.assertIn("break-even: the lowest price that pays back the round trip's ₹73.", text)
        self.assertIn("Ask the user", text)
        self.assertEqual(self.doc(self.paper)["trades"][0]["stop"]["trigger"], 1328.8)         # a ticket only
        code, text, _ = run(self.move("--to", "breakeven", "--price", "1342", "--reply", "yes"))
        self.assertEqual(code, 0, text)
        self.assertIn("Paper: the stop is now trigger 1,335.40 / limit 1,328.70 on 56 shares", text)
        self.assertEqual(self.doc(self.paper)["trades"][0]["stop"], {"type": "SL", "trigger": 1335.4, "price": 1328.7})
        rec = self.doc(self.state)["orders"][-1]
        self.assertEqual((rec["kind"], rec["status"], rec["trigger"], rec["limit"]), ("modify", "approved", 1335.4, 1328.7))
        # the walk now stops at the new trigger: price falls to 1,335 after the move
        t = self.doc(self.paper)["trades"][0]
        ev = ticket.walk(dict(t, checked_to=None), ticket.parse_bars(bar_doc(
            [("10:21", *quiet(1340.0)), ("10:22", 1340.0, 1340.0, 1335.0, 1336.0)])), at_ist("2026-11-13 10:30"))
        self.assertEqual(ev[0][:3], ("stop", at_ist("2026-11-13 10:22"), 1335.4))

    def test_a_stop_only_moves_up_and_never_above_the_last_price(self):
        self.held_paper()
        for to, price, why in (("1325.00", "1342", "only moves UP"), ("1328.80", "1342", "only moves UP"),
                               ("1343.00", "1342", "not under the last price 1,342.00"),
                               ("1342.00", "1342", "not under the last price"),
                               ("breakeven", "1335", "not under the last price 1,335.00"),
                               ("1336.03", "1342", "not on the 0.10 tick"), ("sideways", "1342", "wants breakeven")):
            code, _, err = run(self.move("--to", to, "--price", price))
            self.assertEqual(code, 1, to)
            self.assertIn(why, err, to)
        self.assertEqual(self.doc(self.paper)["trades"][0]["stop"]["trigger"], 1328.8)
        code, text, _ = run(self.move("--to", "1336.00", "--price", "1342", "--reply", "yes TCS"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertEqual(self.doc(self.paper)["trades"][0]["stop"]["trigger"], 1328.8)

    def test_paper_move_stop_reads_the_last_bar(self):
        self.held_paper()
        code, text, _ = run(self.move("--to", "1330.00", "--bars", BARS_PATH, now="2026-11-13 10:10"))
        self.assertEqual(code, 0, text)
        self.assertIn("last price 1,341.00 from the 10:09 IST 1-minute close", text)

    def kite_with_stop(self):
        self.go_kite()
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "E1", "--filled-qty", "56",
                      "--filled-price", "1334", "--portfolio", self.pf, now="2026-11-13 10:06"))
        run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed", "--broker-id", "S1",
                      now="2026-11-13 10:07"))

    def test_kite_move_stop_needs_the_yes_and_the_gate(self):
        self.set_cfg(broker="kite")
        self.kite_with_stop()
        self.say("yes", "2026-11-13 10:20")
        code, text, _ = run(self.move("--to", "1336.00", "--price", "1342", "--reply", "yes"))
        self.assertEqual(code, 0, text)
        call = {"variety": "regular", "order_id": "S1", "quantity": 56, "order_type": "SL", "price": 1329.3,
                "trigger_price": 1336.0, "validity": "DAY"}
        self.assertIn("Now call modify_order with exactly:", text)
        self.assertEqual(json.loads(text[text.index("{"):text.index("}") + 1]), call)
        leg = self.doc(self.state)["orders"][-1]["legs"][0]
        self.assertEqual((leg["tool"], leg["role"], leg["status"]), ("modify_order", "modify", "approved"))
        later = "2026-11-13 10:21"
        for field, value in (("price", 1330.0), ("trigger_price", 1337.0), ("quantity", 100), ("order_id", "S2")):
            ok, why = self.gate("mcp__kite__modify_order", dict(call, **{field: value}), later)
            self.assertFalse(ok, field)
            self.assertIn(field, why)
        ok, why = self.gate("mcp__kite__modify_order", call, later)
        self.assertTrue(ok, why)
        self.assertFalse(self.gate("mcp__kite__modify_order", call, later)[0])
        # the log now treats the stop as moved
        stop = ticket.effective_stop(self.doc(self.state), "RELIANCE", TODAY, "kite")
        self.assertEqual((stop["trigger"], stop["limit"], stop["order_id"]), (1336.0, 1329.3, "S1"))

    def test_kite_move_stop_without_a_fresh_price_or_the_hook_is_refused(self):
        self.set_cfg(broker="kite")
        self.kite_with_stop()
        prices = os.path.join(self.d, "prices.json")
        code, _, err = run(self.move("--to", "1336.00", "--prices", prices))
        self.assertEqual(code, 1)
        self.assertIn("no fresh last price for RELIANCE", err)
        with open(prices, "w") as fh:        # a quote from 09:00: stale at 10:20
            json.dump({"as_of": "2026-11-13T03:30:00Z", "prices": {"RELIANCE": 1342.0}}, fh)
        code, _, err = run(self.move("--to", "1336.00", "--prices", prices))
        self.assertIn("older than 5 minutes", err)
        with open(prices, "w") as fh:
            json.dump({"as_of": "2026-11-13T04:49:00Z", "prices": {"RELIANCE": 1342.0}}, fh)
        code, text, _ = run(self.move("--to", "1336.00", "--prices", prices))        # a ticket needs no hook
        self.assertEqual(code, 0, text)
        code, text, _ = run(self.move("--to", "1336.00", "--prices", prices, "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("recorded 15 min ago, over 10 minutes", text)
        os.remove(self.prompt)
        code, text, _ = run(self.move("--to", "1336.00", "--prices", prices, "--reply", "yes"))
        self.assertIn("not installed", text)
        self.assertEqual(len(self.doc(self.state)["orders"]), 4)

    def test_a_partial_exit_shrinks_the_stop_first_on_kite(self):
        self.set_cfg(broker="kite")
        self.kite_with_stop()
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 11:00",
                             "--positions", self.positions(RELIANCE=56), "--qty", "20", "--limit", "1340"])
        self.assertEqual(code, 0, text)
        self.assertIn("Partial exit RELIANCE: SELL 20 RELIANCE", text)
        self.assertIn("modify_order", text)
        order = self.doc(self.orders)["orders"][0]
        self.assertNotIn("cancel_first", order)
        self.assertEqual((order["modify_first"]["quantity"], order["modify_first"]["order_id"],
                          order["modify_first"]["trigger_price"], order["modify_first"]["price"]),
                         (36, "S1", 1328.8, 1322.1))
        self.write_risk("2026-11-13 11:00")
        self.say("yes sell 20 RELIANCE", "2026-11-13 11:00")
        code, text, err = run(self.args("approve", "--index", "1", "--reply", "yes sell 20 RELIANCE",
                                        now="2026-11-13 11:00"))
        self.assertEqual(code, 0, text + err)
        self.assertEqual(json.loads(text[text.index("{"):text.index("}") + 1]),
                         {"variety": "regular", "order_id": "S1", "quantity": 36, "order_type": "SL",
                          "price": 1322.1, "trigger_price": 1328.8, "validity": "DAY"})
        sell = ticket.tool_input(order["entry"])
        now = "2026-11-13 11:01"
        self.assertFalse(self.gate("mcp__kite__place_order", sell, now)[0])        # before the stop is shrunk
        ok, why = self.gate("mcp__kite__modify_order", {"variety": "regular", "order_id": "S1", "quantity": 36,
                                                         "order_type": "SL", "price": 1322.1,
                                                         "trigger_price": 1328.8, "validity": "DAY"}, now)
        self.assertTrue(ok, why)
        self.assertTrue(self.gate("mcp__kite__place_order", sell, now)[0])
        # the log knows the stop now covers the 36 that stay: no "stop covers fewer shares" or "no stop" warning
        with open(self.pf) as fh:
            pf = json.load(fh)
        pf["positions"][0]["shares"] = 36
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        stop = ticket.effective_stop(self.doc(self.state), "RELIANCE", TODAY, "kite")
        self.assertEqual((stop["qty"], stop["order_id"]), (36, "S1"))
        self.assertEqual(ticket.status_report(self.state, self.pf, self.risk, self.cfg,
                                              nc.parse_now("2026-11-13 11:02"), quiet=True), "")
        # without the stop's trigger on record the partial refuses and says what to pass
        os.remove(self.state)
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 11:00",
                            "--positions", self.positions(RELIANCE=56), "--qty", "20"])
        self.assertEqual(code, 1)
        self.assertIn("--stop-id ID --stop-trigger T --stop-limit L", err)

    def test_a_partial_exit_in_paper_mode_keeps_the_rest_running(self):
        self.held_paper()
        flat = ["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                "--config", self.cfg, "--state", self.state, "--now", "2026-11-13 10:30", "--qty", "20"]
        code, text, _ = run(flat)
        self.assertEqual(code, 0, text)
        self.assertIn("sells 20 of the 56 RELIANCE you hold, keeps 36 running", self.doc(self.orders)["orders"][0]["why"])
        self.assertIn("leaves the paper stop on the 36 that stay", text)
        self.write_risk("2026-11-13 10:30")
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 20 RELIANCE",
                                       now="2026-11-13 10:30"))[0], 0)
        code, text, err = run(self.paper_args("paper-fill", "--index", "1", "--price", "1340", now="2026-11-13 10:30"))
        self.assertEqual(code, 0, err)
        self.assertIn("sold 20 of the 56 RELIANCE at 1,340.00", text)
        self.assertIn("covers the 36 that stay", text)
        paper = self.doc(self.paper)["trades"]
        self.assertEqual([(t["qty"], t["status"], t.get("exit_reason")) for t in paper],
                         [(36, "filled", None), (20, "closed", "partial")])
        self.assertEqual(self.doc(self.pf)["positions"][0]["shares"], 36)
        code, text, _ = run(flat[:-2] + ["--qty", "90"])
        self.assertIn("can't sell 90 RELIANCE", text + _)


class Journal(FlowBase):
    """Item 24."""

    def journal(self, *extra, now="2026-11-13 15:30"):
        return run(["journal", "--state", self.state, "--now", now] + list(extra))

    def test_the_paper_trade_in_the_journal(self):
        self.approve_paper()
        run(self.paper_args("paper-fill", "--index", "1"))
        run(self.check_args("2026-11-13 15:30"))
        code, text, _ = self.journal()
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   56 sh  in 1,334.00 (+0.00 vs plan 1,334.00)  out 1,344.30 target "
                      "(-0.10 vs target 1,344.40)  held 9 min", text)
        self.assertIn("gross ₹576.80, charges ₹73.87, net ₹502.93, R +0.68 (planned max loss ₹739.91)  [paper]", text)
        self.assertIn("day: 1 trade, 1 win, net ₹502.93", text)
        self.assertIn("ALL DAYS: 1 trade, win rate 100%, average R +0.68, total net ₹502.93 after ₹73.87 charges.", text)
        code, text, _ = self.journal("--json")
        out = json.loads(text)
        row = out["trades"][0]
        self.assertEqual((row["symbol"], row["qty"], row["net"], row["r"], row["minutes_held"], row["entry_slippage"],
                          row["exit_vs_ref"]), ("RELIANCE", 56, 502.93, 0.68, 9, 0.0, -0.1))
        self.assertEqual(out["summary"]["win_rate_pct"], 100.0)

    def fixture_state(self):
        def appr(day, sym, fp, qty, ml, entry, stop, target):
            return {"at": day + "T10:00:00+05:30", "date": day, "status": "approved", "kind": "entry", "symbol": sym,
                    "fingerprint": fp, "qty": qty, "max_loss": ml, "plan_entry": entry, "stop": stop,
                    "plan_target": target}

        def fill(day, sym, fp, qty, price, hhmm):
            return {"at": "%sT%s:00+05:30" % (day, hhmm), "date": day, "status": "filled", "side": "buy",
                    "symbol": sym, "fingerprint": fp, "qty": qty, "price": price}

        def closed(day, sym, qty, entry, exit_, reason, hhmm):
            gross = round(qty * (exit_ - entry), 2)
            ch = nc.intraday_charges(qty, entry, exit_, nc.DEFAULT_CONFIG)["total"]
            return {"at": "%sT%s:00+05:30" % (day, hhmm), "date": day, "status": "closed", "symbol": sym, "qty": qty,
                    "entry": entry, "exit": exit_, "reason": reason, "pnl_gross": gross, "charges": ch,
                    "pnl_net": round(gross - ch, 2)}
        return {"orders": [
            appr("2026-11-12", "TCS", "a", 10, 400.0, 3000.0, 2960.0, 3080.0),
            fill("2026-11-12", "TCS", "a", 10, 3001.0, "10:00"),
            closed("2026-11-12", "TCS", 10, 3001.0, 2958.0, "stop", "11:30"),        # a loser: stop hit, 0.8 under the trigger
            appr("2026-11-13", "RELIANCE", "b", 56, 739.91, 1334.0, 1328.8, 1344.4),
            fill("2026-11-13", "RELIANCE", "b", 56, 1334.5, "10:05"),
            closed("2026-11-13", "RELIANCE", 56, 1334.5, 1344.4, "target", "10:14"),
            appr("2026-11-13", "INFY", "c", 20, 300.0, 1500.0, 1485.0, 1530.0),
            fill("2026-11-13", "INFY", "c", 20, 1500.0, "11:00"),
            closed("2026-11-13", "INFY", 20, 1500.0, 1500.0, "flat_by", "15:10"),    # a scratch
            {"at": "2026-11-13T09:00:00+05:30", "date": "2026-11-13", "status": "declined", "symbol": "SBIN"}]}

    def test_days_totals_and_the_all_days_summary(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump(self.fixture_state(), fh)
        code, text, _ = self.journal("--days", "5")
        self.assertEqual(code, 0)
        self.assertIn("TCS        10 sh  in 3,001.00 (+1.00 vs plan 3,000.00)  out 2,958.00 stop (-2.00 vs stop 2,960.00)"
                      "  held 90 min", text)
        self.assertIn("RELIANCE   56 sh  in 1,334.50 (+0.50 vs plan 1,334.00)  out 1,344.40 target "
                      "(+0.00 vs target 1,344.40)  held 9 min", text)
        self.assertIn("INFY       20 sh  in 1,500.00 (+0.00 vs plan 1,500.00)  out 1,500.00 flat_by  held 250 min", text)
        self.assertIn("day: 1 trade, 0 wins, net ", text)
        out = json.loads(self.journal("--days", "5", "--json")[1])
        self.assertEqual([d["date"] for d in out["days"]], ["2026-11-12", "2026-11-13"])
        s = out["summary"]
        self.assertEqual((s["trades"], s["wins"], s["losses"], s["win_rate_pct"]), (3, 1, 2, 33.3))
        rs = {t["symbol"]: t["r"] for t in out["trades"]}
        tcs = out["trades"][0]
        self.assertEqual(rs["TCS"], round(tcs["net"] / 400.0, 2))
        self.assertLess(rs["TCS"], -1.0)                         # lost more than the planned worst case: gap
        self.assertEqual(s["avg_r"], round(sum(rs.values()) / 3, 2))
        self.assertEqual(s["total_net"], round(sum(t["net"] for t in out["trades"]), 2))
        # --days 1 is today only
        out = json.loads(self.journal("--days", "1", "--json")[1])
        self.assertEqual({t["date"] for t in out["trades"]}, {"2026-11-13"})
        code, text, _ = self.journal("--days", "1", now="2026-11-20 10:00")
        self.assertIn("No closed trades in the last 1 day", text)

    def test_a_partial_exit_scales_the_planned_loss(self):
        st = self.fixture_state()
        st["orders"].append({"at": "2026-11-13T12:00:00+05:30", "date": "2026-11-13", "status": "closed",
                             "symbol": "RELIANCE", "qty": 28, "entry": 1334.5, "exit": 1340.0, "reason": "partial",
                             "pnl_gross": 154.0, "charges": 40.0, "pnl_net": 114.0})
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump(st, fh)
        rows = json.loads(self.journal("--days", "1", "--json")[1])["trades"]
        part = [r for r in rows if r["reason"] == "partial"][0]
        self.assertEqual((part["planned_max_loss"], part["r"]), (369.95, round(114.0 / 369.955, 2)))

    def test_old_records_without_plan_fields_still_print(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump({"orders": [{"at": "2026-11-13T10:14:00+05:30", "date": "2026-11-13", "status": "closed",
                                   "symbol": "RELIANCE", "qty": 56, "entry": 1334.0, "exit": 1344.4,
                                   "reason": "target", "pnl_gross": 582.4, "charges": 73.87, "pnl_net": 508.53}]}, fh)
        code, text, _ = self.journal()
        self.assertEqual(code, 0)
        self.assertIn("R n/a", text)
        self.assertIn("held n/a", text)


class RealRiskCheck(FlowBase):
    """One test through the real nse-risk, so the two skills are known to agree on a fingerprint and a PASS."""

    def test_a_real_pass_is_ready_then_expires(self):
        def smaller(d):
            d["created_at"] = "2026-11-13T04:35:00Z"
            for key in ("entry", "stop"):
                d["orders"][0][key]["quantity"] = 37
        self.edit_orders(smaller)
        prices = os.path.join(self.d, "prices.json")
        with open(prices, "w") as fh:
            json.dump({"as_of": "2026-11-13T04:35:00Z", "prices": {"RELIANCE": 1334.0},
                       "detail": {"RELIANCE": {"time": "2026-11-13T04:35:00Z"}}}, fh)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", prices, "--state", self.state, "--sec-list", SEC_PATH,
                               "--now", NOW, "--out", self.risk])
        self.assertEqual(code, 0)
        risk = self.doc(self.risk)
        self.assertEqual(risk["results"][0]["verdict"], "PASS")
        self.assertTrue(risk["results"][0]["checked_at"].startswith("2026-11-13T04:35"))
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0, text)
        self.assertIn("READY: nse-risk PASS", text)
        self.assertIn("BUY 37 RELIANCE", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-13 10:12"))
        self.assertEqual(code, 1)
        self.assertIn("over the 5-minute limit", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0, text)


if __name__ == "__main__":
    unittest.main()
