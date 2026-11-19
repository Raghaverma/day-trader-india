"""The same signals at other account sizes: what the autopilot's paper trades would have made with a budget of
₹1 lakh, ₹10 lakh, ₹50 lakh, ₹1 crore or ₹5 crore. Standard library only; paper; not advice.

The live autopilot trades ONE paper account. The others are not separate accounts: every planned long signal
of the day (traded, or replayed by report.shadow) is re-sized for each budget and run through that budget's
own rules, in the order the signals fired:

  * the config at that size: every rupee limit (max loss a trade, a day, max order) scaled with the budget,
    every percentage and rule the same (scaled_cfg);
  * Claude's veto and an nse-risk block apply at every size (neither depends on the size);
  * the size: nse_common.size_qty with the cash the open positions leave free, as nse-budget does;
  * the cost filter on that size's own charges (brokerage is capped at ₹20 an order, so a big order pays a
    smaller share of its risk: the same signal can be skipped at ₹1 lakh and taken at ₹10 lakh);
  * the trade cap, and nse-risk's daily loss rule: today's entries at their max loss (or their real loss, if
    worse) plus this one must fit in max_daily_loss_inr.

The fills, exits and exit times are the paper broker's, shared by every size. Market impact is NOT modelled: a
₹2 crore order fills at the same price as a ₹50,000 one. study.py reports each order's share of its entry bar's
volume, which is where that stops being believable.
"""

import autopilot_common as ac
import report

nc = ac.nc
DEFAULT_SIZES = [100000, 1000000, 5000000, 10000000, 50000000]
SCALED = ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_order_inr")


def label(budget):
    b = float(budget)
    if b >= 1e7:
        return "₹%s cr" % ("%g" % (b / 1e7))
    if b >= 1e5:
        return "₹%s lakh" % ("%g" % (b / 1e5))
    return nc.inr(b)


def scaled_cfg(cfg, budget):
    """The config at another budget: the rupee limits scaled with it, everything else the same."""
    base = float(cfg.get("budget") or 0)
    if base <= 0:
        raise ValueError("config.json has no budget to scale from")
    k = float(budget) / base
    out = dict(cfg)
    out["budget"] = float(budget)
    for key in SCALED:
        if out.get(key):
            out[key] = round(float(out[key]) * k, 2)
    return out


def _mins(hhmm):
    t = nc.parse_hhmm(hhmm)
    return t.hour * 60 + t.minute


