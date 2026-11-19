#!/usr/bin/env python3
"""Order tickets, your yes, the order gate and a paper broker for nse-broker. Standard library only.

This tool never talks to Zerodha and never places a real order. It does its
work around the one moment that matters, your yes. Three modes (config "broker"):

  paper   (the default) no broker at all. After your yes, paper-fill fills the
          entry at the latest Yahoo price if a LIMIT buy is marketable, and
          paper-check walks the 1-minute bars since the fill: the stop, the
          target and flat_by, with P&L net of charges. trader/state/paper.json
  manual  prints exactly what to type into Kite, then result / fill record it
  kite    the exact JSON for place_order on your self-hosted Kite MCP server:
          the entry leg first, the SL stop leg after the entry fills. The
          hooks/ gate (a plugin PreToolUse hook that calls gate_check below)
          lets a Kite order tool call through only if it matches a leg you approved

Commands:

  show        each proposed order as a plain ticket. READY only if nse-risk
              passed THIS exact order within the last few minutes, the order is
              today's and fresh, and (for an entry) the entry window is open.
  approve     records your explicit yes for ONE order, word for word. It refuses
              "yes to all", "sure", "ok", a yes that names a different stock or
              amount, a reply that is not what you typed (the UserPromptSubmit
              hook records it), and any order that is not READY.
  autopilot-approve
              paper only, and only with "autopilot": true in config.json: the
              unattended paper run's approval for one READY entry, labelled
              approved_by "autopilot" with no reply. It refuses in manual and
              kite mode; there is no switch around that.
  decline     records a no.
  result      records what happened after the Allow once / Deny card or in Kite:
              placed (with Kite's order id and the FILLED quantity), denied or
              failed; --leg stop for the stop leg once the entry has filled.
  fill        records a fill in trader/portfolio.json (cash and shares); a sell
              also logs the trade's P&L net of charges.
  flatten     builds the SELL MIS ticket that closes a position (all of it, or
              --qty N of it) by flat_by, after cancelling or shrinking the stop.
              Outside paper mode it needs the broker's number (--positions FILE
              from get_positions, or --qty N). It still needs nse-risk and your yes.
  move-stop   builds the modify_order ticket that moves the stop up (breakeven
              or a price); needs the same yes.
  reconcile   compares portfolio.json with a get_positions JSON; --apply fixes it.
  status      what needs attention: no stop, an approval nobody placed, a stale PASS.
  paper-fill  paper mode: fill an approved order at the latest price.
  paper-check paper mode: walk the 1-minute bars; stop, target, flat_by.
  log         today's decisions and paper P&L.
  journal     closed trades: R-multiple, slippage, time held, win rate.

    python3 ticket.py show
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py paper-fill --index 1
    python3 ticket.py paper-check
    python3 ticket.py result --index 1 --status placed --broker-id 2511130001 --filled-qty 56 --filled-price 1334
    python3 ticket.py result --index 1 --leg stop --status placed --broker-id 2511130002
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 56 --price 1344.40
    python3 ticket.py flatten --symbol RELIANCE --positions positions.json
    python3 ticket.py move-stop --symbol RELIANCE --to breakeven
    python3 ticket.py reconcile --positions positions.json --apply
    python3 ticket.py status
    python3 ticket.py log
    python3 ticket.py journal --days 7

Everything is logged in trader/state/broker.json (append only apart from a leg
being marked used). Dates are IST dates. --now "2026-11-13 10:05" and --today pin
the clock for tests only: they work with NSE_TEST_MODE=1 and are refused otherwise.
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

try:                                    # a lock for the gate's read-mark-write; absent on Windows
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
# The ONLY words a yes may carry after "yes": everything else (another ticker, another number, a
# condition) means the reply is not a yes for this ticket. The order's own symbol, its share
# count and "shares" are accepted on top of these; "buy" / "sell" only on a ticket of that side.
YES_VOCAB = {"yes", "please", "pls", "go", "ahead", "place", "it", "this", "that", "order", "the", "trade",
             "confirm", "confirmed", "proceed", "send", "execute", "do", "now", "thanks", "thank", "you",
             "for", "me", "approve", "approved", "and", "ok", "okay", "sure", "to", "it's"}
YES_SIDE = {"buy", "sell"}
MODES = ("paper", "manual", "kite")
MODE_HEAD = {"paper": "PAPER (no broker, no money)",
             "manual": "MANUAL: you type it into Kite, REAL MONEY",
             "kite": "KITE MCP, LIVE ACCOUNT, REAL MONEY"}
TARGET_WORDS = "an alert, not an order (Kite has no OCO for MIS): when it rings, cancel the stop, then sell"
PAPER_NOTE = ("Paper trades: no broker, no money. Fills come from Yahoo 1-minute bars, which are unofficial "
              "and can lag. P&L is net of estimated charges.")
EPS = 1e-9
LEG_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product", "order_type",
              "price", "trigger_price", "validity")
MODIFY_FIELDS = ("order_id", "variety", "quantity", "order_type", "price", "trigger_price", "validity")
MODIFY_IDENTITY = ("exchange", "tradingsymbol", "transaction_type", "product")
STOP_LEG_MINUTES = 30                  # how long the stop leg stays approved once the entry fill is recorded
PROMPT_MAX_MINUTES = 10                # kite mode: the user's last message must be this fresh
UNPLACED_MINUTES = 3                   # status: an approval nobody placed after this long
GATE_ACTIONS = ("place_order", "modify_order", "cancel_order")


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def px(x):
    return nc.inr(x, keep_zeros=True).replace("₹", "")


def kite_px(x):
    """What you type into Kite: 1334 -> "1334.00"."""
    return "%.2f" % float(x)


def money(x):
    return nc.inr(x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0").rstrip(".")


def ist_iso(now_utc):
    """naive UTC -> "2026-11-13T10:05:00+05:30"."""
    return nc.to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        return nc.read_json(path)
    except ValueError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def load_cfg(path):
    """The config, which must exist: broker guards money, and a missing config would silently
    fall back to the built-in limits."""
    try:
        return nc.load_config(path, require=True)
    except nc.ConfigMissing as e:
        raise TicketError(str(e))
    except ValueError as e:
        raise TicketError("%s: %s" % (path, e))


def mode_of(cfg):
    m = str(cfg.get("broker") or "paper").lower()
    if m not in MODES:
        raise TicketError("\"broker\" in the config must be paper, manual or kite, not %r" % cfg.get("broker"))
    return m


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def tool_input(leg):
    """The exact arguments for the Kite MCP place_order tool: Kite's field names,
    quantity and prices as numbers. "why", "plan" and "target" are ours and never sent."""
    if not isinstance(leg, dict):
        return None
    out = {}
    for k in nc.KITE_FIELDS:
        v = leg.get(k)
        if v is None:
            continue
        if k == "quantity":
            v = int(float(v))
        elif k in ("price", "trigger_price"):
            v = float(v)
        out[k] = v
    return out


def legs(order):
    e = order.get("entry") if isinstance(order.get("entry"), dict) else None
    s = order.get("stop") if isinstance(order.get("stop"), dict) else None
    return e, s


def target_of(order):
    t = order.get("target")
    if isinstance(t, dict):
        return _f(t.get("price"))
    return _f(t) or _f((order.get("plan") or {}).get("target"))


def leg_plain(leg, with_exchange=True):
    """BUY 56 RELIANCE, NSE, MIS (intraday), LIMIT 1,334.00, DAY"""
    side = str(leg.get("transaction_type", "")).upper()
    q = _num(leg.get("quantity") or 0)
    sym = leg.get("tradingsymbol", "?")
    prod = str(leg.get("product") or "").upper()
    ot = str(leg.get("order_type") or "").upper()
    if ot == "LIMIT":
        how = "LIMIT %s" % px(leg.get("price"))
    elif ot == "SL":
        how = "SL trigger %s, limit %s" % (px(leg.get("trigger_price")), px(leg.get("price")))
    elif ot == "SL-M":
        how = "SL-M trigger %s" % px(leg.get("trigger_price"))
    else:
        how = ot or "?"
    parts = ["%s %s %s" % (side, q, sym)]
    if with_exchange:
        parts.append(str(leg.get("exchange") or "NSE"))
    parts.append("%s%s" % (prod, " (intraday)" if prod == "MIS" else ""))
    parts.append(how)
    parts.append(str(leg.get("validity") or "DAY").upper())
    return ", ".join(parts)


def loss_detail(order, cfg=None):
    """The worst-case loss of an order at its stop, measured where the stop can fill WORST: at the SL
    leg's limit (the trigger when "loss_basis" is "trigger"), plus the round trip's charges when
    "include_charges". Returns a dict {loss, stop, exit, gross, charges, qty, entry} or None."""
    e, s = legs(order)
    plan = order.get("plan") or {}
    cfg = cfg or {}
    stop = _f((s or {}).get("trigger_price")) or _f(plan.get("stop"))
    q = _f((e or {}).get("quantity"))
    if not stop or not q or not e:
        return None
    ot = str(e.get("order_type") or "").upper()
    entry = _f(e.get("price")) if ot in ("LIMIT", "SL") else None
    entry = entry or _f(plan.get("entry"))
    if not entry:
        return None
    long = str(e.get("transaction_type", "")).upper() == "BUY"
    sym = nc.nse_symbol(order.get("symbol") or e.get("tradingsymbol"))
    limit = _f((s or {}).get("price")) if str((s or {}).get("order_type") or "").upper() == "SL" else None
    if str(cfg.get("loss_basis") or "limit").lower() == "trigger":
        exit_px = nc.dec(stop)
    elif limit:
        exit_px = nc.dec(limit)
    else:
        exit_px = nc.worst_exit_price(stop, sym, cfg, long)
    per = (nc.dec(entry) - exit_px) if long else (exit_px - nc.dec(entry))
    gross = float(per * int(q))
    buy, sell = (entry, float(exit_px)) if long else (float(exit_px), entry)
    ch = nc.intraday_charges(int(q), buy, sell, cfg)["total"]
    total = gross + (ch if cfg.get("include_charges", True) else 0)
    return {"loss": round(total, 2), "stop": stop, "exit": float(exit_px), "gross": round(gross, 2),
            "charges": ch, "qty": int(q), "entry": entry}


def max_loss(order, cfg=None):
    """(loss, stop trigger, gross, charges) at the stop's worst fill (see loss_detail);
    (None, stop, None, None) when it can't be worked out."""
    d = loss_detail(order, cfg)
    if d is None:
        _, s = legs(order)
        return None, _f((s or {}).get("trigger_price")) or _f((order.get("plan") or {}).get("stop")), None, None
    return d["loss"], d["stop"], d["gross"], d["charges"]


def plain_parts(order, cfg=None):
    e, s = legs(order)
    if e is None:
        return ["(no entry leg)"]
    parts = [leg_plain(e)]
    if s is not None:
        parts.append("then the stop: %s, placed once the entry fills" % leg_plain(s, with_exchange=False))
    t = target_of(order)
    if t and s is not None:
        parts.append("target %s: %s" % (px(t), TARGET_WORDS))
    d = loss_detail(order, cfg)
    if d is not None:
        trig = abs(d["exit"] - d["stop"]) < EPS
        where = ("at %s" % px(d["stop"])) if trig else ("at its SL limit %s (trigger %s)" % (px(d["exit"]), px(d["stop"])))
        if (cfg or {}).get("include_charges", True):
            parts.append("max loss about %s with charges if the stop fills %s (%s + %s charges; it can fill "
                         "worse, or not at all, in a gap)" % (money(d["loss"]), where, money(d["gross"]),
                                                              money(d["charges"])))
        else:
            parts.append("max loss about %s if the stop fills %s (it can fill worse in a gap)"
                         % (money(d["loss"]), where))
    if order.get("modify_first"):
        mf = order["modify_first"]
        parts.insert(0, "first shrink the stop (Kite order %s) to %s shares: trigger %s, limit %s" % (
            mf.get("order_id") or "<SL order id>", _num(mf.get("quantity")), px(mf.get("trigger_price")),
            px(mf.get("price"))))
    elif order.get("cancel_first"):
        parts.insert(0, "first cancel the open stop%s" % (
            (" (Kite order %s)" % order["cancel_first"]["order_id"]) if order["cancel_first"].get("order_id") else ""))
    return parts


def plain(order, cfg=None):
    return " / ".join(plain_parts(order, cfg))


def order_qty(order):
    e, _ = legs(order)
    return int(_f((e or {}).get("quantity")) or 0)


def order_side(order):
    e, _ = legs(order)
    return str((e or {}).get("transaction_type", "")).upper()


def parse_yes(reply, order, batch_symbols=(), cfg=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why).

    An allowlist, not a blocklist: the reply must start with "yes" and every other word must
    be in YES_VOCAB, or be this order's symbol, its share count, "shares", or "buy"/"sell" on
    a ticket of that side. Any other word, ticker or number (a yes for TCS, "yes buy INFY",
    "yes 100") is not a yes for this ticket. A bare "y", "ok" or "sure" does not count."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    spaced = re.sub(r"(?<!\d),|,(?!\d)|[;!]", " ", raw)
    chunks = []
    for c in spaced.split():
        c = c.strip("\"'`()[].:-_*").strip()
        if c:
            chunks.append(c)
    words = [c.lower() for c in chunks]
    if not words or words[0] != "yes":
        return False, "only a reply that starts with \"yes\" counts (not y, ok, sure, haan, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = nc.nse_symbol(order.get("symbol") or (legs(order)[0] or {}).get("tradingsymbol"))
    qty = order_qty(order)
    side = order_side(order).lower()
    others = {nc.nse_symbol(s) for s in batch_symbols} - {sym}
    odd = []
    for w, c in zip(words[1:], chunks[1:]):
        if w == sym.lower() or nc.nse_symbol(c) == sym:
            continue
        if nc.nse_symbol(c) in others:
            return False, "the reply names %s, but this order is for %s" % (nc.nse_symbol(c), sym)
        if w in YES_VOCAB or w in ("share", "shares"):
            continue
        if w in YES_SIDE:
            if w == side:
                continue
            return False, "the reply says %s, but this ticket is a %s" % (w, side.upper())
        if re.match(r"^\d+$", w) and qty and int(w) == qty:
            continue
        odd.append(c)
    if odd:
        return False, ("the reply has words this ticket does not cover (%s). A yes is just \"yes\", optionally "
                       "with %s or the %s share count: say it again plainly" % (", ".join(odd), sym, qty or "?"))
    return True, "explicit yes for %s" % plain(order, cfg)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def write_state(path, state):
    nc.write_json(path, state)


def latest(state, fp, today):
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def parse_utc(text):
    """An ISO time ("...Z", "+05:30" or IST wall clock) -> naive UTC, or None."""
    if not text:
        return None
    try:
        return nc.parse_now(str(text).strip())
    except (ValueError, TypeError):
        return None


def _minutes(cfg, key, default):
    v = _f((cfg or {}).get(key))
    return default if v is None or v <= 0 else v


def pass_minutes(cfg):
    return _minutes(cfg, "risk_pass_minutes", 5)


def approval_minutes(cfg):
    """How long an approved leg stays good for the gate (config "approval_minutes", else the PASS time)."""
    return _minutes(cfg, "approval_minutes", pass_minutes(cfg))


def ago(seconds):
    m = abs(seconds) / 60.0
    return "%d min" % round(m) if m >= 1 else "under a minute"


