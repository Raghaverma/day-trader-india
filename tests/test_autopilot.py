"""The paper autopilot (autopilot/): the runner, the report, headless Claude and the email, offline.

The replay tests run the real skill scripts as subprocesses on the 13 November 2026 fixtures, so the
autopilot is held to the same RELIANCE trade as the README: 37 shares at 1,334.00, out at 1,344.30 on
the target, +328.40 net after 52.70 charges.
"""

import datetime
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import ROOT, FIX

sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import autopilot_common as ac  # noqa: E402
import claude_run  # noqa: E402
import mail  # noqa: E402
import report  # noqa: E402
import run_day  # noqa: E402

nc = ac.nc
DAY = "2026-11-13"
CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")


def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def utc(ist_text):
    return nc.parse_now(ist_text)


def quote_dir(folder, price=1334.0, ist="2026-11-13 10:04:30"):
    """A Yahoo quote for RELIANCE struck at the 10:04 bar's close, as nse-alerts would read it."""
    os.makedirs(folder, exist_ok=True)
    t = int((datetime.datetime.strptime(ist, "%Y-%m-%d %H:%M:%S") - datetime.timedelta(hours=5, minutes=30))
            .replace(tzinfo=datetime.timezone.utc).timestamp())
    doc = {"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS", "regularMarketPrice": price,
                                          "regularMarketTime": t, "chartPreviousClose": 1339.7,
                                          "previousClose": 1339.7},
                                 "timestamp": [t], "indicators": {"quote": [{"close": [price], "open": [price],
                                                                             "high": [price], "low": [price],
                                                                             "volume": [1]}]}}], "error": None}}
    with open(os.path.join(folder, "yahoo-RELIANCE.json"), "w") as fh:
        json.dump(doc, fh)


class ClaudeStub(object):
    def __init__(self, vetoes=None, analysis="## Today in one paragraph\n\nOne **paper** trade.\n\n- a point",
                 fail_eod=False):
        self.calls, self.vetoes, self.analysis, self.fail_eod = [], vetoes or [], analysis, fail_eod

    def __call__(self, kind, payload, settings, save_path=None):
        self.calls.append((kind, payload))
        if kind == "premarket":
            return {"ok": True, "vetoes": self.vetoes, "summary": "quiet morning", "error": None}
        if self.fail_eod:
            return {"ok": False, "error": "timed out after 900s"}
        return {"ok": True, "text": self.analysis, "error": None}


class MailStub(object):
    def __init__(self):
        self.sent = []

    def __call__(self, settings, rep, paths, analysis):
        self.sent.append((rep, paths, analysis))
        return True, "sent"


class HomeBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"NSE_TEST_MODE": "1", "NSE_TRADER_HOME": self.d})
        p.start()
        self.addCleanup(p.stop)
        # a test must never reach the real Claude account or Gmail: pass stubs, or fail loudly
        for target, name in ((claude_run, "ask"), (mail, "send_report")):
            g = mock.patch.object(target, name, side_effect=AssertionError("real %s.%s called in a test"
                                                                           % (target.__name__, name)))
            g.start()
            self.addCleanup(g.stop)
        shutil.copy(CFG_PATH, os.path.join(self.d, "config.json"))
        self.set_cfg(autopilot=True)
        quote_dir(os.path.join(self.d, "q"))
        self.settings = {"universe": ["RELIANCE"], "notify": False,
                         "claude": {"enabled": True}, "mail": {"enabled": True, "from": "sender", "to": ["owner"]},
                         "offline": self.offline()}
        self.write_settings()

    def tearDown(self):
        shutil.rmtree(self.d)

    def offline(self):
        return {"scan_daily": ["--offline", os.path.join(FIX, "nse-scanner", "daily-spark-nifty50-2026-11-13.json"),
                               "--lists", os.path.join(FIX, "nse-scanner")],
                "scan_orb": ["--offline", os.path.join(FIX, "nse-scanner", "wall5m-nifty50-5d-2026-11-13.json.gz"),
                             "--lists", os.path.join(FIX, "nse-scanner")],
                "news": ["--offline", os.path.join(FIX, "nse-news")],
                "entry": ["--offline", os.path.join(FIX, "nse-entry", "{sym}-2026-11-13-5m.json")],
                "timemachine": ["--offline", os.path.join(FIX, "nse-timemachine", "{sym}-60d5m.json.gz")],
                "quote": ["--offline", os.path.join(self.d, "q")],
                "risk": ["--sec-list", os.path.join(FIX, "nse-scanner", "sec_list.csv.gz")],
                "paper_fill": ["--bars", os.path.join(FIX, "nse-entry", "{sym}-2026-11-13-1m.json")],
                "paper_check": ["--bars", os.path.join(FIX, "nse-entry", "RELIANCE-2026-11-13-1m.json")],
                "shadow_bars": [os.path.join(FIX, "nse-entry", "{sym}-2026-11-13-1m.json")]}

    def set_cfg(self, **over):
        path = os.path.join(self.d, "config.json")
        with open(path) as fh:
            c = json.load(fh)
        c.update(over)
        for k, v in over.items():
            if v is None:
                c.pop(k)
        with open(path, "w") as fh:
            json.dump(c, fh)

    def write_settings(self):
        with open(os.path.join(self.d, "autopilot.json"), "w") as fh:
            json.dump(self.settings, fh)

    def day(self, ist, claude=None, send_mail=None, runner=None):
        settings = ac.load_settings(self.d)
        cfg = ac.load_cfg(self.d)
        self.claude = claude or getattr(self, "claude", None) or ClaudeStub()
        self.mail = send_mail or getattr(self, "mail", None) or MailStub()
        return run_day.Day(self.d, utc(ist), settings, cfg, runner=runner, claude=self.claude,
                           send_mail=self.mail)

    def replay(self, times=("08:45", "10:05", "10:10", "15:35")):
        for t in times:
            self.assertEqual(self.day("%s %s" % (DAY, t)).tick(), 0)

    def doc(self, *parts):
        with open(os.path.join(self.d, *parts), encoding="utf-8") as fh:
            return json.load(fh)

    def state(self):
        return ac.load_day(self.d, DAY)