def simulate(signals, cfg, budget, max_charges_r=None, margin=1.0, overrides=None):
    """signals: [{"day", "symbol", "at", "entry", "stop", "fill", "exit", "exit_at", "result", "eligible",
    "why"}] (long only), in any order. Returns {"budget", "trades": [...], "skipped": {reason: n}, "days": {...},
    "totals": {...}} for this budget, carrying the account's realised P&L from day to day.

    whatif.py's scenarios: `overrides` replaces config keys after the scaling (0 turns a limit off), and
    `margin` lets the account hold that many times its money in stock (5 = Zerodha's usual intraday MIS
    leverage on large stocks): the cash, the account base and the position cap are all multiplied by it.
    The return is still measured on the money itself (`budget`)."""
    c = scaled_cfg(cfg, budget)
    c.update(overrides or {})
    margin = float(margin or 1.0)
    ttl = float(c.get("entry_ttl_minutes") or 0)

    def waiting(t, at):
        """An order that never filled holds its slot, its loss and its cash while it waits: until
        entry_ttl_minutes after the signal, or all day without one (the paper broker cancels it at flat_by)."""
        return t["fill"] is None and (not ttl or at < t["at_m"] + ttl)
    cap = int(c.get("max_trades_per_day") or 0)
    daily = float(c.get("max_daily_loss_inr") or 0)
    equity = float(budget)
    trades, skipped, days = [], {}, {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    by_day = {}
    for s in signals:
        by_day.setdefault(s["day"], []).append(s)
    for day in sorted(by_day):
        taken = []
        for s in sorted(by_day[day], key=lambda s: (s.get("at") or "", s["symbol"])):
            if not s.get("eligible", True):
                skip(s.get("why") or "not eligible")
                continue
            at = _mins(s["at"])
            open_cost = sum(t["qty"] * t["fill"] for t in taken
                            if t["fill"] is not None and (t["exit_m"] is None or t["exit_m"] > at))
            open_cost += sum(t["qty"] * t["entry"] for t in taken if waiting(t, at))
            live = [t for t in taken if t["fill"] is not None or waiting(t, at)]
            realised = sum(t["net"] for t in taken if t["exit_m"] is not None and t["exit_m"] <= at)
            cash = (equity + realised) * margin - open_cost
            qty = nc.size_qty(s["entry"], s["stop"], s["symbol"], c, budget=c["budget"] * margin, cash=cash,
                              portfolio_total=(equity + realised) * margin)
            if qty < 1 or qty * s["entry"] < float(c.get("min_order_inr") or 0):
                skip("no size fits (cash or caps)")
                continue
            risk = s["entry"] - s["stop"]
            per_share = nc.intraday_charges(qty, s["entry"], s["stop"], c)["total"] / float(qty)
            charges_r = per_share / risk if risk > 0 else None
            if max_charges_r is not None and charges_r is not None and charges_r > max_charges_r:
                skip("cost filter")
                continue
            if cap and len(live) >= cap:
                skip("trade cap")
                continue
            ml = float(nc.loss_at_stop(qty, s["entry"], s["stop"], s["symbol"], c)["total"])
            used = sum(max(t["max_loss"], -t["net"] if t["exit_m"] is not None and t["exit_m"] <= at else 0)
                       for t in live)
            if daily and used + ml > daily + 0.01:
                skip("daily loss limit")
                continue
            t = {"day": day, "symbol": s["symbol"], "at": s["at"], "qty": int(qty), "entry": s["entry"],
                 "stop": s["stop"], "fill": s.get("fill"), "exit": s.get("exit"), "exit_at": s.get("exit_at"),
                 "result": s.get("result"), "max_loss": round(ml, 2),
                 "charges_r": round(charges_r, 3) if charges_r is not None else None,
                 "order_value": round(qty * s["entry"], 2), "volume_share": None,
                 "exit_m": _mins(s["exit_at"]) if s.get("exit_at") else None, "at_m": at, "net": 0.0, "gross": 0.0,
                 "charges": 0.0, "r": None}
            if s.get("bar_volume"):
                t["volume_share"] = round(qty / float(s["bar_volume"]), 4)
            if t["fill"] is not None and t["exit"] is not None:
                t["gross"] = round(qty * (t["exit"] - t["fill"]), 2)
                t["charges"] = nc.intraday_charges(qty, t["fill"], t["exit"], c)["total"]
                t["net"] = round(t["gross"] - t["charges"], 2)
                t["r"] = round(t["net"] / ml, 3) if ml else None
                t["bp_r"] = report.bp_r(qty, t["fill"], t["exit"], ml)
            taken.append(t)
        day_net = round(sum(t["net"] for t in taken), 2)
        equity = round(equity + day_net, 2)
        days[day] = {"trades": len([t for t in taken if t["fill"] is not None]), "net": day_net,
                     "charges": round(sum(t["charges"] for t in taken), 2), "equity": equity}
        trades.extend(taken)
    closed = [t for t in trades if t["r"] is not None]
    curve, peak, dd = [], float(budget), 0.0
    for d in sorted(days):
        peak = max(peak, days[d]["equity"])
        dd = min(dd, days[d]["equity"] - peak)
    nets = [days[d]["net"] for d in sorted(days)]
    totals = {"trades": len(closed), "unfilled": len([t for t in trades if t["fill"] is None]),
              "wins": sum(1 for t in closed if t["net"] > 0), "losses": sum(1 for t in closed if t["net"] < 0),
              "gross": round(sum(t["gross"] for t in closed), 2), "charges": round(sum(t["charges"] for t in closed), 2),
              "net": round(sum(t["net"] for t in closed), 2),
              "return_pct": round(100 * sum(t["net"] for t in closed) / float(budget), 3),
              "avg_r": round(sum(t["r"] for t in closed) / len(closed), 3) if closed else None,
              "best_day": max(nets) if nets else None, "worst_day": min(nets) if nets else None,
              "green_days": sum(1 for x in nets if x > 0), "red_days": sum(1 for x in nets if x < 0),
              "max_drawdown": round(dd, 2), "largest_order": max([t["order_value"] for t in trades] or [0])}
    slipped = [t for t in closed if t.get("bp_r")]
    totals["breakeven_bps"] = report.breakeven_bps([(t["r"], t["bp_r"]) for t in slipped])
    totals["avg_r_at_bps"] = {str(b): round(sum(t["r"] - b * t["bp_r"] for t in slipped) / len(slipped), 3)
                              for b in report.SLIP_BPS if b} if slipped else {}
    shares = sorted(t["volume_share"] for t in trades if t["volume_share"] is not None)
    if shares:
        totals["median_volume_share"] = shares[len(shares) // 2]
        totals["max_volume_share"] = shares[-1]
        totals["over_10pct_of_bar"] = sum(1 for x in shares if x > 0.10)
    for t in trades:
        t.pop("exit_m", None)
        t.pop("at_m", None)
    return {"budget": float(budget), "label": label(budget), "trades": trades, "skipped": skipped, "days": days,
            "totals": totals}


def from_report(rep):
    """Today's long signals with a plan, as simulate() reads them: the paper trades with their real fills, the
    rest from their replays. Vetoed and risk-blocked signals stay out at every size."""
    out = []
    trades = {t["symbol"]: t for t in rep.get("trades") or []}
    cands = {c["symbol"]: c for c in rep.get("candidates") or [] if (c.get("side") or "long") == "long"}
    plans = rep.get("plans") or {}
    for sym, c in cands.items():
        p = plans.get(sym)
        if not p or not p.get("entry") or not p.get("stop"):
            continue
        sig = {"day": rep["date"], "symbol": sym, "at": p.get("at") or c.get("scan_at"), "entry": p["entry"],
               "stop": p["stop"], "eligible": c.get("outcome") not in ("vetoed", "risk_block"),
               "why": {"vetoed": "Claude's veto", "risk_block": "nse-risk block"}.get(c.get("outcome"))}
        t = trades.get(sym)
        if t and c.get("outcome") == "traded":
            exit_at = (t.get("exit_time") or "")[11:16] or None
            sig.update(fill=t.get("fill"), exit=t.get("exit"), exit_at=exit_at, result=t.get("exit_reason"))
        else:
            row = next((s for s in (rep.get("shadows") or {}).get("rows") or []
                        if s["symbol"] == sym and s.get("side") != "short"), None)
            if not row or row.get("result") in (None, "no plan", "no data") or \
                    str(row.get("result")).startswith("could not replay"):
                continue
            sig.update(fill=row.get("fill"), exit=row.get("exit"), exit_at=row.get("exit_at"),
                       result=row.get("result"))
        if not sig["at"]:
            continue
        out.append(sig)
    return out