def risk_verdict(risk, fp, today, now=None, cfg=None):
    """(result, why). A PASS is only good on the day it was written (IST) and for risk_pass_minutes
    (config, 5) after its own checked_at (the file's checked_at when the result has none). With no
    `now` the time is not checked (callers that only want the verdict)."""
    if not risk:
        return None, "nse-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") != fp:
            continue
        if now is not None and r.get("verdict") == "PASS":
            when = parse_utc(r.get("checked_at") or risk.get("checked_at"))
            if when is None:
                return None, ("the risk check has no time on it (checked_at), so nobody can tell how old the PASS "
                              "is; run nse-risk again")
            if nc.to_ist(when).date().isoformat() != today:
                return None, ("the PASS was struck on %s IST, not today; run nse-risk again"
                              % nc.to_ist(when).date().isoformat())
            age = (now - when).total_seconds()
            limit = pass_minutes(cfg)
            if age < -60:
                return None, ("the risk check's time is in the future (%s IST): the clock or the file is wrong; "
                              "run nse-risk again" % nc.to_ist(when).strftime("%H:%M"))
            if age > limit * 60 + EPS:
                return None, ("the nse-risk PASS is %s old (%s IST), over the %s-minute limit: the price has moved "
                              "since. Run nse-risk again, then show the ticket" % (ago(age), nc.to_ist(when).strftime("%H:%M"),
                                                                                   _num(limit)))
        return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


WAITING = {"paper": "waiting for ticket.py paper-fill", "manual": "waiting for you to place it in Kite",
           "kite": "waiting for the place_order call"}


def kind_of(order, rv):
    if rv and rv.get("kind"):
        return rv["kind"]
    e, _ = legs(order)
    return "entry" if str((e or {}).get("transaction_type", "")).upper() == "BUY" else "exit"


def entry_clock_problems(order, cfg, now, state_dir=None):
    """Reasons a NEW entry can't go in right now (IST): NSE closed (weekend, holiday, outside the
    session), before no_entries_before, after no_entries_after, or at or after flat_by. Checked again
    at show, approve and paper-fill, because a PASS from 10:05 says nothing about 15:20. Exits never
    come through here."""
    out = []
    try:
        hol, years = nc.load_holidays(cfg, state_dir)
        st = nc.market_status(now, cfg, hol, years)
    except (ValueError, OSError) as e:
        return ["the NSE clock could not be worked out (%s)" % e]
    ist = nc.to_ist(now)
    stamp = ist.strftime("%a %H:%M IST")
    if not st["holidays_known"] and not nc.special_session(ist.date(), cfg):
        return ["NSE's %d holiday list isn't loaded, so %s can't be confirmed as a trading day (nse-alerts "
                "alerts.py holidays --refresh, or put the year's holidays in \"extra_holidays\" and %d in "
                "\"holiday_years\" in config.json)" % (ist.year, ist.strftime("%a %Y-%m-%d"), ist.year)]
    if not st["open"]:
        if st["phase"] == "closed-day":
            return ["the NSE is closed today (%s)" % st["reason"]]
        return ["the NSE market is not open (%s; the session is %s to %s IST)" % (stamp, st["opens"], st["closes"])]
    t = ist.time().replace(second=0, microsecond=0)
    try:
        special = nc.special_session(ist.date(), cfg)
        before = nc.parse_hhmm(cfg.get("no_entries_before"), "no_entries_before") if cfg.get("no_entries_before") else None
        after = nc.parse_hhmm(cfg.get("no_entries_after"), "no_entries_after") if cfg.get("no_entries_after") else None
        flat = nc.parse_hhmm(order.get("flat_by") or (order.get("plan") or {}).get("flat_by") or cfg.get("flat_by")
                             or "15:10", "flat_by")
    except ValueError as e:
        return ["the config's times are wrong (%s)" % e]
    if not special:
        if before is not None and t < before:
            out.append("no new entries before %s IST (it is %s)" % (nc.hhmm(before), nc.hhmm(t)))
        if after is not None and t > after:
            out.append("no new entries after %s IST (it is %s)" % (nc.hhmm(after), nc.hhmm(t)))
    if t >= flat:
        out.append("flat_by %s IST has come (it is %s): no new entry this late, it could not be closed in time"
                   % (nc.hhmm(flat), nc.hhmm(t)))
    return out


def order_age_problems(order, cfg, today, now, created_at=None):
    """(reasons, info). The order's plan.date must be today (IST) and orders.json (or the order's own
    created_at) no older than order_max_age_minutes. nse-risk checks this too: this is a second lock."""
    out, info = [], []
    plan = order.get("plan") or {}
    pdate = str(plan.get("date") or "").strip()[:10]
    if pdate and pdate != today:
        out.append("this order is planned for %s, not today (%s): plan it again (nse-entry, nse-exit, nse-budget)"
                   % (pdate, today))
    elif pdate:
        info.append("plan date %s" % pdate)
    made = order.get("created_at") or created_at
    if made:
        t = parse_utc(made)
        if t is None:
            out.append("orders.json's created_at (%r) is not a time, so the order's age can't be checked" % made)
        else:
            age = (now - t).total_seconds()
            limit = _minutes(cfg, "order_max_age_minutes", 120)
            if age < -120:
                out.append("orders.json says it was written at %s IST, after the clock: the clock or the file is wrong"
                           % nc.to_ist(t).strftime("%H:%M"))
            elif age > limit * 60 + EPS:
                out.append("orders.json was written %s ago (%s IST), over the %s-minute limit: the price has moved; "
                           "size it again with nse-budget" % (ago(age), nc.to_ist(t).strftime("%H:%M"), _num(limit)))
            else:
                info.append("written %s ago (%s IST)" % (ago(max(age, 0)), nc.to_ist(t).strftime("%H:%M")))
    return out, info


def status_of(order, risk, state, today, mode="paper", cfg=None, now=None, created_at=None, state_dir=None):
    """(ready, label, risk_result). READY needs, all at once: a PASS for this exact order (fingerprint)
    written today and no older than risk_pass_minutes; an order dated today and not stale; for an entry,
    the NSE open, inside the entry window and before flat_by; and nobody has decided on it yet."""
    fp = nc.order_fingerprint(order)
    rec = latest(state, fp, today)
    if rec:                              # an order already decided on: say so, whatever the PASS's age
        rv0 = None
        for r in (risk or {}).get("results", []):
            if r.get("fingerprint") == fp:
                rv0 = r
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, %s" % (rec.get("at"), WAITING.get(mode, "")), rv0
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv0
    rv, why = risk_verdict(risk, fp, today, now, cfg)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by nse-risk: " + "; ".join(rv.get("reasons", [])), rv
    t_now, t_then = target_of(order), _f(rv.get("target"))
    if t_then is not None and (t_now is None or abs(t_now - t_then) > 0.005):
        # belt and braces: the fingerprint already covers the entry leg, the stop leg, the target and
        # flat_by, so an order that changed has no PASS; this catches a target that was edited in a result
        return False, ("NOT READY: the target changed since the risk check (%s, now %s); run check.py again"
                       % (px(t_then), px(t_now) if t_now else "none")), rv
    if now is not None:
        problems, _ = order_age_problems(order, cfg or {}, today, now, created_at)
        if kind_of(order, rv) == "entry":
            problems += entry_clock_problems(order, cfg or {}, now, state_dir)
        if problems:
            return False, "NOT READY: " + "; ".join(problems), rv
    return True, "READY: nse-risk PASS, waiting for your yes", rv


# ---------------------------------------------------------------- the order gate (legs, gate_check)

def leg_args(leg, quantity=None):
    """The ten Kite fields a place_order leg is approved on (tag is ours and never compared)."""
    ti = tool_input(leg) or {}
    if quantity is not None:
        ti["quantity"] = int(quantity)
    return {k: ti.get(k) for k in LEG_FIELDS}


def make_leg(fp, role, tool, args, now, minutes, after=None, **extra):
    """One approved order-tool call: what the gate lets through, once, until it expires."""
    leg = {"id": "%s:%s" % (fp, role), "role": role, "tool": tool, "args": args, "status": "approved",
           "approved_at": nc.utc_iso(now), "expires_at": nc.utc_iso(now + datetime.timedelta(minutes=minutes)),
           "used_at": None}
    if after:
        leg["after"] = after
    leg.update(extra)
    return leg


def modify_args(m):
    """The modify_order arguments (Kite Connect's modify_order: variety, order_id, quantity, order_type,
    price, trigger_price, validity) plus the identity fields for the ticket."""
    out = {"variety": m.get("variety") or "regular", "order_id": str(m.get("order_id")),
           "quantity": int(float(m["quantity"])), "order_type": m.get("order_type") or "SL",
           "price": float(m["price"]) if m.get("price") is not None else None,
           "trigger_price": float(m["trigger_price"]) if m.get("trigger_price") is not None else None,
           "validity": m.get("validity") or "DAY"}
    for k in MODIFY_IDENTITY:
        if m.get(k) is not None:
            out[k] = m[k]
    return out


def approval_legs(order, fp, now, cfg, kind="entry"):
    """The legs a yes unlocks, in the order they must go: [cancel or modify the stop], then the order itself.
    The stop leg of an entry does not exist yet: it is built from the FILLED quantity once the fill is recorded."""
    e, _ = legs(order)
    mins = approval_minutes(cfg)
    out, prev = [], None
    mf, cf = order.get("modify_first"), order.get("cancel_first")
    if isinstance(mf, dict) and mf.get("order_id"):
        out.append(make_leg(fp, "modify", "modify_order", modify_args(mf), now, mins))
        prev = out[-1]["id"]
    elif isinstance(cf, dict) and cf.get("order_id"):
        out.append(make_leg(fp, "cancel", "cancel_order",
                            {"variety": cf.get("variety") or "regular", "order_id": str(cf["order_id"])}, now, mins))
        prev = out[-1]["id"]
    role = "flatten" if kind == "exit" else "entry"
    out.append(make_leg(fp, role, "place_order", leg_args(e), now, mins, after=prev,
                        flat_by=order.get("flat_by") or (order.get("plan") or {}).get("flat_by")))
    return out


def stop_leg_for(rec, qty, now):
    """The stop leg of an approved entry, for the quantity that FILLED (not the planned one)."""
    args = rec.get("stop_leg")
    if not isinstance(args, dict) or not qty:
        return None
    args = dict(args, quantity=int(qty))
    return make_leg(rec["fingerprint"], "stop", "place_order", args, now, STOP_LEG_MINUTES)


def add_stop_leg(state, rec, qty, now):
    """Append the stop leg (for `qty` shares) to the approval record `rec`, replacing an unused earlier one."""
    leg = stop_leg_for(rec, qty, now)
    if leg is None:
        return None
    rec["legs"] = [l for l in rec.get("legs", []) if not (l.get("role") == "stop" and l.get("status") != "used")]
    rec.setdefault("legs", []).append(leg)
    return leg


def find_approval(state, fp, today):
    """The approval record of this order today (the latest), or None."""
    found = None
    for r in state.get("orders", []):
        if r.get("fingerprint") == fp and r.get("status") == "approved" and rec_day(r) == today:
            found = r
    return found


def gate_action(tool_name):
    """"mcp__kite__place_order" -> "place_order"."""
    name = str(tool_name or "")
    if name.startswith("mcp__"):
        name = name[len("mcp__"):].split("__")[-1]
    return name


def _unset(v):
    return v is None or v == "" or (not isinstance(v, bool) and _f(v) == 0)


def _same(field, sent, want):
    """Is the sent value the approved one? Numbers as numbers, words case-blind, "no price" = 0 = missing."""
    if field in ("price", "trigger_price"):
        if _unset(sent) or _unset(want):
            return _unset(sent) and _unset(want)
        a_, b_ = _f(sent), _f(want)
        return a_ is not None and b_ is not None and abs(a_ - b_) < 0.0005
    if field == "quantity":
        a_, b_ = _f(sent), _f(want)
        return a_ is not None and b_ is not None and abs(a_ - b_) < EPS
    if field == "order_id":
        return str(sent).strip() == str(want).strip() and str(sent).strip() != ""
    if sent is None or want is None:
        return sent is None and want is None
    return str(sent).strip().lower() == str(want).strip().lower()


def _diffs(action, sent, args):
    """What differs between a tool call and an approved leg: ["price: sent 1335.0, approved 1334.0", ...]."""
    out = []
    if action == "place_order":
        fields = LEG_FIELDS
    elif action == "modify_order":
        fields = MODIFY_FIELDS + tuple(k for k in MODIFY_IDENTITY if k in sent and k in args)
    else:
        fields = ("variety", "order_id")
    for k in fields:
        if not _same(k, sent.get(k), args.get(k)):
            out.append("%s: sent %s, approved %s" % (k, json.dumps(sent.get(k)), json.dumps(args.get(k))))
    extra = [k for k, v in sent.items() if k not in fields and k != "tag" and not _unset(v)
             and not (action == "modify_order" and k in MODIFY_IDENTITY)]
    for k in sorted(extra):
        out.append("%s: sent %s, which the approved ticket does not include" % (k, json.dumps(sent.get(k))))
    return out


