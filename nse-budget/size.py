#!/usr/bin/env python3
"""Position sizing for nse-budget. Standard library only, no network.

Sizes ONE intraday (MIS) trade on the NSE cash market from trader/plan.json
(written by nse-exit) to your budget and your max loss per trade, then writes
one Kite-shaped order to trader/orders.json: an entry leg, a stop leg (an SL
order placed after the entry fills) and the target as an alert. It never
places anything: nse-risk checks the order and nse-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit 1333.50 --write          # a LIMIT buy at 1,333.50 instead of the plan's entry
    python3 size.py --market --write                 # a MARKET buy (only if you asked for one)
    python3 size.py --now "2026-11-13 10:05" --json  # pin the clock (IST)

How many shares. Whole shares only.

    shares = floor of the smallest of
      max loss       max_loss_per_trade_inr / risk per share (entry minus stop),
                     and with "include_charges" the estimated round-trip charges
                     at the stop count against the max loss too
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

The entry is a LIMIT buy at the plan's entry by default (or your --limit
price). With --market it is sized at the plan's entry, or the price in
trader/prices.json if that is fresh (under 15 minutes old) and higher. Every
price sits on the NSE tick: the stop rounds DOWN, the limit and the target
round to the nearest tick. The stop leg is an SL order: trigger at the stop,
limit stop_limit_buffer_pct under it, rounded down to the tick.

Kite has no bracket orders and no OCO for MIS, so the target is an alert,
not a resting order: when it rings, cancel the stop, then sell.
"""

import argparse
import decimal
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

D = nc.D
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "level": "close above a level", "flag": "flag",
               "bounce": "oversold bounce"}
BOUND_WORDS = {"position cap": "position cap", "max order": "max order", "cash": "cash after the buffer"}
NOTE = ("Proposals only. Nothing is placed until nse-risk passes each order and you say yes to it in the chat.")
TARGET_HOW = ("an alert, not a resting order: Kite has no OCO for MIS. When it rings, cancel the stop, then sell.")


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def px(x):
    """A price, Indian grouping, always 2 decimals: 1334 -> "1,334.00"."""
    return nc.inr(x, keep_zeros=True).replace("₹", "")


