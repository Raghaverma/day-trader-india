#!/usr/bin/env python3
"""Kite Connect as the market-data source: the daily login, and a check that it works.

While a login from today is valid, every NSE chart the skills and the autopilot ask Yahoo for is
served from Kite instead (nse_common.kite_answer): the scans, the entry check, the paper fills, the
quotes, the archive, the regime tags. With no login, or after it expires at 06:00 IST, or if Kite
fails, they use Yahoo exactly as before. Nothing here places an order.

Set up once (a Kite Connect app at developers.kite.trade, with its redirect URL set to
http://127.0.0.1:5005/kite):

    python3 autopilot/kite.py setup       # asks for the API key and the API secret (secret -> keychain)

Then each trading day, on this Mac, before the pre-market (any time after 06:00 IST):

    python3 autopilot/kite.py login       # opens Kite in the browser; log in; the token is caught here
    python3 autopilot/kite.py status      # is Kite on, until when, and does Kite accept the token
    python3 autopilot/kite.py compare RELIANCE   # Kite's and Yahoo's last 1-minute bars side by side
    python3 autopilot/kite.py logout      # ends the session; Yahoo from now on

The API secret and the day's access token live in the macOS login keychain, never in a file.
<trader folder>/kite.json holds the API key; kite-session.json holds the user id and the expiry.
"""

import argparse
import getpass
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402

nc = ac.nc
CONFIG_FILE = "kite.json"
DEFAULT_PORT = 5005
REDIRECT_PATH = "/kite"
WAIT_S = 300
AUTOPILOT_HOME = os.path.join("~", "Trading", "nse-autopilot")     # install.sh's default trader folder
_HOME = {"path": None}


class KiteSetupError(Exception):
    pass


def home():
    """--home; else $NSE_TRADER_HOME; else the autopilot's folder (what launchd uses) when it exists, so a
    login from a plain Terminal lands where the jobs look; else the skills' usual trader folder."""
    if _HOME["path"]:
        return _HOME["path"]
    if os.environ.get(nc.HOME_ENV):
        return nc.trader_home()
    auto = os.path.expanduser(AUTOPILOT_HOME)
    if os.path.isfile(os.path.join(auto, "autopilot.json")):
        return auto
    return nc.trader_home()


def config_path(h=None):
    return os.path.join(h or home(), CONFIG_FILE)


def load_config(h=None):
    try:
        doc = nc.read_json(config_path(h)) or {}
    except ValueError as e:
        raise KiteSetupError("%s is not valid JSON: %s" % (config_path(h), e))
    if not doc.get("api_key"):
        raise KiteSetupError("no Kite API key yet: run `python3 autopilot/kite.py setup` first")
    doc.setdefault("port", DEFAULT_PORT)
    return doc


def redirect_url(port):
    return "http://127.0.0.1:%d%s" % (int(port), REDIRECT_PATH)