class _Locked:
    """An exclusive lock around the gate's read-mark-write, so two calls at once cannot both use a leg."""

    def __init__(self, path):
        self.path, self.fh = path + ".lock", None

    def __enter__(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if fcntl is not None:
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()
        return False


def _gate(tool_name, tool_input_, state_path, config_path, now_utc):
    action = gate_action(tool_name)
    if action not in GATE_ACTIONS:
        if "gtt" in action:
            return False, ("%s: GTT orders are not supported by nse-broker (they rest at the broker with no OCO "
                           "and no stop discipline); nothing was sent" % action)
        return False, "%s is not an order tool nse-broker knows, so the gate cannot match it to an approved ticket" % action
    if not isinstance(tool_input_, dict):
        return False, "the %s call has no arguments the gate can read" % action
    cfg_path = config_path or nc.tpath("config.json")
    try:
        cfg = nc.load_config(cfg_path, require=True)
    except nc.ConfigMissing as e:
        return False, "no config, so no approvals to match: %s" % e
    except (ValueError, OSError) as e:
        return False, "the config is unreadable (%s): the gate fails closed" % e
    path = state_path or nc.tpath("state", "broker.json")
    now = now_utc or nc.utc_now()
    with _Locked(path):
        if not os.path.exists(path):
            return False, ("no approval on record: %s does not exist. Show the ticket, get the user's yes and run "
                           "ticket.py approve first" % path)
        try:
            state = nc.read_json(path)
        except (ValueError, OSError) as e:
            return False, "the broker state is unreadable (%s): the gate fails closed" % e
        if not isinstance(state, dict) or not isinstance(state.get("orders"), list):
            return False, "the broker state is not in the expected shape: the gate fails closed"
        all_legs = [l for rec in state["orders"] if isinstance(rec, dict) for l in (rec.get("legs") or [])
                    if isinstance(l, dict)]
        cands = [l for l in all_legs if l.get("tool") == action and isinstance(l.get("args"), dict)]
        if not cands:
            return False, ("no approved %s on record: nothing the user said yes to covers this call. Show the "
                           "ticket, get the user's yes and run ticket.py approve first" % action)
        match, used, expired, near = [], [], [], []
        for leg in cands:
            d = _diffs(action, tool_input_, leg["args"])
            exp = parse_utc(leg.get("expires_at"))
            live = leg.get("status") == "approved" and exp is not None and now <= exp
            if not d:
                if leg.get("status") == "used":
                    used.append(leg)
                elif exp is None or now > exp:
                    expired.append(leg)
                elif leg.get("status") == "approved":
                    match.append(leg)
            elif live:
                near.append((leg, d))
        for leg in match:
            after = leg.get("after")
            if after:
                prior = [l for l in all_legs if l.get("id") == after]
                if not prior or prior[0].get("status") != "used":
                    return False, ("this %s must wait: the %s step has not gone through yet. Cancel or shrink the "
                                   "stop first, then send the sell" % (leg.get("role"), after.split(":")[-1]))
            if leg.get("role") == "entry":
                try:
                    flat = nc.parse_hhmm(leg.get("flat_by") or cfg.get("flat_by") or "15:10", "flat_by")
                except ValueError:
                    flat = nc.parse_hhmm("15:10")
                if nc.to_ist(now).time().replace(second=0, microsecond=0) >= flat:
                    return False, ("flat_by %s IST has come: no new entry is placed this late (the entry leg %s is "
                                   "refused)" % (nc.hhmm(flat), leg.get("id")))
            leg["status"] = "used"
            leg["used_at"] = nc.utc_iso(now)
            write_state(path, state)
            return True, "matches the approved %s leg %s; marked used, so the same call twice is refused" % (
                leg.get("role"), leg.get("id"))
        if used:
            return False, ("this exact call was already sent once (leg %s, used at %s): a yes covers one call. "
                           "Get a new yes if it must go again" % (used[0].get("id"), used[0].get("used_at")))
        if expired:
            return False, ("the approval for this call expired at %s (an approval is good for %s minutes): run "
                           "nse-risk and ask the user again" % (expired[0].get("expires_at"),
                                                                _num(approval_minutes(cfg))))
        if near:
            near.sort(key=lambda x: len(x[1]))
            leg, d = near[0]
            return False, ("the call differs from the approved ticket (%s). Send exactly what the ticket printed: %s"
                           % ("; ".join(d[:3]), json.dumps({k: v for k, v in leg["args"].items() if v is not None})))
        return False, ("no approved %s matches this call (%d approved leg%s on record, none for this order). Show "
                       "the ticket and get the user's yes first" % (action, len(cands), "" if len(cands) == 1 else "s"))


def gate_check(tool_name, tool_input, state_path=None, config_path=None, now_utc=None):
    """-> (allow: bool, reason: str). The order gate behind hooks/gate.py (a PreToolUse hook).

    tool_name is like "mcp__kite__place_order"; the action is place_order, modify_order or cancel_order
    (place_gtt_* and anything else is refused). ALLOW only when tool_input equals, on the Kite fields
    (variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type, price,
    trigger_price, validity; tag ignored; numbers compared as numbers; for modify_order and cancel_order also
    order_id), a leg that is "approved", not expired and not used, recorded by `approve` in state/broker.json.
    On ALLOW the leg is marked "used" (used_at) and saved before this returns, so the same call twice is
    refused. Everything else is a DENY with a plain reason, and the gate FAILS CLOSED: no approval, a
    mismatch (the reason names the field), an expired or used leg, a sell sent before its cancel/modify step,
    an entry at or after flat_by, corrupt or missing state, no config, an unknown action, any exception."""
    try:
        return _gate(tool_name, tool_input, state_path, config_path, now_utc)
    except Exception as e:                       # noqa: BLE001 - the gate must never let a call through on a bug
        return False, "the order gate failed closed (%s: %s); nothing was sent" % (type(e).__name__, e)


def record_user_prompt(text, path=None, now_utc=None):
    """Save the user's latest message for `approve --reply` to compare with. Never raises."""
    try:
        path = path or nc.tpath("state", "last_prompt.json")
        nc.write_json(path, {"text": str(text if text is not None else "")[:20000],
                             "at": nc.utc_iso(now_utc or nc.utc_now())})
        return True
    except Exception:                            # noqa: BLE001
        return False


def norm_reply(text):
    return " ".join(str(text or "").lower().split())


GATE_MISSING = ("the approval gate is not installed: install this as a plugin, hooks do not ship with copied "
                "skill folders (claude plugin install, see the README), then say yes again")


def verify_reply(reply, prompt_path, now, mode):
    """(verified, problem). The reply Claude passes must equal the user's last message, which the
    UserPromptSubmit hook saved. Kite mode: a missing or stale record refuses (it also proves the hooks
    are not installed). Paper and manual: a missing or stale record is accepted, unverified."""
    doc = None
    try:
        doc = nc.read_json(prompt_path) if prompt_path else None
    except (ValueError, OSError):
        doc = None
    at = parse_utc((doc or {}).get("at")) if isinstance(doc, dict) else None
    fresh = at is not None and -60 <= (now - at).total_seconds() <= PROMPT_MAX_MINUTES * 60
    if not fresh:
        if mode == "kite":
            why = GATE_MISSING if at is None else (
                "the user's last message was recorded %s ago, over %d minutes: either the hook is not working or "
                "the yes is not a fresh reply. Ask the user to say yes again" % (ago((now - at).total_seconds()),
                                                                                  PROMPT_MAX_MINUTES))
            return False, why
        return False, None
    if norm_reply(reply) != norm_reply(doc.get("text")):
        return False, ("the reply you passed (%r) is not what the user last typed (%r). Pass the user's message "
                       "word for word; never write a yes yourself" % (str(reply)[:80], str(doc.get("text"))[:80]))
    return True, None


# ---------------------------------------------------------------- rendering

def modify_call(m):
    """The modify_order arguments exactly as Claude sends them (no identity fields, no nulls)."""
    a_ = modify_args(m)
    return {k: v for k, v in a_.items() if k in MODIFY_FIELDS and v is not None}


def manual_leg_line(leg, when="", qty=None):
    ot = str(leg.get("order_type") or "").upper()
    side = "Buy" if str(leg.get("transaction_type")).upper() == "BUY" else "Sell"
    bits = ["%sSearch %s, pick NSE, press %s." % (when, leg.get("tradingsymbol"), side),
            "Product: Intraday (MIS).", "Order type: %s." % {"LIMIT": "Limit", "MARKET": "Market"}.get(ot, ot),
            "Qty: %s." % _num(qty if qty is not None else leg.get("quantity"))]
    if ot in ("SL", "SL-M"):
        bits.append("Trigger price: %s." % kite_px(leg.get("trigger_price")))
    if ot in ("LIMIT", "SL"):
        bits.append("Price: %s." % kite_px(leg.get("price")))
    bits.append("Validity: Day.")
    return " ".join(bits)


STOP_WHEN = "Once the buy has filled (Kite > Orders shows COMPLETE): "


def manual_lines(order, stop_qty=None):
    """Exactly what to type into Kite. `stop_qty` sizes the stop leg to the shares that really filled."""
    e, s = legs(order)
    out = []
    mf, cf = order.get("modify_first"), order.get("cancel_first")
    if mf:
        out.append("First, in Kite > Orders, modify the pending SL order for %s%s: Qty %s, Trigger price %s, Price %s." % (
            order.get("symbol"), (" (order %s)" % mf["order_id"]) if mf.get("order_id") else "",
            _num(mf.get("quantity")), kite_px(mf.get("trigger_price")), kite_px(mf.get("price"))))
    elif cf:
        out.append("First, in Kite > Orders, cancel the pending SL order for %s%s." % (
            order.get("symbol"), (" (order %s)" % cf["order_id"]) if cf.get("order_id") else ""))
    if e:
        out.append(manual_leg_line(e))
    if s:
        out.append(manual_leg_line(s, STOP_WHEN, stop_qty))
    t = target_of(order)
    if t and s:
        out.append("The target %s is an alert, not an order: when it rings, cancel the SL order in Kite > Orders, "
                   "then Sell %s %s MIS." % (kite_px(t), _num(stop_qty if stop_qty is not None else s.get("quantity")),
                                             order.get("symbol")))
    return out


def age_line(order, cfg, today, now, created_at):
    """"order from plan date 2026-11-13, written 4 min ago (10:05 IST)" for the ticket."""
    if now is None:
        return None
    _, info = order_age_problems(order, cfg, today, now, created_at)
    return ("order: " + ", ".join(info)) if info else "order: no plan date or written time on it"


def render(order, idx, total, cfg, risk, state, today, now=None, created_at=None, state_dir=None):
    mode = mode_of(cfg)
    ready, label, rv = status_of(order, risk, state, today, mode, cfg, now, created_at, state_dir)
    lines = ["ORDER %d of %d, %s" % (idx, total, MODE_HEAD[mode])]
    for part in plain_parts(order, cfg):
        lines.append("  " + part)
    if order.get("flat_by"):
        lines.append("  flat by %s IST" % order["flat_by"])
    al = age_line(order, cfg, today, now, created_at)
    if al:
        lines.append("  " + al)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    e, s = legs(order)
    if mode == "paper" and str((e or {}).get("transaction_type", "")).upper() == "SELL":
        lines.append("  Paper mode: after your yes, ticket.py paper-fill cancels the paper stop and sells at the")
        lines.append("  latest Yahoo price. No broker, no money.")
    elif mode == "paper":
        lines.append("  Paper mode: after your yes, ticket.py paper-fill fills it at the latest Yahoo price (a LIMIT")
        lines.append("  buy only if the price is at or under the limit), and paper-check watches the stop, the")
        lines.append("  target and flat_by on 1-minute bars. No broker, no money.")
    elif mode == "manual":
        lines.append("  Type this into Kite (web or app), exactly:")
        for n, l in enumerate(manual_lines(order), 1):
            lines.append("    %d. %s" % (n, l))
    else:
        if order.get("modify_first"):
            lines.append("  First Claude calls modify_order to shrink the open stop to the shares that stay:")
            lines.append("    " + json.dumps(modify_call(order["modify_first"])))
        elif order.get("cancel_first"):
            cf = order["cancel_first"]
            lines.append("  First Claude calls cancel_order for the open stop:")
            lines.append("    " + json.dumps({"variety": cf.get("variety", "regular"),
                                              "order_id": cf.get("order_id") or "<the SL order id from get_orders>"}))
        lines.append("  Claude will call place_order (your self-hosted Kite MCP) with exactly:")
        for l in json.dumps(tool_input(e), indent=2).splitlines():
            lines.append("    " + l)
        if s:
            lines.append("  Once the entry fills (get_orders shows COMPLETE), Claude calls place_order for the stop:")
            for l in json.dumps(tool_input(s), indent=2).splitlines():
                lines.append("    " + l)
            lines.append("  The target is an alert, not an order: Kite has no OCO for MIS. When it rings: cancel_order")
            lines.append("  the stop, then place_order a SELL MIS (a new ticket: flatten).")
        if str((e or {}).get("order_type", "")).upper() == "MARKET":
            lines.append("  Market orders sent by API may need market protection; if Kite rejects it, use a LIMIT.")
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if mode == "kite":
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, now=None, created_at=None, state_dir=None):
    mode = mode_of(cfg)
    ready, label, rv = status_of(order, risk, state, today, mode, cfg, now, created_at, state_dir)
    loss, stop, gross, ch = max_loss(order, cfg)
    e, s = legs(order)
    _, age_info = order_age_problems(order, cfg, today, now, created_at) if now is not None else ([], [])
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg), "order_age": age_info,
            "plan_date": (order.get("plan") or {}).get("date"),
            "fingerprint": nc.order_fingerprint(order), "broker": mode, "tool": "place_order",
            "tool_input": tool_input(e), "stop_tool_input": tool_input(s), "target": target_of(order),
            "cancel_first": order.get("cancel_first"), "modify_first": order.get("modify_first"),
            "flat_by": order.get("flat_by"),
            "why": order.get("why", ""), "max_loss": loss, "max_loss_gross": gross, "charges_at_stop": ch,
            "stop": stop, "manual": manual_lines(order) if mode == "manual" else None}


# ---------------------------------------------------------------- fills -> portfolio and log

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges=0.0):
    """Record a fill in trader/portfolio.json: cash and shares, a cost lot per buy
    (oldest lots go first on a sell). `charges` (paper mode: the round trip's estimated
    charges, taken on the closing sell) come off the cash. Returns (portfolio, cost
    basis of what was sold)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = nc.nse_symbol(symbol)
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if abs(qty - round(qty)) > EPS:
        raise TicketError("NSE trades whole shares: %s is not a whole number" % _num(qty))
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if nc.nse_symbol(p.get("ticker") or p.get("symbol")) == symbol:
            pos = p
    basis = None
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": int(qty), "cost": cost})
        pos["shares"] = int(round(float(pos.get("shares", 0)) + qty))
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + EPS < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_num(qty), symbol, _num(pos.get("shares", 0)) if pos else 0))
        left, basis = qty, 0.0
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= EPS:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            basis += per * take
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = int(round(float(lot["shares"]) - take))
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > EPS]
        pos["shares"] = int(round(float(pos["shares"]) - qty))
        if pos["shares"] <= 0:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost - float(charges or 0), 2)
        basis = round(basis, 2)
    nc.write_json(portfolio_path, pf)
    return pf, basis


def log_fill(state, now, today, symbol, side, qty, price, extra=None):
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": nc.nse_symbol(symbol), "side": side,
           "qty": int(float(qty)), "price": float(price),
           "plain": "%s %s %s at %s" % (side.upper(), _num(qty), nc.nse_symbol(symbol), px(price))}
    rec.update(extra or {})
    state.setdefault("orders", []).append(rec)
    return rec


def log_close(state, now, today, symbol, qty, buy, sell, reason, cfg, extra=None):
    """The closed trade's P&L net of charges, so nse-risk can count real losses."""
    ch = nc.intraday_charges(int(qty), buy, sell, cfg)["total"]
    gross = round(float(qty) * (float(sell) - float(buy)), 2)
    rec = {"at": ist_iso(now), "date": today, "status": "closed", "symbol": nc.nse_symbol(symbol),
           "side": "sell", "kind": "exit", "qty": int(qty), "entry": float(buy), "exit": float(sell),
           "reason": reason, "pnl_gross": gross, "charges": ch, "pnl_net": round(gross - ch, 2),
           "plain": "closed %s %s: %s to %s, %s (net %s after %s charges)"
                    % (_num(qty), nc.nse_symbol(symbol), px(buy), px(sell), reason, money(gross - ch), money(ch))}
    rec.update(extra or {})
    state.setdefault("orders", []).append(rec)
    return rec


# ---------------------------------------------------------------- 1-minute bars (paper mode)

