"""The plugin hooks: hooks/hooks.json wiring and the three scripts (gate, record_prompt, session_status).

The scripts are thin wrappers around nse-broker/ticket.py (gate_check, record_user_prompt,
status_report). Most tests inject a stub ticket through the scripts' `load=` argument, so they
do not depend on ticket.py; the subprocess tests copy the hooks folder next to a fake
nse-broker/ticket.py (the same relative layout as ${CLAUDE_PLUGIN_ROOT}); the last class checks
the real ticket.py against the contract and skips until it exposes the three functions.
"""

import ast
import contextlib
import datetime
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from _load import load, ROOT

gate = load("hooks", "gate")
record_prompt = load("hooks", "record_prompt")
session_status = load("hooks", "session_status")

HOOKS_DIR = os.path.join(ROOT, "hooks")
SCRIPTS = ("gate", "record_prompt", "session_status")
ORDER_TOOLS = ("place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order")
KITE_ORDER = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "quantity": 37, "product": "MIS", "order_type": "LIMIT", "price": 1334.0, "validity": "DAY",
              "tag": "dtindia"}


def pretool(tool_name="mcp__kite__place_order", tool_input=KITE_ORDER, **extra):
    """A PreToolUse payload in the shape Claude Code documents."""
    doc = {"session_id": "abc123", "transcript_path": "/tmp/t.jsonl", "cwd": ROOT, "permission_mode": "default",
           "hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input,
           "tool_use_id": "toolu_01ABC"}
    doc.update(extra)
    return json.dumps(doc)


def prompt_payload(prompt, **extra):
    doc = {"session_id": "abc123", "transcript_path": "/tmp/t.jsonl", "cwd": ROOT, "permission_mode": "default",
           "hook_event_name": "UserPromptSubmit", "prompt": prompt}
    doc.update(extra)
    return json.dumps(doc)


def start_payload(source="startup", **extra):
    doc = {"session_id": "abc123", "transcript_path": "/tmp/t.jsonl", "cwd": ROOT,
           "hook_event_name": "SessionStart", "source": source, "model": "claude-opus-5"}
    doc.update(extra)
    return json.dumps(doc)


class FakeTicket(object):
    """Stands in for nse-broker/ticket.py: records its calls, answers as told."""

    def __init__(self, verdict=(True, ""), raises=None, status="", noise="", status_raises=None):
        self.verdict = verdict
        self.raises = raises
        self.status = status
        self.noise = noise
        self.status_raises = status_raises
        self.gate_calls = []
        self.prompts = []
        self.status_calls = []
        self.cwds = []

    def gate_check(self, tool_name, tool_input, state_path=None, config_path=None, now_utc=None):
        self.gate_calls.append((tool_name, tool_input, state_path, config_path, now_utc))
        self.cwds.append(os.getcwd())
        if self.noise:
            print(self.noise)
        if self.raises is not None:
            raise self.raises
        return self.verdict

    def record_user_prompt(self, text, path=None, now_utc=None):
        self.prompts.append((text, path, now_utc))
        self.cwds.append(os.getcwd())
        if self.noise:
            print(self.noise)
        if self.raises is not None:
            raise self.raises
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"text": text, "at": "2026-11-18T05:00:00Z"}, fh)

    def status_report(self, state_path=None, portfolio_path=None, risk_path=None, config_path=None,
                      now_utc=None, quiet=False):
        self.status_calls.append({"quiet": quiet})
        self.cwds.append(os.getcwd())
        if self.noise:
            print(self.noise)
        if self.status_raises is not None:
            raise self.status_raises
        return self.status


def run_gate(stdin_text, ticket=None, load_fn=None, **kw):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = gate.main(stdin_text, out=out, load=load_fn or (lambda: ticket), **kw)
    return code, out.getvalue(), err.getvalue()


def raiser(exc):
    def fn():
        raise exc
    return fn


