#!/usr/bin/env python3
"""SessionStart hook: warn when a trade needs attention (day-trader-india).

Runs nse-broker/ticket.py status_report (an entry filled with no stop recorded, an approved order
never placed, an open position with no stop, a stale PASS) and, only when it has something to
say, prints it as SessionStart context for Claude and as a warning line for the user:

    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<text>"},
     "systemMessage": "<text>"}

When there is nothing to report it prints nothing. It fails OPEN and stays silent: any error is
swallowed and the exit code is always 0, so it can never get in the way of starting a session.

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
MAX_TEXT = 4000  # Claude Code moves context over 10,000 characters into a file


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


def build_output(text):
    text = text.strip()
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 3] + "..."
    return json.dumps({
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text},
        "systemMessage": text,
    })


def main(stdin_text=None, out=None, load=None, broker_dir=None, **status_kwargs):
    """Print the status warning when there is one. Returns 0 always."""
    out = out if out is not None else sys.stdout
    try:
        cwd = None
        try:
            if isinstance(stdin_text, bytes):
                stdin_text = stdin_text.decode("utf-8")
            payload = json.loads(stdin_text)
            if isinstance(payload, dict):
                cwd = payload.get("cwd")
        except Exception:
            pass  # the payload is optional here: the status check does not depend on it
        kwargs = {k: v for k, v in status_kwargs.items() if v is not None}
        kwargs["quiet"] = True
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), _in_cwd(cwd):
            ticket = (load or (lambda: load_ticket(broker_dir)))()
            text = ticket.status_report(**kwargs)
        if isinstance(text, str) and text.strip():
            out.write(build_output(text) + "\n")
            out.flush()
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