def parse_bars(doc):
    """A Yahoo v8 chart doc -> [(ist datetime, open, high, low, close)], bars with no data dropped."""
    try:
        r = doc["chart"]["result"][0]
        ts = r.get("timestamp") or []
        q = r["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        raise TicketError("the bars are not a Yahoo chart document")
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        out.append((nc.ist_from_epoch(t), round(float(o), 2), round(float(h), 2), round(float(l), 2),
                    round(float(c), 2)))
    return out


def chart_symbol(doc):
    """The bare NSE symbol a Yahoo chart doc is for ("RELIANCE.NS" -> "RELIANCE"), or None."""
    try:
        return nc.nse_symbol(doc["chart"]["result"][0]["meta"]["symbol"])
    except (KeyError, IndexError, TypeError):
        return None


def read_chart_file(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        raise TicketError("can't read the bars %s: %s" % (path, e.strerror or e))
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def read_bars_file(path):
    return parse_bars(read_chart_file(path))


def fetch_bars(sym, timeout=15):
    url = nc.yahoo_chart_url(sym, "5d", "1m")
    req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return parse_bars(json.loads(raw.decode("utf-8")))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TicketError("couldn't fetch 1-minute bars for %s from Yahoo (%s); try again, or pass --bars FILE"
                          % (sym, getattr(e, "reason", None) or e))


def bars_for(sym, a, cache):
    if sym in cache:
        return cache[sym]
    if getattr(a, "bars", None):
        doc = read_chart_file(a.bars)
        cs = chart_symbol(doc)
        if cs and cs != sym:
            raise TicketError("the bars in %s are for %s, not %s" % (a.bars, cs, sym))
        bars = parse_bars(doc)
    else:
        bars = fetch_bars(sym)
    cache[sym] = bars
    return bars


def done_bars(bars, now_ist, step=1):
    """Bars complete by now (a 1-minute bar stamped 10:04 is complete at 10:05; with step=5, a 5-minute bar
    stamped 10:00 is complete at 10:05)."""
    return [b for b in bars if b[0] + datetime.timedelta(minutes=step) <= now_ist]


# ---------------------------------------------------------------- paper broker

def load_paper(path):
    doc = read_json(path, "paper state", required=False) or {}
    doc.setdefault("note", PAPER_NOTE)
    doc.setdefault("trades", [])
    return doc


def open_trade(paper, symbol):
    for t in paper["trades"]:
        if t["symbol"] == symbol and t["status"] in ("open", "filled"):
            return t
    return None


def close_trade(t, when, price, reason, now, a, cfg, state, note=None):
    """Close a paper position: portfolio, broker log, P&L net of charges."""
    t["status"] = "closed"
    t["exit_time"] = when.strftime("%Y-%m-%d %H:%M")
    t["exit_price"] = float(price)
    t["exit_reason"] = reason
    ch = nc.intraday_charges(t["qty"], t["fill_price"], price, cfg)["total"]
    t["gross"] = round(t["qty"] * (float(price) - t["fill_price"]), 2)
    t["charges"] = ch
    t["pnl_net"] = round(t["gross"] - ch, 2)
    if note:
        t["note"] = note
    apply_fill(a.portfolio, cfg, t["symbol"], "sell", t["qty"], price, t["date"], charges=ch)
    at = nc.to_utc(when)
    log_fill(state, at, t["date"], t["symbol"], "sell", t["qty"], price, {"paper": True, "reason": reason})
    log_close(state, at, t["date"], t["symbol"], t["qty"], t["fill_price"], price, reason, cfg,
              {"paper": True, "fingerprint": t.get("fingerprint")})


def fill_entry(t, when, price, now, a, cfg, state):
    t["status"] = "filled"
    t["fill_time"] = when.strftime("%Y-%m-%d %H:%M")
    t["fill_price"] = float(price)
    apply_fill(a.portfolio, cfg, t["symbol"], "buy", t["qty"], price, t["date"])
    at = nc.to_utc(when)
    log_fill(state, at, t["date"], t["symbol"], "buy", t["qty"], price,
             {"paper": True, "fingerprint": t.get("fingerprint"), "index": t.get("index")})
    if t.get("stop"):
        state["orders"].append({"at": ist_iso(at), "date": t["date"], "index": t.get("index"),
                                "fingerprint": t.get("fingerprint"), "symbol": t["symbol"], "side": "sell",
                                "status": "placed", "leg": "stop", "kind": "stop", "paper": True,
                                "plain": "paper stop: SELL %d %s, %s trigger %s" % (
                                    t["qty"], t["symbol"], t["stop"].get("type", "SL"),
                                    px(t["stop"]["trigger"]))})


STAMP = "%Y-%m-%d %H:%M"


def _stamp(text):
    try:
        return datetime.datetime.strptime(text, STAMP) if text else None
    except ValueError:
        return None


def walk(t, bars, now_ist, cfg=None, step=1):
    """Walk 1-minute bars for one paper trade. Returns a list of events
    (kind, bar time, price, note) and leaves the trade as it stands at now. Only bars that are COMPLETE
    by now are used (a bar stamped 10:04 is complete at 10:05).

    step is the bar length in minutes. The paper broker always walks 1-minute bars (step=1). The archive study
    can replay 5-minute bars (step=5) for days whose 1-minute bars Yahoo has dropped: the rules are the same,
    but the next bar is 5 minutes on, so a target exit waits up to 5 minutes for its market sell and more bars
    touch both the stop and the target (counted as the stop).

    The paper broker is deliberately no kinder than the real one:
      * the target is an ALERT on Kite (no OCO for MIS): when a bar's high reaches it you cancel the stop and
        sell at market, so the exit fills at the NEXT bar's open, not at the target. With no next bar on
        Yahoo yet it fills at the target minus one tick and says so;
      * the stop triggers when a bar's low reaches the trigger and fills at the trigger, but an SL is a
        limit order: if the bar OPENS below its limit (a gap) it does not fill. The position stays open,
        an event ("stop_gap", ...) says so, and the stop fills at the limit only when a later bar's high
        reaches it; otherwise the trade is closed at flat_by at that bar's close;
      * a bar that touches both the stop and the target counts as the stop;
      * "entry_ttl_minutes" (config): a LIMIT buy that has not filled that many minutes after the order is
        cancelled, so a breakout that ran away (or a pullback long after it) never fills late; off when unset;
      * "paper_fill": "through" (config) fills a resting LIMIT buy only when a bar trades at least one tick
        BELOW it (a touch at the limit can leave the order unfilled behind the queue); "touch", the default,
        fills on a touch.

    Yahoo leaves many late-session 1-minute bars empty (most of 15:16-15:29), so empty bars are
    skipped. If the flat_by bar itself is empty, the trade closes at the close of the last real bar at
    or before flat_by, and the note says so."""
    events = []
    day = datetime.date.fromisoformat(t["date"])
    try:
        flat = nc.parse_hhmm(t.get("flat_by") or "15:10", "flat_by")
    except ValueError as e:
        raise TicketError("paper trade %s: %s" % (t.get("id"), e))
    flat_at = datetime.datetime.combine(day, flat)
    ttl = _f((cfg or {}).get("entry_ttl_minutes"))
    ordered_at = _stamp(t.get("ordered_at"))
    expire_at = ordered_at + datetime.timedelta(minutes=ttl) if ttl and ttl > 0 and ordered_at else None
    through = str((cfg or {}).get("paper_fill") or "touch").lower() == "through"
    fill_tick = float(nc.order_tick(t["limit"], t.get("symbol"), cfg)) if through and t.get("limit") else 0.0
    start = _stamp(t["fill_time"] if t["status"] == "filled" else t["ordered_at"])
    if t.get("checked_to"):
        start = max(start, _stamp(t["checked_to"]))
    state = t["status"]
    gap = _stamp(t.get("stop_gap"))              # the bar on which the stop triggered but could not fill
    fill_bar = _stamp(t.get("fill_bar"))         # the bar a resting LIMIT filled on: not read again as a filled bar
    stop = t.get("stop") or {}
    trig, lim = _f(stop.get("trigger")), _f(stop.get("price"))
    target = _f(t.get("target"))
    done = [b for b in done_bars(bars, now_ist, step) if b[0].date() == day]
    by_time = {b[0]: b for b in done}
    bar = datetime.timedelta(minutes=step)
    last_real = None                      # (time, close) of the last real bar at or before flat_by

    def flat_exit():
        if last_real is None:
            return None
        note = "the %s bar has no data on Yahoo: closed at the %s bar's close" % (
            nc.hhmm(flat), nc.hhmm(last_real[0].time()))
        if gap is not None:
            note += "; the stop had gapped through its limit and never filled"
        return ("flat_by", last_real[0], last_real[1], note)

    for when, o, h, l, c in done:
        if when > flat_at and state in ("open", "filled"):
            if state == "open":
                events.append(("cancel", when, None, "the LIMIT buy never filled by flat_by %s" % nc.hhmm(flat)))
                state = "cancelled"
            elif flat_exit():
                events.append(flat_exit())
                state = "closed"
            break
        if when <= flat_at:
            last_real = (when, c)
        if when < start:
            continue
        if state == "open":
            if when >= flat_at:
                events.append(("cancel", when, None, "the LIMIT buy never filled by flat_by %s" % nc.hhmm(flat)))
                state = "cancelled"
                break
            if expire_at is not None and when >= expire_at:
                events.append(("cancel", when, None, "the LIMIT buy did not fill within %s minutes of the order "
                                                     "(entry_ttl_minutes)" % _num(ttl)))
                state = "cancelled"
                break
            if l <= t["limit"] - fill_tick + EPS:
                events.append(("fill", when, min(o, t["limit"]), None))
                state = "filled"
                fill_bar = when
            continue
        if state != "filled":
            break
        if fill_bar is not None and when <= fill_bar:
            continue
        if gap is not None:                           # the stop triggered earlier and is still waiting for its limit
            if when <= gap:
                continue
            if lim is not None and h >= lim - EPS:
                events.append(("stop", when, lim, "the stop gapped through its SL limit %s at %s and sat unfilled; "
                               "it filled at the limit when the %s bar's high reached it" % (
                                   px(lim), gap.strftime("%H:%M"), when.strftime("%H:%M"))))
                state = "closed"
                break
            if when == flat_at:
                events.append(("flat_by", when, c, "the stop gapped through its limit at %s and never filled: sold "
                               "at the %s bar's close" % (gap.strftime("%H:%M"), nc.hhmm(when.time()))))
                state = "closed"
                break
            continue
        if trig and l <= trig + EPS:
            tgt_too = target and h >= target - EPS
            if lim is not None and o < lim - EPS:
                events.append(("stop_gap", when, None,
                               "STOP TRIGGERED BUT NOT FILLED: the %s bar opened at %s, under the SL limit %s, so "
                               "the stop (a limit order) found no buyer at its price. The position is STILL OPEN and "
                               "has no working stop. It fills at %s if the price comes back up to it; otherwise it "
                               "is closed at flat_by %s" % (when.strftime("%H:%M"), px(o), px(lim), px(lim),
                                                            nc.hhmm(flat))))
                gap = when
                continue
            price = o if o < trig else trig
            note = None
            if o < trig:
                note = "gapped below the trigger: filled at the bar's open %s (still at or above the SL limit)" % px(o)
            if tgt_too:
                note = (note + "; " if note else "") + "the same bar touched the target too: counted as the stop"
            events.append(("stop", when, price, note))
            state = "closed"
            break
        if target and h >= target - EPS and when < flat_at:
            nxt = by_time.get(when + bar)
            if nxt is not None:
                events.append(("target", nxt[0], nxt[1],
                               "the target %s was reached on the %s bar; on Kite it is only an alert, so you cancel "
                               "the stop and sell at market: filled at the next bar's open" % (
                                   px(target), when.strftime("%H:%M"))))
            else:
                tick = nc.order_tick(target, t.get("symbol"), cfg)
                price = float(nc.round_to_tick(nc.dec(target) - tick, tick, "down"))
                events.append(("target", when + bar, price,
                               "the target %s was reached on the %s bar and the next bar is not on Yahoo yet: "
                               "filled at the target minus one tick, %s (the alert-then-market-sell round trip "
                               "is never filled at the target itself)" % (px(target), when.strftime("%H:%M"), px(price))))
            state = "closed"
            break
        if when == flat_at:
            events.append(("flat_by", when, c, "closed at the %s bar's close" % nc.hhmm(when.time())))
            state = "closed"
            break
    if state == "open" and expire_at is not None and expire_at < flat_at and \
            now_ist >= expire_at + datetime.timedelta(minutes=1) and not any(e[0] == "cancel" for e in events):
        # the order's time ran out but no bar at or after it has data yet
        events.append(("cancel", expire_at, None, "the LIMIT buy did not fill within %s minutes of the order "
                                                  "(entry_ttl_minutes)" % _num(ttl)))
        return events
    if state in ("open", "filled") and now_ist >= flat_at + datetime.timedelta(minutes=1):
        # flat_by has passed but no bar at or after it has data
        if state == "open":
            events.append(("cancel", flat_at, None, "the LIMIT buy never filled by flat_by %s" % nc.hhmm(flat)))
        elif flat_exit():
            events.append(flat_exit())
    return events


def last_price(bars, now_ist, day=None):
    done = [b for b in done_bars(bars, now_ist) if day is None or b[0].date() == day]
    return (done[-1][0], done[-1][4]) if done else (None, None)


# ---------------------------------------------------------------- commands

def _clock(a):
    given = getattr(a, "now", None) or getattr(a, "today", None)
    if given and not nc.test_mode():
        raise TicketError("--now and --today are test switches and only work with NSE_TEST_MODE=1. A real ticket "
                          "always runs on the real clock, so an old PASS can never be shown as ready on another day")
    try:
        now = nc.parse_now(getattr(a, "now", None))
    except ValueError as e:
        raise TicketError(str(e))
    today = getattr(a, "today", None) or nc.to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-13")
    return now, today


def _state_dir(a):
    return os.path.dirname(os.path.abspath(a.state)) if getattr(a, "state", None) else None


def _prompt_path(a):
    return getattr(a, "prompt_file", None) or os.path.join(_state_dir(a) or nc.tpath("state"), "last_prompt.json")


def _load(a):
    doc = read_json(a.orders, "orders file (run nse-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = load_cfg(a.config)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    created = doc.get("created_at") if isinstance(doc, dict) else None
    return orders, cfg, risk, state, today, now, created


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def _symbol(order):
    return nc.nse_symbol(order.get("symbol") or (legs(order)[0] or {}).get("tradingsymbol"))


def cmd_show(a):
    orders, cfg, risk, state, today, now, created = _load(a)
    sd = _state_dir(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, now, created, sd) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, now, created, sd)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Not financial advice; nothing is "
          "placed without your explicit yes.")
    return 0 if any_ready else 1


def print_calls(order, e, s, a, mode, cfg, stop_qty=None):
    """What happens after the yes, for the mode (kite: the exact JSON Claude sends)."""
    if mode == "paper":
        print("Paper mode: now run ticket.py paper-fill --index %d (no broker, no money)." % a.index)
    elif mode == "manual":
        print("Now type it into Kite, exactly:")
        for n, l in enumerate(manual_lines(order, stop_qty), 1):
            print("  %d. %s" % (n, l))
        print("Then: ticket.py result --index %d --status placed --broker-id <Kite order id> --filled-qty Q "
              "--filled-price P (or --status failed --note ...)." % a.index)
    else:
        if order.get("modify_first"):
            print("First call modify_order with exactly:")
            print(json.dumps(modify_call(order["modify_first"]), indent=2))
        elif order.get("cancel_first"):
            cf = order["cancel_first"]
            print("First call cancel_order with exactly:")
            print(json.dumps({"variety": cf.get("variety", "regular"),
                              "order_id": cf.get("order_id") or "<the SL order id from get_orders>"}, indent=2))
        print("Now call place_order with exactly:")
        print(json.dumps(tool_input(e), indent=2))
        print("The order gate lets exactly this call through, once, for the next %s minutes. Claude's card shows "
              "Allow once / Deny. Then: ticket.py result --index %d --status placed|denied|failed"
              % (_num(approval_minutes(cfg)), a.index))
        if s:
            print("Once the entry fills, ticket.py result --index %d ... --filled-qty Q prints the stop leg for the "
                  "shares that filled (planned stop below, for %s shares):" % (a.index, _num(s.get("quantity"))))
            print(json.dumps(tool_input(s), indent=2))


def cmd_approve(a):
    orders, cfg, risk, state, today, now, created = _load(a)
    mode = mode_of(cfg)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, mode, cfg, now, created, _state_dir(a))
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [_symbol(o) for o in orders], cfg)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    verified, problem = verify_reply(a.reply, _prompt_path(a), now, mode)
    if problem:
        print("Not recorded: %s." % problem)
        return 1
    e, s = legs(order)
    cf = order.get("cancel_first")
    if mode == "kite" and isinstance(cf, dict) and not cf.get("order_id") and not order.get("modify_first"):
        raise TicketError("order %d must cancel the open stop first and its Kite order id is not recorded. Call "
                          "get_orders, find the SL order for %s, and run ticket.py flatten --symbol %s --stop-id ID "
                          "(or --no-stop if there is none), then nse-risk, show and the yes again"
                          % (a.index, _symbol(order), _symbol(order)))
    rec = approval_record(order, a.index, cfg, rv, now, today, mode)
    rec["reply"], rec["reply_verified"] = a.reply, bool(verified)
    if mode in ("kite", "manual"):
        rec["legs"] = approval_legs(order, rec["fingerprint"], now, cfg, rec["kind"])
        if s:
            rec["stop_leg"] = leg_args(s)
    mf = order.get("modify_first")
    if isinstance(mf, dict):                 # a partial exit: what the stop looks like once it is shrunk
        rec["stop_after"] = {"order_id": mf.get("order_id"), "trigger": _f(mf.get("trigger_price")),
                             "limit": _f(mf.get("price")), "qty": int(float(mf.get("quantity") or 0))}
    state.setdefault("orders", []).append(rec)
    write_state(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, cfg)))
    if not verified:
        print("Note: the reply could not be checked against what you typed (no recent message on record; the "
              "UserPromptSubmit hook is not installed or has not run), so it is marked reply_verified=false.")
    print_calls(order, e, s, a, mode, cfg)
    return 0


