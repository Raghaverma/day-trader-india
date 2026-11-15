#!/usr/bin/env python3
"""Order tickets, your yes, and a paper broker for nse-broker. Standard library only.

This tool never talks to Zerodha and never places a real order. It does its
work around the one moment that matters, your yes. Three modes (config "broker"):

  paper   (the default) no broker at all. After your yes, paper-fill fills the
          entry at the latest Yahoo price if a LIMIT buy is marketable, and
          paper-check walks the 1-minute bars since the fill: the stop, the
          target and flat_by, with P&L net of charges. trader/state/paper.json
  manual  prints exactly what to type into Kite, then result / fill record it
  kite    the exact JSON for place_order on your self-hosted Kite MCP server:
          the entry leg first, the SL stop leg after the entry fills

Commands:

  show        each proposed order as a plain ticket. READY only if nse-risk
              passed THIS exact order today (matched by fingerprint) and
              nobody has decided on it yet.
  approve     records your explicit yes for ONE order, word for word. It refuses
              "yes to all", "sure", "ok", a yes that names a different stock or
              amount, and any order that is not READY.
  decline     records a no.
  result      records what happened after the Allow once / Deny card or in Kite:
              placed (with Kite's order id), denied or failed; --leg stop for
              the stop leg once the entry has filled.
  fill        records a fill in trader/portfolio.json (cash and shares); a sell
              also logs the trade's P&L net of charges.
  flatten     builds the SELL MIS ticket that closes a position by flat_by,
              after cancelling the stop. It still goes through nse-risk and your yes.
  paper-fill  paper mode: fill an approved order at the latest price.
  paper-check paper mode: walk the 1-minute bars; stop, target, flat_by.
  log         today's decisions and paper P&L.

    python3 ticket.py show
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py paper-fill --index 1
    python3 ticket.py paper-check
    python3 ticket.py result --index 1 --status placed --broker-id 2511130001 --filled-qty 56 --filled-price 1334
    python3 ticket.py result --index 1 --leg stop --status placed --broker-id 2511130002
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 56 --price 1344.40
    python3 ticket.py flatten --symbol RELIANCE
    python3 ticket.py log

Everything is logged in trader/state/broker.json (append only). Dates are IST
dates; --now "2026-11-13 10:05" pins the clock (IST).
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
MODES = ("paper", "manual", "kite")
MODE_HEAD = {"paper": "PAPER (no broker, no money)",
             "manual": "MANUAL: you type it into Kite, REAL MONEY",
             "kite": "KITE MCP, LIVE ACCOUNT, REAL MONEY"}
TARGET_WORDS = "an alert, not an order (Kite has no OCO for MIS): when it rings, cancel the stop, then sell"
PAPER_NOTE = ("Paper trades: no broker, no money. Fills come from Yahoo 1-minute bars, which are unofficial "
              "and can lag. P&L is net of estimated charges.")
EPS = 1e-9


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
    try:
        return nc.load_config(path)
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


def max_loss(order, cfg=None):
    """(loss, stop, gross, charges) if the stop fills exactly at its trigger; loss
    includes the estimated charges when "include_charges". (None, stop, None, None)
    when it can't be worked out."""
    e, s = legs(order)
    plan = order.get("plan") or {}
    stop = _f((s or {}).get("trigger_price")) or _f(plan.get("stop"))
    q = _f((e or {}).get("quantity"))
    if not stop or not q or not e:
        return None, stop, None, None
    ot = str(e.get("order_type") or "").upper()
    entry = _f(e.get("price")) if ot in ("LIMIT", "SL") else None
    entry = entry or _f(plan.get("entry"))
    if not entry:
        return None, stop, None, None
    long = str(e.get("transaction_type", "")).upper() == "BUY"
    gross = round(q * round(abs(entry - stop), 2), 2)
    ch = nc.intraday_charges(int(q), entry if long else stop, stop if long else entry, cfg)["total"]
    with_ch = (cfg or {}).get("include_charges", True)
    return round(gross + (ch if with_ch else 0), 2), stop, gross, ch


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
    loss, stop, gross, ch = max_loss(order, cfg)
    if loss is not None:
        if (cfg or {}).get("include_charges", True):
            parts.append("max loss about %s with charges if the stop fills at %s (%s + %s charges; it can fill "
                         "worse, or not at all, in a gap)" % (money(loss), px(stop), money(gross), money(ch)))
        else:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse in a gap)"
                         % (money(loss), px(stop)))
    if order.get("cancel_first"):
        parts.insert(0, "first cancel the open stop%s" % (
            (" (Kite order %s)" % order["cancel_first"]["order_id"]) if order["cancel_first"].get("order_id") else ""))
    return parts