def checksum(api_key, request_token, api_secret):
    """Kite's session checksum: SHA-256 of api_key + request_token + api_secret, hex."""
    return hashlib.sha256((api_key + request_token + api_secret).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- setup

def cmd_setup(a):
    h = home()
    try:
        old = nc.read_json(config_path(h)) or {}
    except ValueError:
        old = {}
    key = (a.api_key or input("Kite Connect API key%s: " % (" [%s]" % old["api_key"] if old.get("api_key") else ""))
           ).strip() or old.get("api_key", "")
    if not key or not key.isalnum():
        raise KiteSetupError("the API key is letters and digits only (from developers.kite.trade)")
    port = int(a.port or old.get("port") or DEFAULT_PORT)
    if nc.keychain_get(nc.KITE_SECRET_SERVICE, key) and not a.new_secret:
        print("The API secret for %s is already in the keychain (pass --new-secret to replace it)." % key)
    else:
        secret = getpass.getpass("Kite Connect API secret (not shown): ").strip()
        if not secret:
            raise KiteSetupError("no secret given")
        nc.keychain_set(nc.KITE_SECRET_SERVICE, key, secret)
        print("API secret saved in the login keychain (service %s)." % nc.KITE_SECRET_SERVICE)
    nc.write_json(config_path(h), {"api_key": key, "port": port})
    print("Saved %s." % config_path(h))
    print("\nIn your app at developers.kite.trade, the Redirect URL must be exactly:\n    %s" % redirect_url(port))
    print("\nThen log in for today:  python3 autopilot/kite.py login")
    return 0


# ---------------------------------------------------------------- login

def exchange(api_key, request_token, api_secret):
    """request_token -> Kite's session data (access_token, user_id, ...)."""
    return nc.kite_call(None, "/session/token", method="POST",
                        data={"api_key": api_key, "request_token": request_token,
                              "checksum": checksum(api_key, request_token, api_secret)})


def save_session(h, api_key, data, now_utc=None):
    now = now_utc or nc.utc_now()
    token = data.get("access_token")
    if not token:
        raise KiteSetupError("Kite's answer had no access token")
    if not nc.test_mode():
        nc.keychain_set(nc.KITE_TOKEN_SERVICE, api_key, token)
    doc = {"api_key": api_key, "user_id": data.get("user_id"), "login_at": nc.utc_iso(now),
           "expires_at": nc.utc_iso(nc.kite_expiry(now)),
           "note": "the access token is in the login keychain (service %s); this file holds no secret"
                   % nc.KITE_TOKEN_SERVICE}
    if nc.test_mode():
        doc["access_token"] = token
    path = nc.kite_session_path(h)
    nc.write_json(path, doc)
    os.chmod(path, 0o600)
    return doc


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Answers Kite's redirect to 127.0.0.1 once, keeping the request_token."""
    got = None
    state = None

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        ok = (u.path == REDIRECT_PATH and q.get("status") == "success" and q.get("request_token")
              and q.get("state") == type(self).state)
        if ok:
            type(self).got = q["request_token"]
            text = "Logged in. Kite data is on for today; you can close this tab."
        elif u.path == REDIRECT_PATH and q.get("state") != type(self).state:
            text = "This login link is stale or from another attempt. Run the login command again."
        else:
            text = "Kite did not log in (%s)." % (q.get("status") or "no status")
        body = ("<!doctype html><meta charset=utf-8><title>Kite login</title>"
                "<p style='font:16px -apple-system,sans-serif;margin:3em'>%s</p>" % text).encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_for_token(api_key, port, open_browser=True, wait_s=WAIT_S):
    state = secrets.token_urlsafe(16)
    _Catcher.got, _Catcher.state = None, state
    try:
        srv = http.server.HTTPServer(("127.0.0.1", int(port)), _Catcher)
    except OSError as e:
        raise KiteSetupError("can't listen on 127.0.0.1:%s (%s); pass --port or --request-token" % (port, e))
    url = nc.KITE_LOGIN % urllib.parse.quote(api_key) + "&redirect_params=" + urllib.parse.quote(
        urllib.parse.urlencode({"state": state}))
    print("Log in to Kite here (it opens in your browser):\n    %s\n" % url)
    if open_browser:
        subprocess.run(["open", url], check=False)
    timer = threading.Timer(wait_s, srv.shutdown)
    timer.daemon = True
    timer.start()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        while t.is_alive() and not _Catcher.got:
            t.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        timer.cancel()
        srv.shutdown()
        srv.server_close()
    if not _Catcher.got:
        raise KiteSetupError("no login within %d minutes; run the command again" % (wait_s // 60))
    return _Catcher.got


def cmd_login(a):
    h = home()
    cfg = load_config(h)
    key = cfg["api_key"]
    secret = nc.keychain_get(nc.KITE_SECRET_SERVICE, key)
    if not secret:
        raise KiteSetupError("no API secret in the keychain for %s: run `python3 autopilot/kite.py setup`" % key)
    token = a.request_token or wait_for_token(key, a.port or cfg["port"], open_browser=not a.no_browser)
    data = exchange(key, token.strip(), secret)
    doc = save_session(h, key, data)
    nc._KITE["session"] = None
    print("Logged in as %s. Kite data is on until %s." % (doc.get("user_id") or "?",
                                                           nc.ist_text(nc.parse_now(doc["expires_at"]))))
    return cmd_status(argparse.Namespace(json=False, offline=False))


# ---------------------------------------------------------------- status, logout, compare

def status(h=None, offline=False, now_utc=None):
    h = h or home()
    now = now_utc or nc.utc_now()
    out = {"source": "yahoo", "session_file": nc.kite_session_path(h)}
    try:
        doc = nc.read_json(nc.kite_session_path(h))
    except ValueError:
        doc = None
    if (os.environ.get(nc.DATA_ENV) or "").lower() == "yahoo":
        out["why"] = "%s=yahoo is set" % nc.DATA_ENV
        return out
    if not doc:
        out["why"] = "no Kite login yet"
        return out
    out.update(user_id=doc.get("user_id"), login_at=doc.get("login_at"), expires_at=doc.get("expires_at"))
    if doc.get("dead"):
        out["why"] = "Kite rejected the token at %s: %s" % (doc.get("dead_at"), doc["dead"])
        return out
    sess = nc.kite_session(h, now)
    if not sess:
        exp = nc.parse_now(doc["expires_at"]) if doc.get("expires_at") else None
        out["why"] = ("the login expired at %s" % nc.ist_text(exp)) if exp and now >= exp else \
            "no access token in the keychain"
        return out
    if not offline:
        try:
            prof = nc.kite_call(sess, "/user/profile")
            out["user_name"] = (prof or {}).get("user_name")
        except nc.KiteError as e:
            out["why"] = str(e)
            return out
    out["source"] = "kite"
    return out


def cmd_status(a):
    st = status(offline=a.offline)
    if a.json:
        print(json.dumps(st, indent=2))
    elif st["source"] == "kite":
        print("Market data: Kite (%s%s), until %s." % (st.get("user_id") or "?",
                                                      ", " + st["user_name"] if st.get("user_name") else "",
                                                      nc.ist_text(nc.parse_now(st["expires_at"]))))
    else:
        print("Market data: Yahoo (%s)." % st["why"])
    if not a.json:
        print("Trader folder: %s" % home())
    return 0


def cmd_logout(a):
    h = home()
    sess = nc.kite_session(h)
    if sess:
        try:
            nc.kite_call(sess, "/session/token", {"api_key": sess["api_key"], "access_token": sess["access_token"]},
                         method="DELETE")
        except nc.KiteError as e:
            print("Kite didn't confirm the logout (%s); removing the local session anyway." % e)
        subprocess.run(["security", "delete-generic-password", "-s", nc.KITE_TOKEN_SERVICE, "-a", sess["api_key"]],
                       capture_output=True, check=False)
    try:
        os.remove(nc.kite_session_path(h))
    except FileNotFoundError:
        pass
    print("Logged out. Market data: Yahoo.")
    return 0


def _bars(text, n):
    r = json.loads(text)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [(nc.ist_from_epoch(t).strftime("%H:%M"), q["close"][i], q["volume"][i])
            for i, t in enumerate(r["timestamp"]) if q["close"][i] is not None]
    return rows[-n:], r["meta"]


def cmd_compare(a):
    """The last few 1-minute bars from Kite and from Yahoo, and each one's quote time."""
    sym = nc.nse_symbol(a.symbol)
    url = nc.yahoo_chart_url(sym, "1d", "1m")
    kite = nc.kite_answer(url, home=home())
    if kite is None:
        print("Kite didn't answer (%s)." % (nc._KITE.get("error") or status(offline=True).get("why")))
        return 1
    req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        yahoo = r.read().decode("utf-8")
    kb, km = _bars(kite, a.bars)
    yb, ym = _bars(yahoo, a.bars)
    now = nc.utc_now()
    for name, m in (("Kite", km), ("Yahoo", ym)):
        t = m.get("regularMarketTime")
        lag = (now - (nc.to_utc(nc.ist_from_epoch(t)))).total_seconds() if t else None
        print("%-5s last %s at %s (%s)" % (name, nc.inr(m.get("regularMarketPrice")),
                                            nc.ist_from_epoch(t).strftime("%H:%M:%S") if t else "?",
                                            "%.0f s ago" % lag if lag is not None else "no time"))
    print("\n%-6s %12s %10s   %12s %10s" % ("bar", "Kite close", "volume", "Yahoo close", "volume"))
    yd = {r[0]: r for r in yb}
    for t, c, v in kb:
        y = yd.get(t)
        print("%-6s %12.2f %10d   %12s %10s" % (t, c, v or 0, "%.2f" % y[1] if y else "-",
                                               "%d" % (y[2] or 0) if y else "-"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Kite Connect as the market-data source (data only, no orders).")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="save the API key, and the API secret in the keychain")
    s.add_argument("--api-key")
    s.add_argument("--port", type=int)
    s.add_argument("--new-secret", action="store_true", help="replace the secret already in the keychain")
    s = sub.add_parser("login", help="today's login: opens Kite, catches the token on 127.0.0.1")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    s.add_argument("--request-token", help="finish a login by hand with the request_token from the redirect URL")
    s = sub.add_parser("status", help="which source the tools use now, and until when")
    s.add_argument("--json", action="store_true")
    s.add_argument("--offline", action="store_true", help="don't ask Kite to confirm the token")
    sub.add_parser("logout", help="end the session; Yahoo from now on")
    s = sub.add_parser("compare", help="Kite's and Yahoo's latest 1-minute bars side by side")
    s.add_argument("symbol")
    s.add_argument("--bars", type=int, default=8)
    p.add_argument("--home", help="the trader folder (default: $NSE_TRADER_HOME, else ~/Trading/nse-autopilot)")
    a = p.parse_args(argv)
    _HOME["path"] = os.path.abspath(os.path.expanduser(a.home)) if a.home else None
    try:
        return {"setup": cmd_setup, "login": cmd_login, "status": cmd_status, "logout": cmd_logout,
                "compare": cmd_compare}[a.cmd](a)
    except (KiteSetupError, nc.KiteError) as e:
        print("kite: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