def approval_record(order, index, cfg, rv, now, today, mode):
    """The approval record approve and autopilot-approve share (the caller adds who approved it)."""
    e, _ = legs(order)
    loss, stop, gross, ch = max_loss(order, cfg)
    if rv and rv.get("max_loss") is not None:
        loss = rv["max_loss"]
    return {"at": ist_iso(now), "date": today, "index": index, "fingerprint": nc.order_fingerprint(order),
            "symbol": _symbol(order), "side": str(e.get("transaction_type", "")).lower(),
            "qty": int(float(e.get("quantity") or 0)), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
            "plain": plain(order, cfg), "status": "approved", "reply": None, "reply_verified": False,
            "broker": mode, "plan_entry": _f(e.get("price")) or _f((order.get("plan") or {}).get("entry")),
            "plan_target": target_of(order), "flat_by": order.get("flat_by")}


def autopilot_problem(cfg):
    """Why the unattended paper autopilot may not approve under this config, or None. Paper only, by code:
    there is no switch that lets it approve a manual or kite order, because nobody is there to say yes."""
    mode = mode_of(cfg)
    if mode != "paper":
        return ("the autopilot is paper only and this config says \"broker\": \"%s\". A real order needs the "
                "user's own yes, every time" % mode)
    if cfg.get("autopilot") is not True:
        return ("the autopilot is off: it needs \"autopilot\": true in config.json (and \"broker\": \"paper\"), "
                "which only the user sets")
    return None


def cmd_autopilot_approve(a):
    """Paper only: the approval an unattended paper run records in place of a user's yes. It is labelled
    approved_by "autopilot" with no reply, so nobody can read it as the user's yes, and it needs the same
    READY ticket as approve (a fresh nse-risk PASS for this exact order, today's order, the entry window)."""
    orders, cfg, risk, state, today, now, created = _load(a)
    problem = autopilot_problem(cfg)
    if problem:
        raise TicketError("autopilot-approve refused: %s" % problem)
    mode = mode_of(cfg)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, mode, cfg, now, created, _state_dir(a))
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    rec = approval_record(order, a.index, cfg, rv, now, today, mode)
    if rec["kind"] != "entry" or rec["side"] != "buy":
        raise TicketError("autopilot-approve takes paper entries (a BUY) only; paper-check closes the trades")
    rec["approved_by"] = "autopilot"
    state.setdefault("orders", []).append(rec)
    write_state(a.state, state)
    print("Autopilot (paper only) approved order %d: %s. No yes was asked for: this is the unattended paper run, "
          "no broker, no money." % (a.index, plain(order, cfg)))
    print("Paper mode: now run ticket.py paper-fill --index %d." % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now, created = _load(a)
    order = _pick(orders, a.index)
    e, _ = legs(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": nc.order_fingerprint(order),
           "symbol": _symbol(order), "side": str((e or {}).get("transaction_type", "")).lower(),
           "plain": plain(order, cfg), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_state(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, cfg)))
    return 0


def _whole(x, what):
    q = _f(x)
    if q is None or q <= 0 or abs(q - round(q)) > EPS:
        raise TicketError("%s wants a whole number of shares, like 56 (got %r)" % (what, x))
    return int(round(q))


def recorded_fill_qty(state, fp, today, default=0):
    """The quantity that FILLED for this order today (the entry's recorded fills), else `default`."""
    got = 0
    for r in state.get("orders", []):
        if r.get("fingerprint") == fp and rec_day(r) == today and r.get("status") == "filled" \
                and r.get("side") == "buy":
            got += int(r.get("qty") or 0)
    return got or default


def cmd_result(a):
    orders, cfg, risk, state, today, now, created = _load(a)
    mode = mode_of(cfg)
    order = _pick(orders, a.index)
    fp = nc.order_fingerprint(order)
    e, s = legs(order)
    if a.leg == "stop":
        if not s:
            raise TicketError("order %d has no stop leg" % a.index)
        entry_done = [r for r in state.get("orders", []) if r.get("fingerprint") == fp and rec_day(r) == today
                      and r.get("status") == "placed" and r.get("leg", "entry") == "entry"]
        if not entry_done:
            raise TicketError("order %d's entry isn't recorded as placed today; the stop leg follows the entry"
                              % a.index)
        qty = recorded_fill_qty(state, fp, today, int(float(s.get("quantity") or 0)))
        new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": _symbol(order),
               "side": str(s.get("transaction_type", "")).lower(), "leg": "stop", "kind": "stop",
               "qty": qty, "status": a.status, "trigger": _f(s.get("trigger_price")), "limit": _f(s.get("price")),
               "plain": "stop: " + leg_plain(dict(s, quantity=qty), with_exchange=False)}
        if a.broker_id:
            new["broker_order_id"] = a.broker_id
        if a.note:
            new["note"] = a.note
        state["orders"].append(new)
        write_state(a.state, state)
        print("Recorded order %d's stop leg (%d shares) as %s." % (a.index, qty, a.status))
        if a.status != "placed":
            print("The position has no stop at the broker. Place it again, or flatten (ticket.py flatten --symbol %s)."
                  % _symbol(order))
        return 0
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    filled = None
    if a.filled_qty or a.filled_price:
        if not (a.filled_qty and a.filled_price):
            raise TicketError("give both --filled-qty and --filled-price (what Kite shows as filled)")
        filled = _whole(a.filled_qty, "--filled-qty")
        planned = int(rec.get("qty") or 0)
        if planned and filled > planned:
            raise TicketError("--filled-qty %d is more than the %d shares this order asked for" % (filled, planned))
        if _f(a.filled_price) is None or _f(a.filled_price) <= 0:
            raise TicketError("--filled-price wants a price, like 1334.00")
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status, "leg": "entry"}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    if filled is not None and a.status == "placed":
        new["filled_qty"] = filled
    state["orders"].append(new)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and filled is not None:
        pf, basis = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], filled, a.filled_price, today)
        log_fill(state, now, today, rec["symbol"], rec["side"], filled, a.filled_price,
                 {"fingerprint": fp, "index": a.index, "kind": rec.get("kind")})
        if rec["side"] == "sell" and basis is not None:
            log_close(state, now, today, rec["symbol"], float(filled), basis / filled, float(a.filled_price), "sold",
                      cfg, {"fingerprint": fp})
        print("Added the fill (%d shares) to %s." % (filled, a.portfolio))
        planned = int(rec.get("qty") or filled)
        if filled < planned:
            print("PARTIAL FILL: %d of %d shares. The stop covers the %d that filled, never the planned %d."
                  % (filled, planned, filled, planned))
        if s and rec.get("kind") == "entry":
            appr = find_approval(state, fp, today)
            leg = add_stop_leg(state, appr, filled, now) if appr else None
            stop_call = tool_input(dict(s, quantity=filled))
            if mode == "kite":
                print("Now the stop leg: place_order with exactly (the gate allows it once, for %d minutes):"
                      % STOP_LEG_MINUTES)
                print(json.dumps(stop_call, indent=2))
            else:
                print("Now the stop leg in Kite: %s" % manual_lines(order, filled)[1 if not (
                    order.get("cancel_first") or order.get("modify_first")) else 2])
            print("Then: ticket.py result --index %d --leg stop --status placed --broker-id <id>" % a.index)
            if filled < planned:
                print("The other %d shares of the entry may still be working at Kite. Cancel them%s, or if they "
                      "fill later, record that with ticket.py fill and resize the stop with ticket.py move-stop."
                      % (planned - filled, (" (cancel_order %s, allowed once)" % json.dumps(
                          {"variety": "regular", "order_id": a.broker_id})) if a.broker_id and mode == "kite" else
                         " in Kite > Orders"))
                if appr and a.broker_id and mode == "kite":
                    appr.setdefault("legs", []).append(make_leg(
                        fp, "cancel_rest", "cancel_order", {"variety": "regular", "order_id": str(a.broker_id)}, now,
                        STOP_LEG_MINUTES))
            elif leg is None and appr is None:
                print("Note: no approval record to hang the stop leg on; the gate will refuse the stop. Run "
                      "ticket.py approve again.")
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if s and rec.get("kind") == "entry":
            print("Then place the stop leg for the shares that filled and record it: ticket.py result --index %d "
                  "--leg stop --status placed" % a.index)
    write_state(a.state, state)
    return 0


def cmd_fill(a):
    cfg = load_cfg(a.config)
    now, _ = _clock(a)
    date = a.date or nc.to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-13")
    sym = nc.nse_symbol(a.symbol)
    state = load_state(a.state)
    appr = None
    if a.side == "buy":                    # the entry this fill belongs to: the stop leg follows it
        for r in state.get("orders", []):
            if r.get("status") == "approved" and r.get("symbol") == sym and rec_day(r) == date \
                    and r.get("kind") == "entry" and r.get("stop_leg"):
                appr = r
    pf, basis = apply_fill(a.portfolio, cfg, sym, a.side, a.qty, a.price, date)
    qty = int(round(float(a.qty)))
    log_fill(state, now, date, sym, a.side, qty, a.price,
             {"fingerprint": appr["fingerprint"], "index": appr.get("index"), "kind": "entry"} if appr else None)
    extra = ""
    if a.side == "sell" and basis is not None:
        rec = log_close(state, now, date, sym, float(qty), basis / qty, float(a.price), a.reason or "sold", cfg)
        extra = " P&L %s net of %s charges." % (money(rec["pnl_net"]), money(rec["charges"]))
    stop_note = ""
    if appr is not None:
        have = recorded_fill_qty(state, appr["fingerprint"], date, 0)
        leg = add_stop_leg(state, appr, have, now)
        if leg:
            stop_note = ("\nThe stop leg for the %d shares that filled: place_order with exactly (the gate allows it "
                         "once, for %d minutes):\n%s" % (have, STOP_LEG_MINUTES, json.dumps(
                             dict(leg["args"], tag=nc.ORDER_TAG), indent=2)))
    write_state(a.state, state)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s.%s%s"
          % (a.side, _num(qty), sym, px(a.price), date, a.portfolio, money(pf["cash"]), extra, stop_note))
    return 0


# ---------------------------------------------------------------- positions at the broker

def broker_rows(doc):
    """The position rows of a Kite get_positions JSON: {"net": [...], "day": [...]} (also under "data", or a bare
    list). "net" is the position, "day" only what traded today, so net wins when both are there."""
    if isinstance(doc, dict) and isinstance(doc.get("data"), (dict, list)):
        doc = doc["data"]
    if isinstance(doc, dict):
        for key in ("net", "positions", "day"):
            if isinstance(doc.get(key), list):
                return [r for r in doc[key] if isinstance(r, dict)]
        raise TicketError("the positions file has no \"net\" list: pass the JSON that get_positions returned")
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    raise TicketError("the positions file is not a get_positions JSON (an object with \"net\", or a list)")


def broker_positions(doc, product="MIS"):
    """{SYMBOL: (net quantity, average_price or None)} for the NSE intraday (MIS) rows of a get_positions
    JSON. A CNC holding or a BSE row is not the intraday position and is ignored."""
    out = {}
    for r in broker_rows(doc):
        sym = nc.nse_symbol(r.get("tradingsymbol") or r.get("symbol"))
        if not sym:
            continue
        ex = str(r.get("exchange") or "NSE").upper()
        pr = str(r.get("product") or product).upper()
        if ex != "NSE" or pr != str(product).upper():
            continue
        q = _f(r.get("quantity", r.get("net_quantity")))
        if q is None:
            continue
        q0, avg0 = out.get(sym, (0, None))
        avg = _f(r.get("average_price")) or avg0
        out[sym] = (int(round(q0 + q)), avg)
    return out


def portfolio_shares(pf, sym):
    return int(round(sum(_f(p.get("shares", p.get("qty"))) or 0 for p in (pf or {}).get("positions", [])
                         if nc.nse_symbol(p.get("ticker") or p.get("symbol")) == sym)))


def effective_stop(state, sym, today, mode="paper"):
    """The stop at the broker as far as the log knows: {order_id, trigger, limit, qty} from the stop leg
    recorded as placed today, then from any move-stop since (kite: only once the gate let its modify_order
    through; manual: once you were told to change it). None when no stop is on record."""
    cur = None
    for r in state.get("orders", []):
        if r.get("symbol") != sym or rec_day(r) != today:
            continue
        if r.get("leg") == "stop" and r.get("status") == "placed":
            cur = {"order_id": r.get("broker_order_id"), "trigger": _f(r.get("trigger")), "limit": _f(r.get("limit")),
                   "qty": r.get("qty")}
        elif r.get("kind") == "modify" and r.get("status") == "approved" and cur is not None:
            done = all(l.get("status") == "used" for l in r.get("legs", [])) if mode == "kite" else True
            if done:
                cur = dict(cur, trigger=_f(r.get("trigger")), limit=_f(r.get("limit")), qty=r.get("qty"))
        elif r.get("stop_after") and r.get("status") == "approved" and cur is not None:     # a partial exit
            mods = [l for l in r.get("legs", []) if l.get("role") == "modify"]
            if mode != "kite" or all(l.get("status") == "used" for l in mods):
                sa = r["stop_after"]
                cur = dict(cur, qty=sa.get("qty"))
    return cur


