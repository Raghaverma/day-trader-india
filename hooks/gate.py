#!/usr/bin/env python3
"""PreToolUse gate for broker order tools (day-trader-india, kite mode).

Claude Code runs this before every mcp__<server>__{place_order, modify_order, cancel_order,
place_gtt_order, modify_gtt_order} call and pipes the hook JSON in on stdin:

    {"hook_event_name": "PreToolUse", "tool_name": "mcp__kite__place_order",
     "tool_input": {...the tool arguments...}, "cwd": "...", ...}

All the logic lives in nse-broker/ticket.py (gate_check). This file is a thin wrapper:

  * It only ever DENIES or stays silent. It never prints an allow/approve decision, so the
    user's own "Needs approval" permission setting still asks as the second gate.
  * It fails CLOSED: unparsable stdin, a tool_input that is not an object, a missing or
    broken ticket module, an exception in gate_check, or any answer other than exactly
    (True, reason) prints a deny decision. A deny is printed as JSON on stdout with exit 0:

      {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                              "permissionDecisionReason": "<reason>"}}

  * Anything ticket.py prints while it runs is captured and sent to stderr, because stray text
    on stdout would stop Claude Code from reading the JSON (and a hook whose JSON cannot be
    parsed is a non-blocking error: the order would go ahead).

Standard library only. ticket.py is found relative to this file (../nse-broker), so it works
from ${CLAUDE_PLUGIN_ROOT}/hooks/gate.py.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER_DIR = os.path.join(os.path.dirname(HERE), "nse-broker")

# the five order actions the gate understands; the hooks.json matcher is the same list
ORDER_ACTIONS = ("place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order")
ORDER_TOOL = re.compile(r"^mcp__.+__(%s)$" % "|".join(ORDER_ACTIONS))
MAX_REASON = 700


def load_ticket(broker_dir=None):
    """Import nse-broker/ticket.py from its file (never from whatever 'ticket' is on sys.path)."""
    folder = broker_dir or BROKER_DIR
    path = os.path.join(folder, "ticket.py")
    if not os.path.isfile(path):
        raise ImportError("no ticket.py in %s" % folder)
    if folder not in sys.path:
        sys.path.insert(0, folder)  # ticket.py does `import nse_common` from its own folder
    spec = importlib.util.spec_from_file_location("ticket", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ticket"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop("ticket", None)
        raise
    return mod


@contextlib.contextmanager
def _in_cwd(path):
    """Run in the directory Claude is working in (the payload's cwd follows `cd`), so ticket.py
    finds the same trader/ folder it finds when Claude runs it from Bash."""
    old = None
    if isinstance(path, str) and os.path.isabs(path) and os.path.isdir(path):
        try:
            old = os.getcwd()
        except OSError:
            old = None
        try:
            os.chdir(path)
        except OSError:
            old = None
    try:
        yield
    finally:
        if old:
            try:
                os.chdir(old)
            except OSError:
                pass


def _short(text, limit=MAX_REASON):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def deny_output(reason):
    """The documented PreToolUse deny decision, as one line of JSON."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _short(reason) or "blocked by the day-trader-india order gate",
    }})


def decide(stdin_text, load=None, broker_dir=None, **gate_kwargs):
    """-> (allowed, reason). Never raises. allowed is True only when ticket.gate_check answered
    exactly (True, ...); every other outcome is (False, plain reason)."""
    noise = io.StringIO()
    try:
        if isinstance(stdin_text, bytes):
            stdin_text = stdin_text.decode("utf-8")
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            return False, "the hook input was not a JSON object, so the order cannot be checked"
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_name, str) or not tool_name:
            return False, "the hook input has no tool_name, so the order cannot be checked"
        if not ORDER_TOOL.match(tool_name):
            return False, "%s is not a recognised order tool (place_order, modify_order, cancel_order, " \
                          "place_gtt_order, modify_gtt_order)" % _short(tool_name, 120)
        if not isinstance(tool_input, dict):
            return False, "the order arguments were missing or not an object, so the order cannot be checked"
        with contextlib.redirect_stdout(noise), _in_cwd(payload.get("cwd")):
            ticket = (load or (lambda: load_ticket(broker_dir)))()
            kwargs = {k: v for k, v in gate_kwargs.items() if v is not None}
            verdict = ticket.gate_check(tool_name, tool_input, **kwargs)
        if not isinstance(verdict, (tuple, list)) or len(verdict) != 2:
            return False, "the order gate gave an answer it could not understand, so the order is blocked"
        allow, reason = verdict
        if allow is True:
            return True, ""
        return False, str(reason).strip() if reason else "no approved ticket matches this order"
    except (Exception, SystemExit) as exc:
        return False, "the order gate could not run (%s: %s), so the order is blocked. Fix the " \
                      "day-trader-india install; nothing was sent" % (type(exc).__name__, _short(exc, 200))
    finally:
        text = noise.getvalue().strip()
        if text:
            try:
                sys.stderr.write(_short(text, 2000) + "\n")
            except Exception:
                pass


def main(stdin_text=None, out=None, load=None, broker_dir=None, **gate_kwargs):
    """Print a deny decision (or nothing) and return the exit code, which is always 0: the
    decision is carried by the JSON, not by the exit code."""
    out = out if out is not None else sys.stdout
    allowed, reason = decide(stdin_text, load=load, broker_dir=broker_dir, **gate_kwargs)
    if allowed:
        return 0
    out.write(deny_output(reason) + "\n")
    out.flush()
    return 0


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = None  # unreadable stdin is unparsable stdin: main() denies
    sys.exit(main(raw))
