#!/usr/bin/env python3
"""Position sizing for nse-budget. Standard library only, no network.

Sizes ONE intraday (MIS) trade on the NSE cash market from trader/plan.json
(written by nse-exit) to your budget and your max loss per trade, then writes
a Kite-shaped order to trader/orders.json: an entry leg, a stop leg (an SL
order placed after the entry fills) and the target as an alert. It never
places anything: nse-risk checks the order and nse-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json (this order only)
    python3 size.py --write --append                 # add this order, keep today's orders for other symbols
    python3 size.py --limit 1333.50 --write          # a LIMIT buy at 1,333.50 instead of the plan's entry
    python3 size.py --market --write                 # a MARKET buy (only if you asked for one)
    python3 size.py --now "2026-11-13 10:05" --json  # pin the clock (IST)
    python3 size.py --init-config --budget 100000 --max-loss 500 --max-daily-loss 1500

The trader folder is nse_common.trader_home(): $NSE_TRADER_HOME, else the
nearest trader/ with a config.json, else ~/.day-trader-india. Without a
config.json it stops (exit 2) and writes nothing: the rules are never
guessed. --init-config writes one from the defaults and the values you pass,
and never overwrites an existing file.

How many shares. Whole shares only, nse_common.size_limits (the same rules
nse-exit and nse-timemachine estimate with, and nse-risk checks against):

    shares = the smallest of
      max loss       the most shares whose loss at the WORST fill of the stop
                     leg (its SL limit, stop_limit_buffer_pct under the
                     trigger; "loss_basis": "trigger" measures at the trigger)
                     plus the estimated round-trip charges fits in
                     max_loss_per_trade_inr
      position cap   account x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (account minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json)
    where account = the budget, or the account's real value (cash + holdings
    in trader/portfolio.json) when that is smaller (nse_common.account_base).

The entry is a LIMIT buy at the plan's entry by default (or your --limit
price). With --market it is sized at the plan's entry, or the price in
trader/prices.json if that is fresh (price_fresh_minutes, 5) and higher.
Every price sits on the stock's order tick (nse_common.order_tick: the
coarsest tick within tick_band_pct of the price, since NSE sets a stock's
tick from an earlier close): the stop rounds DOWN, the limit and the target
round to the nearest tick. The stop leg is an SL order: trigger at the stop,
limit stop_limit_buffer_pct under it, rounded down (nse_common.stop_limit_price).

The max loss and the size refusal are measured at the worst fill, and every
line says which price that is. Reward:risk after charges is the plan's R, to
the stop trigger (the number nse-exit prints and nse-risk gates on, so the
three agree); what it would be if the stop filled at its limit is printed next
to it (reward_risk_worst) but does not gate: the max loss already caps that.

Kite has no bracket orders and no OCO for MIS, so the target is an alert,
not a resting order: when it rings, cancel the stop, then sell.
"""

import argparse
import decimal
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

D = nc.D
SETUP_WORDS = {"orb": "opening-range breakout", "level": "close above a level", "flag": "flag",
               "bounce": "oversold bounce"}
BOUND_WORDS = {"position cap": "position cap", "max order": "max order", "cash": "cash after the buffer"}
NOTE = ("Proposals only. Nothing is placed until nse-risk passes each order and you say yes to it in the chat.")
TARGET_HOW = ("an alert, not a resting order: Kite has no OCO for MIS. When it rings, cancel the stop, then sell.")
# --init-config: (argparse dest, config key, what it is, kind)
INIT_KEYS = (
    ("budget", "budget", "budget", "inr"),
    ("max_loss", "max_loss_per_trade_inr", "max loss per trade (charges included)", "inr"),
    ("max_daily_loss", "max_daily_loss_inr", "daily loss limit", "inr"),
    ("cash_buffer_pct", "cash_buffer_pct", "always kept in cash", "pct"),
    ("max_position_pct", "max_position_pct", "biggest position", "pct"),
    ("max_order", "max_order_inr", "biggest order", "inr"),
    ("min_order", "min_order_inr", "smallest order", "inr"),
    ("max_trades", "max_trades_per_day", "entries a day", "count"),
    ("min_reward_risk", "min_reward_risk", "minimum reward:risk after charges", "ratio"),
)


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