def cmd_flatten(a):
    cfg = load_cfg(a.config)
    mode = mode_of(cfg)
    sym = nc.nse_symbol(a.symbol)
    now, today = _clock(a)
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    mine = portfolio_shares(pf, sym)
    held, source, sell = None, None, None
    if a.positions:
        rows = broker_positions(read_json(a.positions, "positions file"))
        held, source = rows.get(sym, (0, None))[0], "the broker (get_positions, %s)" % a.positions
    elif a.held is not None:
        held, source = _whole(a.held, "--held"), "the broker (--held, from get_positions)"
    elif mode != "paper":
        if a.qty is None:
            raise TicketError(
                "flatten needs the BROKER's number, not portfolio.json's: if the stop already filled at Zerodha "
                "and nobody recorded it, a sell from portfolio.json leaves you short. Call get_positions (Kite MCP), "
                "save the JSON and run ticket.py flatten --symbol %s --positions FILE (or --qty N with the "
                "quantity get_positions shows; in manual mode read it in Kite > Positions)" % sym)
        held, source = _whole(a.qty, "--qty"), "the broker (--qty, from get_positions)"
        sell = held
    else:
        held, source = mine, a.portfolio
        paper_t = open_trade(load_paper(os.path.join(_state_dir(a), "paper.json")), sym) \
            if os.path.exists(os.path.join(_state_dir(a), "paper.json")) else None
        if paper_t and paper_t["status"] == "filled":
            held, source = paper_t["qty"], "the paper position"
    if sell is None:
        sell = _whole(a.qty, "--qty") if a.qty is not None else held
    notes = []
    if held < 0:
        raise TicketError("the broker shows a SHORT position of %d %s. This pack does not sell short and will not "
                          "build a ticket that adds to it: buy it back in Kite" % (-held, sym))
    if held <= 0:
        raise TicketError("nothing to flatten: %s shows no %s shares. Check the broker (get_positions) and pass "
                          "--qty if you do hold some" % (source, sym))
    if sell > held:
        raise TicketError("can't sell %d %s: %s shows only %d" % (sell, sym, source, held))
    if pf and mine != held:
        notes.append("%s says %d %s, but %s says %d: using the broker's %d. Fix it with ticket.py reconcile "
                     "--positions FILE --apply." % (a.portfolio, mine, sym, source, held, held)
                     if mode != "paper" or a.positions or a.held is not None else
                     "%s says %d %s, the paper position says %d: using %d." % (a.portfolio, mine, sym, held, held))
    limit = _f(a.limit) if a.limit is not None else None
    if a.limit is not None and (limit is None or limit <= 0):
        raise TicketError("--limit wants a price, like --limit 1338.00")
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if nc.nse_symbol(plan.get("symbol")) == sym else None) \
        or cfg.get("flat_by") or "15:10"
    leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
           "quantity": sell, "product": str(cfg.get("product") or "MIS").upper(),
           "order_type": "LIMIT" if limit else "MARKET"}
    if limit:
        leg["price"] = float(nc.round_to_tick(limit, nc.order_tick(limit, sym, cfg), "down"))
    leg["validity"] = "DAY"
    leg["tag"] = nc.ORDER_TAG
    state = load_state(a.state)
    stop = effective_stop(state, sym, today, mode)
    stop_id = a.stop_id or (stop or {}).get("order_id")
    order = {"symbol": sym, "entry": leg, "stop": None, "flat_by": flat_by, "created_at": nc.utc_iso(now),
             "why": "flat by %s IST: closes the %s intraday trade before Zerodha's 15:25 MIS square-off "
                    "(qty %d from %s)" % (flat_by, sym, sell, source)}
    partial = sell < held
    if partial:
        remaining = held - sell
        order["why"] = ("sells %d of the %d %s you hold, keeps %d running with the stop shrunk to %d shares "
                        "(qty from %s)" % (sell, held, sym, remaining, remaining, source))
        trig = _f(a.stop_trigger) or (stop or {}).get("trigger")
        lim = _f(a.stop_limit) or (stop or {}).get("limit")
        if mode != "paper":
            if not (trig and lim and stop_id):
                raise TicketError(
                    "a partial exit has to shrink the stop to the %d shares that stay, and the stop's order id, "
                    "trigger and limit are not all on record for %s: call get_orders and run again with "
                    "--stop-id ID --stop-trigger T --stop-limit L" % (remaining, sym))
            order["modify_first"] = {"what": "shrink the open SL stop to the shares that stay", "variety": "regular",
                                     "order_id": str(stop_id), "quantity": remaining, "order_type": "SL",
                                     "price": lim, "trigger_price": trig, "validity": "DAY", "tradingsymbol": sym}
    elif a.no_stop:
        pass
    else:
        order["cancel_first"] = {"what": "the open SL stop leg", "variety": "regular", "order_id": stop_id}
    old = read_json(a.orders, "orders", required=False)
    old_orders = [o for o in (old.get("orders") or []) if isinstance(o, dict)] if isinstance(old, dict) else []
    made = (old or {}).get("created_at") if isinstance(old, dict) else None
    if not made and old_orders and os.path.exists(a.orders):
        made = nc.utc_iso(datetime.datetime.fromtimestamp(os.path.getmtime(a.orders), datetime.timezone.utc)
                          .replace(tzinfo=None, microsecond=0))
    out_orders, replaced = [], False
    for o in old_orders:                 # only THIS symbol's order is replaced; the others keep their age
        if _symbol(o) == sym:
            if not replaced:
                out_orders.append(order)
                replaced = True
            continue
        if not o.get("created_at") and made:
            o["created_at"] = made
        out_orders.append(o)
    if not replaced:
        out_orders.append(order)
    idx = out_orders.index(order) + 1
    doc = {"created_by": "nse-broker", "created_at": nc.utc_iso(now), "status": "proposed", "broker": mode,
           "note": "Flatten ticket. Nothing is placed until nse-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": out_orders}
    nc.write_json(a.orders, doc)
    print("%s %s: %s (flat by %s IST)." % ("Partial exit" if partial else "Flatten", sym, leg_plain(leg), flat_by))
    others = len(out_orders) - 1
    print("Wrote it to %s as order %d%s." % (a.orders, idx, (" (%s; the other %d order%s kept)" % (
        "it replaced this symbol's earlier proposal" if replaced else "added", others, "" if others == 1 else "s"))
        if others else (" (it replaced this symbol's earlier proposal)" if replaced else "")))
    for n in notes:
        print("Note: " + n)
    if a.qty is None and a.positions is None and a.held is None and mode == "paper":
        print("The qty comes from %s." % source)
    print("Next:")
    print("  1. nse-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show --index %d, then the user's yes for this one order." % idx)
    if mode == "paper":
        print("  3. ticket.py paper-fill --index %d: %s." % (idx, (
            "sells %d of the paper position and leaves the paper stop on the %d that stay" % (sell, held - sell))
            if partial else "cancels the paper stop and sells at the latest price"))
    elif mode == "manual":
        if partial:
            print("  3. In Kite > Orders, modify the pending SL order to %d shares FIRST, or the stop will try to sell "
                  "shares you no longer hold." % (held - sell))
        else:
            print("  3. In Kite > Orders, cancel the pending SL order first, or Kite sells twice and leaves you short.")
        print("     Then the SELL as the ticket says, then ticket.py result.")
    else:
        if partial:
            print("  3. Shrink the stop first, or it will try to sell shares you no longer hold: modify_order %s"
                  % json.dumps(modify_call(order["modify_first"])))
        else:
            print("  3. Cancel the stop first, or both sells can fill and leave you short: cancel_order %s"
                  % json.dumps({"variety": "regular", "order_id": stop_id or "<the SL order id from get_orders>"}))
        print("     (the gate allows the sell only after that step). Then place_order with the ticket, then "
              "ticket.py result.")
        if not stop_id and not a.no_stop and not partial:
            print("  The stop's Kite order id is not on record: call get_orders, then run flatten again with "
                  "--stop-id ID (or --no-stop if no stop is resting). approve will refuse until then.")
        if not limit:
            print("  Market orders sent by API may need market protection; if Kite rejects it, flatten again with")
            print("  --limit a few ticks under the last price.")
    return 0


def cmd_reconcile(a):
    cfg = load_cfg(a.config)
    now, today = _clock(a)
    rows = broker_positions(read_json(a.positions, "positions file"))
    pf = read_json(a.portfolio, "portfolio", required=False)
    mine = {}
    for p in (pf or {}).get("positions", []):
        sym = nc.nse_symbol(p.get("ticker") or p.get("symbol"))
        mine[sym] = mine.get(sym, 0) + int(round(_f(p.get("shares", p.get("qty"))) or 0))
    diffs = []
    for sym in sorted(set(rows) | set(mine)):
        b, m = rows.get(sym, (0, None))[0], mine.get(sym, 0)
        if b != m:
            diffs.append((sym, b, m))
    if not diffs:
        print("portfolio.json matches the broker (%s)." % (", ".join("%s %d" % (k, v[0]) for k, v in sorted(rows.items()) if v[0])
                                                          or "no open intraday positions"))
        return 0
    for sym, b, m in diffs:
        print("%s: the broker shows %d, portfolio.json shows %d." % (sym, b, m))
    if not a.apply:
        print("Nothing changed. Run again with --apply to make portfolio.json match the broker (cash is left alone "
              "unless you pass --cash N from get_margins).")
        return 1
    pf = pf if pf is not None else {"cash": float(cfg.get("budget") or 0), "positions": []}
    pf.setdefault("positions", [])
    for sym, b, m in diffs:
        pos = next((p for p in pf["positions"] if nc.nse_symbol(p.get("ticker") or p.get("symbol")) == sym), None)
        if b < 0:
            print("%s: the broker is SHORT %d. Not recorded (this pack does not hold shorts): buy it back in Kite."
                  % (sym, -b))
            continue
        if b == 0:
            pf["positions"].remove(pos)
            print("%s: removed from portfolio.json (the broker holds none)." % sym)
            continue
        avg = rows[sym][1]
        if pos is None:
            pos = {"ticker": sym, "shares": 0, "lots": []}
            pf["positions"].append(pos)
        if b > m:
            cost = round((avg or 0) * (b - m), 2)
            pos.setdefault("lots", []).append({"id": "%s-r%d" % (sym, len(pos.get("lots", [])) + 1), "date": today,
                                               "shares": b - m, "cost": cost})
        else:
            left = m - b
            for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
                if left <= 0:
                    break
                take = min(left, int(lot["shares"]))
                per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
                lot["cost"] = round(float(lot["cost"]) - per * take, 2)
                lot["shares"] = int(lot["shares"]) - take
                left -= take
            pos["lots"] = [l for l in pos.get("lots", []) if int(l["shares"]) > 0]
        pos["shares"] = b
        print("%s: portfolio.json now shows %d." % (sym, b))
    if a.cash is not None:
        c = _f(a.cash)
        if c is None or c < 0:
            raise TicketError("--cash wants the cash balance in rupees, like --cash 100508.53")
        pf["cash"] = round(c, 2)
        print("Cash set to %s." % money(c))
    nc.write_json(a.portfolio, pf)
    state = load_state(a.state)
    state.setdefault("orders", []).append({"at": ist_iso(now), "date": today, "status": "reconciled",
                                           "kind": "reconcile", "plain": "portfolio.json reconciled with the broker: "
                                           + ", ".join("%s %d (was %d)" % d for d in diffs)})
    write_state(a.state, state)
    return 0


# ---------------------------------------------------------------- move the stop

def breakeven_price(qty, entry, symbol=None, cfg=None):
    """The lowest price on the order tick at which selling `qty` shares bought at `entry` pays back the
    round trip's charges (nc.intraday_charges): net = qty x (P - entry) - charges >= 0. Returns a float."""
    tick = nc.order_tick(entry, symbol, cfg)
    p = nc.round_to_tick(entry, tick, "up")
    for _ in range(20000):
        net = nc.dec(qty) * (p - nc.dec(entry)) - nc.dec(nc.intraday_charges(int(qty), entry, float(p), cfg)["total"])
        if net >= 0:
            return float(p)
        p += tick
    raise TicketError("no break-even price found within 20000 ticks of %s" % px(entry))


def cmd_move_stop(a):
    cfg = load_cfg(a.config)
    mode = mode_of(cfg)
    sym = nc.nse_symbol(a.symbol)
    now, today = _clock(a)
    state = load_state(a.state)
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    paper = trade = None
    if mode == "paper":
        paper = load_paper(a.paper)
        trade = next((t for t in paper["trades"] if t["symbol"] == sym and t["status"] == "filled"), None)
        if trade is None:
            raise TicketError("no filled paper position in %s: nothing to move the stop on" % sym)
        held, entry = trade["qty"], trade["fill_price"]
        cur = {"order_id": None, "trigger": _f((trade.get("stop") or {}).get("trigger")),
               "limit": _f((trade.get("stop") or {}).get("price")), "qty": trade["qty"]}
        if not cur["trigger"]:
            raise TicketError("the paper position in %s has no stop to move" % sym)
    else:
        held = portfolio_shares(pf, sym)
        pos = next((p_ for p_ in pf.get("positions", []) if nc.nse_symbol(p_.get("ticker") or p_.get("symbol")) == sym),
                   None)
        lots = (pos or {}).get("lots") or []
        sh = sum(_f(l.get("shares")) or 0 for l in lots)
        entry = (sum(_f(l.get("cost")) or 0 for l in lots) / sh) if sh else None
        cur = effective_stop(state, sym, today, mode)
        if not cur or not cur.get("trigger"):
            raise TicketError("no stop on record for %s today: record the stop leg first (ticket.py result --index N "
                              "--leg stop --status placed --broker-id ID), or flatten" % sym)
        if a.stop_id:
            cur = dict(cur, order_id=a.stop_id)
        if not cur.get("order_id"):
            raise TicketError("the stop's Kite order id is not on record: call get_orders, then run again with "
                              "--stop-id ID")
    if a.qty is not None:
        held = _whole(a.qty, "--qty")
    if held <= 0:
        raise TicketError("no %s shares held (portfolio.json): nothing to move the stop on" % sym)
    # the last price: a stop is never moved above it, or it would trigger at once
    if a.price is not None:
        last, src = _f(a.price), "your --price"
        if last is None or last <= 0:
            raise TicketError("--price wants a price, like --price 1340.50")
    elif mode == "paper":
        at, last = last_price(bars_for(sym, a, {}), nc.to_ist(now), nc.to_ist(now).date())
        src = "the %s IST 1-minute close" % at.strftime("%H:%M") if at else None
        if last is None:
            raise TicketError("no 1-minute bars for %s today to get a last price from: pass --price P" % sym)
    else:
        last, when, fresh = nc.fresh_price(read_json(a.prices, "prices", required=False), sym, now, cfg=cfg)
        src = "prices.json"
        if last is None or not fresh:
            raise TicketError("no fresh last price for %s (%s): refresh it (nse-alerts quote %s --write %s) or pass "
                              "--price P with the price get_quote shows. A stop is never moved above the last price, "
                              "and a stale price cannot prove it isn't" % (
                                  sym, "not in prices.json" if last is None else "older than %s minutes" % _num(
                                      _minutes(cfg, "price_fresh_minutes", 5)), sym, a.prices))
    tick = nc.order_tick(last, sym, cfg)
    to = str(a.to).strip().lower()
    why = ""
    if to in ("breakeven", "break-even", "be"):
        if entry is None:
            raise TicketError("no cost basis for %s in portfolio.json, so break-even can't be worked out: give "
                              "--to PRICE" % sym)
        new_trigger = breakeven_price(held, entry, sym, cfg)
        ch = nc.intraday_charges(held, entry, new_trigger, cfg)["total"]
        why = "break-even: the lowest price that pays back the round trip's %s charges on %d shares bought at %s" % (
            money(ch), held, px(entry))
    else:
        v = _f(a.to)
        if v is None or v <= 0:
            raise TicketError("--to wants breakeven or a price, like --to 1336.00")
        tk = nc.order_tick(v, sym, cfg)
        if not nc.on_tick(v, tk):
            raise TicketError("%s is not on the %s tick: use %s or %s" % (
                px(v), tk, px(nc.round_to_tick(v, tk, "down")), px(nc.round_to_tick(v, tk, "up"))))
        new_trigger = float(v)
        why = "your price"
    if new_trigger <= cur["trigger"] + EPS and held == int(cur.get("qty") or 0):
        raise TicketError("a stop only moves UP: the stop is already at %s and %s is not higher. Nothing to do"
                          % (px(cur["trigger"]), px(new_trigger)))
    if new_trigger < cur["trigger"] - EPS:
        raise TicketError("a stop only moves UP (it is %s, you asked for %s): a lower stop adds risk" % (
            px(cur["trigger"]), px(new_trigger)))
    if new_trigger >= last - EPS:
        raise TicketError("the new stop %s is not under the last price %s (%s): a sell stop at or above the market "
                          "triggers at once. %s" % (px(new_trigger), px(last), src, (
                              "The trade is not above break-even yet." if to in ("breakeven", "break-even", "be")
                              else "Pick a price under it.")))
    new_limit = float(nc.stop_limit_price(new_trigger, sym, cfg, True))
    m = {"variety": "regular", "order_id": cur.get("order_id") or "(paper)", "quantity": held, "order_type": "SL",
         "price": new_limit, "trigger_price": new_trigger, "validity": "DAY", "tradingsymbol": sym,
         "exchange": "NSE", "transaction_type": "SELL", "product": str(cfg.get("product") or "MIS").upper()}
    worst = None
    if entry:
        worst = held * (new_limit - entry) - nc.intraday_charges(held, entry, new_limit, cfg)["total"]
    plain_line = ("MOVE STOP %s: %s the SL%s from trigger %s / limit %s to trigger %s / limit %s on %d shares (%s)%s"
                  % (sym, "change" if mode != "kite" else "modify_order",
                     (" (Kite order %s)" % cur["order_id"]) if cur.get("order_id") else "", px(cur["trigger"]),
                     px(cur["limit"]) if cur.get("limit") else "?", px(new_trigger), px(new_limit), held, why,
                     ("; if it triggers and fills at the limit you net about %s" % money(worst))
                     if worst is not None else ""))
    print("%s, %s" % (MODE_HEAD[mode], "last price %s from %s" % (px(last), src)))
    print(plain_line)
    fp = hashlib.sha256(json.dumps([sym, str(cur.get("order_id")), new_trigger, new_limit, held, today],
                                   sort_keys=True).encode()).hexdigest()[:16]
    pseudo = {"symbol": sym, "entry": {"tradingsymbol": sym, "transaction_type": "SELL", "quantity": held}}
    if a.reply is None:
        print("Ask the user: \"Move the stop? Reply yes or no.\" With the yes, run the same command again with "
              "--reply \"<their reply, word for word>\". Moving a stop up never adds risk, but it still needs a yes.")
        return 0
    ok, why_no = parse_yes(a.reply, pseudo, [sym], cfg)
    if not ok:
        print("Not recorded: %s. Ask the user again." % why_no)
        return 1
    verified, problem = verify_reply(a.reply, _prompt_path(a), now, mode)
    if problem:
        print("Not recorded: %s." % problem)
        return 1
    for r in state.get("orders", []):
        if r.get("fingerprint") == fp and r.get("status") == "approved" and rec_day(r) == today and r.get("legs") \
                and any(l.get("status") == "approved" and (parse_utc(l.get("expires_at")) or now) >= now
                        for l in r["legs"]):
            raise TicketError("this stop move is already approved (%s) and waiting for the modify_order call" % r["at"])
    rec = {"at": ist_iso(now), "date": today, "fingerprint": fp, "symbol": sym, "side": "sell", "kind": "modify",
           "status": "approved", "reply": a.reply, "reply_verified": bool(verified), "broker": mode,
           "trigger": new_trigger, "limit": new_limit, "qty": held, "order_id": cur.get("order_id"),
           "prev_trigger": cur["trigger"], "plain": plain_line}
    if mode in ("kite", "manual"):
        rec["legs"] = [make_leg(fp, "modify", "modify_order", modify_args(m), now, approval_minutes(cfg))]
    state.setdefault("orders", []).append(rec)
    if mode == "paper":
        trade["stop"] = dict(trade["stop"], trigger=new_trigger, price=new_limit)
        trade.setdefault("stop_moves", []).append({"at": trade_stamp(now), "from": cur["trigger"], "to": new_trigger})
        nc.write_json(a.paper, paper)
        print("Paper: the stop is now trigger %s / limit %s on %d shares." % (px(new_trigger), px(new_limit), held))
    elif mode == "manual":
        print("Now in Kite > Orders, modify the SL order%s: Qty %d, Trigger price %s, Price %s." % (
            (" " + str(cur["order_id"])) if cur.get("order_id") else "", held, kite_px(new_trigger), kite_px(new_limit)))
    else:
        print("Now call modify_order with exactly:")
        print(json.dumps(modify_call(m), indent=2))
        print("The gate lets exactly this call through once, for %s minutes. Claude's card shows Allow once / Deny."
              % _num(approval_minutes(cfg)))
    if not verified:
        print("Note: the reply could not be checked against what you typed; recorded reply_verified=false.")
    write_state(a.state, state)
    return 0