class Wiring(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HOOKS_DIR, "hooks.json"), encoding="utf-8") as fh:
            self.doc = json.load(fh)
        self.events = self.doc["hooks"]

    def test_plugin_hooks_file_shape(self):
        # a plugin hooks file wraps the event map in a top-level "hooks" key
        self.assertIn("hooks", self.doc)
        self.assertEqual(sorted(self.events), ["PreToolUse", "SessionStart", "UserPromptSubmit"])
        for event, groups in self.events.items():
            self.assertIsInstance(groups, list, event)
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command", event)
                    self.assertIsInstance(hook["timeout"], int, event)
                    self.assertGreater(hook["timeout"], 0, event)

    def test_each_event_runs_its_script_from_the_plugin_root(self):
        expect = {"PreToolUse": "gate", "UserPromptSubmit": "record_prompt", "SessionStart": "session_status"}
        for event, name in expect.items():
            commands = [h["command"] for g in self.events[event] for h in g["hooks"]]
            self.assertEqual(commands, ['python3 "${CLAUDE_PLUGIN_ROOT}/hooks/%s.py"' % name], event)
            self.assertTrue(os.path.isfile(os.path.join(HOOKS_DIR, name + ".py")), name)

    def test_the_gate_blocks_the_order_when_it_fails(self):
        # without onFailure a gate that crashes, times out or cannot start lets the order through
        hook = self.events["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["onFailure"], "block")
        for event in ("UserPromptSubmit", "SessionStart"):
            for g in self.events[event]:
                for h in g["hooks"]:
                    self.assertNotEqual(h.get("onFailure"), "block", event)  # these must never block the user

    def matcher(self):
        groups = self.events["PreToolUse"]
        self.assertEqual(len(groups), 1)
        return groups[0]["matcher"]

    def test_matcher_is_a_regex_not_an_exact_string(self):
        # Claude Code treats a matcher made only of letters, digits, _ - space , | as exact names
        self.assertIsNone(re.fullmatch(r"[A-Za-z0-9_\- ,|]*", self.matcher()))

    def test_matcher_catches_the_five_order_tools_on_any_server(self):
        rx = re.compile(self.matcher())
        for server in ("kite", "zerodha", "my-kite", "kite_prod", "plugin_x_kite", "a__b", "KITE"):
            for tool in ORDER_TOOLS:
                name = "mcp__%s__%s" % (server, tool)
                self.assertTrue(rx.search(name), name)

    def test_matcher_leaves_everything_else_alone(self):
        rx = re.compile(self.matcher())
        for name in ("mcp__alpaca__place_stock_order", "Bash", "Read", "Write", "Edit", "Task",
                     "mcp__kite__get_orders", "mcp__kite__get_positions", "mcp__kite__get_holdings",
                     "mcp__kite__get_quotes", "mcp__kite__get_order_history", "mcp__kite__delete_gtt_order",
                     "mcp__figma__use_figma", "place_order"):
            self.assertIsNone(rx.search(name), name)

    def test_scripts_compile_and_are_importable(self):
        for name in SCRIPTS:
            path = os.path.join(HOOKS_DIR, name + ".py")
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            compile(src, path, "exec")
            self.assertIn("def main(", src)
            self.assertIn('if __name__ == "__main__":', src)

    def test_scripts_use_only_the_standard_library(self):
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("needs Python 3.10+ for sys.stdlib_module_names")
        std = set(sys.stdlib_module_names)
        for name in SCRIPTS:
            with open(os.path.join(HOOKS_DIR, name + ".py"), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    self.assertIn(m, std, "%s imports %s" % (name, m))

    def test_hooks_folder_holds_only_the_four_files(self):
        found = sorted(f for f in os.listdir(HOOKS_DIR) if not f.startswith((".", "__")))
        self.assertEqual(found, ["gate.py", "hooks.json", "record_prompt.py", "session_status.py"])


class GateDecisions(unittest.TestCase):
    def assert_denied(self, out, reason_part=None):
        doc = json.loads(out)  # exactly one JSON document on stdout
        self.assertEqual(list(doc), ["hookSpecificOutput"])
        inner = doc["hookSpecificOutput"]
        self.assertEqual(sorted(inner), ["hookEventName", "permissionDecision", "permissionDecisionReason"])
        self.assertEqual(inner["hookEventName"], "PreToolUse")
        self.assertEqual(inner["permissionDecision"], "deny")
        self.assertTrue(inner["permissionDecisionReason"].strip())
        if reason_part:
            self.assertIn(reason_part, inner["permissionDecisionReason"])
        return inner["permissionDecisionReason"]

    def test_allow_prints_nothing(self):
        # silence (not an "allow") so the user's own Needs approval setting still asks
        for reason in ("", "approved leg RELIANCE:entry"):
            fake = FakeTicket(verdict=(True, reason))
            code, out, err = run_gate(pretool(), fake)
            self.assertEqual((code, out, err), (0, "", ""))
            self.assertEqual(len(fake.gate_calls), 1)

    def test_allow_never_prints_an_allow_or_approve_decision(self):
        fake = FakeTicket(verdict=(True, "approved"))
        _, out, _ = run_gate(pretool(), fake)
        self.assertNotIn("allow", out.lower())
        self.assertNotIn("approve", out.lower())

    def test_the_tool_call_reaches_gate_check_unchanged(self):
        fake = FakeTicket()
        run_gate(pretool("mcp__kite__modify_order", dict(KITE_ORDER, order_id="2411180001")), fake,
                 state_path="/s.json", config_path="/c.json", now_utc="NOW")
        tool, tin, state, cfg, now = fake.gate_calls[0]
        self.assertEqual(tool, "mcp__kite__modify_order")
        self.assertEqual(tin, dict(KITE_ORDER, order_id="2411180001"))
        self.assertEqual((state, cfg, now), ("/s.json", "/c.json", "NOW"))

    def test_optional_arguments_are_not_passed_when_unset(self):
        class Strict(object):
            seen = None

            def gate_check(self, tool_name, tool_input):
                Strict.seen = (tool_name, tool_input)
                return True, ""
        code, out, _ = run_gate(pretool(), Strict())
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(Strict.seen[0], "mcp__kite__place_order")

    def test_deny_from_ticket_has_the_documented_shape(self):
        fake = FakeTicket(verdict=(False, "no approved ticket matches RELIANCE BUY 37: say yes first"))
        code, out, _ = run_gate(pretool(), fake)
        self.assertEqual(code, 0)  # the decision is the JSON, the exit code stays 0
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(out.count("\n"), 1)
        reason = self.assert_denied(out, "no approved ticket matches RELIANCE BUY 37")
        self.assertNotIn("\n", reason)

    def test_every_order_tool_is_passed_through_to_the_ticket(self):
        for tool in ORDER_TOOLS:
            fake = FakeTicket(verdict=(False, "nope"))
            _, out, _ = run_gate(pretool("mcp__kite__" + tool), fake)
            self.assert_denied(out, "nope")
            self.assertEqual(fake.gate_calls[0][0], "mcp__kite__" + tool)

    def test_a_deny_without_a_reason_still_says_why(self):
        for verdict in ((False, ""), (False, None), (False, "  ")):
            _, out, _ = run_gate(pretool(), FakeTicket(verdict=verdict))
            self.assert_denied(out)

    def test_long_reasons_are_trimmed_and_unicode_survives(self):
        _, out, _ = run_gate(pretool(), FakeTicket(verdict=(False, "stop \u20b91,322.10 " + "x" * 5000)))
        reason = self.assert_denied(out, "\u20b91,322.10")
        self.assertLessEqual(len(reason), gate.MAX_REASON)
        out.encode("ascii")  # ensure_ascii keeps stdout safe on any terminal encoding

    def test_only_an_exact_true_allows(self):
        for verdict in (None, "allow", (), (True,), (True, "a", "b"), ("yes", ""), (1, ""), ([], ""), 7,
                        {"allow": True}, (None, "x"), ("True", "x")):
            fake = FakeTicket(verdict=verdict)
            _, out, _ = run_gate(pretool(), fake)
            self.assert_denied(out)

    def test_list_verdict_from_json_style_returns_is_accepted(self):
        _, out, _ = run_gate(pretool(), FakeTicket(verdict=[True, "ok"]))
        self.assertEqual(out, "")

    # fail closed ---------------------------------------------------------------------------

    def test_unparsable_stdin_denies(self):
        for text in ("", "   ", "not json", "{", "[]", "[1, 2]", "null", "7", '"mcp__kite__place_order"', "{}",
                     b"\xff\xfe\x00", b"", None):
            fake = FakeTicket()
            code, out, _ = run_gate(text, fake)
            self.assertEqual(code, 0, repr(text))
            self.assert_denied(out)
            self.assertEqual(fake.gate_calls, [], "ticket must not be asked about %r" % (text,))

    def test_a_payload_without_a_usable_tool_name_or_input_denies(self):
        bad = [
            {"tool_input": KITE_ORDER},
            {"tool_name": "", "tool_input": KITE_ORDER},
            {"tool_name": 5, "tool_input": KITE_ORDER},
            {"tool_name": None, "tool_input": KITE_ORDER},
            {"tool_name": "mcp__kite__place_order"},
            {"tool_name": "mcp__kite__place_order", "tool_input": None},
            {"tool_name": "mcp__kite__place_order", "tool_input": "RELIANCE"},
            {"tool_name": "mcp__kite__place_order", "tool_input": [KITE_ORDER]},
        ]
        for doc in bad:
            fake = FakeTicket()
            _, out, _ = run_gate(json.dumps(doc), fake)
            self.assert_denied(out)
            self.assertEqual(fake.gate_calls, [], doc)

    def test_a_tool_that_is_not_an_order_tool_is_denied_without_asking_the_ticket(self):
        for name in ("Bash", "mcp__alpaca__place_stock_order", "place_order", "mcp__kite__place_order_v2",
                     "mcp__kite__delete_order", "mcp____place_order", "mcp__kite__"):
            fake = FakeTicket()
            _, out, _ = run_gate(pretool(name), fake)
            self.assert_denied(out, "not a recognised order tool")
            self.assertEqual(fake.gate_calls, [], name)

    def test_missing_ticket_module_denies(self):
        for exc in (ImportError("no ticket.py"), ModuleNotFoundError("No module named 'nse_common'"),
                    FileNotFoundError("ticket.py"), SyntaxError("bad ticket.py"), SystemExit(2),
                    RuntimeError("boom")):
            _, out, _ = run_gate(pretool(), load_fn=raiser(exc))
            reason = self.assert_denied(out, "blocked")
            self.assertIn(type(exc).__name__, reason)

    def test_default_loader_with_no_ticket_py_in_the_broker_folder_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err):
                code = gate.main(pretool(), out=out, broker_dir=tmp)
            self.assertEqual(code, 0)
            self.assert_denied(out.getvalue(), "no ticket.py")

    def test_default_loader_with_a_broken_ticket_py_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ticket.py"), "w") as fh:
                fh.write("import module_that_is_not_installed_anywhere\n")
            out = io.StringIO()
            code = gate.main(pretool(), out=out, broker_dir=tmp)
            self.assertEqual(code, 0)
            self.assert_denied(out.getvalue(), "ModuleNotFoundError")

    def test_default_loader_with_a_ticket_that_lacks_gate_check_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ticket.py"), "w") as fh:
                fh.write("VERSION = 1\n")
            out = io.StringIO()
            gate.main(pretool(), out=out, broker_dir=tmp)
            self.assert_denied(out.getvalue(), "AttributeError")

    def test_an_exception_in_gate_check_denies(self):
        for exc in (ValueError("corrupt state"), KeyError("legs"), TypeError("x"), OSError("disk"),
                    json.JSONDecodeError("bad", "{", 1), SystemExit(1), RecursionError("deep")):
            fake = FakeTicket(raises=exc)
            code, out, _ = run_gate(pretool(), fake)
            self.assertEqual(code, 0)
            reason = self.assert_denied(out, "blocked")
            self.assertIn(type(exc).__name__, reason)

    def test_stray_output_from_ticket_never_reaches_stdout(self):
        # a stray print would make stdout unparsable, and Claude Code lets the call through then
        fake = FakeTicket(verdict=(False, "no approval"), noise="DEBUG loading state")
        _, out, err = run_gate(pretool(), fake)
        self.assert_denied(out, "no approval")
        self.assertIn("DEBUG loading state", err)
        fake = FakeTicket(verdict=(True, ""), noise="DEBUG loading state")
        _, out, err = run_gate(pretool(), fake)
        self.assertEqual(out, "")
        fake = FakeTicket(raises=ValueError("x"), noise="half a line")
        _, out, _ = run_gate(pretool(), fake)
        self.assert_denied(out)

    def test_runs_in_the_directory_claude_is_working_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            before = os.getcwd()
            fake = FakeTicket()
            run_gate(pretool(cwd=tmp), fake)
            self.assertEqual(os.path.realpath(fake.cwds[0]), tmp)
            self.assertEqual(os.getcwd(), before)  # and puts it back
            fake = FakeTicket(raises=ValueError("x"))
            run_gate(pretool(cwd=tmp), fake)
            self.assertEqual(os.getcwd(), before)

    def test_a_missing_or_bogus_cwd_is_ignored(self):
        for cwd in (None, "", 7, "relative/dir", "/definitely/not/a/folder"):
            fake = FakeTicket()
            _, out, _ = run_gate(pretool(cwd=cwd), fake)
            self.assertEqual(out, "", repr(cwd))
            self.assertEqual(os.path.realpath(fake.cwds[0]), os.path.realpath(os.getcwd()))


class RecordPrompt(unittest.TestCase):
    def run_hook(self, stdin_text, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = record_prompt.main(stdin_text, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_stores_the_prompt_text(self):
        fake = FakeTicket()
        code, out, err = self.run_hook(prompt_payload("yes, place it"), load=lambda: fake)
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(fake.prompts, [("yes, place it", None, None)])

    def test_writes_last_prompt_json_through_the_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "last_prompt.json")
            os.makedirs(os.path.dirname(path))
            code, out, _ = self.run_hook(prompt_payload("yes"), load=lambda: FakeTicket(), path=path,
                                         now_utc="2026-11-18T05:00:00Z")
            self.assertEqual((code, out), (0, ""))
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["text"], "yes")
            self.assertIn("at", doc)

    def test_the_prompt_is_stored_verbatim(self):
        for text in ("", " yes ", "yes\nfor RELIANCE", "\u20b91,334 \u2014 buy", "/day-trader-india:nse-broker",
                     'say "yes"', "x" * 20000, "<pasted_content id=\"1\">\nyes\n</pasted_content id=\"1\">"):
            fake = FakeTicket()
            self.run_hook(prompt_payload(text), load=lambda: fake)
            self.assertEqual(fake.prompts[0][0], text)

    def test_an_empty_prompt_is_still_recorded_so_an_old_yes_cannot_linger(self):
        fake = FakeTicket()
        self.run_hook(prompt_payload(""), load=lambda: fake)
        self.assertEqual(len(fake.prompts), 1)

    def test_never_raises_and_never_prints_on_garbage(self):
        for text in ("", "   ", "garbage", "{", "[]", "null", "7", '"yes"', "{}", '{"prompt": 5}',
                     '{"prompt": null}', '{"prompt": ["yes"]}', '{"prompt": {"a": 1}}', b"\xff\xfe", b"", None,
                     '{"hook_event_name": "UserPromptSubmit"}'):
            fake = FakeTicket()
            code, out, err = self.run_hook(text, load=lambda: fake)
            self.assertEqual((code, out, err), (0, "", ""), repr(text))
            self.assertEqual(fake.prompts, [], "nothing to record for %r" % (text,))

    def test_never_raises_when_the_ticket_is_missing_or_broken(self):
        for exc in (ImportError("x"), FileNotFoundError("ticket.py"), SyntaxError("bad"), SystemExit(3),
                    RuntimeError("boom"), ValueError("v")):
            code, out, err = self.run_hook(prompt_payload("yes"), load=raiser(exc))
            self.assertEqual((code, out, err), (0, "", ""), repr(exc))
            fake = FakeTicket(raises=exc)
            code, out, err = self.run_hook(prompt_payload("yes"), load=lambda: fake)
            self.assertEqual((code, out, err), (0, "", ""), repr(exc))

    def test_default_loader_with_no_ticket_py_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_hook(prompt_payload("yes"), broker_dir=tmp), (0, "", ""))

    def test_stdout_stays_empty_even_if_the_ticket_prints(self):
        # plain stdout from a UserPromptSubmit hook is added to Claude's context
        code, out, err = self.run_hook(prompt_payload("yes"), load=lambda: FakeTicket(noise="saved prompt"))
        self.assertEqual((code, out, err), (0, "", ""))

    def test_runs_in_the_directory_claude_is_working_in_and_restores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            before = os.getcwd()
            fake = FakeTicket()
            self.run_hook(prompt_payload("yes", cwd=tmp), load=lambda: fake)
            self.assertEqual(os.path.realpath(fake.cwds[0]), tmp)
            self.assertEqual(os.getcwd(), before)