class Replay(HomeBase):
    def test_the_readme_trade_end_to_end(self):
        self.replay()
        st = self.state()
        c = st["candidates"]["RELIANCE"]
        self.assertEqual(c["outcome"], "traded")
        self.assertEqual((c["trigger"]["at"], c["trigger"]["price"]), ("10:05", 1334.0))
        self.assertEqual((c["plan"]["entry"], c["plan"]["stop"], c["plan"]["target"], c["plan"]["quantity"]),
                         (1334.0, 1328.8, 1344.4, 37))
        self.assertEqual((c["tm"]["p_target"], c["tm"]["category"]), (0.2258, "Leans toward the stop"))
        self.assertEqual((c["order"]["qty"], c["order"]["bound_by"]), (37, "max loss"))
        self.assertEqual(c["risk"]["verdict"], "PASS")
        self.assertEqual(c["daily_setups"], ["breakout_20d"])

        paper = self.doc("state", "paper.json")["trades"]
        self.assertEqual(len(paper), 1)
        t = paper[0]
        self.assertEqual((t["qty"], t["fill_price"], t["exit_price"], t["exit_reason"], t["pnl_net"], t["charges"]),
                         (37, 1334.0, 1344.3, "target", 328.4, 52.7))
        appr = [r for r in self.doc("state", "broker.json")["orders"] if r.get("status") == "approved"]
        self.assertEqual([(r["approved_by"], r["reply"]) for r in appr], [("autopilot", None)])

        rep = self.doc("reports", DAY + ".json")
        self.assertEqual((rep["day"]["closed"], rep["day"]["wins"], rep["day"]["net"], rep["day"]["charges"]),
                         (1, 1, 328.4, 52.7))
        self.assertEqual(rep["trades"][0]["r"], 0.67)
        self.assertEqual(rep["trades"][0]["hit"], 1)
        self.assertEqual(rep["funnel"]["outcomes"], {"traded": 1})
        self.assertEqual(rep["time_machine"]["n"], 1)
        self.assertAlmostEqual(rep["time_machine"]["brier"], round((1 - 0.2258) ** 2, 4))
        self.assertTrue(rep["paper"])
        self.assertEqual(rep["analysis"], self.claude.analysis)
        self.assertEqual(rep["health"]["failures"], [])

        card = self.doc("reports", "scorecard.json")
        self.assertEqual((card["totals"]["days"], card["totals"]["net"], card["equity"][0]["cumulative"]),
                         (1, 328.4, 328.4))
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("+₹328.40 net after ₹52.70 charges, 1 trade (1 win, 0 losses)", md)
        self.assertIn("| RELIANCE | 37 | ₹1,334.00 | ₹1,344.30 | target | +₹328.40 | +0.67R |", md)
        self.assertIn("Not financial advice", md)
        page = text(os.path.join(self.d, "reports", DAY + ".html"))
        self.assertIn("<strong>paper</strong>", page)              # Claude's Markdown, rendered
        self.assertIn("prefers-color-scheme: dark", page)

        self.assertEqual([k for k, _ in self.claude.calls], ["premarket", "eod"])
        brief = self.claude.calls[0][1]
        self.assertEqual(brief["universe"], [{"symbol": "RELIANCE", "name": None}])
        self.assertTrue(brief["news_items"])
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(self.mail.sent[0][2], self.claude.analysis)
        ph = st["phases"]
        self.assertEqual((ph["eod"]["mail"], ph["eod"]["analysis"]), ("sent", "ok"))
        self.assertTrue(ph["eod"]["done"])

    def test_ticks_are_idempotent(self):
        self.replay(("08:45", "10:05"))
        self.day("%s 10:05" % DAY).tick()                  # the same minute again: the bar is already scanned
        self.day("%s 10:10" % DAY).tick()
        self.assertEqual(len(self.doc("state", "paper.json")["trades"]), 1)
        self.assertEqual(len([k for k, _ in self.claude.calls if k == "premarket"]), 1)
        self.day("%s 15:35" % DAY).tick()
        self.day("%s 15:40" % DAY).tick()
        self.assertEqual(len(self.mail.sent), 1)

    def test_a_veto_is_planned_then_replayed_as_a_shadow(self):
        self.claude = ClaudeStub(vetoes=[{"symbol": "RELIANCE", "reason": "results today"},
                                         {"symbol": "NOTINUNIVERSE", "reason": "x"}])
        self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual(c["outcome"], "vetoed")
        self.assertIn("results today", c["reason"])
        self.assertEqual(self.doc("autopilot", DAY, "vetoes.json")["vetoes"],
                         [{"symbol": "RELIANCE", "reason": "results today"}])
        self.assertEqual(self.doc("state", "paper.json")["trades"], [])
        rep = self.doc("reports", DAY + ".json")
        row = rep["shadows"]["rows"][0]
        # the same rules on the same bars: the target, the same +328.40 the trade made
        self.assertEqual((row["why"], row["result"], row["fill"], row["exit"], row["net"]),
                         ("vetoed", "target", 1334.0, 1344.3, 328.4))
        self.assertEqual(rep["shadows"]["by_why"]["vetoed"]["net"], 328.4)
        self.assertEqual(rep["day"]["closed"], 0)

    def test_after_the_trade_cap_a_signal_is_a_shadow(self):
        os.makedirs(os.path.join(self.d, "state"))
        with open(os.path.join(self.d, "state", "broker.json"), "w") as fh:
            json.dump({"orders": [{"date": DAY, "status": "approved", "kind": "entry", "symbol": s}
                                  for s in ("TCS", "INFY", "SBIN")]}, fh)
        self.replay()
        c = self.state()["candidates"]["RELIANCE"]
        self.assertEqual(c["outcome"], "shadow")
        self.assertIn("cap of 3 trades", c["reason"])
        self.assertNotIn("tm", c)                                   # no Time Machine for a trade that can't happen
        rep = self.doc("reports", DAY + ".json")
        self.assertEqual(rep["shadows"]["rows"][0]["result"], "target")

    def test_claude_failing_never_stops_the_report(self):
        self.claude = ClaudeStub(fail_eod=True)
        self.replay()
        rep = self.doc("reports", DAY + ".json")
        self.assertNotIn("analysis", rep)
        md = text(os.path.join(self.d, "reports", DAY + ".md"))
        self.assertIn("Claude's analysis is unavailable today", md)
        self.assertEqual(len(self.mail.sent), 1)
        self.assertTrue(self.state()["phases"]["eod"]["analysis"].startswith("failed"))

    def test_a_failing_end_of_day_gives_up_after_three_tries(self):
        self.replay(("08:45", "10:05"))
        with mock.patch.object(report, "build", side_effect=RuntimeError("disk full")):
            for t in ("15:35", "15:36", "15:37", "15:38", "15:39"):
                self.day("%s %s" % (DAY, t)).tick()
        eod = self.state()["phases"]["eod"]
        self.assertEqual(eod["attempts"], 3)
        self.assertIn("RuntimeError: disk full", eod["error"])
        self.assertTrue(eod["done"])
        self.assertEqual([k for k, _ in self.claude.calls], ["premarket"])   # never a Claude run per minute
        self.assertEqual(self.mail.sent, [])

    def test_a_missed_report_is_caught_up_the_next_day(self):
        self.replay(("08:45", "10:05"))                    # the Mac went off before 15:35
        self.assertEqual(run_day.unfinished_day(self.d, "2026-11-16"), DAY)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(claude_run, "ask", self.claude), \
                mock.patch.object(mail, "send_report", self.mail):
            self.assertEqual(run_day.main(["tick", "--now", "2026-11-16 07:00"]), 0)
        self.assertEqual([k for k, _ in self.claude.calls], ["premarket", "eod"])
        self.assertEqual(len(self.mail.sent), 1)
        self.assertTrue(self.state()["phases"]["eod"]["done"])
        rep = self.doc("reports", DAY + ".json")
        self.assertEqual(rep["day"]["net"], 328.4)          # paper-check closed it on that day's own bars
        self.assertIsNone(rep["benchmark"])
        self.assertIsNone(run_day.unfinished_day(self.d, "2026-11-16"))

    def test_a_late_start_skips_the_veto_but_still_trades(self):
        self.replay(("10:05", "10:10", "15:35"))
        ph = self.state()["phases"]
        self.assertIn("skipped", ph["premarket"]["veto"])
        self.assertEqual([k for k, _ in self.claude.calls], ["eod"])
        self.assertEqual(self.state()["candidates"]["RELIANCE"]["outcome"], "traded")


