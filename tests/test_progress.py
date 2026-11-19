"""The half-hourly progress email (autopilot/progress.py): when it is due, what it says, and that it never
resends a slot or reaches Gmail in a test."""

import json
import os
import sys
import unittest
from unittest import mock

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import autopilot_common as ac  # noqa: E402
import progress  # noqa: E402
from test_autopilot import DAY, HomeBase, utc  # noqa: E402

JOBS = {"tick": {"loaded": True, "pid": None, "last_exit": 0}, "awake": {"loaded": True, "pid": 123}}
SETTINGS = {"premarket_from": "08:40", "eod_at": "15:35"}


def fake_fetch(price=1340.0):
    def fetch(sym, interval="5m", rng=None, period=None, timeout=20):
        t = int(utc("%s 10:29" % DAY).replace(tzinfo=__import__("datetime").timezone.utc).timestamp())
        return {"chart": {"result": [{"timestamp": [t], "indicators": {"quote": [
            {"open": [price], "high": [price], "low": [price], "close": [price], "volume": [1]}]}}]}}
    return fetch


class SenderStub(object):
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, settings, pm, subj, text):
        self.sent.append((subj, text))
        return (True, "sent") if self.ok else (False, "SMTPAuthenticationError: bad password")


class Due(unittest.TestCase):
    def due(self, hhmm, sent=None, closed=False, eod=None):
        h, m = map(int, hhmm.split(":"))
        return progress.due(h * 60 + m, SETTINGS, dict(progress.DEFAULTS), sent or {}, closed, eod or {})

    def test_the_day(self):
        self.assertIsNone(self.due("08:41"))
        self.assertEqual(self.due("08:42"), ("start", "08:42"))
        self.assertEqual(self.due("08:55"), ("start", "08:42"))       # not sent yet, still inside the grace
        self.assertEqual(self.due("09:00"), ("update", "09:00"))
        self.assertEqual(self.due("09:20"), ("update", "09:00"))
        self.assertIsNone(self.due("09:21"))                          # the Mac slept through it: skipped, not late
        self.assertEqual(self.due("15:30"), ("update", "15:30"))
        self.assertIsNone(self.due("15:35"))                          # the report's own email takes over

    def test_sent_and_failed_slots(self):
        self.assertIsNone(self.due("09:05", {"09:00": {"ok": True, "attempts": 1}}))
        self.assertEqual(self.due("09:05", {"09:00": {"ok": False, "attempts": 2}}), ("update", "09:00"))
        self.assertIsNone(self.due("09:05", {"09:00": {"ok": False, "attempts": 3}}))

    def test_wrapup_only_without_the_report_email(self):
        self.assertIsNone(self.due("16:00", eod={"mail": "sent", "done": "x"}))
        self.assertEqual(self.due("16:00", eod={"mail": "failed: x", "done": "x"}), ("wrapup", "wrapup"))
        self.assertIsNone(self.due("16:10", eod={"started": "x"}))    # still running: wait
        self.assertEqual(self.due("16:30", eod={"started": "x"}), ("wrapup", "wrapup"))
        self.assertIsNone(self.due("18:30", eod={}))

    def test_closed_day_once(self):
        self.assertEqual(self.due("08:42", closed=True), ("closed", "closed"))
        self.assertEqual(self.due("12:00", closed=True), ("closed", "closed"))
        self.assertIsNone(self.due("12:00", {"closed": {"ok": True}}, closed=True))
        self.assertIsNone(self.due("16:00", closed=True))

    def test_slots_every_half_hour(self):
        s = progress.slots(SETTINGS, dict(progress.DEFAULTS))
        self.assertEqual(s[0], ("start", 8 * 60 + 42))
        self.assertEqual([progress.hm(m) for _k, m in s[1:3]], ["09:00", "09:30"])
        self.assertEqual(progress.hm(s[-1][1]), "15:30")
        self.assertEqual(len(s), 15)


class Mails(HomeBase):
    def run_at(self, hhmm, sender, **kw):
        settings = ac.load_settings(self.d)
        cfg = ac.load_cfg(self.d)
        with mock.patch.object(progress.mail, "deliver", side_effect=AssertionError("real Gmail in a test")):
            return progress.run(self.d, settings, cfg, utc("%s %s" % (DAY, hhmm)), sender=sender,
                                fetch=fake_fetch(), jobs=JOBS, running=False, **kw)

    def sent_file(self):
        return self.doc("autopilot", DAY, "progress.json")

    def test_update_has_the_trade_and_the_money(self):
        self.replay(times=("08:45", "10:05", "10:10"))
        stub = SenderStub()
        self.run_at("10:30", stub)
        self.assertEqual(len(stub.sent), 1)
        subj, text = stub.sent[0]
        self.assertIn("[PAPER] NSE autopilot 10:30 update", subj)
        for words in ("WHAT IT IS DOING NOW", "MONEY TODAY", "Made (winning trades", "Lost (losing trades",
                      "TRADES", "RELIANCE", "SIGNALS", "PRE-MARKET", "Next update 11:00 IST"):
            self.assertIn(words, text)
        self.assertNotIn("No paper trades yet today.", text)
        self.assertTrue(self.sent_file()["10:30"]["ok"])

    def test_a_slot_is_sent_once(self):
        stub = SenderStub()
        self.run_at("09:00", stub)
        self.run_at("09:01", stub)
        self.run_at("09:15", stub)
        self.assertEqual(len(stub.sent), 1)
        self.run_at("09:30", stub)
        self.assertEqual(len(stub.sent), 2)

    def test_failures_are_retried_three_times(self):
        stub = SenderStub(ok=False)
        for t in ("09:00", "09:01", "09:02", "09:03", "09:04"):
            self.run_at(t, stub)
        self.assertEqual(len(stub.sent), 3)
        self.assertEqual(self.sent_file()["09:00"]["attempts"], 3)
        self.assertIn("SMTPAuthenticationError", self.sent_file()["09:00"]["error"])

    def test_start_mail_says_it_will_run(self):
        self.replay(times=("08:40", "08:41"))
        stub = SenderStub()
        self.run_at("08:42", stub)
        subj, text = stub.sent[0]
        self.assertIn("trading day %s, running" % DAY, subj)
        self.assertIn("The autopilot WILL run today", text)

    def test_off_config_says_it_will_not_run(self):
        self.set_cfg(autopilot=False)
        stub = SenderStub()
        self.run_at("08:42", stub)
        subj, text = stub.sent[0]
        self.assertIn("will NOT trade", subj)
        self.assertIn("WILL NOT run", text)

    def test_no_wrapup_when_the_report_went_out(self):
        self.replay()                       # through 15:35: the report's email (MailStub) is "sent"
        stub = SenderStub()
        self.run_at("16:00", stub)
        self.assertEqual(stub.sent, [])

    def test_disabled(self):
        self.settings["progress_mail"] = {"enabled": False}
        self.write_settings()
        stub = SenderStub()
        self.run_at("09:00", stub)
        self.assertEqual(stub.sent, [])

    def test_recipients_override(self):
        captured = {}

        def deliver(m, msg, smtp=None, lookup=None):
            captured["to"] = msg["To"]
            return True, "sent"
        settings = dict(ac.load_settings(self.d), progress_mail={"to": ["the-phone-inbox"]})
        with mock.patch.object(progress.mail, "deliver", side_effect=deliver):
            progress.send(settings, progress.cfg_of(settings), "s", "t")
        self.assertEqual(captured["to"], "the-phone-inbox")


if __name__ == "__main__":
    unittest.main()