def r2(d):
    return float(nc.dec(d).quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP))


def break_even(qty, entry, tick, cfg):
    """The lowest sell price on the tick that pays back the round-trip charges."""
    if qty <= 0:
        return None
    est = entry + nc.charges_total(qty, entry, entry, cfg) / qty
    p = nc.round_to_tick(est, tick, "up")
    for _ in range(100000):
        if qty * (p - entry) >= nc.charges_total(qty, entry, p, cfg):
            return p
        p += tick
    return p


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


def basis_of(cfg):
    return "trigger" if str((cfg or {}).get("loss_basis") or "limit").lower() == "trigger" else "limit"


def at_words(s):
    """Where the max loss is measured, for people: "at the SL limit 1,322.10"."""
    if s["loss_basis"] == "trigger":
        return "at the stop trigger %s" % px(s["worst_exit"])
    if s["stop_order_type"] == "SL-M":
        return "at %s, %s%% under the SL-M trigger" % (px(s["worst_exit"]), _num(s["stop_limit_buffer_pct"]))
    return "at the SL limit %s" % px(s["worst_exit"])


def pending_entries(paper_path, today):
    """Rupees in today's paper LIMIT buys still waiting to fill (qty x limit), from the paper broker's trades."""
    doc = nc.read_json(paper_path) if paper_path and os.path.isfile(paper_path) else None
    total = 0.0
    for t in (doc or {}).get("trades") or []:
        if isinstance(t, dict) and t.get("status") == "open" and t.get("date") == today:
            total += float(t.get("qty") or 0) * float(t.get("limit") or 0)
    return round(total, 2)


def account(portfolio_path, prices_doc=None):
    """(cash, portfolio_total) from trader/portfolio.json, holdings valued at prices.json's prices
    (nse_common.portfolio_total). (None, None) when there is no portfolio file."""
    if not portfolio_path or not os.path.exists(portfolio_path):
        return None, None
    pf = read_json(portfolio_path, "portfolio")
    if not isinstance(pf, dict):
        raise SizeError("%s is not a portfolio (a JSON object with \"cash\")" % portfolio_path)
    prices = (prices_doc or {}).get("prices") if isinstance(prices_doc, dict) else None
    prices = {nc.nse_symbol(k): v for k, v in (prices or {}).items()} if isinstance(prices, dict) else {}
    return _f(pf.get("cash")), nc.portfolio_total(pf, prices)


# ---------------------------------------------------------------- the sizing

