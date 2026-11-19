"""Shared pieces of the paper autopilot: settings, the trader folder, the IST clock, the day's
folder and event log, and the one place that runs the skill scripts. Standard library only.

The autopilot never trades anything but paper. guard() refuses to run unless config.json says
"broker": "paper" and "autopilot": true, and ticket.py autopilot-approve checks the same two
things again on its own.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "nse-broker"))
import nse_common as nc  # noqa: E402

SCRIPTS = {
    "scan": "nse-scanner/scan.py",
    "news": "nse-news/news.py",
    "dossier": "nse-news/dossier.py",
    "entry": "nse-entry/entry.py",
    "exit": "nse-exit/exit.py",
    "timemachine": "nse-timemachine/timemachine.py",
    "size": "nse-budget/size.py",
    "alerts": "nse-alerts/alerts.py",
    "risk": "nse-risk/check.py",
    "ticket": "nse-broker/ticket.py",
}

SETTINGS_FILE = "autopilot.json"
DEFAULTS = {
    "universe": "nifty50",             # a scanner list name, or a list of symbols
    "premarket_from": "08:40",         # scan, news and Claude's veto, once
    "veto_by": "09:25",                # a Claude veto that has not run by then is skipped for the day
    "orb_until": "14:31",              # the last ORB scan (the entry window closes at 14:30)
    "eod_at": "15:35",                 # final paper-check, report, Claude's analysis, email
    "news": True,
    "daily_scan": True,
    "step_timeout_s": 300,
    "claude": {"enabled": True, "bin": "claude", "premarket_model": "sonnet", "eod_model": "sonnet",
               "premarket_timeout_s": 600, "eod_timeout_s": 900},
    "mail": {"enabled": False, "from": "", "to": [], "keychain_service": "nse-autopilot-gmail",
             "host": "smtp.gmail.com", "port": 465},
    "notify": True,
    # the P&L journal (journal.py): every trade, what it made or lost, and why, rebuilt after each report
    "journal": {"enabled": False, "dir": "~/Desktop/NSE Trading P&L"},
    # Measurements (autopilot.example.json turns them on; without the keys they are off):
    # skip a long signal whose round-trip charges at the stop exceed this share of its risk (R); every signal
    # skipped is replayed in the report, so the filter's cost or saving is measured. null = off.
    "cost_filter": {"max_charges_r": None},
    # "shadow": scan breakdowns too, plan them and replay them at the close; never traded. "off": long only.
    "shorts": "off",
    # record the Nifty trend, the VIX band and the Nifty's move so far on every signal (the report splits by them)
    "tags": False,
    # a JSON file {"as_of": "YYYY-MM-DD", "ranks": {"SYMBOL": 0.0-1.0}} (1 = ranked best), e.g. from Touchstone;
    # recorded as a tag on each signal. null = none.
    "rank_file": None,
    # the report re-sizes each day's signals for these budgets (rupees), each with its own scaled rules. [] = off.
    "sizes": [],
    # the deep scan before the open (nse-news/dossier.py): a file per stock in the universe (price and trend,
    # results, board meetings and ex-dates, NSE filings, headlines, shareholding, pledges, insider trades, bulk and
    # block deals, the F&O ban list). Its flags go to Claude's veto and the report. It stops starting new stocks at
    # budget_s or 09:10, whichever is sooner, so it never runs into the session; results: false skips the XBRL.
    "dossier": {"enabled": False, "budget_s": 900, "results": True},
    # the report's "why only N trades" (the limits lifted one at a time) and "the whole principal" (every trade
    # all in, with the money alone and with `margin`x intraday leverage). Measurement only: nothing is traded.
    "whatif": {"enabled": False, "margin": 5},
    # the same signals at several trades a day (each with a daily loss limit of N x the max loss a trade), at one
    # budget (null = config.json's). Measurement only: the paper account keeps config.json's cap.
    "trade_caps": {"enabled": False, "caps": [3, 5, 8, 10], "budget": None},
    # more paper accounts that trade for real on paper, one per cap, in <trader folder>/accounts/cap<N>/ (accounts.py):
    # the main config with that many trades a day and a daily loss limit of that many times the max loss a trade
    "accounts": {"enabled": False, "caps": [5, 8, 10]},
    # keep the day's 1- and 5-minute bars (Yahoo drops them after 30 / 60 days) in archive/ in the trader folder
    "archive": {"enabled": False, "pause_s": 0.4},
    # test and replay only (NSE_TEST_MODE=1): extra arguments per step, "{sym}" is replaced by the symbol,
    # e.g. {"entry": ["--offline", "fixtures/{sym}-5m.json"]}. Refused outside test mode.
    "offline": {},
}


class AutopilotError(Exception):
    pass


def trader_home():
    return nc.trader_home()


def load_settings(home):
    """DEFAULTS overlaid with $NSE_TRADER_HOME/autopilot.json (one level deep for claude and mail)."""
    s = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(home, SETTINGS_FILE)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            raise AutopilotError("%s is not a JSON object" % path)
        for k, v in user.items():
            if isinstance(s.get(k), dict) and isinstance(v, dict):
                s[k].update(v)
            else:
                s[k] = v
    if s.get("offline") and not nc.test_mode():
        raise AutopilotError("\"offline\" in %s is for tests and replays only (NSE_TEST_MODE=1); remove it" % path)
    return s


def load_cfg(home):
    path = os.path.join(home, "config.json")
    if not os.path.isfile(path):
        raise AutopilotError("no config.json in %s: run autopilot/install.sh (it writes a paper config)" % home)
    return nc.load_config(path, require=True)


def guard(cfg):
    """None if the autopilot may run on this config, else why not. Paper only, and only when the user
    has switched it on; nothing here ever turns either on."""
    mode = str(cfg.get("broker") or "paper").lower()
    if mode != "paper":
        return ("config.json says \"broker\": \"%s\". The autopilot runs paper only: nobody is at the keyboard "
                "to say yes to a real order" % mode)
    if cfg.get("autopilot") is not True:
        return "config.json does not say \"autopilot\": true, so the autopilot is off"
    return None


# ---------------------------------------------------------------- clock

def parse_clock(text):
    """--now for tests only, like nse-broker: a real run is always on the real clock."""
    if text and not nc.test_mode():
        raise AutopilotError("--now is a test switch and only works with NSE_TEST_MODE=1")
    return nc.parse_now(text) if text else nc.utc_now()


def hhmm(text):
    return nc.parse_hhmm(text)


def ist_of(utc):
    return nc.to_ist(utc)


# ---------------------------------------------------------------- the day's folder and log

def day_dir(home, day):
    return os.path.join(home, "autopilot", day)


def reports_dir(home):
    return os.path.join(home, "reports")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    nc.write_json(path, doc)


def load_day(home, day):
    st = read_json(os.path.join(day_dir(home, day), "state.json")) or {}
    st.setdefault("date", day)
    st.setdefault("phases", {})
    st.setdefault("candidates", {})
    st.setdefault("ticks", [])
    return st


def save_day(home, st):
    write_json(os.path.join(day_dir(home, st["date"]), "state.json"), st)


def log_event(home, day, event):
    """One JSON line per step in autopilot/<day>/log.jsonl: the raw material for the report."""
    folder = day_dir(home, day)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "log.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def read_log(home, day):
    out = []
    path = os.path.join(day_dir(home, day), "log.jsonl")
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


# ---------------------------------------------------------------- running the skill scripts

class Result(object):
    def __init__(self, code, out, err, data, seconds):
        self.code, self.out, self.err, self.data, self.seconds = code, out, err, data, seconds

    @property
    def ok(self):
        return self.code == 0


def tail(text, n=600):
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


class Runner(object):
    """Runs one skill script as a subprocess with NSE_TRADER_HOME set, logs it, and parses --json output.
    Tests swap `execute` for a fake."""

    def __init__(self, home, day, settings, now_text=None, execute=None):
        self.home, self.day, self.settings = home, day, settings
        self.now_text = now_text                # pinned clock (tests / replay): passed on as --now
        self.execute = execute or self._subprocess

    def _subprocess(self, argv, timeout):
        env = dict(os.environ, NSE_TRADER_HOME=self.home)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=self.home)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, out, "timed out after %ss" % timeout
        except OSError as e:
            return None, "", "could not run: %s" % e

    def extra(self, step, sym):
        rows = (self.settings.get("offline") or {}).get(step) or []
        return [str(x).replace("{sym}", sym or "") for x in rows]

    def run(self, step, script, args, sym=None, parse=True, now=True, timeout=None):
        argv = [sys.executable, os.path.join(REPO, SCRIPTS[script])] + list(args) + self.extra(step, sym)
        if now and self.now_text:
            argv += ["--now", self.now_text]
        t0 = time.time()
        code, out, err = self.execute(argv, timeout or self.settings.get("step_timeout_s") or 300)
        secs = round(time.time() - t0, 2)
        data = None
        if parse and out:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        event = {"at": nc.utc_iso(self.clock()), "step": step, "symbol": sym, "code": code, "seconds": secs,
                 "args": [a for a in argv[2:]]}
        if code != 0 and err:
            event["stderr"] = tail(err)
        if data is None and out:
            event["stdout"] = tail(out)
        log_event(self.home, self.day, event)
        return Result(code, out, err, data, secs)

    def clock(self):
        return nc.parse_now(self.now_text) if self.now_text else nc.utc_now()

    def note(self, step, **fields):
        event = {"at": nc.utc_iso(self.clock()), "step": step}
        event.update(fields)
        log_event(self.home, self.day, event)


def keychain(service, account=None):
    """A secret from the login keychain (macOS `security`), or None. Secrets never go in a file."""
    argv = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        argv[4:4] = ["-a", account]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def notify(settings, title, message):
    """A macOS notification; silent anywhere else or when switched off."""
    if not settings.get("notify") or sys.platform != "darwin" or nc.test_mode():
        return
    script = 'display notification %s with title %s' % (json.dumps(message[:200]), json.dumps(title))
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