def plain(order, cfg=None):
    return " / ".join(plain_parts(order, cfg))


def amount_numbers(order, cfg=None):
    nums = []
    e, s = legs(order)
    for leg in (e, s):
        for k in ("quantity", "price", "trigger_price"):
            v = _f((leg or {}).get(k))
            if v is not None:
                nums.append(v)
    t = target_of(order)
    if t:
        nums.append(t)
    loss, _, gross, _ = max_loss(order, cfg)
    for v in (loss, gross):
        if v is not None:
            nums.append(v)
    return nums


SYM_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9&\-]*")


def parse_yes(reply, order, batch_symbols=(), cfg=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, haan, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = nc.nse_symbol(order.get("symbol") or (legs(order)[0] or {}).get("tradingsymbol"))
    tokens = {t for t in SYM_TOKEN.findall(raw.upper()) if re.search(r"[A-Z]", t)}
    others = {nc.nse_symbol(s) for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order, cfg)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_num(n) for n in nums), plain(order, cfg))
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


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "nse-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


WAITING = {"paper": "waiting for ticket.py paper-fill", "manual": "waiting for you to place it in Kite",
           "kite": "waiting for the place_order call"}


def status_of(order, risk, state, today, mode="paper"):
    """(ready, label, risk_result)"""
    fp = nc.order_fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by nse-risk: " + "; ".join(rv.get("reasons", [])), rv
    t_now, t_then = target_of(order), _f(rv.get("target"))
    if t_then is not None and (t_now is None or abs(t_now - t_then) > 0.005):
        # the fingerprint covers the entry and stop legs only; the target decides reward:risk
        return False, ("NOT READY: the target changed since the risk check (%s, now %s); run check.py again"
                       % (px(t_then), px(t_now) if t_now else "none")), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, %s" % (rec.get("at"), WAITING.get(mode, "")), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: nse-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    if rv and rv.get("kind"):
        return rv["kind"]
    e, _ = legs(order)
    return "entry" if str((e or {}).get("transaction_type", "")).upper() == "BUY" else "exit"


# ---------------------------------------------------------------- rendering

def manual_lines(order):
    """Exactly what to type into Kite."""
    e, s = legs(order)
    out = []
    if order.get("cancel_first"):
        out.append("First, in Kite > Orders, cancel the pending SL order for %s%s." % (
            order.get("symbol"), (" (order %s)" % order["cancel_first"]["order_id"])
            if order["cancel_first"].get("order_id") else ""))
    for n, (leg, when) in enumerate(((e, ""), (s, "Once the buy has filled (Kite > Orders shows COMPLETE): ")), 1):
        if not leg:
            continue
        ot = str(leg.get("order_type") or "").upper()
        side = "Buy" if str(leg.get("transaction_type")).upper() == "BUY" else "Sell"
        bits = ["%sSearch %s, pick NSE, press %s." % (when, leg.get("tradingsymbol"), side),
                "Product: Intraday (MIS).", "Order type: %s." % {"LIMIT": "Limit", "MARKET": "Market"}.get(ot, ot),
                "Qty: %s." % _num(leg.get("quantity"))]
        if ot in ("SL", "SL-M"):
            bits.append("Trigger price: %s." % kite_px(leg.get("trigger_price")))
        if ot in ("LIMIT", "SL"):
            bits.append("Price: %s." % kite_px(leg.get("price")))
        bits.append("Validity: Day.")
        out.append(" ".join(bits))
    t = target_of(order)
    if t and s:
        out.append("The target %s is an alert, not an order: when it rings, cancel the SL order in Kite > Orders, "
                   "then Sell %s %s MIS." % (kite_px(t), _num(s.get("quantity")), order.get("symbol")))
    return out