class Guards(HomeBase):
    def tick(self, now="2026-11-13 10:05"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_day.main(["tick", "--now", now])
        return code, out.getvalue(), err.getvalue()

    def test_refuses_anything_but_paper(self):
        for mode in ("manual", "kite"):
            self.set_cfg(broker=mode)
            code, _, err = self.tick()
            self.assertEqual(code, 2, mode)
            self.assertIn("paper only", err)
        self.assertFalse(os.path.exists(os.path.join(self.d, "autopilot")))

    def test_refuses_without_the_switch(self):
        for val in (None, "true", 1):
            self.set_cfg(autopilot=val)
            code, _, err = self.tick()
            self.assertEqual(code, 2)
            self.assertIn("autopilot is off", err)

    def test_offline_and_now_are_test_only(self):
        with mock.patch.dict(os.environ, {"NSE_TEST_MODE": ""}):
            with self.assertRaises(ac.AutopilotError):
                ac.load_settings(self.d)
            with self.assertRaises(ac.AutopilotError):
                ac.parse_clock("2026-11-13 10:05")

    def test_a_weekend_runs_nothing(self):
        calls = []
        runner = ac.Runner(self.d, "2026-11-14", ac.load_settings(self.d), None,
                           execute=lambda argv, timeout: calls.append(argv) or (0, "", ""))
        self.assertEqual(self.day("2026-11-14 10:05", runner=runner).tick(), 0)
        self.assertEqual(calls, [])
        self.assertEqual(ac.load_day(self.d, "2026-11-14")["phases"]["closed"], "Saturday")


class Pieces(unittest.TestCase):
    def test_orb_is_due_a_minute_after_each_bar(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)

        def due(ist, last=None):
            day = run_day.Day.__new__(run_day.Day)
            day.ist = datetime.datetime.strptime("2026-11-13 " + ist, "%Y-%m-%d %H:%M")
            day.settings = dict(ac.DEFAULTS)
            day.st = {"phases": {"orb_last_bar": last} if last else {}}
            return day.orb_due()
        self.assertIsNone(due("09:35"))
        self.assertEqual(due("09:36"), "09:35")
        self.assertIsNone(due("09:39", "09:35"))
        self.assertEqual(due("09:41", "09:35"), "09:40")
        self.assertEqual(due("14:31"), "14:30")
        self.assertIsNone(due("14:32"))

    def test_refusals_read_without_file_paths(self):
        self.assertEqual(run_day.plain_reason("exit.py: /Users/a b/Trading/x/entries/TCS.json: the TCS trigger is 3.9 "
                                              "hours old"), "exit.py: the TCS trigger is 3.9 hours old")
        self.assertEqual(run_day.plain_reason("ticket.py: order 1 is not ready: BLOCKED (1/2)"),
                         "ticket.py: order 1 is not ready: BLOCKED (1/2)")

    def test_clean_vetoes(self):
        got = run_day.clean_vetoes([{"symbol": "reliance", "reason": "r"}, {"symbol": "RELIANCE"}, "junk",
                                    {"symbol": "ZZZ"}, {"reason": "no symbol"}], ["RELIANCE", "TCS"])
        self.assertEqual(got, [{"symbol": "RELIANCE", "reason": "r"}])

    def test_calibration(self):
        c = report.calibration([(0.2, 1), (0.2, 0), (0.6, 1), (None, 1), (0.3, None)])
        self.assertEqual(c["n"], 3)
        self.assertAlmostEqual(c["brier"], round(((0.2 - 1) ** 2 + 0.2 ** 2 + 0.4 ** 2) / 3, 4))
        self.assertAlmostEqual(c["hit_rate"], round(2 / 3.0, 4))
        self.assertEqual([b["n"] for b in c["buckets"]], [2, 1])
        self.assertEqual(report.calibration([]), {"n": 0})
        self.assertEqual(report.max_drawdown([100, 50, 120, 20, 60]), -100)

    def test_markdown_to_html(self):
        page = report.md_to_html("## Head\n\nA **bold** line\nwrapped.\n\n- one\n- two\n\n1. first\n<script>")
        self.assertIn("<h4>Head</h4>", page)
        self.assertIn("<p>A <strong>bold</strong> line wrapped.</p>", page)
        self.assertIn("<ul><li>one</li><li>two</li></ul>", page)
        self.assertIn("<ol><li>first</li></ol>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_last_json_object(self):
        text = 'Quiet.\n```json\n{"vetoes": [{"symbol": "TCS", "reason": "results"}], "summary": "s"}\n```'
        self.assertEqual(claude_run.last_json_object(text)["vetoes"][0]["symbol"], "TCS")
        self.assertEqual(claude_run.last_json_object('x {"vetoes": []} y'), {"vetoes": []})
        self.assertIsNone(claude_run.last_json_object("no json here"))

    def test_claude_gets_no_tools_and_no_trader_folder(self):
        argv = claude_run.argv_for({"claude": {"bin": "claude", "eod_model": "opus"}}, "eod")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        with mock.patch.dict(os.environ, {"NSE_TRADER_HOME": "/x", "NSE_TEST_MODE": "1"}):
            env = claude_run.env_for({"claude": {}})
        self.assertNotIn("NSE_TRADER_HOME", env)
        self.assertNotIn("NSE_TEST_MODE", env)


class FakeClaudeBinary(unittest.TestCase):
    """claude_run.ask against a stand-in `claude` that answers like `claude -p --output-format json`."""

    def fake(self, result, is_error=False, raw=None):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "claude")
        body = raw if raw is not None else json.dumps({"type": "result", "is_error": is_error, "result": result,
                                                       "total_cost_usd": 0.01, "num_turns": 1})
        with open(path, "w") as fh:
            fh.write("#!%s\nimport sys\nsys.stdin.read()\nprint(%r)\n" % (sys.executable, body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return {"claude": {"bin": path, "premarket_timeout_s": 30, "eod_timeout_s": 30}}, d

    def test_premarket_and_eod(self):
        settings, d = self.fake('Fine.\n```json\n{"vetoes": [{"symbol": "TCS", "reason": "results"}], '
                                '"summary": "TCS reports today"}\n```')
        with mock.patch.object(claude_run, "workdir", lambda: d):
            out = claude_run.ask("premarket", {"universe": []}, settings, os.path.join(d, "saved.json"))
        self.assertEqual((out["ok"], out["vetoes"][0]["symbol"], out["summary"]), (True, "TCS", "TCS reports today"))
        saved = json.loads(text(os.path.join(d, "saved.json")))
        self.assertEqual((saved["ok"], saved["total_cost_usd"]), (True, 0.01))
        settings, d = self.fake("## Today\n\nFine.")
        with mock.patch.object(claude_run, "workdir", lambda: d):
            out = claude_run.ask("eod", {}, settings)
        self.assertEqual((out["ok"], out["text"]), (True, "## Today\n\nFine."))

    def test_failures_are_reported_not_raised(self):
        settings, d = self.fake("", raw="not json at all")
        with mock.patch.object(claude_run, "workdir", lambda: d):
            out = claude_run.ask("eod", {}, settings)
        self.assertFalse(out["ok"])
        settings, d = self.fake("Credit balance is too low", is_error=True)
        with mock.patch.object(claude_run, "workdir", lambda: d):
            out = claude_run.ask("premarket", {}, settings)
        self.assertFalse(out["ok"])
        self.assertIn("Credit balance", out["error"])
        settings, d = self.fake("I would rather not.")
        with mock.patch.object(claude_run, "workdir", lambda: d):
            out = claude_run.ask("premarket", {}, settings)
        self.assertFalse(out["ok"])
        settings = {"claude": {"bin": os.path.join(d, "missing"), "eod_timeout_s": 5}}
        with mock.patch.object(claude_run, "workdir", lambda: d):
            out = claude_run.ask("eod", {}, settings)
        self.assertIn("could not run claude", out["error"])


class Mail(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.rep = {"date": DAY, "day": {"net": 328.4, "charges": 52.7, "closed": 1, "wins": 1, "losses": 0},
                    "funnel": {"signals": 1}, "benchmark": None}
        self.paths = {}
        for k, name, text in (("md", DAY + ".md", "# report"), ("html", DAY + ".html", "<p>report</p>"),
                              ("analysis", DAY + ".analysis.md", "## analysis")):
            self.paths[k] = os.path.join(self.d, name)
            with open(self.paths[k], "w") as fh:
                fh.write(text)
        self.settings = {"mail": {"enabled": True, "from": "sender", "to": ["a", "b"]}}

    def test_the_message(self):
        msg = mail.build_message(self.settings, self.rep, self.paths)
        self.assertEqual(msg["Subject"], "[PAPER] NSE autopilot 2026-11-13: +₹328.40 net after ₹52.70 charges, "
                                         "1 trade (1 win, 0 losses), 1 ORB signal")
        self.assertEqual(msg["To"], "a, b")
        names = [p.get_filename() for p in msg.iter_attachments()]
        self.assertEqual(names, [DAY + ".md", DAY + ".analysis.md"])
        self.assertIn("<p>report</p>", msg.get_body(("html",)).get_content())

    def test_send_with_a_fake_gmail(self):
        sent = []

        class FakeSMTP(object):
            def __init__(self, host, port, context=None, timeout=None):
                self.host, self.port = host, port

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, user, pw):
                sent.append(("login", user, pw))

            def send_message(self, msg):
                sent.append(("send", msg["Subject"]))
        ok, why = mail.send_report(self.settings, self.rep, self.paths, smtp=FakeSMTP, lookup=lambda s, a: "app-pw")
        self.assertTrue(ok, why)
        self.assertEqual(sent[0], ("login", "sender", "app-pw"))
        self.assertTrue(sent[1][1].startswith("[PAPER]"))
        ok, why = mail.send_report(self.settings, self.rep, self.paths, smtp=FakeSMTP, lookup=lambda s, a: None)
        self.assertFalse(ok)
        self.assertIn("no app password in the keychain", why)
        ok, why = mail.send_report({"mail": {"enabled": True}}, self.rep, self.paths, smtp=FakeSMTP,
                                   lookup=lambda s, a: "pw")
        self.assertFalse(ok)
        self.assertIn("no \"from\" address", why)


if __name__ == "__main__":
    unittest.main()