class SessionStatus(unittest.TestCase):
    def run_hook(self, stdin_text="", **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = session_status.main(stdin_text, out=out, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_silent_when_there_is_nothing_to_say(self):
        for status in ("", "   ", "\n", None, 0, [], ["x"]):
            fake = FakeTicket(status=status)
            code, out, err = self.run_hook(start_payload(), load=lambda: fake)
            self.assertEqual((code, out, err), (0, "", ""), repr(status))
            self.assertEqual(fake.status_calls, [{"quiet": True}])  # quiet: no "all clear" line

    def test_silent_when_the_ticket_is_missing_or_broken(self):
        for exc in (ImportError("x"), FileNotFoundError("ticket.py"), SystemExit(1), RuntimeError("boom")):
            self.assertEqual(self.run_hook(start_payload(), load=raiser(exc)), (0, "", ""), repr(exc))
            fake = FakeTicket(status_raises=exc)
            self.assertEqual(self.run_hook(start_payload(), load=lambda: fake), (0, "", ""), repr(exc))

    def test_default_loader_with_no_ticket_py_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_hook(start_payload(), broker_dir=tmp), (0, "", ""))

    def test_prints_the_report_as_session_start_context_when_there_is_one(self):
        text = "RELIANCE: entry filled 37 shares at 10:12 but no stop is recorded.\nRun nse-broker status."
        code, out, err = self.run_hook(start_payload(), load=lambda: FakeTicket(status=text))
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        inner = doc["hookSpecificOutput"]
        self.assertEqual(inner["hookEventName"], "SessionStart")
        self.assertEqual(inner["additionalContext"], text)
        self.assertEqual(doc["systemMessage"], text)  # shown to the user too
        self.assertEqual(out.count("\n"), 1)

    def test_every_session_start_source_runs_the_check(self):
        for source in ("startup", "resume", "clear", "compact", "fork"):
            fake = FakeTicket(status="TCS has no stop.")
            _, out, _ = self.run_hook(start_payload(source), load=lambda: fake)
            self.assertIn("TCS has no stop.", out, source)

    def test_the_payload_is_optional(self):
        for text in ("", "garbage", "[]", None, b"\xff"):
            fake = FakeTicket(status="TCS has no stop.")
            _, out, _ = self.run_hook(text, load=lambda: fake)
            self.assertIn("TCS has no stop.", out, repr(text))

    def test_very_long_reports_are_trimmed(self):
        _, out, _ = self.run_hook(start_payload(), load=lambda: FakeTicket(status="line\n" * 5000))
        self.assertLessEqual(len(json.loads(out)["hookSpecificOutput"]["additionalContext"]),
                             session_status.MAX_TEXT)

    def test_stray_output_from_the_ticket_is_not_printed(self):
        code, out, err = self.run_hook(start_payload(), load=lambda: FakeTicket(status="", noise="loading..."))
        self.assertEqual((code, out), (0, ""))
        _, out, _ = self.run_hook(start_payload(), load=lambda: FakeTicket(status="TCS has no stop.",
                                                                            noise="loading..."))
        json.loads(out)  # still exactly one JSON document

    def test_runs_in_the_directory_claude_is_working_in_and_restores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            before = os.getcwd()
            fake = FakeTicket()
            self.run_hook(start_payload(cwd=tmp), load=lambda: fake)
            self.assertEqual(os.path.realpath(fake.cwds[0]), tmp)
            self.assertEqual(os.getcwd(), before)


FAKE_TICKET_PY = '''
import json, os

def _home():
    return os.environ["NSE_TRADER_HOME"]

def gate_check(tool_name, tool_input, state_path=None, config_path=None, now_utc=None):
    if tool_input.get("tradingsymbol") == "RELIANCE":
        return True, "approved"
    return False, "no approved ticket for %s" % tool_input.get("tradingsymbol")

def record_user_prompt(text, path=None, now_utc=None):
    path = path or os.path.join(_home(), "state", "last_prompt.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"text": text, "at": "2026-11-18T05:00:00Z"}, fh)

def status_report(state_path=None, portfolio_path=None, risk_path=None, config_path=None, now_utc=None, quiet=False):
    p = os.path.join(_home(), "status.txt")
    return open(p).read() if os.path.exists(p) else ""
'''


class Subprocess(unittest.TestCase):
    """Pipe real hook JSON into the scripts, laid out as ${CLAUDE_PLUGIN_ROOT}/hooks next to
    ${CLAUDE_PLUGIN_ROOT}/nse-broker/ticket.py (a fake ticket)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = os.path.join(self.tmp, "plugin root with spaces")
        shutil.copytree(HOOKS_DIR, os.path.join(self.plugin, "hooks"), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(self.plugin, "nse-broker"))
        with open(os.path.join(self.plugin, "nse-broker", "ticket.py"), "w") as fh:
            fh.write(FAKE_TICKET_PY)
        self.home = os.path.join(self.tmp, "trader")
        os.makedirs(os.path.join(self.home, "state"))

    def run_script(self, name, stdin, cwd=None):
        env = dict(os.environ, NSE_TRADER_HOME=self.home)
        p = subprocess.run([sys.executable, os.path.join(self.plugin, "hooks", name + ".py")],
                           input=stdin if isinstance(stdin, bytes) else stdin.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or self.tmp, env=env, timeout=30)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def test_gate_is_silent_on_an_approved_call(self):
        self.assertEqual(self.run_script("gate", pretool()), (0, "", ""))

    def test_gate_denies_an_unapproved_call(self):
        code, out, _ = self.run_script("gate", pretool(tool_input=dict(KITE_ORDER, tradingsymbol="TCS")))
        self.assertEqual(code, 0)
        inner = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(inner["permissionDecision"], "deny")
        self.assertIn("no approved ticket for TCS", inner["permissionDecisionReason"])

    def test_gate_denies_garbage_stdin_and_empty_stdin(self):
        for stdin in ("", "not json at all", "{", b"\xff\xfe", "[]"):
            code, out, _ = self.run_script("gate", stdin)
            self.assertEqual(code, 0, repr(stdin))
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny", repr(stdin))

    def test_gate_denies_when_the_ticket_module_is_missing(self):
        os.remove(os.path.join(self.plugin, "nse-broker", "ticket.py"))
        code, out, _ = self.run_script("gate", pretool())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gate_denies_when_the_ticket_module_will_not_import(self):
        with open(os.path.join(self.plugin, "nse-broker", "ticket.py"), "w") as fh:
            fh.write("raise SystemExit(5)\n")
        code, out, _ = self.run_script("gate", pretool())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gate_denies_a_non_order_tool(self):
        _, out, _ = self.run_script("gate", pretool("Bash", {"command": "ls"}))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_record_prompt_writes_the_file_and_prints_nothing(self):
        code, out, err = self.run_script("record_prompt", prompt_payload("yes, go ahead"))
        self.assertEqual((code, out), (0, ""))
        with open(os.path.join(self.home, "state", "last_prompt.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["text"], "yes, go ahead")

    def test_record_prompt_survives_garbage_and_a_missing_ticket(self):
        for stdin in ("", "garbage", "{", b"\xff\xfe", "[]", '{"prompt": 7}'):
            self.assertEqual(self.run_script("record_prompt", stdin), (0, "", ""), repr(stdin))
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "last_prompt.json")))
        os.remove(os.path.join(self.plugin, "nse-broker", "ticket.py"))
        self.assertEqual(self.run_script("record_prompt", prompt_payload("yes")), (0, "", ""))

    def test_session_status_is_silent_when_all_is_well(self):
        self.assertEqual(self.run_script("session_status", start_payload()), (0, "", ""))

    def test_session_status_prints_context_when_something_needs_attention(self):
        with open(os.path.join(self.home, "status.txt"), "w") as fh:
            fh.write("RELIANCE has 37 shares open and no stop recorded.")
        code, out, _ = self.run_script("session_status", start_payload("resume"))
        self.assertEqual(code, 0)
        inner = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(inner["hookEventName"], "SessionStart")
        self.assertIn("RELIANCE has 37 shares open", inner["additionalContext"])

    def test_session_status_survives_a_missing_ticket(self):
        os.remove(os.path.join(self.plugin, "nse-broker", "ticket.py"))
        self.assertEqual(self.run_script("session_status", start_payload()), (0, "", ""))


STOP_ARGS = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
             "quantity": 37, "product": "MIS", "order_type": "SL", "price": 1322.1, "trigger_price": 1328.8,
             "validity": "DAY"}


@unittest.skipUnless(os.path.isfile(os.path.join(ROOT, "nse-broker", "ticket.py")), "no nse-broker/ticket.py")
class RealTicketContract(unittest.TestCase):
    """The real nse-broker/ticket.py against the interface the hooks call (see the broker contract).
    Each test skips until ticket.py exposes the functions it needs."""

    @classmethod
    def setUpClass(cls):
        cls.ticket = load("nse-broker", "ticket")

    def need(self, *names):
        missing = [n for n in names if not hasattr(self.ticket, n)]
        if missing:
            self.skipTest("ticket.py does not expose %s yet" % ", ".join(missing))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "trader")
        os.makedirs(os.path.join(self.home, "state"))

    def env(self):
        return dict(os.environ, NSE_TRADER_HOME=self.home)

    def run_script(self, name, stdin):
        p = subprocess.run([sys.executable, os.path.join(HOOKS_DIR, name + ".py")], input=stdin.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp, env=self.env(), timeout=60)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def approve_stop_leg(self, minutes=5):
        """What `approve` leaves in state/broker.json (the broker contract's leg record), with a config."""
        shutil.copy(os.path.join(ROOT, "nse-budget", "config.example.json"), os.path.join(self.home, "config.json"))
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        leg = {"id": "fp:stop", "role": "stop", "tool": "place_order", "args": STOP_ARGS, "status": "approved",
               "approved_at": iso(now), "expires_at": iso(now + datetime.timedelta(minutes=minutes)),
               "used_at": None}
        with open(os.path.join(self.home, "state", "broker.json"), "w") as fh:
            json.dump({"orders": [{"kind": "approval", "legs": [leg]}]}, fh)

    def denied(self, out):
        self.assertTrue(out.strip(), "expected a deny decision, got silence")
        inner = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(inner["permissionDecision"], "deny")
        self.assertTrue(inner["permissionDecisionReason"])
        return inner["permissionDecisionReason"]

    def test_signatures_match_the_contract(self):
        self.need("gate_check", "record_user_prompt", "status_report")

        def params(fn):
            return list(inspect.signature(fn).parameters)
        self.assertEqual(params(self.ticket.gate_check),
                         ["tool_name", "tool_input", "state_path", "config_path", "now_utc"])
        self.assertEqual(params(self.ticket.record_user_prompt), ["text", "path", "now_utc"])
        self.assertEqual(params(self.ticket.status_report),
                         ["state_path", "portfolio_path", "risk_path", "config_path", "now_utc", "quiet"])

    def test_real_gate_denies_every_order_tool_with_no_approval(self):
        self.need("gate_check")
        for tool in ORDER_TOOLS:
            code, out, _ = self.run_script("gate", pretool("mcp__kite__" + tool))
            self.assertEqual(code, 0, tool)
            self.denied(out)

    def test_real_gate_allows_the_approved_call_once_then_denies_the_repeat(self):
        self.need("gate_check")
        self.approve_stop_leg()
        sent = dict(STOP_ARGS, tag="dtindia")  # the tag is ignored
        self.assertEqual(self.run_script("gate", pretool(tool_input=sent)), (0, "", ""))
        code, out, _ = self.run_script("gate", pretool(tool_input=sent))
        self.assertEqual(code, 0)
        self.assertIn("once", self.denied(out))

    def test_real_gate_denies_a_changed_price_and_leaves_the_approval_unused(self):
        self.need("gate_check")
        self.approve_stop_leg()
        _, out, _ = self.run_script("gate", pretool(tool_input=dict(STOP_ARGS, price=1330.0)))
        self.denied(out)
        self.assertEqual(self.run_script("gate", pretool(tool_input=STOP_ARGS)), (0, "", ""))

    def test_real_gate_denies_an_expired_approval(self):
        self.need("gate_check")
        self.approve_stop_leg(minutes=-1)
        _, out, _ = self.run_script("gate", pretool(tool_input=STOP_ARGS))
        self.denied(out)

    def test_real_gate_denies_on_corrupt_state(self):
        self.need("gate_check")
        self.approve_stop_leg()
        with open(os.path.join(self.home, "state", "broker.json"), "w") as fh:
            fh.write("{ not json")
        _, out, _ = self.run_script("gate", pretool(tool_input=STOP_ARGS))
        self.denied(out)

    def test_real_gate_denies_gtt_orders_even_when_everything_else_matches(self):
        self.need("gate_check")
        self.approve_stop_leg()
        for tool in ("mcp__kite__place_gtt_order", "mcp__kite__modify_gtt_order"):
            _, out, _ = self.run_script("gate", pretool(tool, STOP_ARGS))
            self.denied(out)

    def test_real_gate_lets_only_one_of_several_simultaneous_calls_through(self):
        self.need("gate_check")
        self.approve_stop_leg()
        payload = pretool(tool_input=STOP_ARGS).encode("utf-8")
        procs = [subprocess.Popen([sys.executable, os.path.join(HOOKS_DIR, "gate.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp, env=self.env())
                 for _ in range(6)]
        outs = [p.communicate(payload, timeout=60)[0].decode("utf-8") for p in procs]
        self.assertEqual(sum(1 for o in outs if not o.strip()), 1, outs)
        for o in outs:
            if o.strip():
                self.denied(o)

    def test_real_record_prompt_writes_last_prompt_json(self):
        self.need("record_user_prompt")
        code, out, _ = self.run_script("record_prompt", prompt_payload("yes please"))
        self.assertEqual((code, out), (0, ""))
        with open(os.path.join(self.home, "state", "last_prompt.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["text"], "yes please")
        self.assertRegex(doc["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_real_record_prompt_replaces_the_previous_prompt(self):
        self.need("record_user_prompt")
        self.run_script("record_prompt", prompt_payload("yes"))
        self.run_script("record_prompt", prompt_payload("actually wait"))
        with open(os.path.join(self.home, "state", "last_prompt.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["text"], "actually wait")

    def test_real_status_report_returns_a_string_and_the_hook_prints_valid_json_or_nothing(self):
        self.need("status_report")
        with contextlib.redirect_stdout(io.StringIO()):
            text = self.ticket.status_report(quiet=True)
        self.assertIsInstance(text, str)
        code, out, _ = self.run_script("session_status", start_payload())
        self.assertEqual(code, 0)
        if out:  # whatever it says must be one valid SessionStart document
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "SessionStart")


if __name__ == "__main__":
    unittest.main()
