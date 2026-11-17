#!/usr/bin/env python3
"""UserPromptSubmit hook: remember the user's latest message (day-trader-india, kite mode).

Claude Code pipes the hook JSON in on stdin:

    {"hook_event_name": "UserPromptSubmit", "prompt": "yes", "cwd": "...", ...}

and this stores the prompt text through nse-broker/ticket.py record_user_prompt, which writes
{"text": ..., "at": ...} to <trader home>/state/last_prompt.json. `ticket.py approve --reply TEXT`
compares TEXT to that file, so the "yes" that releases an order has to be what the user really
typed, not whatever string Claude passes.

This hook fails OPEN and stays silent: it must never block or slow the user's message. Every
error is swallowed, nothing is printed (plain stdout from a UserPromptSubmit hook is added to
Claude's context, so even a stray print would leak into the conversation), exit code 0.

Note: Claude Code also runs UserPromptSubmit hooks for /loop iterations, background subagent
reports and messages from other sessions, and gives no field that tells them from typed text.

Standard library only. ticket.py is found relative to this file (../nse-broker).
"""

import contextlib
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER_DIR = os.path.join(os.path.dirname(HERE), "nse-broker")


def load_ticket(broker_dir=None):
    """Import nse-broker/ticket.py from its file (never from whatever 'ticket' is on sys.path)."""
    folder = broker_dir or BROKER_DIR
    path = os.path.join(folder, "ticket.py")
    if not os.path.isfile(path):
        raise ImportError("no ticket.py in %s" % folder)
    if folder not in sys.path:
        sys.path.insert(0, folder)
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
    """Run in the directory Claude is working in, so the same trader/ folder is found."""
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


def main(stdin_text=None, load=None, broker_dir=None, path=None, now_utc=None):
    """Record the prompt. Returns 0 always and prints nothing, whatever goes wrong."""
    try:
        if isinstance(stdin_text, bytes):
            stdin_text = stdin_text.decode("utf-8")
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            return 0
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            return 0
        kwargs = {}
        if path is not None:
            kwargs["path"] = path
        if now_utc is not None:
            kwargs["now_utc"] = now_utc
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                _in_cwd(payload.get("cwd")):
            ticket = (load or (lambda: load_ticket(broker_dir)))()
            ticket.record_user_prompt(prompt, **kwargs)
    except (Exception, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = None
    try:
        code = main(raw)
    except BaseException:
        code = 0
    sys.exit(code)