def trade_stamp(now):
    return nc.to_ist(now).strftime(STAMP)


# ---------------------------------------------------------------- status

def status_findings(state_path=None, portfolio_path=None, risk_path=None, config_path=None, now_utc=None):
    """[{"kind", "symbol", "message"}]: what needs attention right now. Never raises."""
    out = []
    try:
        now = now_utc or nc.utc_now()
        today = nc.to_ist(now).date().isoformat()
        state_path = state_path or nc.tpath("state", "broker.json")
        cfg = nc.load_config(config_path or nc.tpath("config.json"))
        mode = mode_of(cfg)
        state = read_json(state_path, "broker state", required=False) or {"orders": []}
        recs = [r for r in state.get("orders", []) if isinstance(r, dict) and rec_day(r) == today]
        pf = read_json(portfolio_path or nc.tpath("portfolio.json"), "portfolio", required=False) or {}
        held = {}
        for p_ in pf.get("positions", []):
            sym = nc.nse_symbol(p_.get("ticker") or p_.get("symbol"))
            held[sym] = held.get(sym, 0) + int(round(_f(p_.get("shares", p_.get("qty"))) or 0))
        filled = {}
        for r in recs:
            if r.get("status") == "filled" and r.get("side") == "buy":
                filled.setdefault(r.get("symbol"), r)
        for sym in sorted(k for k, v in held.items() if v > 0):
            stop = effective_stop(state, sym, today, mode)
            if stop is None:
                if sym in filled:
                    out.append({"kind": "no_stop", "symbol": sym, "message": (
                        "%s: the entry filled (%s) but no stop leg is recorded, so %d shares have no stop at the "
                        "broker. Place the stop now (ticket.py result --leg stop ...) or flatten (ticket.py "
                        "flatten --symbol %s ...)" % (sym, filled[sym].get("plain", ""), held[sym], sym))})
                else:
                    out.append({"kind": "no_stop", "symbol": sym, "message": (
                        "%s: portfolio.json holds %d shares and no stop is on record today. If it is a real open "
                        "position, protect it or flatten it; if it was squared off, ticket.py reconcile --positions "
                        "FILE --apply" % (sym, held[sym]))})
            elif stop.get("qty") and int(stop["qty"]) < held[sym]:
                out.append({"kind": "stop_small", "symbol": sym, "message": (
                    "%s: the stop covers %d of the %d shares held. Resize it: ticket.py move-stop --symbol %s "
                    "--to <the current stop>" % (sym, int(stop["qty"]), held[sym], sym))})
        decided = {"placed", "denied", "failed", "declined", "cancelled"}
        for r in recs:
            if r.get("status") != "approved" or r.get("kind") == "modify":
                continue
            when = parse_utc(r.get("at"))
            later = [x for x in recs if x.get("fingerprint") == r.get("fingerprint") and x.get("status") in decided]
            if when is not None and not later and (now - when).total_seconds() > UNPLACED_MINUTES * 60:
                out.append({"kind": "unplaced", "symbol": r.get("symbol"), "message": (
                    "%s: you said yes at %s IST (%s) but nothing has been recorded since (%s ago). If it went in, "
                    "record it (ticket.py result ...); if not, the approval has lapsed: run nse-risk and ask again"
                    % (r.get("symbol"), nc.to_ist(when).strftime("%H:%M"), r.get("plain", "")[:60],
                       ago((now - when).total_seconds())))})
        risk = read_json(risk_path or nc.tpath("state", "risk.json"), "risk check", required=False)
        if isinstance(risk, dict) and risk.get("date") == today:
            seen = {r.get("fingerprint") for r in recs}
            for r in risk.get("results", []):
                if r.get("verdict") != "PASS" or r.get("fingerprint") in seen:
                    continue
                when = parse_utc(r.get("checked_at") or risk.get("checked_at"))
                if when is not None and (now - when).total_seconds() > pass_minutes(cfg) * 60 + EPS:
                    out.append({"kind": "stale_pass", "symbol": r.get("symbol"), "message": (
                        "%s: the nse-risk PASS from %s IST is %s old (limit %s min) and the order was never "
                        "placed: run nse-risk again before showing or placing it" % (
                            r.get("symbol") or "an order", nc.to_ist(when).strftime("%H:%M"),
                            ago((now - when).total_seconds()), _num(pass_minutes(cfg))))})
    except Exception as e:                       # noqa: BLE001 - a status check must not take a session down
        out.append({"kind": "error", "symbol": None, "message": "the broker status could not be checked: %s" % e})
    return out


def status_report(state_path=None, portfolio_path=None, risk_path=None, config_path=None, now_utc=None,
                  quiet=False):
    """-> str: plain-language lines about an entry filled with no stop recorded, an approved order nobody
    placed (older than 3 minutes), an open position with no stop, a stale PASS. "" when nothing needs attention
    and quiet=True; the one "all clear" line when nothing does and quiet=False. Never raises."""
    try:
        found = status_findings(state_path, portfolio_path, risk_path, config_path, now_utc)
        if not found:
            return "" if quiet else "nse-broker: nothing needs attention (no unprotected position, no approval waiting, no stale PASS)."
        lines = ["nse-broker: %d thing%s need%s attention:" % (len(found), "" if len(found) == 1 else "s",
                                                               "s" if len(found) == 1 else "")]
        lines += ["  - " + f["message"] for f in found]
        return "\n".join(lines)
    except Exception as e:                       # noqa: BLE001
        return "nse-broker: the status could not be worked out (%s)" % e


def cmd_status(a):
    now, today = _clock(a)
    found = status_findings(a.state, a.portfolio, a.risk, a.config, now)
    if a.json:
        print(json.dumps({"ok": not found, "findings": found,
                          "text": status_report(a.state, a.portfolio, a.risk, a.config, now, quiet=True)},
                         indent=2, ensure_ascii=False))
        return 0
    text = status_report(a.state, a.portfolio, a.risk, a.config, now, quiet=a.quiet)
    if text:
        print(text)
    return 0


# ---------------------------------------------------------------- journal

def signed(x, digits=2):
    return ("%+." + str(digits) + "f") % x


def journal_rows(state, today, days):
    """One row per closed trade in the last `days` calendar days (today included)."""
    first = (datetime.date.fromisoformat(today) - datetime.timedelta(days=max(days, 1) - 1)).isoformat()
    recs = [r for r in state.get("orders", []) if isinstance(r, dict) and first <= rec_day(r) <= today]
    appr, fills = {}, {}
    for r in recs:
        key = (rec_day(r), r.get("symbol"))
        if r.get("status") == "approved" and r.get("kind") == "entry":
            appr[key] = r
        elif r.get("status") == "filled" and r.get("side") == "buy":
            fills.setdefault(key, []).append(r)
    rows = []
    for r in recs:
        if r.get("status") != "closed" or _f(r.get("pnl_net")) is None:
            continue
        key = (rec_day(r), r.get("symbol"))
        ap, fl = appr.get(key), fills.get(key) or []
        qty = int(r.get("qty") or 0)
        entry, exit_ = _f(r.get("entry")), _f(r.get("exit"))
        planned = _f((ap or {}).get("max_loss"))
        if planned is not None and ap and ap.get("qty"):
            planned = planned * qty / float(ap["qty"])
        net = _f(r["pnl_net"])
        plan_entry = _f((ap or {}).get("plan_entry"))
        reason = r.get("reason")
        ref = {"stop": _f((ap or {}).get("stop")), "target": _f((ap or {}).get("plan_target"))}.get(reason)
        held = None
        t0 = parse_utc(fl[0].get("at")) if fl else None
        t1 = parse_utc(r.get("at"))
        if t0 and t1:
            held = max(0, int(round((t1 - t0).total_seconds() / 60.0)))
        rows.append({"date": rec_day(r), "symbol": r.get("symbol"), "qty": qty, "entry": entry, "exit": exit_,
                     "reason": reason, "gross": _f(r.get("pnl_gross")), "charges": _f(r.get("charges")), "net": net,
                     "planned_max_loss": round(planned, 2) if planned else None,
                     "r": round(net / planned, 2) if planned else None,
                     "plan_entry": plan_entry,
                     "entry_slippage": round(entry - plan_entry, 2) if entry is not None and plan_entry else None,
                     "exit_ref": ref, "exit_vs_ref": round(exit_ - ref, 2) if exit_ is not None and ref else None,
                     "minutes_held": held, "paper": bool(r.get("paper")), "at": r.get("at"),
                     "approved_by": (ap or {}).get("approved_by") or ("user" if ap else None)})
    rows.sort(key=lambda x: (x["date"], x.get("at") or ""))
    return rows


def journal_summary(rows):
    n = len(rows)
    wins = sum(1 for x in rows if x["net"] > 0)
    rs = [x["r"] for x in rows if x["r"] is not None]
    return {"trades": n, "wins": wins, "losses": sum(1 for x in rows if x["net"] < 0),
            "win_rate_pct": round(100.0 * wins / n, 1) if n else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "total_net": round(sum(x["net"] for x in rows), 2),
            "total_charges": round(sum(x["charges"] or 0 for x in rows), 2)}