def money(x):
    return nc.inr(x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")


def fl(d):
    """Decimal price -> JSON number (1334.0, 1328.8)."""
    return float(d)


def shares_for(inr, per_share):
    """Whole shares that fit in `inr` at `per_share`, exactly (Decimal)."""
    u, p = nc.dec(inr), nc.dec(per_share)
    if p <= 0 or u <= 0:
        return 0
    return int((u / p).to_integral_value(rounding=decimal.ROUND_FLOOR))


def charges_total(qty, buy, sell, cfg):
    if qty <= 0:
        return D(0)
    return nc.dec(nc.intraday_charges(qty, buy, sell, cfg)["total"])


def max_loss_shares(cap, entry, stop, cfg, with_charges):
    """The most shares whose loss at the stop (plus charges when asked) fits in cap."""
    risk = entry - stop
    hi = shares_for(cap, risk)
    if not with_charges or hi <= 0:
        return hi
    cap = nc.dec(cap)

    def fits(n):
        return n * risk + charges_total(n, entry, stop, cfg) <= cap

    lo = 0
    while lo < hi:                      # charges grow with n, so binary search
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def break_even(qty, entry, tick, cfg):
    """The lowest sell price on the tick that pays back the round-trip charges."""
    if qty <= 0:
        return None
    est = entry + charges_total(qty, entry, entry, cfg) / qty
    p = nc.round_to_tick(est, tick, "up")
    for _ in range(100000):
        if qty * (p - entry) >= charges_total(qty, entry, p, cfg):
            return p
        p += tick
    return p


def parse_utc(text):
    """ "2026-11-13T04:50:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        return nc.parse_now(str(text).strip())
    except ValueError:
        return None


def price_time(doc, sym):
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time_utc, fresh) for sym from a prices.json doc (nse-alerts writes it)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


def read_json(path, what):
    try:
        doc = nc.read_json(path)
    except ValueError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))
    if doc is None:
        raise SizeError("no %s at %s" % (what, path))
    return doc


def plan_time(text):
    """ "10:05" or "10:05 AM" -> "10:05" (IST), else the text as it is."""
    s = str(text or "").strip()
    if not s:
        return ""
    up = s.upper()
    for suffix, add in ((" AM", 0), (" PM", 12)):
        if up.endswith(suffix):
            try:
                h, m = up[:-3].split(":")
                h = int(h) % 12 + add
                return "%02d:%02d" % (h, int(m))
            except ValueError:
                return s
    return s


# ---------------------------------------------------------------- the sizing

def size_trade(plan, cfg, budget=None, limit=None, market=False, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit   the LIMIT price (None = the plan's entry)
    market  True for a MARKET buy (sized at the higher of the plan's entry and a fresh price)
    price   a FRESH latest price (prices.json), used for a market order
    cash    the cash in trader/portfolio.json, if there is one
    today   the IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = json.loads(json.dumps(nc.DEFAULT_CONFIG))
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = nc.nse_symbol(plan.get("symbol"))
    if not sym:
        raise SizeError("the plan has no symbol (run nse-exit)")
    if not nc.is_nse_symbol(sym) or sym.startswith("^"):
        raise SizeError("%r is not an NSE stock symbol" % plan.get("symbol"))
    exch = str(plan.get("exchange") or "NSE").upper()
    if exch != "NSE":
        raise SizeError("the plan is for %s; this pack trades the NSE cash market only" % exch)
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) intraday trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India (IST): make a new plan with nse-exit"
                        % (plan["date"], today))
    entry_plan, stop_raw, target_raw = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop_raw), ("target", target_raw)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run nse-exit): the order needs an entry, a stop "
                            "and a target" % name)
    blocked = {nc.nse_symbol(b) for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    slb = float(c.get("stop_limit_buffer_pct") if c.get("stop_limit_buffer_pct") is not None else 0.5)
    if not 0 <= slb < 20:
        raise SizeError("stop_limit_buffer_pct must be between 0 and 20 (it is %s)" % _num(slb))
    stop_type = str(c.get("stop_order_type") or "SL").upper()
    if stop_type not in ("SL", "SL-M"):
        raise SizeError("stop_order_type must be \"SL\" or \"SL-M\", not %r" % c.get("stop_order_type"))
    with_charges = bool(c.get("include_charges", True))

    def tick_at(p):
        return nc.tick_size(p, sym, c)

    if market:
        e_plan = nc.round_to_tick(entry_plan, tick_at(entry_plan))
        if price is not None and nc.round_to_tick(price, tick_at(price)) > e_plan:
            ref, ref_from = nc.round_to_tick(price, tick_at(price)), "price"
        else:
            ref, ref_from = e_plan, "plan"
    elif limit is not None:
        ref, ref_from = nc.round_to_tick(limit, tick_at(limit)), "limit"
    else:
        ref, ref_from = nc.round_to_tick(entry_plan, tick_at(entry_plan)), "plan limit"
    tick = tick_at(ref)
    stop = nc.round_to_tick(stop_raw, tick_at(stop_raw), "down")
    target = nc.round_to_tick(target_raw, tick_at(target_raw))
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (px(stop), px(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with nse-exit" % (px(target), px(ref)))
    risk = ref - stop
    reward = target - ref

    cash_buffer = (nc.dec(budget) * nc.dec(buf_pct) / 100).quantize(D("0.01"))
    available = nc.dec(budget) - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and nc.dec(cash) - cash_buffer < available:
        available = nc.dec(cash) - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(available, D(0))

    limits = []
    max_loss_cap = _f(c.get("max_loss_per_trade_inr"))
    if max_loss_cap:
        n = max_loss_shares(max_loss_cap, ref, stop, c, with_charges)
        if with_charges and n > 0:
            ch = charges_total(n, ref, stop, c)
            how = "%s incl. charges: %d x %s = %s + %s charges = %s" % (
                money(max_loss_cap), n, px(risk), money(n * risk), money(ch), money(n * risk + ch))
        elif with_charges:
            how = "%s incl. charges / %s risk a share" % (money(max_loss_cap), px(risk))
        else:
            how = "%s max loss / %s risk a share" % (money(max_loss_cap), px(risk))
        limits.append({"name": "max loss", "inr": max_loss_cap, "shares": n, "how": how})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = (nc.dec(budget) * nc.dec(cap_pct) / 100).quantize(D("0.01"))
        limits.append({"name": "position cap", "inr": float(cap), "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), px(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), px(ref))})
    limits.append({"name": "cash", "inr": float(available), "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), px(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = qty * ref
    loss = qty * risk
    ch_stop = charges_total(qty, ref, stop, c)
    ch_target = charges_total(qty, ref, target, c)
    gain = qty * reward
    be = break_even(qty, ref, tick, c)
    if qty > 0 and with_charges:
        rr = (gain - ch_target) / (loss + ch_stop)
    else:
        rr = reward / risk
    rr = rr.quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP)
    rr_gross = (reward / risk).quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP)

    refusal = None
    min_rr = _f(c.get("min_reward_risk"))
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s)%s, more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with nse-exit."
                       % (money(risk), px(ref), px(stop), " plus charges" if with_charges else "",
                          money(max_loss_cap)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], [l for l in limits if l["name"] == bound[0]][0]["how"]))
    elif min_rr and rr < nc.dec(min_rr):
        refusal = ("reward:risk %s%s (target %s, entry %s, stop %s) is under your minimum %s, so nse-risk "
                   "would block it. No order. Skip it, or make a new plan with nse-exit."
                   % (rr, " after charges" if with_charges else "", px(target), px(ref), px(stop), _num(min_rr)))
    elif cost < nc.dec(c.get("min_order_inr") or 0):
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))

    stop_limit = None
    if stop_type == "SL":
        stop_limit = nc.round_to_tick(stop * (1 - nc.dec(slb) / 100), tick_at(stop), "down")
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan_time(plan.get("entry_time")), "entry_plan": fl(nc.round_to_tick(entry_plan, tick_at(entry_plan))),
        "entry": fl(ref), "entry_from": ref_from, "order_type": "MARKET" if market else "LIMIT",
        "stop": fl(stop), "stop_raw": stop_raw, "target": fl(target), "tick": str(tick),
        "stop_order_type": stop_type, "stop_limit": fl(stop_limit) if stop_limit is not None else None,
        "stop_limit_buffer_pct": slb, "risk_per_share": fl(risk), "reward_per_share": fl(reward),
        "reward_risk": float(rr), "reward_risk_gross": float(rr_gross), "include_charges": with_charges,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": float(cash_buffer),
        "cash_available": float(available), "max_loss_per_trade_inr": max_loss_cap, "limits": limits,
        "qty": qty, "bound_by": bound[0], "cost": float(cost), "max_loss": float(loss),
        "charges_at_stop": float(ch_stop), "max_loss_with_charges": float(loss + ch_stop),
        "gain_at_target": float(gain), "charges_at_target": float(ch_target),
        "net_at_target": float(gain - ch_target),
        "break_even": fl(be) if be is not None else None,
        "break_even_move": fl(be - ref) if be is not None else None,
        "flat_by": plan.get("flat_by") or c.get("flat_by"), "product": str(c.get("product") or "MIS").upper(),
        "refusal": refusal,
    }


def kite_leg(sym, side, qty, product, order_type, price=None, trigger=None):
    """One Kite Connect place_order call (nc.KITE_FIELDS), numbers on the tick."""
    leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": side,
           "quantity": int(qty), "product": product, "order_type": order_type}
    if trigger is not None:
        leg["trigger_price"] = float(trigger)
    if price is not None:
        leg["price"] = float(price)
    leg["validity"] = "DAY"
    leg["tag"] = nc.ORDER_TAG
    return leg


def build_order(s):
    """The one proposed order in the orders.json shape (PORTING.md), or None."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    sym, q = s["symbol"], s["qty"]
    entry = kite_leg(sym, "BUY", q, s["product"], s["order_type"],
                     price=s["entry"] if s["order_type"] == "LIMIT" else None)
    stop = kite_leg(sym, "SELL", q, s["product"], s["stop_order_type"], price=s["stop_limit"], trigger=s["stop"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    if s["bound_by"] == "max loss":
        bound = "%s max loss per trade" % money(s["max_loss_per_trade_inr"] or 0)
    else:
        bound = BOUND_WORDS[s["bound_by"]]
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop%s)"
           % (setup[:1].upper() + setup[1:], sym, q, bound, q, px(s["risk_per_share"]), money(s["max_loss"]),
              (", %s with charges" % money(s["max_loss_with_charges"])) if s["include_charges"] else ""))
    return {
        "symbol": sym,
        "entry": entry,
        "stop": stop,
        "target": {"price": s["target"], "how": TARGET_HOW},
        "flat_by": s["flat_by"],
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"],
                 "charges_at_stop": s["charges_at_stop"], "max_loss_with_charges": s["max_loss_with_charges"],
                 "charges_at_target": s["charges_at_target"], "reward_risk": s["reward_risk"],
                 "break_even": s["break_even"], "setup": s["setup"], "date": s["date"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"], "tick": s["tick"]},
    }


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy on NSE, %s, entry %s%s" % (s["symbol"], setup, px(s["entry_plan"]),
                                                    (" at %s IST" % s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s. Tick %s."
                 % (px(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "", px(s["target"]),
                    (": " + s["target_label"]) if s["target_label"] else "", s["tick"]))
    if abs(s["stop"] - s["stop_raw"]) > 1e-9:
        lines.append("The plan's stop %s is not on the %s tick: rounded down to %s."
                     % (px(s["stop_raw"]), s["tick"], px(s["stop"])))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry, a market order", "price": "the latest price, above the plan's entry",
           "limit": "your limit price", "plan limit": "a LIMIT buy at the plan's entry"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s)."
                 % (px(s["entry"]), src, px(s["risk_per_share"]), px(s["entry"]), px(s["stop"])))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-66s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Not financial advice. Nothing is placed without your explicit yes.")
        return "\n".join(lines)
    q = s["qty"]
    lines.append("Size: %d shares of %s = %s at %s." % (q, s["symbol"], money(s["cost"]), px(s["entry"])))
    lines.append("At the stop: %d x %s = %s, plus about %s in charges = %s."
                 % (q, px(s["risk_per_share"]), money(s["max_loss"]), money(s["charges_at_stop"]),
                    money(s["max_loss_with_charges"])))
    lines.append("At the target: %d x %s = %s, minus about %s in charges = %s. Reward:risk %.2f%s."
                 % (q, px(s["reward_per_share"]), money(s["gain_at_target"]), money(s["charges_at_target"]),
                    money(s["net_at_target"]), s["reward_risk"],
                    (" after charges (%.2f before)" % s["reward_risk_gross"]) if s["include_charges"] else ""))
    if s["break_even"] is not None:
        lines.append("Break-even: %s, a move of %s a share just to pay the charges."
                     % (px(s["break_even"]), px(s["break_even_move"])))
    lines.append("")
    lines.append("Proposed order (Kite place_order arguments; nothing has been placed):")
    e, st = order["entry"], order["stop"]
    how = "LIMIT %s" % px(e["price"]) if e["order_type"] == "LIMIT" else "MARKET"
    lines.append("  entry   BUY %d %s, NSE, %s (intraday), %s, DAY" % (q, s["symbol"], e["product"], how))
    if st["order_type"] == "SL":
        lines.append("  stop    SELL %d %s, %s, SL trigger %s, limit %s (%s%% under, rounded down), DAY, "
                     "placed once the entry fills" % (q, s["symbol"], st["product"], px(st["trigger_price"]),
                                                      px(st["price"]), _num(s["stop_limit_buffer_pct"])))
    else:
        lines.append("  stop    SELL %d %s, %s, SL-M trigger %s, DAY, placed once the entry fills"
                     % (q, s["symbol"], st["product"], px(st["trigger_price"])))
    lines.append("  target  %s: %s" % (px(s["target"]), TARGET_HOW))
    if st["order_type"] == "SL":
        lines.append("An SL order is a stop with a limit: in a gap below %s it may not fill, and then you sell "
                     "by hand." % px(st["price"]))
    else:
        lines.append("SL-M is a market order once triggered. Market orders sent by API may need market "
                     "protection at your broker, and Kite can reject them without it; SL is the safer default.")
    if e["order_type"] == "MARKET":
        lines.append("A MARKET buy fills at whatever the market gives. Market orders sent by API may need "
                     "market protection at your broker.")
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (nse-alerts rings, nse-broker "
                     "flatten). Zerodha squares MIS off itself at 15:25 and charges for it." % s["flat_by"])
    lines.append("")
    lines.append("Next: nse-risk checks it, then nse-broker asks you yes or no.")
    lines.append("Not financial advice. Nothing is placed without your explicit yes.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source, now_utc):
    doc = {
        "created_by": "nse-budget",
        "created_at": nc.utc_iso(now_utc),
        "status": "proposed",
        "broker": str((cfg or {}).get("broker") or "paper").lower(),
        "note": NOTE,
        "source": source,
        "orders": orders,
    }
    nc.write_json(path, doc)
    return doc


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    try:
        now = nc.parse_now(a.now)
    except ValueError as e:
        raise SizeError(str(e))
    today = nc.to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run nse-exit first)")
    if a.market and a.limit is not None:
        raise SizeError("pick one: --market or --limit")
    limit = None
    if a.limit is not None and a.limit != "plan":
        limit = _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 1334.00")
    sym = nc.nse_symbol((plan or {}).get("symbol"))
    notes, price = [], None
    ppath = a.prices or (os.path.join("trader", "prices.json")
                         if os.path.exists(os.path.join("trader", "prices.json")) else None)
    if ppath and sym and os.path.exists(ppath):
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        ref = limit if limit is not None else _f(plan.get("entry")) or 0
        if p is not None and fresh:
            if a.market:
                price = p
                if p > ref + 1e-9:
                    notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk is "
                                 "bigger: sized at %s." % (sym, px(p), nc.to_ist(t).strftime("%H:%M"),
                                                           px(ref), px(p)))
            elif p > ref + 1e-9:
                notes.append("%s last %s (as of %s IST) is above the limit %s: the order rests until the price "
                             "comes back, and may never fill." % (sym, px(p), nc.to_ist(t).strftime("%H:%M"),
                                                                  px(ref)))
        elif p is not None and a.market:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + nc.ist_text(t)) if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, a.market, price, cash, today)
    order = build_order(s)
    if order and a.write:
        write_orders(a.write, [order], cfg, a.plan, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Size an NSE intraday trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default=os.path.join("trader", "plan.json"),
                    help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default=os.path.join("trader", "config.json"),
                    help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT buy at PRICE (the default is a LIMIT at the plan's entry)")
    ap.add_argument("--market", action="store_true", help="a MARKET buy instead (only if the user asked)")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default=os.path.join("trader", "portfolio.json"),
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-13 10:05\"")
    ap.add_argument("--write", nargs="?", const=os.path.join("trader", "orders.json"), metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        try:
            cfg = nc.load_config(a.config)
        except ValueError as e:
            raise SizeError("%s: %s" % (a.config, e))
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
