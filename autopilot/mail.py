#!/usr/bin/env python3
"""The paper autopilot's email: Gmail over SMTP (smtp.gmail.com:465, SSL) with an app password kept in the
macOS login keychain. Standard library only.

Set up once (Gmail needs 2-Step Verification on to make an app password, at
myaccount.google.com/apppasswords):

    security add-generic-password -s nse-autopilot-gmail -a <your Gmail address> -w
    (it asks for the 16-character app password; nothing is typed on the command line or saved in a file)

and in $NSE_TRADER_HOME/autopilot.json:

    "mail": {"enabled": true, "from": "<your Gmail address>", "to": ["<where the report goes>"]}

    python3 mail.py check     # finds the password and logs in to Gmail, sends nothing
    python3 mail.py test      # sends a one-line test email
"""

import mimetypes
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_common as ac  # noqa: E402
import report  # noqa: E402

nc = ac.nc


def _cfg(settings):
    m = dict(ac.DEFAULTS["mail"])
    m.update(settings.get("mail") or {})
    to = m.get("to") or []
    m["to"] = [to] if isinstance(to, str) else list(to)
    return m


def problems(m):
    out = []
    if not m.get("from"):
        out.append("no \"from\" address under \"mail\" in autopilot.json")
    if not m["to"]:
        out.append("no \"to\" addresses under \"mail\" in autopilot.json")
    return out


def password(m, lookup=None):
    return (lookup or ac.keychain)(m.get("keychain_service") or "nse-autopilot-gmail", m.get("from"))


def build_message(settings, rep, paths, analysis=None):
    m = _cfg(settings)
    msg = EmailMessage()
    msg["Subject"] = "[PAPER] NSE autopilot %s" % report.headline(rep)
    msg["From"] = m["from"]
    msg["To"] = ", ".join(m["to"])
    text = _read(paths.get("md")) or report.headline(rep)
    msg.set_content(text)
    page = _read(paths.get("html"))
    if page:
        msg.add_alternative(page, subtype="html")
    for key in ("md", "analysis", "json"):
        p = paths.get(key)
        if p and os.path.isfile(p):
            ctype = mimetypes.guess_type(p)[0] or "text/plain"
            if p.endswith(".md"):
                ctype = "text/markdown"
            main, sub = ctype.split("/", 1)
            with open(p, "rb") as fh:
                msg.add_attachment(fh.read(), maintype=main, subtype=sub, filename=os.path.basename(p))
    return msg


def _read(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def deliver(m, msg, smtp=None, lookup=None):
    """(ok, why). smtp and lookup are swapped in tests."""
    bad = problems(m)
    if bad:
        return False, "; ".join(bad)
    pw = password(m, lookup)
    if not pw:
        return False, ("no app password in the keychain (service %s, account %s): run security "
                       "add-generic-password -s %s -a <from> -w" % (m["keychain_service"], m["from"],
                                                                    m["keychain_service"]))
    try:
        cls = smtp or smtplib.SMTP_SSL
        with cls(m["host"], int(m["port"]), context=ssl.create_default_context(), timeout=30) as s:
            s.login(m["from"], pw)
            if msg is not None:
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        return False, "%s: %s" % (type(e).__name__, ac.tail(str(e), 200))
    return True, "sent to %d recipient%s" % (len(m["to"]), "" if len(m["to"]) == 1 else "s") if msg is not None \
        else "logged in to %s as the sender" % m["host"]


def send_report(settings, rep, paths, analysis=None, smtp=None, lookup=None):
    m = _cfg(settings)
    bad = problems(m)
    if bad:
        return False, "; ".join(bad)
    return deliver(m, build_message(settings, rep, paths, analysis), smtp, lookup)


def check(settings, smtp=None, lookup=None):
    """Log in without sending: proves the address, the keychain entry and the app password."""
    return deliver(_cfg(settings), None, smtp, lookup)


def send_test(settings, smtp=None, lookup=None):
    m = _cfg(settings)
    bad = problems(m)
    if bad:
        return False, "; ".join(bad)
    msg = EmailMessage()
    msg["Subject"] = "[PAPER] NSE autopilot: test email"
    msg["From"] = m["from"]
    msg["To"] = ", ".join(m["to"])
    msg.set_content("The NSE paper autopilot can send email from this Mac. The daily report comes after 15:35 IST "
                    "on trading days. Paper only; not financial advice.")
    return deliver(m, msg, smtp, lookup)


def main(argv=None):
    nc.utf8_stdout()
    args = sys.argv[1:] if argv is None else argv
    if args[:1] not in (["check"], ["test"]):
        print("usage: mail.py check|test", file=sys.stderr)
        return 2
    settings = ac.load_settings(ac.trader_home())
    ok, why = (check if args[0] == "check" else send_test)(settings)
    print("%s: %s" % ("OK" if ok else "FAIL", why))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