def render(order, idx, total, cfg, risk, state, today):
    mode = mode_of(cfg)
    ready, label, rv = status_of(order, risk, state, today, mode)
    lines = ["ORDER %d of %d, %s" % (idx, total, MODE_HEAD[mode])]
    for part in plain_parts(order, cfg):
        lines.append("  " + part)
    if order.get("flat_by"):
        lines.append("  flat by %s IST" % order["flat_by"])
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
        if order.get("cancel_first"):
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


def ticket_json(order, idx, cfg, risk, state, today):
    mode = mode_of(cfg)
    ready, label, rv = status_of(order, risk, state, today, mode)
    loss, stop, gross, ch = max_loss(order, cfg)
    e, s = legs(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg),
            "fingerprint": nc.order_fingerprint(order), "broker": mode, "tool": "place_order",
            "tool_input": tool_input(e), "stop_tool_input": tool_input(s), "target": target_of(order),
            "cancel_first": order.get("cancel_first"), "flat_by": order.get("flat_by"),
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


def done_bars(bars, now_ist):
    """Bars complete by now (a 1-minute bar stamped 10:04 is complete at 10:05)."""
    return [b for b in bars if b[0] + datetime.timedelta(minutes=1) <= now_ist]


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


def walk(t, bars, now_ist):
    """Walk 1-minute bars for one paper trade. Returns a list of events
    (kind, bar time, price, note) and leaves the trade as it stands at now.

    Yahoo leaves many late-session 1-minute bars empty (most of 15:16-15:29), so
    empty bars are skipped. If the flat_by bar itself is empty, the trade closes
    at the close of the last real bar at or before flat_by, and the note says so."""
    events = []
    day = datetime.date.fromisoformat(t["date"])
    try:
        flat = nc.parse_hhmm(t.get("flat_by") or "15:10", "flat_by")
    except ValueError as e:
        raise TicketError("paper trade %s: %s" % (t.get("id"), e))
    flat_at = datetime.datetime.combine(day, flat)
    start = datetime.datetime.strptime(t["fill_time"] if t["status"] == "filled" else t["ordered_at"],
                                       "%Y-%m-%d %H:%M")
    if t.get("checked_to"):
        start = max(start, datetime.datetime.strptime(t["checked_to"], "%Y-%m-%d %H:%M"))
    state = t["status"]
    last_real = None                      # (time, close) of the last real bar at or before flat_by

    def flat_exit():
        if last_real is None:
            return None
        note = "the %s bar has no data on Yahoo: closed at the %s bar's close" % (
            nc.hhmm(flat), nc.hhmm(last_real[0].time()))
        return ("flat_by", last_real[0], last_real[1], note)

    for when, o, h, l, c in done_bars(bars, now_ist):
        if when.date() != day:
            continue
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
            if l <= t["limit"] + EPS:
                events.append(("fill", when, min(o, t["limit"]), None))
                state = "filled"
            continue
        if state != "filled":
            break
        stop = t.get("stop") or {}
        trig = _f(stop.get("trigger"))
        if trig and l <= trig + EPS:
            price = o if o < trig else trig
            note = None
            if o < trig:
                note = "gapped below the trigger: filled at the bar's open %s" % px(o)
                if stop.get("price") and o < stop["price"] - EPS:
                    note += " (a real SL limit at %s might not have filled at all)" % px(stop["price"])
            if t.get("target") and h >= t["target"] - EPS:
                note = (note + "; " if note else "") + "the same bar touched the target too: counted as the stop"
            events.append(("stop", when, price, note))
            state = "closed"
            break
        if t.get("target") and h >= t["target"] - EPS:
            events.append(("target", when, t["target"], None))
            state = "closed"
            break
        if when == flat_at:
            events.append(("flat_by", when, c, "closed at the %s bar's close" % nc.hhmm(when.time())))
            state = "closed"
            break
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
    try:
        now = nc.parse_now(getattr(a, "now", None))
    except ValueError as e:
        raise TicketError(str(e))
    today = getattr(a, "today", None) or nc.to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-13")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run nse-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = load_cfg(a.config)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def _symbol(order):
    return nc.nse_symbol(order.get("symbol") or (legs(order)[0] or {}).get("tradingsymbol"))


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Not financial advice; nothing is "
          "placed without your explicit yes.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    mode = mode_of(cfg)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, mode)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [_symbol(o) for o in orders], cfg)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    e, s = legs(order)
    loss, stop, gross, ch = max_loss(order, cfg)
    if rv and rv.get("max_loss") is not None:
        loss = rv["max_loss"]
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": nc.order_fingerprint(order),
           "symbol": _symbol(order), "side": str(e.get("transaction_type", "")).lower(),
           "qty": int(float(e.get("quantity") or 0)), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order, cfg), "status": "approved", "reply": a.reply, "broker": mode}
    state.setdefault("orders", []).append(rec)
    write_state(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, cfg)))
    if mode == "paper":
        print("Paper mode: now run ticket.py paper-fill --index %d (no broker, no money)." % a.index)
    elif mode == "manual":
        print("Now type it into Kite, exactly:")
        for n, l in enumerate(manual_lines(order), 1):
            print("  %d. %s" % (n, l))
        print("Then: ticket.py result --index %d --status placed --broker-id <Kite order id> --filled-qty Q "
              "--filled-price P (or --status failed --note ...)." % a.index)
    else:
        if order.get("cancel_first"):
            cf = order["cancel_first"]
            print("First call cancel_order with exactly:")
            print(json.dumps({"variety": cf.get("variety", "regular"),
                              "order_id": cf.get("order_id") or "<the SL order id from get_orders>"}, indent=2))
        print("Now call place_order with exactly:")
        print(json.dumps(tool_input(e), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status placed|denied|failed"
              % a.index)
        if s:
            print("Once the entry fills, the stop leg (ticket.py result --index %d --leg stop ... afterwards):"
                  % a.index)
            print(json.dumps(tool_input(s), indent=2))
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    e, _ = legs(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": nc.order_fingerprint(order),
           "symbol": _symbol(order), "side": str((e or {}).get("transaction_type", "")).lower(),
           "plain": plain(order, cfg), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_state(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, cfg)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
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
        new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": _symbol(order),
               "side": str(s.get("transaction_type", "")).lower(), "leg": "stop", "kind": "stop",
               "qty": int(float(s.get("quantity") or 0)), "status": a.status,
               "plain": "stop: " + leg_plain(s, with_exchange=False)}
        if a.broker_id:
            new["broker_order_id"] = a.broker_id
        if a.note:
            new["note"] = a.note
        state["orders"].append(new)
        write_state(a.state, state)
        print("Recorded order %d's stop leg as %s." % (a.index, a.status))
        if a.status != "placed":
            print("The position has no stop at the broker. Place it again, or flatten (ticket.py flatten --symbol %s)."
                  % _symbol(order))
        return 0
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status, "leg": "entry"}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        pf, basis = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
        if rec["side"] == "sell" and basis is not None:
            q = float(a.filled_qty)
            log_close(state, now, today, rec["symbol"], q, basis / q, float(a.filled_price), "sold", cfg)
        print("Added the fill to %s." % a.portfolio)
        if s and rec.get("kind") == "entry":
            if mode == "kite":
                print("Now the stop leg: place_order with exactly:")
                print(json.dumps(tool_input(s), indent=2))
            else:
                print("Now the stop leg in Kite: %s" % manual_lines(order)[1 if not order.get("cancel_first") else 2])
            print("Then: ticket.py result --index %d --leg stop --status placed --broker-id <id>" % a.index)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if s and rec.get("kind") == "entry":
            print("Then place the stop leg and record it: ticket.py result --index %d --leg stop --status placed"
                  % a.index)
    write_state(a.state, state)
    return 0


