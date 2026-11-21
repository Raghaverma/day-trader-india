"""Why only N trades, and what the whole principal would have done. Standard library only; paper; not advice.

Two questions the report answers every day, from the day's own signals and fills (sizes.simulate, the same
engine as "the same signals at other account sizes"):

1. Why so few trades? Three rules each stop the day at about three entries:
   * the trade cap (max_trades_per_day; a placed entry counts even if its LIMIT never fills);
   * the daily loss limit (max_daily_loss_inr): every entry reserves its whole max loss for the rest of the day,
     even after it closes, so the limit divided by the max loss a trade is a second cap on entries;
   * the cash: the budget less the cash kept back (cash_buffer_pct), with no margin; an order sized to lose
     max_loss_per_trade_inr at a tight stop is a large share of it.
   The "ladder" lifts them one at a time: as run, without the cap, without the cap and the loss limit, and
   with cash for every signal too (every signal at its own max-loss size).

2. What if every trade used the whole principal? No risk sizing: each signal takes all the cash that is
   free (nothing kept back, no cap, no daily limit), once with the money alone and once with intraday margin
   (MIS; `margin`, 5 by default, Zerodha's usual leverage on large stocks). While a trade is open its money is
   in it, so later signals wait for it to close. The stress lines say what a 5%, 10% or 20% fall in the
   largest position would cost: a stop is not a guarantee (a gap, a halt, a lower circuit).

Same fills and exits as the paper broker, no market impact: the largest orders are the least believable.
"""

import autopilot_common as ac
import sizes

nc = ac.nc
DEFAULT_MARGIN = 5.0
STRESS = (5, 10, 20)
DEFAULT_CAPS = (3, 5, 8, 10)


def _cfg_text(cfg):
    return {"cap": int(cfg.get("max_trades_per_day") or 0), "daily": float(cfg.get("max_daily_loss_inr") or 0),
            "per_trade": float(cfg.get("max_loss_per_trade_inr") or 0), "budget": float(cfg.get("budget") or 0),
            "buffer_pct": float(cfg.get("cash_buffer_pct") or 0)}


def ladder_steps():
    return [("As run (every rule)", {}, 1.0),
            ("Without the trade cap", {"max_trades_per_day": 0}, 1.0),
            ("Without the cap or the daily loss limit", {"max_trades_per_day": 0, "max_daily_loss_inr": 0}, 1.0),
            ("Without the cap, the loss limit or the cash limit", {"max_trades_per_day": 0,
                                                                   "max_daily_loss_inr": 0}, 1000.0)]


ALL_IN_MIN_SHARE = 0.5        # an all-in trade waits until at least half the money is free (no crumbs)


def all_in_overrides(budget, margin=1.0):
    return {"max_trades_per_day": 0, "max_daily_loss_inr": 0, "max_loss_per_trade_inr": 0, "max_order_inr": 0,
            "max_position_pct": 100, "cash_buffer_pct": 0, "min_order_inr": budget * margin * ALL_IN_MIN_SHARE}


def peak_open(trades):
    """The most money in stock at one time (filled trades, from the signal to the exit)."""
    edges = []
    for x in trades:
        if x.get("fill") is None or not x.get("at"):
            continue
        a = sizes._mins(x["at"])
        b = sizes._mins(x["exit_at"]) if x.get("exit_at") else 24 * 60
        edges += [(a, 1, x["order_value"]), (b, 0, -x["order_value"])]
    run, peak = 0.0, 0.0
    for _, _, v in sorted(edges):
        run += v
        peak = max(peak, run)
    return round(peak, 2)


def _row(label, sim, key):
    t = sim["totals"]
    closed = [x for x in sim["trades"] if x.get("r") is not None]
    worst = min(closed, key=lambda x: x["net"]) if closed else None
    big = max(sim["trades"], key=lambda x: x["order_value"]) if sim["trades"] else None
    return {"key": key, "label": label, "totals": t, "skipped": sim["skipped"], "peak_open": peak_open(sim["trades"]),
            "trades": [{k: x.get(k) for k in ("day", "symbol", "at", "exit_at", "qty", "order_value", "fill", "exit",
                                              "result", "net",
                                              "charges", "max_loss", "r")} for x in sim["trades"]],
            "worst_trade": {"symbol": worst["symbol"], "net": worst["net"]} if worst else None,
            "largest": {"symbol": big["symbol"], "order_value": big["order_value"], "max_loss": big["max_loss"],
                        "stress": {str(p): round(big["order_value"] * p / 100.0, 2) for p in STRESS}}
            if big else None}