def size_trade(plan, cfg, budget=None, limit=None, market=False, price=None, cash=None, today=None,
               portfolio_total=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit            the LIMIT price (None = the plan's entry)
    market           True for a MARKET buy (sized at the higher of the plan's entry and a fresh price)
    price            a FRESH latest price (prices.json), used for a market order
    cash             the cash in trader/portfolio.json, if there is one
    today            the IST date (YYYY-MM-DD); a plan for another day is refused
    portfolio_total  cash + holdings from portfolio.json (nse_common.portfolio_total), if there is one"""
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

    budget = _f(budget if budget is not None else c.get("budget")) or 0.0
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in config.json")
    buf_pct = _f(c.get("cash_buffer_pct") or 0)
    if buf_pct is None or not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    slb = _f(c.get("stop_limit_buffer_pct") if c.get("stop_limit_buffer_pct") is not None else 0.5)
    if slb is None or not 0 <= slb < 20:
        raise SizeError("stop_limit_buffer_pct must be between 0 and 20 (it is %s)" % c.get("stop_limit_buffer_pct"))
    stop_type = str(c.get("stop_order_type") or "SL").upper()
    if stop_type not in ("SL", "SL-M"):
        raise SizeError("stop_order_type must be \"SL\" or \"SL-M\", not %r" % c.get("stop_order_type"))
    with_charges = bool(c.get("include_charges", True))

    def tick_at(p):
        return nc.order_tick(p, sym, c)

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
    risk = ref - stop                                   # to the stop trigger
    reward = target - ref
    basis = basis_of(c)
    worst = nc.worst_exit_price(stop, sym, c)           # the SL limit (or the trigger with loss_basis "trigger")
    risk_worst = ref - worst
    stop_limit = nc.stop_limit_price(stop, sym, c) if stop_type == "SL" else None

    base = nc.account_base(c, portfolio_total, budget)
    try:
        raw = nc.size_limits(ref, stop, sym, c, budget=budget, cash=cash, portfolio_total=portfolio_total)
    except (TypeError, ValueError, decimal.InvalidOperation):
        raise SizeError("config.json has a non-number in budget, cash_buffer_pct, max_position_pct, "
                        "max_order_inr or max_loss_per_trade_inr")
    cash_buffer = (nc.dec(base) * nc.dec(buf_pct) / 100).quantize(D("0.01"))
    base_word = "budget" if portfolio_total is None or base >= budget else "account"
    base_text = "%s %s" % (base_word, money(base))
    if base_word == "account":
        base_text += " (cash + holdings in portfolio.json, under your %s budget)" % money(budget)
    s_basis = {"loss_basis": basis, "worst_exit": worst, "stop_order_type": stop_type, "stop_limit_buffer_pct": slb}
    limits = []
    for l in raw:
        n, name = l["shares"], l["name"]
        if name == "max loss":
            lo = nc.loss_at_stop(max(n, 1), ref, stop, sym, c)
            if with_charges:
                how = "%s incl. charges %s: %d x %s = %s + %s charges = %s" % (
                    money(l["inr"]), at_words(s_basis), max(n, 1), px(lo["per_share"]), money(lo["gross"]),
                    money(lo["charges"]), money(lo["total"]))
            else:
                how = "%s %s: %d x %s = %s" % (money(l["inr"]), at_words(s_basis), max(n, 1), px(lo["per_share"]),
                                               money(lo["gross"]))
        elif name == "position cap":
            how = "%s%% of %s = %s / %s" % (_num(c.get("max_position_pct")), base_text, money(l["inr"]), px(ref))
        elif name == "max order":
            how = "%s / %s" % (money(l["inr"]), px(ref))
        else:
            if cash is not None and nc.dec(cash) < nc.dec(base):
                note = "cash %s in portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
            else:
                note = "%s minus the %s cash buffer" % (base_text, money(cash_buffer))
            how = "%s = %s / %s" % (note, money(l["inr"]), px(ref))
        limits.append({"name": name, "inr": l["inr"], "shares": n, "how": how})
    available = D(repr(float([l for l in raw if l["name"] == "cash"][0]["inr"])))

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = qty * ref
    lo = nc.loss_at_stop(qty, ref, stop, sym, c)                # the worst fill: what the max loss is
    loss, ch_stop = lo["gross"], lo["charges"]
    ch_target = nc.charges_total(qty, ref, target, c)
    ch_trigger = nc.charges_total(qty, ref, stop, c)            # the round trip if the stop fills AT the trigger
    gain = qty * reward
    be = break_even(qty, ref, tick, c)
    # Reward:risk is the plan's R to the stop trigger, net of charges: the number nse-exit shows and
    # nse-risk gates on (so the three skills agree). The worst fill is the max-loss limit's business;
    # it is reported next to it as reward_risk_worst, not used to gate.
    if qty > 0 and with_charges:
        rr = (gain - ch_target) / (qty * risk + ch_trigger)
        rr_worst = (gain - ch_target) / (loss + ch_stop)
    else:
        rr = reward / risk
        rr_worst = reward / risk_worst
    q2 = D("0.01")
    rr, rr_worst = (x.quantize(q2, rounding=decimal.ROUND_HALF_UP) for x in (rr, rr_worst))
    rr_gross = (reward / risk).quantize(q2, rounding=decimal.ROUND_HALF_UP)

    max_loss_cap = _f(c.get("max_loss_per_trade_inr"))
    refusal = None
    min_rr = _f(c.get("min_reward_risk"))
    where = at_words(s_basis)
    if qty == 0:
        if bound[0] == "max loss":
            one = nc.loss_at_stop(1, ref, stop, sym, c)
            refusal = ("one share loses %s %s (entry %s minus %s%s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with nse-exit."
                       % (money(one["total"] if with_charges else one["gross"]), where, px(ref), px(worst),
                          ", plus charges" if with_charges else "", money(max_loss_cap)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], [l for l in limits if l["name"] == bound[0]][0]["how"]))
    elif min_rr and rr < nc.dec(min_rr):
        refusal = ("reward:risk %s%s (to the stop trigger %s; target %s, entry %s) is under your minimum %s. "
                   "No order. Skip it, or make a new plan with nse-exit (a farther target)."
                   % (rr, " after charges" if with_charges else "", px(stop), px(target), px(ref), _num(min_rr)))
    elif cost < nc.dec(c.get("min_order_inr") or 0):
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))

    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date") or today,
        "entry_time": plan_time(plan.get("entry_time")),
        "entry_plan": fl(nc.round_to_tick(entry_plan, tick_at(entry_plan))),
        "entry": fl(ref), "entry_from": ref_from, "order_type": "MARKET" if market else "LIMIT",
        "stop": fl(stop), "stop_raw": stop_raw, "target": fl(target), "tick": str(tick),
        "stop_order_type": stop_type, "stop_limit": fl(stop_limit) if stop_limit is not None else None,
        "stop_limit_buffer_pct": slb, "loss_basis": basis, "worst_exit": fl(worst),
        "risk_per_share": fl(risk), "risk_per_share_worst": fl(risk_worst), "reward_per_share": fl(reward),
        "reward_risk": float(rr), "reward_risk_gross": float(rr_gross), "reward_risk_worst": float(rr_worst),
        "include_charges": with_charges,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "account_base": float(base), "portfolio_total": portfolio_total, "cash": cash,
        "cash_buffer_pct": buf_pct, "cash_buffer": float(cash_buffer),
        "cash_available": float(available), "max_loss_per_trade_inr": max_loss_cap, "limits": limits,
        "qty": qty, "bound_by": bound[0], "cost": float(cost), "max_loss": float(loss),
        "charges_at_stop": float(ch_stop), "max_loss_with_charges": float(loss + ch_stop),
        "charges_at_trigger": float(ch_trigger),
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
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s %s%s)"
           % (setup[:1].upper() + setup[1:], sym, q, bound, q, px(s["risk_per_share_worst"]), money(s["max_loss"]),
              at_words(s), (", %s with charges" % money(s["max_loss_with_charges"])) if s["include_charges"] else ""))
    return {
        "symbol": sym,
        "entry": entry,
        "stop": stop,
        "target": {"price": s["target"], "how": TARGET_HOW},
        "flat_by": s["flat_by"],
        "why": why,
        "plan": {"symbol": sym, "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"],
                 "charges_at_stop": s["charges_at_stop"], "max_loss_with_charges": s["max_loss_with_charges"],
                 "charges_at_target": s["charges_at_target"], "reward_risk": s["reward_risk"],
                 "reward_risk_worst": s["reward_risk_worst"], "break_even": s["break_even"], "setup": s["setup"],
                 "date": s["date"], "entry_time": s["entry_time"], "flat_by": s["flat_by"], "tick": s["tick"],
                 "loss_basis": s["loss_basis"], "worst_exit": s["worst_exit"],
                 "risk_per_share_worst": s["risk_per_share_worst"]},
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
    lines.append("Sized at %s (%s). Risk %s a share to the stop trigger %s."
                 % (px(s["entry"]), src, px(s["risk_per_share"]), px(s["stop"])))
    if s["loss_basis"] == "trigger":
        lines.append("Max loss measured at the stop trigger (\"loss_basis\": \"trigger\"): a gap below it can "
                     "lose more than this.")
    else:
        lines.append("Max loss measured at the worst fill, %s: %s a share (entry %s minus %s, %.1f times the %s to "
                     "the trigger). A stop that gaps fills no lower than its limit."
                     % (at_words(s), px(s["risk_per_share_worst"]), px(s["entry"]), px(s["worst_exit"]),
                        s["risk_per_share_worst"] / s["risk_per_share"], px(s["risk_per_share"])))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    width = max(len(l["how"]) for l in s["limits"])
    for l in s["limits"]:
        lines.append("  %-13s %-*s %4d shares%s" % (l["name"], width, l["how"], l["shares"],
                                                    "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Not financial advice. Nothing is placed without your explicit yes.")
        return "\n".join(lines)
    q = s["qty"]
    lines.append("Size: %d shares of %s = %s at %s." % (q, s["symbol"], money(s["cost"]), px(s["entry"])))
    lines.append("If the stop is hit and fills %s: %d x %s = %s, plus about %s in charges = %s."
                 % (at_words(s), q, px(s["risk_per_share_worst"]), money(s["max_loss"]), money(s["charges_at_stop"]),
                    money(s["max_loss_with_charges"])))
    lines.append("At the target: %d x %s = %s, minus about %s in charges = %s. Reward:risk %.2f%s, measured to the "
                 "stop trigger %s like nse-exit and nse-risk (%.2f before charges; %.2f if the stop fills %s)."
                 % (q, px(s["reward_per_share"]), money(s["gain_at_target"]), money(s["charges_at_target"]),
                    money(s["net_at_target"]), s["reward_risk"], " after charges" if s["include_charges"] else "",
                    px(s["stop"]), s["reward_risk_gross"], s["reward_risk_worst"], at_words(s)))
    if s["break_even"] is not None:
        lines.append("Break-even: %s, a move of %s a share just to pay the charges on %d shares."
                     % (px(s["break_even"]), px(s["break_even_move"]), q))
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
        lines.append("SL-M is a market order once triggered, so it has no worst price: the max loss assumes %s. "
                     "Market orders sent by API may need market protection at your broker, and Kite can reject "
                     "them without it; SL is the safer default." % px(s["worst_exit"]))
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

def order_symbol(o):
    if not isinstance(o, dict):
        return ""
    return nc.nse_symbol(o.get("symbol") or (o.get("entry") or {}).get("tradingsymbol") or "")


def write_orders(path, orders, cfg, source, now_utc, append=False, today=None):
    """Write orders.json. append=True keeps the orders already there for OTHER symbols (an order for
    the same symbol is replaced; one for another IST day is dropped, nse-risk would refuse it). The
    file's created_at is always this write's time; each order keeps its own created_at.
    Returns (doc, notes)."""
    kept, notes = [], []
    if append and os.path.exists(path):
        try:
            old = nc.read_json(path)
        except ValueError as e:
            raise SizeError("%s is not valid JSON (%s): fix or delete it, or write without --append" % (path, e))
        if not isinstance(old, dict) or not isinstance(old.get("orders"), list):
            raise SizeError("%s is not an orders file (no \"orders\" list): fix or delete it, or write without "
                            "--append" % path)
        new = {order_symbol(o) for o in orders}
        for o in old["orders"]:
            sym = order_symbol(o)
            day = ((o or {}).get("plan") or {}).get("date") if isinstance(o, dict) else None
            if sym in new:
                notes.append("Replaced the %s order already in %s." % (sym, path))
            elif today and day and str(day) != today:
                notes.append("Dropped the %s order for %s from %s: it is not for today (%s IST)."
                             % (sym or "unnamed", day, path, today))
            else:
                if isinstance(o, dict) and not o.get("created_at") and old.get("created_at"):
                    # the file's created_at is about to become this write's time: an older order keeps its own age
                    o = dict(o, created_at=old["created_at"])
                kept.append(o)
    doc = {
        "created_by": "nse-budget",
        "created_at": nc.utc_iso(now_utc),
        "status": "proposed",
        "broker": str((cfg or {}).get("broker") or "paper").lower(),
        "note": NOTE,
        "source": source,
        "orders": kept + list(orders),
    }
    nc.write_json(path, doc)
    return doc, notes


# ---------------------------------------------------------------- config

def _clean_number(v):
    return int(v) if float(v) == int(v) else float(v)


def init_config(a):
    """--init-config: write config.json from the defaults and the values passed. Never overwrites."""
    path = a.config
    if os.path.exists(path):
        raise SizeError("%s already exists: nothing written. Your rules are in it; edit it to change one "
                        "(this never overwrites a config)." % path)
    cfg = json.loads(json.dumps(nc.DEFAULT_CONFIG))
    given = {}
    for dest, key, what, kind in INIT_KEYS:
        v = getattr(a, dest, None)
        if v is None:
            continue
        if not math.isfinite(v):
            raise SizeError("--%s wants a number (got %s): nothing written" % (dest.replace("_", "-"), v))
        if kind == "pct" and not 0 <= v < 100:
            raise SizeError("--%s must be between 0 and 100 (got %s): nothing written" % (dest.replace("_", "-"), _num(v)))
        if kind != "pct" and v <= 0:
            raise SizeError("--%s must be above 0 (got %s): nothing written" % (dest.replace("_", "-"), _num(v)))
        if kind == "count" and v != int(v):
            raise SizeError("--%s wants a whole number (got %s): nothing written" % (dest.replace("_", "-"), _num(v)))
        given[key] = _clean_number(v)
    cfg.update(given)
    cfg["broker"] = "paper"
    if cfg["max_loss_per_trade_inr"] >= cfg["budget"]:
        raise SizeError("the max loss per trade %s is not under the budget %s: nothing written"
                        % (money(cfg["max_loss_per_trade_inr"]), money(cfg["budget"])))
    if cfg["max_daily_loss_inr"] < cfg["max_loss_per_trade_inr"]:
        raise SizeError("the daily loss limit %s is under the max loss per trade %s, so nse-risk would block "
                        "every trade: nothing written" % (money(cfg["max_daily_loss_inr"]),
                                                          money(cfg["max_loss_per_trade_inr"])))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:        # "x": fails if the file appeared since the check
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except FileExistsError:
        raise SizeError("%s already exists: nothing written (this never overwrites a config)." % path)
    print("Wrote %s:" % path)
    for dest, key, what, kind in INIT_KEYS:
        v = cfg[key]
        shown = money(v) if kind == "inr" else ("%s%%" % _num(v)) if kind == "pct" else _num(v)
        print("  %-24s %-12s %s%s" % (key, shown, what, "  (yours)" if key in given else "  (default)"))
    print("  %-24s %-12s %s" % ("broker", "paper", "no broker: going live is your own change, in this file"))
    print("Everything else is the default (config.example.json). Edit the file to change a rule.")
    return 0


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
    notes, price, pdoc = [], None, None
    ppath = a.prices or (nc.tpath("prices.json") if os.path.exists(nc.tpath("prices.json")) else None)
    if ppath and os.path.exists(ppath):
        pdoc = read_json(ppath, "prices")
    if pdoc is not None and sym:
        p, t, fresh = nc.fresh_price(pdoc, sym, now, cfg=cfg)
        ref = limit if limit is not None else _f((plan or {}).get("entry")) or 0
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
            notes.append("%s in %s is %s: not used (older than %s minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + nc.ist_text(t)) if t else "undated",
                            _num(cfg.get("price_fresh_minutes") or 5)))
    cash, total = account(a.portfolio, pdoc)
    waiting = pending_entries(a.paper, today)
    if waiting:
        cash = (cash if cash is not None else float(a.budget or cfg.get("budget") or 0)) - waiting
        notes.append("%s of today's paper LIMIT buys are still waiting to fill: that cash is held back, so "
                     "this trade is sized on the rest" % money(waiting))
    s = size_trade(plan, cfg, a.budget, limit, a.market, price, cash, today, portfolio_total=total)
    order = build_order(s)
    if order:
        order = dict(order, created_at=nc.utc_iso(now))      # the order's own age: nse-risk reads it before the file's
    wrote = None
    if order and a.write:
        doc, more = write_orders(a.write, [order], cfg, a.plan, now, append=a.append, today=today)
        notes.extend(more)
        wrote = len(doc["orders"])
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if wrote is not None:
            print("Wrote the %s order to %s (%d order%s in the file)."
                  % (s["symbol"], a.write, wrote, "" if wrote == 1 else "s"))
    return 0 if order else 1


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Size an NSE intraday trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default=nc.tpath("plan.json"), help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default=nc.tpath("config.json"), help="your rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees); with --init-config, "
                                                 "the budget to write")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT buy at PRICE (the default is a LIMIT at the plan's entry)")
    ap.add_argument("--market", action="store_true", help="a MARKET buy instead (only if the user asked)")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--paper", default=nc.tpath("state", "paper.json"),
                    help="the paper broker's trades: LIMIT buys still waiting to fill hold their cash")
    ap.add_argument("--portfolio", default=nc.tpath("portfolio.json"),
                    help="your account; its cash and value cap the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-13 10:05\"")
    ap.add_argument("--write", nargs="?", const=nc.tpath("orders.json"), metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--append", action="store_true",
                    help="with --write: add this order to orders.json, keeping today's orders for other symbols "
                         "(one for the same symbol is replaced)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("set your rules once")
    g.add_argument("--init-config", action="store_true",
                   help="write config.json (the defaults plus the values below) and stop; never overwrites")
    g.add_argument("--max-loss", type=float, help="max loss per trade in rupees, charges included")
    g.add_argument("--max-daily-loss", type=float, help="daily loss limit in rupees")
    g.add_argument("--cash-buffer-pct", type=float, help="percent always kept in cash")
    g.add_argument("--max-position-pct", type=float, help="the biggest position, percent of the account")
    g.add_argument("--max-order", type=float, help="the biggest order in rupees")
    g.add_argument("--min-order", type=float, help="the smallest order in rupees")
    g.add_argument("--max-trades", type=float, help="entries a day")
    g.add_argument("--min-reward-risk", type=float, help="minimum reward:risk after charges")
    a = ap.parse_args(argv)
    try:
        if a.init_config:
            return init_config(a)
        extra = [d for d, _, _, _ in INIT_KEYS if d != "budget" and getattr(a, d, None) is not None]
        if extra:
            raise SizeError("--%s only goes with --init-config: to change a rule, edit %s"
                            % (extra[0].replace("_", "-"), a.config))
        if a.append and not a.write:
            a.write = nc.tpath("orders.json")
        try:
            cfg = nc.load_config(a.config, require=True)
        except nc.ConfigMissing as e:
            print("size.py: %s" % e, file=sys.stderr)
            print("size.py: nothing was sized or written. Ask the user for the budget, the max loss per trade and "
                  "the daily loss limit in rupees, then: size.py --init-config --budget N --max-loss N "
                  "--max-daily-loss N", file=sys.stderr)
            return 2
        except ValueError as e:
            raise SizeError("%s: %s" % (a.config, e))
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
