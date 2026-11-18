#!/usr/bin/env python3
"""Headless Claude for the paper autopilot, on the Claude account logged in on this Mac. Standard library only.

Two short runs a day:
  premarket  reads the morning's news brief and answers with the stocks to sit out today (a veto list)
  eod        reads the day's report and the all-days scorecard and answers with a plain-words analysis

Claude gets no tools at all (--tools "") and no MCP servers: everything it needs is in the prompt, and it
only answers. This script saves the answer. It never sees the broker, the config or any credential.

    python3 claude_run.py selftest          # is `claude -p` logged in and answering, from here?

Auth: `claude` uses the login in this user's keychain (log in once with `claude` in Terminal). A launchd
job in the user's GUI session can read it. If that ever fails, run `claude setup-token`, store the token
with `security add-generic-password -s nse-autopilot-claude -a claude -w`, and set
"token_keychain_service": "nse-autopilot-claude" under "claude" in autopilot.json.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402

nc = ac.nc
PROMPTS = os.path.join(ac.HERE, "prompts")


def build_prompt(kind, payload):
    with open(os.path.join(PROMPTS, kind + ".md"), encoding="utf-8") as fh:
        head = fh.read().strip()
    return "%s\n\n<data>\n%s\n</data>\n" % (head, json.dumps(payload, ensure_ascii=False, indent=1))


def argv_for(settings, kind):
    c = settings.get("claude") or {}
    model = c.get("%s_model" % kind) or c.get("model") or "sonnet"
    return [c.get("bin") or "claude", "-p", "--output-format", "json", "--model", model, "--tools", "",
            "--strict-mcp-config", "--setting-sources", "project", "--permission-mode", "dontAsk",
            "--no-session-persistence"]


def env_for(settings):
    env = dict(os.environ)
    env.pop("NSE_TRADER_HOME", None)        # nothing Claude starts should find the autopilot's trader folder
    env.pop("NSE_TEST_MODE", None)
    service = (settings.get("claude") or {}).get("token_keychain_service")
    if service:
        tok = ac.keychain(service)
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def workdir():
    """An empty folder for Claude to start in: no project settings, no CLAUDE.md, no trader files."""
    path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "nse-autopilot", "claude")
    os.makedirs(path, exist_ok=True)
    return path


def run_claude(settings, kind, prompt, timeout):
    """(ok, result dict or None, error). The CLI's JSON envelope: {"result": text, "is_error": ...}."""
    t0 = time.time()
    try:
        p = subprocess.run(argv_for(settings, kind), input=prompt, capture_output=True, text=True,
                           timeout=timeout, env=env_for(settings), cwd=workdir())
    except subprocess.TimeoutExpired:
        return False, None, "timed out after %ss" % timeout
    except OSError as e:
        return False, None, "could not run claude: %s (is it on PATH for launchd?)" % e
    try:
        env = json.loads(p.stdout)
    except ValueError:
        return False, None, "exit %s: %s" % (p.returncode, ac.tail(p.stderr or p.stdout, 300))
    env["_seconds"] = round(time.time() - t0, 1)
    if p.returncode != 0 or env.get("is_error") or not isinstance(env.get("result"), str):
        return False, env, "exit %s: %s" % (p.returncode, ac.tail(str(env.get("result") or p.stderr), 300))
    return True, env, None


def last_json_object(text):
    """The last ```json fenced block, else the outermost {...} in the text, parsed; None if neither parses."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for b in reversed(blocks):
        try:
            return json.loads(b)
        except ValueError:
            continue
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except ValueError:
            return None
    return None


def ask(kind, payload, settings, save_path=None):
    """premarket -> {"ok", "vetoes", "summary", "error"}; eod -> {"ok", "text", "error"}."""
    c = settings.get("claude") or {}
    timeout = int(c.get("%s_timeout_s" % kind) or 600)
    prompt = build_prompt(kind, payload)
    ok, env, err = run_claude(settings, kind, prompt, timeout)
    out = {"ok": False, "error": err}
    if ok:
        text = env["result"].strip()
        if kind == "premarket":
            doc = last_json_object(text)
            if isinstance(doc, dict) and isinstance(doc.get("vetoes"), list):
                out = {"ok": True, "vetoes": doc["vetoes"], "summary": str(doc.get("summary") or "")[:1500],
                       "error": None}
            else:
                out = {"ok": False, "error": "Claude's answer had no {\"vetoes\": [...]} JSON"}
        else:
            out = {"ok": bool(text), "text": text, "error": None if text else "empty answer"}
    if save_path:
        rec = {"kind": kind, "at": nc.utc_iso(), "ok": out["ok"], "error": out.get("error"),
               "prompt_chars": len(prompt), "model": argv_for(settings, kind)[5]}
        if env:
            rec.update({k: env.get(k) for k in ("result", "total_cost_usd", "duration_ms", "num_turns", "usage")})
            rec["seconds"] = env.get("_seconds")
        ac.write_json(save_path, rec)
    return out


def selftest(settings):
    t0 = time.time()
    ok, env, err = run_claude(settings, "premarket", "Reply with exactly the word OK and nothing else.", 180)
    if not ok:
        return False, err
    said = env["result"].strip()
    if "OK" not in said.upper():
        return False, "answered %r, not OK" % said[:80]
    return True, "answered OK in %.0fs (model %s)" % (time.time() - t0, argv_for(settings, "premarket")[5])


def main(argv=None):
    nc.utf8_stdout()
    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["selftest"]:
        print("usage: claude_run.py selftest", file=sys.stderr)
        return 2
    home = ac.trader_home()
    settings = ac.load_settings(home)
    ok, why = selftest(settings)
    print("%s: %s" % ("OK" if ok else "FAIL", why))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