def build(rep, cfg, margin=DEFAULT_MARGIN, max_charges_r=None):
    """{"limits": {...}, "ladder": [rows], "all_in": [rows], "margin"} for the day's report."""
    sigs = sizes.from_report(rep)
    budget = float(cfg.get("budget") or 0)
    if budget <= 0:
        raise ValueError("config.json has no budget")
    mc = None if max_charges_r is None else float(max_charges_r)
    ladder = []
    for i, (label, over, m) in enumerate(ladder_steps()):
        ladder.append(_row(label, sizes.simulate(sigs, cfg, budget, mc, margin=m, overrides=over), "ladder%d" % i))
    all_in = [_row("Today's rules", sizes.simulate(sigs, cfg, budget, mc), "rules"),
              _row("Whole %s in each trade, no borrowing" % sizes.label(budget),
                   sizes.simulate(sigs, cfg, budget, mc, margin=1.0, overrides=all_in_overrides(budget)),
                   "all_in_1x"),
              _row("Whole %s with %gx intraday margin (%s of stock)" % (sizes.label(budget), margin,
                                                                        sizes.label(budget * margin)),
                   sizes.simulate(sigs, cfg, budget, mc, margin=margin, overrides=all_in_overrides(budget, margin)),
                   "all_in_margin")]
    return {"limits": limits(rep, cfg), "ladder": ladder, "all_in": all_in, "margin": float(margin),
            "signals": len(sigs), "budget": budget}


def cap_overrides(cfg, budget, cap):
    """N trades a day: the cap, and a daily loss limit of N x the max loss a trade (at this budget), so the Nth
    trade is never kept out by the loss rule's reservation. Every other rule as configured."""
    per = float(sizes.scaled_cfg(cfg, budget).get("max_loss_per_trade_inr") or 0)
    return {"max_trades_per_day": int(cap), "max_daily_loss_inr": round(per * int(cap), 2) if per else 0}


def trade_caps(signals, cfg, caps=DEFAULT_CAPS, budget=None, max_charges_r=None):
    """The same signals at each number of trades a day, at one budget (the config's unless given). The rows are
    "cap3", "cap5", ... in the order asked; each carries its daily loss limit, totals, skips, trades and the most
    money in stock at once."""
    budget = float(budget or cfg.get("budget") or 0)
    if budget <= 0:
        raise ValueError("config.json has no budget")
    out = []
    for n in caps:
        over = cap_overrides(cfg, budget, n)
        sim = sizes.simulate(signals, cfg, budget, max_charges_r, overrides=over)
        row = _row("%d trades a day" % int(n), sim, "cap%d" % int(n))
        row.update(cap=int(n), daily_loss=over["max_daily_loss_inr"], budget=budget, days=sim["days"])
        out.append(row)
    return out


def caps_for_report(rep, cfg, caps=DEFAULT_CAPS, budget=None, max_charges_r=None):
    rows = trade_caps(sizes.from_report(rep, keep_filtered=True), cfg, caps, budget, max_charges_r)
    for r in rows:
        r.pop("days", None)
    return {"budget": rows[0]["budget"] if rows else float(budget or cfg.get("budget") or 0),
            "live_cap": int(cfg.get("max_trades_per_day") or 0), "rows": rows}


def limits(rep, cfg):
    """The three limits in numbers, and what the day used of them."""
    c = _cfg_text(cfg)
    entries = list(rep.get("trades") or [])
    plans = rep.get("plans") or {}
    unfilled = [t["symbol"] for t in entries if t.get("fill") is None]
    orders = [float(t.get("qty") or 0) * float(t.get("fill") or (plans.get(t["symbol"]) or {}).get("entry") or 0)
              for t in entries]
    orders = [o for o in orders if o > 0]
    avg_order = sum(orders) / len(orders) if orders else None
    usable = c["budget"] * (1 - c["buffer_pct"] / 100.0)
    why = (rep.get("shadows") or {}).get("by_why") or {}
    capped = int((why.get("shadow") or {}).get("n") or 0)
    capped_net = float((why.get("shadow") or {}).get("net") or 0)
    return {"cap": c["cap"], "daily": c["daily"], "per_trade": c["per_trade"],
            "entries_by_loss_limit": int(c["daily"] // c["per_trade"]) if c["daily"] and c["per_trade"] else None,
            "budget": c["budget"], "buffer_pct": c["buffer_pct"], "usable_cash": round(usable, 2),
            "avg_order": round(avg_order, 2) if avg_order else None,
            "orders_that_fit": int(usable // avg_order) if avg_order else None,
            "entries": len(entries), "unfilled": unfilled, "orders_total": round(sum(orders), 2),
            "capped": capped, "capped_net": round(capped_net, 2)}


def instruments(cfg, settings):
    """What the autopilot trades, in words, from the config itself."""
    fno = bool(cfg.get("allow_fno"))
    short = bool(cfg.get("allow_short"))
    margin = bool(cfg.get("allow_margin"))
    series = ", ".join(cfg.get("allowed_series") or ["EQ"])
    u = settings.get("universe") or "nifty50"
    words = ["Shares only, in NSE's cash market: series %s, the %s stocks" % (
        series, ", ".join(u) if isinstance(u, list) else {"nifty50": "Nifty 50", "nifty500": "Nifty 500",
                                                         "fno": "F&O-list"}.get(str(u), str(u)))]
    words.append("intraday (%s, flat by %s)" % (cfg.get("product") or "MIS", cfg.get("flat_by") or "15:10"))
    words.append("long only (buy, then sell)" if not short else "long and short")
    words.append("no futures or options" if not fno else "futures and options allowed by config")
    words.append("paid in full from cash, no margin" if not margin else "with margin")
    if (settings.get("shorts") or "off") == "shadow":
        words.append("short breakdowns are replayed in the report, never traded")
    return "; ".join(words) + "."
