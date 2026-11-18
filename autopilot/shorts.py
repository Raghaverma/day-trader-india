"""The short side of the opening range, in SHADOW only.

An opening-range breakdown (the first 5-minute close under the range low, nse-scanner orb --side short) is
planned here as the mirror image of nse-exit's long ORB plan, then replayed at the end of the day by
report.py with the paper broker's own rules, mirrored. Nothing in this file places, approves or
paper-fills anything: the skills' order path (nse-budget, nse-risk, ticket.py, the hooks) stays long-only,
so a short can be measured for weeks before anyone decides whether it is worth building for real.

The plan, mirrored from nse-exit (or-mid stop, 2R target):
  entry   the breakdown bar's close, on the order-tick grid
  stop    the opening-range midpoint, rounded UP onto the tick (a short's stop sits above the entry)
  worst   the SL limit above the trigger (nse_common.worst_exit_price(long=False)): where the loss is measured
  target  entry - 2 x (stop - entry), on the grid
  qty     nse_common.size_qty(long=False): the same max-loss-at-the-worst-fill sizing as a long, charges in

Shorts carry risks a long does not: a stock that locks at its upper circuit cannot be bought back, and a
short that is not covered by the close goes to NSE's auction. A shadow cannot show those; the report says so.
"""

from decimal import Decimal

import autopilot_common as ac

nc = ac.nc
MIN_RISK_PCT = Decimal("0.05")     # nse-exit: a stop closer than 0.05% of the price is inside the noise
MIN_RISK_TICKS = 3                 # nse-exit: and at least three spreads, a spread being one tick or more
TARGET_R = Decimal(2)


class ShortPlanError(Exception):
    pass


def grid(x, mode, sym, cfg):
    d = nc.dec(x)
    return nc.round_to_tick(d, nc.order_tick(d, sym, cfg), mode)


def plan(hit, cfg, budget=None):
    """The shadow plan for a short hit {"symbol", "price", "or_high", "or_low", "at"}. Raises ShortPlanError."""
    sym = nc.nse_symbol(hit.get("symbol"))
    try:
        e = grid(hit["price"], "nearest", sym, cfg)
        oh = grid(hit["or_high"], "nearest", sym, cfg)
        ol = grid(hit["or_low"], "nearest", sym, cfg)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ShortPlanError("the hit has no price or opening range")
    if oh <= ol:
        raise ShortPlanError("the opening-range high %s is not above its low %s" % (oh, ol))
    stop = grid((oh + ol) / 2, "up", sym, cfg)
    if stop <= e:
        raise ShortPlanError("the stop %s (the range midpoint) is not above the entry %s" % (stop, e))
    tick = nc.order_tick(e, sym, cfg)
    risk = stop - e
    floor = max(e * MIN_RISK_PCT / 100, MIN_RISK_TICKS * tick)
    if risk < floor:
        raise ShortPlanError("the stop is %s over the entry, closer than %s: too tight to survive the spread"
                             % (risk, nc.round_to_tick(floor, tick, "up")))
    target = grid(e - TARGET_R * risk, "nearest", sym, cfg)
    if target <= 0 or target >= e:
        raise ShortPlanError("the 2R target %s does not fit under the entry %s" % (target, e))
    qty = nc.size_qty(e, stop, sym, cfg, budget=budget, long=False)
    if qty < 1:
        raise ShortPlanError("the rules fit no whole share (max loss %s at the SL limit, charges in)"
                             % nc.inr(cfg.get("max_loss_per_trade_inr") or 0))
    worst = nc.loss_at_stop(qty, e, stop, sym, cfg, long=False)
    at_stop = nc.intraday_charges(qty, stop, e, cfg)            # buy back at the stop, sold at the entry
    per_share = nc.dec(at_stop["total"]) / qty
    return {"side": "short", "entry": float(e), "stop": float(stop), "target": float(target),
            "worst_exit": float(worst["exit_price"]), "quantity": int(qty), "risk_per_share": float(risk),
            "risk_per_share_worst": float(worst["per_share"]), "reward_risk": float((e - target) / risk),
            "flat_by": cfg.get("flat_by") or "15:10", "max_loss": float(worst["total"]),
            "charges_per_share_at_stop": round(float(per_share), 2),
            "charges_r": round(float(per_share / risk), 3)}


def charges_r_of_long(plan_doc):
    """A long plan's round-trip charges at the stop, per share, as a share of its risk (R at the trigger):
    the README's RELIANCE trade is 1.42 / 5.20 = 0.27R. None when the plan lacks either number."""
    ch = plan_doc.get("charges") or {}
    per, risk = ch.get("per_share_at_stop"), plan_doc.get("risk_per_share")
    try:
        per, risk = float(per), float(risk)
    except (TypeError, ValueError):
        return None
    return round(per / risk, 3) if risk > 0 else None