def cmd_fill(a):
    cfg = load_cfg(a.config)
    now, _ = _clock(a)
    date = a.date or nc.to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-13")
    pf, basis = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    state = load_state(a.state)
    log_fill(state, now, date, a.symbol, a.side, a.qty, a.price)
    extra = ""
    if a.side == "sell" and basis is not None:
        q = float(a.qty)
        rec = log_close(state, now, date, a.symbol, q, basis / q, float(a.price), a.reason or "sold", cfg)
        extra = " P&L %s net of %s charges." % (money(rec["pnl_net"]), money(rec["charges"]))
    write_state(a.state, state)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s.%s"
          % (a.side, _num(a.qty), nc.nse_symbol(a.symbol), px(a.price), date, a.portfolio, money(pf["cash"]), extra))
    return 0


def cmd_flatten(a):
    cfg = load_cfg(a.config)
    mode = mode_of(cfg)
    sym = nc.nse_symbol(a.symbol)
    now, today = _clock(a)
    source = "the broker, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or abs(qty - round(qty)) > EPS:
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 56")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if nc.nse_symbol(p.get("ticker") or p.get("symbol")) == sym)
        source = a.portfolio
        if qty <= EPS:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_positions) and pass "
                              "--qty if you do hold some" % (sym, a.portfolio))
    limit = _f(a.limit) if a.limit is not None else None
    if a.limit is not None and (limit is None or limit <= 0):
        raise TicketError("--limit wants a price, like --limit 1338.00")
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if nc.nse_symbol(plan.get("symbol")) == sym else None) \
        or cfg.get("flat_by") or "15:10"
    leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
           "quantity": int(round(qty)), "product": str(cfg.get("product") or "MIS").upper(),
           "order_type": "LIMIT" if limit else "MARKET"}
    if limit:
        leg["price"] = float(nc.round_to_tick(limit, nc.tick_size(limit, sym, cfg), "down"))
    leg["validity"] = "DAY"
    leg["tag"] = nc.ORDER_TAG
    state = load_state(a.state)
    stop_id = None
    for r in state.get("orders", []):
        if rec_day(r) == today and r.get("symbol") == sym and r.get("leg") == "stop" and r.get("status") == "placed":
            stop_id = r.get("broker_order_id") or stop_id
    order = {"symbol": sym, "entry": leg, "stop": None,
             "cancel_first": {"what": "the open SL stop leg", "variety": "regular", "order_id": stop_id},
             "flat_by": flat_by,
             "why": "flat by %s IST: closes the %s intraday trade before Zerodha's 15:25 MIS square-off "
                    "(qty from %s)" % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "nse-broker", "created_at": nc.utc_iso(now), "status": "proposed", "broker": mode,
           "note": "Flatten ticket. Nothing is placed until nse-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    nc.write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, leg_plain(leg), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_positions before you say yes." % source)
    print("Next:")
    print("  1. nse-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if mode == "paper":
        print("  3. ticket.py paper-fill --index 1: cancels the paper stop and sells at the latest price.")
    elif mode == "manual":
        print("  3. In Kite > Orders, cancel the pending SL order first, or Kite sells twice and leaves you short.")
        print("     Then the SELL as the ticket says, then ticket.py result.")
    else:
        print("  3. Cancel the stop first, or both sells can fill and leave you short: cancel_order %s"
              % json.dumps({"variety": "regular", "order_id": stop_id or "<the SL order id from get_orders>"}))
        print("     (Needs approval). Then place_order with the ticket, then ticket.py result.")
        if not limit:
            print("  Market orders sent by API may need market protection; if Kite rejects it, flatten again with")
            print("  --limit a few ticks under the last price.")
    return 0


def cmd_paper_fill(a):
    orders, cfg, risk, state, today, now = _load(a)
    mode = mode_of(cfg)
    if mode != "paper":
        raise TicketError("paper-fill is for \"broker\": \"paper\"; this config says %s" % mode)
    order = _pick(orders, a.index)
    fp = nc.order_fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        ready, label, _ = status_of(order, risk, state, today, mode)
        raise TicketError("order %d has no yes recorded today (%s); paper-fill only follows an approval"
                          % (a.index, label))
    e, s = legs(order)
    sym = _symbol(order)
    now_ist = nc.to_ist(now)
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
    limit = _f(e.get("price"))
    stamp = now_ist.strftime("%Y-%m-%d %H:%M")
    placed = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": sym,
              "side": side.lower(), "plain": rec["plain"], "status": "placed", "leg": "entry", "paper": True,
              "broker_order_id": "PAPER-%s-%s-%d" % (sym, now_ist.strftime("%H%M"), a.index)}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            placed[k] = rec[k]
    if side == "SELL":
        t = open_trade(paper, sym)
        if ot == "LIMIT" and price < limit - EPS:
            raise TicketError("the SELL LIMIT %s isn't marketable: the latest price is %s. Nothing filled."
                              % (px(limit), px(price)))
        state["orders"].append(placed)
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
        for kind, when, price, note in walk(t, bars, now_ist):
            if kind == "fill":
                fill_entry(t, when, price, now, a, cfg, state)
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
            else:
                close_trade(t, when, price, kind, now, a, cfg, state, note)
                word = {"stop": "STOPPED OUT", "target": "TARGET HIT", "flat_by": "FLAT BY %s" % t["flat_by"]}[kind]
                lines.append("%s %s: %s at %s (%s IST bar). Gross %s, charges %s, net %s.%s"
                             % (t["id"], t["symbol"], word, px(price), when.strftime("%H:%M"), money(t["gross"]),
                                money(t["charges"]), money(t["pnl_net"]), (" " + note[:1].upper() + note[1:] + ".")
                                if note else ""))
        if t["status"] in ("open", "filled"):
            at, last = last_price(bars, now_ist, datetime.date.fromisoformat(t["date"]))
            t["checked_to"] = now_ist.strftime("%Y-%m-%d %H:%M") if at is None else \
                (at + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
            if t["status"] == "filled" and last is not None:
                gross = round(t["qty"] * (last - t["fill_price"]), 2)
                ch = nc.intraday_charges(t["qty"], t["fill_price"], last, cfg)["total"]
                lines.append("%s %s: still open, %d at %s; last %s (%s IST): %s gross, %s after charges if sold "
                             "there. Stop %s, target %s, flat by %s."
                             % (t["id"], t["symbol"], t["qty"], px(t["fill_price"]), px(last), at.strftime("%H:%M"),
                                money(gross), money(gross - ch), px(t["stop"]["trigger"]) if t["stop"] else "none",
                                px(t["target"]) if t["target"] else "none", t["flat_by"]))
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
    T = os.path.join

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-13 10:05\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default=T("trader", "orders.json"))
        p.add_argument("--config", default=T("trader", "config.json"))
        p.add_argument("--risk", default=T("trader", "state", "risk.json"))
        p.add_argument("--state", default=T("trader", "state", "broker.json"))
        clock(p)

    def paper_files(p):
        p.add_argument("--paper", default=T("trader", "state", "paper.json"))
        p.add_argument("--portfolio", default=T("trader", "portfolio.json"))
        p.add_argument("--bars", help="a Yahoo v8 1-minute chart JSON (or .gz) instead of fetching it")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
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
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default=T("trader", "portfolio.json"))
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--reason", help="for a sell: stop, target, flat_by, sold")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default=T("trader", "portfolio.json"))
    p.add_argument("--config", default=T("trader", "config.json"))
    p.add_argument("--state", default=T("trader", "state", "broker.json"))
    p = sub.add_parser("flatten", help="a SELL MIS ticket that closes a position by flat_by (cancel the stop first)")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_positions (default: trader/portfolio.json)")
    p.add_argument("--limit", help="a LIMIT sell at this price instead of MARKET")
    p.add_argument("--orders", default=T("trader", "orders.json"), help="where the ticket is written")
    p.add_argument("--portfolio", default=T("trader", "portfolio.json"))
    p.add_argument("--plan", default=T("trader", "plan.json"))
    p.add_argument("--config", default=T("trader", "config.json"))
    p.add_argument("--state", default=T("trader", "state", "broker.json"))
    clock(p)
    p = sub.add_parser("paper-fill", help="paper mode: fill an approved order at the latest price")
    common(p)
    paper_files(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--price", help="fill at this price instead of the latest 1-minute close")
    p = sub.add_parser("paper-check", help="paper mode: walk the 1-minute bars (stop, target, flat_by)")
    p.add_argument("--config", default=T("trader", "config.json"))
    p.add_argument("--state", default=T("trader", "state", "broker.json"))
    paper_files(p)
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default=T("trader", "state", "broker.json"))
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "paper-fill": cmd_paper_fill,
                "paper-check": cmd_paper_check, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