def cmd_journal(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = journal_rows(state, today, a.days)
    by_day = {}
    for x in rows:
        by_day.setdefault(x["date"], []).append(x)
    days = [dict(journal_summary(v), date=k) for k, v in sorted(by_day.items())]
    summary = journal_summary(rows)
    if a.json:
        print(json.dumps({"to": today, "days_back": a.days, "trades": rows, "days": days, "summary": summary},
                         indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No closed trades in the last %d day%s (to %s)." % (a.days, "" if a.days == 1 else "s", today))
        return 0
    print("JOURNAL: closed trades, last %d day%s to %s (net of charges; R = net / the planned worst-case loss)"
          % (a.days, "" if a.days == 1 else "s", today))
    for d in days:
        print(d["date"])
        for x in by_day[d["date"]]:
            ins = "in %s%s" % (px(x["entry"]), (" (%s vs plan %s)" % (signed(x["entry_slippage"]), px(x["plan_entry"]))
                                                 if x["entry_slippage"] is not None else ""))
            outs = "out %s %s%s" % (px(x["exit"]), x["reason"], (" (%s vs %s %s)" % (
                signed(x["exit_vs_ref"]), "stop" if x["reason"] == "stop" else "target", px(x["exit_ref"]))
                if x["exit_vs_ref"] is not None else ""))
            print("  %-10s %d sh  %s  %s  held %s" % (x["symbol"], x["qty"], ins, outs,
                                                      ("%d min" % x["minutes_held"]) if x["minutes_held"] is not None
                                                      else "n/a"))
            print("  %-10s gross %s, charges %s, net %s, R %s%s%s" % (
                "", money(x["gross"]), money(x["charges"]), money(x["net"]),
                signed(x["r"]) if x["r"] is not None else "n/a",
                (" (planned max loss %s)" % money(x["planned_max_loss"])) if x["planned_max_loss"] else "",
                "  [paper]" if x["paper"] else ""))
        print("  day: %d trade%s, %d win%s, net %s" % (d["trades"], "" if d["trades"] == 1 else "s", d["wins"],
                                                         "" if d["wins"] == 1 else "s", money(d["total_net"])))
    print("ALL DAYS: %d trade%s, win rate %s%%, average R %s, total net %s after %s charges." % (
        summary["trades"], "" if summary["trades"] == 1 else "s", ("%g" % summary["win_rate_pct"]),
        signed(summary["avg_r"]) if summary["avg_r"] is not None else "n/a", money(summary["total_net"]),
        money(summary["total_charges"])))
    return 0


def cmd_paper_fill(a):
    orders, cfg, risk, state, today, now, created = _load(a)
    mode = mode_of(cfg)
    if mode != "paper":
        raise TicketError("paper-fill is for \"broker\": \"paper\"; this config says %s" % mode)
    order = _pick(orders, a.index)
    fp = nc.order_fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        ready, label, _ = status_of(order, risk, state, today, mode, cfg, now, created, _state_dir(a))
        raise TicketError("order %d has no yes recorded today (%s); paper-fill only follows an approval"
                          % (a.index, label))
    e, s = legs(order)
    sym = _symbol(order)
    now_ist = nc.to_ist(now)
    if rec.get("kind") != "exit" and str(e.get("transaction_type", "")).upper() == "BUY":
        # a PASS and a yes from 10:05 say nothing about 15:20: look at the clock again before any new entry
        problems = entry_clock_problems(order, cfg, now, _state_dir(a))
        said = parse_utc(rec.get("at"))
        if said is not None and (now - said).total_seconds() > approval_minutes(cfg) * 60 + EPS:
            problems.append("your yes was %s ago (over the %s-minute approval window)"
                            % (ago((now - said).total_seconds()), _num(approval_minutes(cfg))))
        if problems:
            raise TicketError("not filling order %d: %s. Nothing was bought. Run nse-risk again and ask for a new "
                              "yes if you still want it" % (a.index, "; ".join(problems)))
    if a.price is not None:
        price, at = _f(a.price), now_ist
        if price is None or price <= 0:
            raise TicketError("--price wants a price, like --price 1334.00")
        src = "your --price"
    else:
        bars = bars_for(sym, a, {})
        at, price = last_price(bars, now_ist, now_ist.date())
        if price is None:
            raise TicketError("no 1-minute bars for %s today up to %s IST" % (sym, now_ist.strftime("%H:%M")))
        src = "the %s IST 1-minute close on Yahoo" % at.strftime("%H:%M")
    paper = load_paper(a.paper)
    side = str(e.get("transaction_type", "")).upper()
    ot = str(e.get("order_type", "")).upper()
    qty = int(float(e.get("quantity")))
    planned_qty = qty
    if getattr(a, "filled_qty", None) and side == "BUY":       # rehearse a partial fill: stop and exit follow the filled shares
        qty = _whole(a.filled_qty, "--filled-qty")
        if qty > planned_qty:
            raise TicketError("--filled-qty %d is more than the %d shares ordered" % (qty, planned_qty))
    limit = _f(e.get("price"))
    stamp = now_ist.strftime("%Y-%m-%d %H:%M")
    placed = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": sym,
              "side": side.lower(), "plain": rec["plain"], "status": "placed", "leg": "entry", "paper": True,
              "broker_order_id": "PAPER-%s-%s-%d" % (sym, now_ist.strftime("%H%M"), a.index)}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            placed[k] = rec[k]
    if side == "BUY" and qty != planned_qty:
        placed["qty"], placed["planned_qty"] = qty, planned_qty
    if side == "SELL":
        t = open_trade(paper, sym)
        if ot == "LIMIT" and price < limit - EPS:
            raise TicketError("the SELL LIMIT %s isn't marketable: the latest price is %s. Nothing filled."
                              % (px(limit), px(price)))
        if t and t["status"] == "filled" and qty > t["qty"]:
            raise TicketError("can't sell %d %s: the paper position is %d" % (qty, sym, t["qty"]))
        state["orders"].append(placed)
        if t and t["status"] == "filled" and qty < t["qty"]:
            # a partial exit: the sold shares are a closed trade of their own, the paper stop stays on the rest
            part = dict(t, id="%s-p%d" % (t["id"], 1 + sum(1 for x in paper["trades"]
                                                           if str(x.get("id", "")).startswith(t["id"] + "-p"))),
                        qty=qty)
            paper["trades"].append(part)
            close_trade(part, now_ist, price, "partial", now, a, cfg, state)
            t["qty"] -= qty
            nc.write_json(a.paper, paper)
            write_state(a.state, state)
            print("Paper: sold %d of the %d %s at %s (%s). P&L on those %d: %s net of %s charges. The paper stop now "
                  "covers the %d that stay." % (qty, qty + t["qty"], sym, px(price), src, qty, money(part["pnl_net"]),
                                                money(part["charges"]), t["qty"]))
            return 0
        if t and t["status"] == "filled":
            close_trade(t, now_ist, price, "sold", now, a, cfg, state)
            nc.write_json(a.paper, paper)
            write_state(a.state, state)
            print("Paper: cancelled the stop and sold %d %s at %s (%s). P&L %s net of %s charges."
                  % (t["qty"], sym, px(price), src, money(t["pnl_net"]), money(t["charges"])))
            return 0
        ch = 0.0
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        held = [p for p in pf.get("positions", []) if nc.nse_symbol(p.get("ticker") or p.get("symbol")) == sym]
        if held and held[0].get("lots"):
            avg = sum(float(l["cost"]) for l in held[0]["lots"]) / max(sum(float(l["shares"]) for l in held[0]["lots"]), 1)
            ch = nc.intraday_charges(qty, avg, price, cfg)["total"]
        pf, basis = apply_fill(a.portfolio, cfg, sym, "sell", qty, price, today, charges=ch)
        log_fill(state, now, today, sym, "sell", qty, price, {"paper": True})
        if basis is not None:
            log_close(state, now, today, sym, qty, basis / qty, price, "sold", cfg, {"paper": True})
        write_state(a.state, state)
        print("Paper: sold %d %s at %s (%s)." % (qty, sym, px(price), src))
        return 0
    if open_trade(paper, sym):
        raise TicketError("there is already an open paper trade in %s; flatten it first" % sym)
    t = {"id": "%s-%s-%d" % (sym, today, len(paper["trades"]) + 1), "date": today, "index": a.index,
         "fingerprint": fp, "symbol": sym, "qty": qty, "entry_type": ot, "limit": limit, "ordered_at": stamp,
         "status": "open", "stop": None, "target": target_of(order), "flat_by": order.get("flat_by")
         or cfg.get("flat_by") or "15:10", "max_loss": rec.get("max_loss")}
    if s:
        t["stop"] = {"type": str(s.get("order_type", "SL")).upper(), "trigger": _f(s.get("trigger_price")),
                     "price": _f(s.get("price"))}
    paper["trades"].append(t)
    state["orders"].append(placed)
    if ot == "MARKET" or (limit is not None and price <= limit + EPS):
        fill_entry(t, now_ist, price, now, a, cfg, state)
        msg = ("Paper: bought %d %s at %s (%s)%s. The paper stop is in at %s; the target %s is watched by "
               "paper-check." % (qty, sym, px(price), src,
                                 (", at or under your LIMIT %s" % px(limit)) if ot == "LIMIT" else "",
                                 px(t["stop"]["trigger"]) if t["stop"] else "none",
                                 px(t["target"]) if t["target"] else "none"))
    else:
        msg = ("Paper: the LIMIT buy at %s isn't marketable (the latest price is %s, %s). It rests, open; "
               "paper-check fills it if the price comes down to it before flat_by %s."
               % (px(limit), px(price), src, t["flat_by"]))
    nc.write_json(a.paper, paper)
    write_state(a.state, state)
    print(msg)
    return 0


def cmd_paper_check(a):
    cfg = load_cfg(a.config)
    if mode_of(cfg) != "paper":
        raise TicketError("paper-check is for \"broker\": \"paper\"; this config says %s" % mode_of(cfg))
    now, today = _clock(a)
    now_ist = nc.to_ist(now)
    paper = load_paper(a.paper)
    state = load_state(a.state)
    live = [t for t in paper["trades"] if t["status"] in ("open", "filled")]
    lines, cache = [], {}
    for t in live:
        bars = bars_for(t["symbol"], a, cache)
        for kind, when, price, note in walk(t, bars, now_ist, cfg):
            if kind == "fill":
                fill_entry(t, when, price, now, a, cfg, state)
                t["fill_bar"] = when.strftime(STAMP)
                lines.append("%s %s: the LIMIT buy filled at %s (%s IST bar)." % (
                    t["id"], t["symbol"], px(price), when.strftime("%H:%M")))
            elif kind == "cancel":
                t["status"] = "cancelled"
                t["exit_reason"] = "cancelled"
                state["orders"].append({"at": ist_iso(now), "date": t["date"], "index": t.get("index"),
                                        "fingerprint": t.get("fingerprint"), "symbol": t["symbol"],
                                        "side": "buy", "status": "cancelled", "paper": True,
                                        "plain": "paper LIMIT buy cancelled: " + note})
                lines.append("%s %s: %s; cancelled." % (t["id"], t["symbol"], note))
            elif kind == "stop_gap":
                t["stop_gap"] = when.strftime(STAMP)
                state["orders"].append({"at": ist_iso(now), "date": t["date"], "symbol": t["symbol"],
                                        "kind": "stop_gap", "status": "warning", "paper": True,
                                        "fingerprint": t.get("fingerprint"), "plain": "paper stop gapped: " + note})
                lines.append("%s %s: !!! %s." % (t["id"], t["symbol"], note))
            else:
                close_trade(t, when, price, kind, now, a, cfg, state, note)
                word = {"stop": "STOPPED OUT", "target": "TARGET HIT", "flat_by": "FLAT BY %s" % t["flat_by"]}[kind]
                lines.append("%s %s: %s at %s (%s IST bar). Gross %s, charges %s, net %s.%s"
                             % (t["id"], t["symbol"], word, px(price), when.strftime("%H:%M"), money(t["gross"]),
                                money(t["charges"]), money(t["pnl_net"]), (" " + note[:1].upper() + note[1:] + ".")
                                if note else ""))
        if t["status"] in ("open", "filled"):
            at, last = last_price(bars, now_ist, datetime.date.fromisoformat(t["date"]))
            if at is not None:
                # the START of the last bar read: Yahoo revises and finishes bars late, so the next
                # check reads that bar again instead of trusting what it saw the first time
                t["checked_to"] = at.strftime(STAMP)
            if t["status"] == "filled" and last is not None:
                gross = round(t["qty"] * (last - t["fill_price"]), 2)
                ch = nc.intraday_charges(t["qty"], t["fill_price"], last, cfg)["total"]
                lines.append("%s %s: still open, %d at %s; last %s (%s IST): %s gross, %s after charges if sold "
                             "there. Stop %s%s, target %s, flat by %s."
                             % (t["id"], t["symbol"], t["qty"], px(t["fill_price"]), px(last), at.strftime("%H:%M"),
                                money(gross), money(gross - ch), px(t["stop"]["trigger"]) if t["stop"] else "none",
                                " (TRIGGERED, NOT FILLED since %s: no working stop)" % t["stop_gap"][11:]
                                if t.get("stop_gap") else "", px(t["target"]) if t["target"] else "none", t["flat_by"]))
            elif t["status"] == "open":
                lines.append("%s %s: the LIMIT buy at %s is still resting." % (t["id"], t["symbol"], px(t["limit"])))
    nc.write_json(a.paper, paper)
    write_state(a.state, state)
    closed = [t for t in paper["trades"] if t["status"] == "closed" and t["date"] == today]
    if not live and not closed:
        print("No open paper trades.")
    for l in lines:
        print(l)
    if closed:
        net = sum(t["pnl_net"] for t in closed)
        print("Paper P&L today (%s): %s net of %s charges over %d trade%s." % (
            today, money(net), money(sum(t["charges"] for t in closed)), len(closed), "" if len(closed) == 1 else "s"))
    print("Paper only: no broker, no money. Yahoo bars are unofficial and can lag. Not financial advice.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        if r.get("approved_by") == "autopilot":
            reply = " by the paper autopilot (no yes asked)"
        loss = (" max loss %s" % money(r["max_loss"])) if r.get("max_loss") is not None and \
            r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed" and r.get("leg", "entry") == "entry")
    closed = [r for r in rows if r.get("status") == "closed" and _f(r.get("pnl_net")) is not None]
    tail = ""
    if closed:
        tail = " Closed: %d, net %s after charges." % (len(closed), money(sum(_f(r["pnl_net"]) for r in closed)))
    print("%d placed%s.%s" % (placed, "" if a.all else " today", tail))
    return 0


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Order tickets, your yes and a paper broker (nse-broker). "
                                             "Never places a real order.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-13 10:05\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default=nc.tpath("orders.json"))
        p.add_argument("--config", default=nc.tpath("config.json"))
        p.add_argument("--risk", default=nc.tpath("state", "risk.json"))
        p.add_argument("--state", default=nc.tpath("state", "broker.json"))
        clock(p)

    def paper_files(p):
        p.add_argument("--paper", default=nc.tpath("state", "paper.json"))
        p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
        p.add_argument("--bars", help="a Yahoo v8 1-minute chart JSON (or .gz) instead of fetching it")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p.add_argument("--prompt-file", help="where the UserPromptSubmit hook saves the user's last message "
                                         "(default: state/last_prompt.json beside the broker state)")
    p = sub.add_parser("autopilot-approve", help="paper only, \"autopilot\": true in config.json: the unattended "
                                                 "paper run's approval, labelled as such (never a user's yes)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card or in Kite")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--leg", choices=["entry", "stop"], default="entry")
    p.add_argument("--broker-id", help="Kite's order id")
    p.add_argument("--note")
    p.add_argument("--filled-qty", help="the shares that FILLED (the stop is sized to this, not the plan)")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--reason", help="for a sell: stop, target, flat_by, sold")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    p = sub.add_parser("flatten", help="a SELL MIS ticket that closes a position (or part of it); needs the "
                                       "broker's number outside paper mode")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="outside paper mode: the shares get_positions shows (a full flatten), or with "
                                 "--positions/--held the shares to SELL (a partial exit). Paper: shares to sell "
                                 "(default all)")
    p.add_argument("--positions", help="a get_positions JSON file: the broker's number")
    p.add_argument("--held", help="the shares the broker holds, when you sell only --qty of them")
    p.add_argument("--limit", help="a LIMIT sell at this price instead of MARKET")
    p.add_argument("--stop-id", help="the Kite order id of the open SL stop (from get_orders)")
    p.add_argument("--stop-trigger", help="a partial exit: the stop's current trigger, if not on record")
    p.add_argument("--stop-limit", help="a partial exit: the stop's current limit, if not on record")
    p.add_argument("--no-stop", action="store_true", help="no stop is resting at the broker: nothing to cancel")
    p.add_argument("--orders", default=nc.tpath("orders.json"), help="where the ticket is written")
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p.add_argument("--plan", default=nc.tpath("plan.json"))
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    clock(p)
    p = sub.add_parser("reconcile", help="compare portfolio.json with a get_positions JSON")
    p.add_argument("--positions", required=True, help="a get_positions JSON file")
    p.add_argument("--apply", action="store_true", help="make portfolio.json match the broker")
    p.add_argument("--cash", help="with --apply: set the cash balance (from get_margins)")
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    clock(p)
    p = sub.add_parser("move-stop", help="move the stop up (breakeven or a price): a modify_order ticket")
    p.add_argument("--symbol", required=True)
    p.add_argument("--to", required=True, help="breakeven, or a price")
    p.add_argument("--reply", help="the user's yes, word for word: records the approval (without it only the "
                                   "ticket is shown)")
    p.add_argument("--qty", help="the shares the stop should cover (default: what portfolio.json holds)")
    p.add_argument("--price", help="the last price (default: --prices, or the paper bars)")
    p.add_argument("--prices", default=nc.tpath("prices.json"))
    p.add_argument("--stop-id", help="the Kite order id of the open SL stop, if not on record")
    p.add_argument("--prompt-file")
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p.add_argument("--paper", default=nc.tpath("state", "paper.json"))
    p.add_argument("--bars", help="a Yahoo v8 1-minute chart JSON (or .gz) instead of fetching it")
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    clock(p)
    p = sub.add_parser("status", help="what needs attention: no stop, an approval nobody placed, a stale PASS")
    p.add_argument("--quiet", action="store_true", help="print nothing when all is well")
    p.add_argument("--json", action="store_true")
    p.add_argument("--risk", default=nc.tpath("state", "risk.json"))
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    clock(p)
    p = sub.add_parser("paper-fill", help="paper mode: fill an approved order at the latest price")
    common(p)
    paper_files(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--price", help="fill at this price instead of the latest 1-minute close")
    p.add_argument("--filled-qty", help="rehearse a partial fill: only this many shares fill; the stop and the "
                                        "exit follow the filled shares")
    p = sub.add_parser("paper-check", help="paper mode: walk the 1-minute bars (stop, target, flat_by)")
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    paper_files(p)
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    clock(p)
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("journal", help="closed trades: R-multiple, slippage, time held, win rate")
    p.add_argument("--state", default=nc.tpath("state", "broker.json"))
    p.add_argument("--days", type=int, default=30, help="how many calendar days back (default 30)")
    p.add_argument("--json", action="store_true")
    clock(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "autopilot-approve": cmd_autopilot_approve,
                "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "reconcile": cmd_reconcile, "move-stop": cmd_move_stop,
                "status": cmd_status, "paper-fill": cmd_paper_fill, "paper-check": cmd_paper_check,
                "log": cmd_log, "journal": cmd_journal}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
