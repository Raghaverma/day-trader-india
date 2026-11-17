#!/usr/bin/env python3
"""Exit Planner for nse-exit: sets the stop and the target BEFORE you enter,
so you know when to get out. NSE cash market, intraday (MIS), long side only.
Standard library only. It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # write trader/plan.json
    python3 exit.py plan --symbol RELIANCE --entry 1334 --or-high 1333.40 --or-low 1324.30
    python3 exit.py plan --stop atr:1.5 --offline RELIANCE.json   # ATR needs the session's bars
    python3 exit.py plan --symbol INFY --entry 1150 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the NSE tick for its price, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo .NS). With fewer than
             14 bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round half up to the NSE tick for their price):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Ticks (NSE cash market since 15 Apr 2025): under 250 -> 0.01, 250-1,000 -> 0.05,
1,000-5,000 -> 0.10, 5,000-10,000 -> 0.50, 10,000-20,000 -> 1.00, above -> 5.00.
Every price goes on nse_common.order_tick: the coarsest tick within
tick_band_pct (10%) of the price, because NSE sets a stock's tick from an
earlier close, not today's price (a 999.99 stop goes to 999.90, valid on the
0.05 and the 0.10 grid). "tick_sizes" in the config overrides a symbol.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a stop too tight to survive the spread (closer than 0.05% of the price or
three spreads, where a spread is at least one tick; --spread X gives the one
you see in Kite's market depth), an entry.json that has not triggered (unless
you give --entry), a trigger older than trigger_max_age_minutes (30; its
triggered_at, else its date and time in IST: the price has probably moved, so
give --entry PRICE, or --allow-late to plan from the old price anyway), a
short (this pack plans longs only).

Worst fill: the stop leg is an SL order whose limit sits stop_limit_buffer_pct
under the trigger (nse_common.worst_exit_price). The max loss is measured
there (the way nse-budget sizes and nse-risk checks), and printed next to the
trigger numbers. Reward:risk after charges is the plan's R to the trigger, the
same figure nse-budget and nse-risk use; the worst-fill version is shown too.

Charges: Zerodha's intraday (MIS) equity rates from nse_common.intraday_charges,
on --qty shares (default: nse_common.size_qty, the size nse-budget would give,
with portfolio.json's cash and value next to the config when it exists; at
least one share for the estimate). It shows the charges a share, the
reward:risk after charges and the break-even sell price on the tick grid.
plan.json carries that estimate as "quantity" (0 when your rules fit no whole
share: nse-budget would refuse) and the worst-fill risk as "risk_per_share_worst".

flat_by (default 15:10 IST, or "flat_by" in trader/config.json): out by then,
win or lose. Zerodha squares off MIS at 15:25 and charges Rs 50 + GST an order
for it. On a special session (Muhurat trading, from "special_sessions" in the
config) it keeps the same distance before that session's close.
"min_reward_risk" in the config (default 1.0) only warns here; nse-risk
enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Times are IST. Exit codes: plan 0 = a plan, 1 = refused or an error.
show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULT_FLAT_BY = "15:10"
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the noise
MIN_RISK_SPREADS = 3               # in on the ask, out on the bid, and a tick of wiggle
SQUARE_OFF = datetime.time(15, 25)  # Zerodha's MIS auto square-off (Z-Connect, 26 Dec 2025)
LATE_FLAT = datetime.time(15, 20)
CENT = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."
KITE_NOTE = ("On Kite the stop is an SL sell placed after the entry fills. The target is an alert, not an order "
             "(Kite has no OCO for MIS): when it rings, cancel the stop, then sell.")


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """--now "2026-11-13 10:05" (IST) -> naive UTC."""
    try:
        return nc.parse_now(text)
    except ValueError as e:
        raise PlanError(str(e))


def parse_hhmm(text, what):
    try:
        return nc.parse_hhmm(text, what)
    except ValueError:
        raise PlanError("%s wants an IST time like 15:10, not %r" % (what, text))


def parse_clock(text, what):
    """10:05, 10:05 AM, 2:35 PM -> time (IST)."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*(?:IST)?\s*$", str(text))
    if not m:
        raise PlanError("%s wants an IST time like 10:05, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants an IST time like 10:05, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants an IST time like 10:05, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


# ---------------------------------------------------------------- money math (Decimal, on the NSE tick)

def D(x, what="a number"):
    try:
        if isinstance(x, Decimal):
            return x
        if isinstance(x, float):
            return Decimal(repr(x))
        s = str(x).strip().replace(",", "").replace("₹", "")
        s = re.sub(r"^(?i:rs\.?|inr)\s*", "", s)
        return Decimal(s)
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def tick_of(price, symbol=None, cfg=None):
    """The order tick for this price (nse_common.order_tick: safe near a slab edge)."""
    return nc.order_tick(price, symbol, cfg)


def on_grid(x, mode, symbol=None, cfg=None):
    """x on the NSE order-tick grid for its price. mode: nearest | down | up."""
    return nc.round_to_tick(D(x), tick_of(D(x), symbol, cfg), mode)


def money(x):
    """A price: ₹1,334.00."""
    return nc.inr(D(x), keep_zeros=True)


def money_raw(x):
    """A worked-out price before rounding, up to 4 decimals: ₹1,328.85, ₹1,330.8149."""
    v = D(x).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()
    exp = v.as_tuple().exponent
    n = max(2, -exp if exp < 0 else 0)
    return nc.inr(v, decimals=n, keep_zeros=True)


def tick_text(t):
    """Decimal tick -> "0.10" (the string plan.json carries)."""
    return format(D(t).quantize(CENT, rounding=ROUND_HALF_UP), "f")


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    return format(D(x).normalize(), "f")


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
_NUM = r"(?:₹|rs\.?\s*|inr\s*)?([0-9][0-9,]*\.?[0-9]*)"


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*" + _NUM + r"$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*" + _NUM + r"$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def min_risk(e, tick, spread=None):
    """The closest a stop may sit under the entry: 0.05% of the price, and three
    spreads, where a spread is at least one tick. Returns (floor, spread used)."""
    sp = tick
    if spread is not None:
        s = nc.round_to_tick(D(spread), tick, "up")
        sp = max(tick, s)
    return max(e * MIN_RISK_PCT / 100, MIN_RISK_SPREADS * sp), sp


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, symbol=None, cfg=None, spread=None):
    """The stop, the target and the risk, on the NSE tick grid. Raises PlanError with the reason."""
    if D(entry, "the entry") <= 0:
        raise PlanError("the entry price must be above ₹0")
    e = on_grid(entry, "nearest", symbol, cfg)
    tick = tick_of(e, symbol, cfg)
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = on_grid(or_high, "nearest", symbol, cfg), on_grid(or_low, "nearest", symbol, cfg)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh), money(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (money(oh), money(ol), money_raw(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = D(stop_param), "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (money(e), num(stop_param), money_raw(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR ₹%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    if raw <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % money_raw(raw))
    s_tick = tick_of(raw, symbol, cfg)
    stop = nc.round_to_tick(raw, s_tick, "down")
    if stop != D(raw):
        what = "rounded down to the ₹%s tick" % tick_text(s_tick)
        detail = (detail + ", " + what) if detail else "%s %s" % (money_raw(raw), what)
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop), label, money(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % money(stop))
    risk = e - stop
    floor, sp = min_risk(e, tick, spread)
    if risk < floor:
        floor_t = nc.round_to_tick(floor, tick, "up")
        raise PlanError(
            "the stop is %s under the entry (%s), closer than %s (%s): 0.05%% of the price and at least %d "
            "spreads of %s. Too tight to survive the spread: normal wiggle would stop you out. Widen it."
            % (money(risk), ticks_words(risk, tick), money(floor_t), ticks_words(floor_t, tick), MIN_RISK_SPREADS,
               money(sp)))
    if target_rule == "r":
        raw_t = e + target_param * risk
        t_label = r_label(target_param)
    else:
        raw_t = D(target_param)
        t_label = None
    target = on_grid(raw_t, "nearest", symbol, cfg)
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target), money(e)))
    rr = ((target - e) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tick, "risk_ticks": int(risk / tick),
            "min_risk": nc.round_to_tick(floor, tick, "up")}


def ticks_words(x, tick):
    n = D(x) / D(tick)
    if n == n.to_integral_value():
        n = int(n)
        return "%d tick%s of ₹%s" % (n, "" if n == 1 else "s", tick_text(tick))
    return "%s ticks of ₹%s" % (num(n.quantize(CENT)), tick_text(tick))


# ---------------------------------------------------------------- charges (nse_common.intraday_charges)

def loss_basis(cfg):
    """Where the max loss is measured: "limit" (the SL limit, the default) or "trigger"."""
    return "trigger" if str((cfg or {}).get("loss_basis") or "limit").lower() == "trigger" else "limit"


def account_files(config_path, portfolio_path=None):
    """(cash, portfolio total, note) from the portfolio.json next to the config, holdings valued at the
    prices.json next to it when there is one (nse_common.portfolio_total: what nse-budget and nse-risk
    size against). (None, None, "") when there is no portfolio. An unreadable file is ignored with a note."""
    home = os.path.dirname(config_path or "") or "."
    pf_path = portfolio_path or os.path.join(home, "portfolio.json")
    if not os.path.isfile(pf_path):
        return None, None, ""
    try:
        pf = nc.read_json(pf_path)
        prices = {}
        pp = os.path.join(home, "prices.json")
        if os.path.isfile(pp):
            doc = nc.read_json(pp) or {}
            prices = {nc.nse_symbol(k): v for k, v in (doc.get("prices") or {}).items()}
        total = nc.portfolio_total(pf, prices)
        if total is None:
            raise ValueError("no \"cash\" number")
        return float(pf["cash"]), total, ""
    except (ValueError, TypeError, AttributeError, OSError) as e:
        return None, None, "%s could not be read (%s): the share estimate ignores it." % (pf_path, e)


def estimate_qty(c, symbol, cfg, cash=None, total=None):
    """Whole shares your rules allow for this plan (nse_common.size_qty, the same arithmetic as
    nse-budget and nse-timemachine). May be 0."""
    try:
        return nc.size_qty(c["entry"], c["stop"], symbol, cfg, cash=cash, portfolio_total=total)
    except (TypeError, ValueError, InvalidOperation):
        raise PlanError("the config has a non-number in budget, cash_buffer_pct, max_position_pct, max_order_inr "
                        "or max_loss_per_trade_inr")


def no_size_note(c, symbol, cfg, cash=None, total=None):
    """Why your rules fit no whole share of this plan, for a warning."""
    lim = nc.size_limits(c["entry"], c["stop"], symbol, cfg, cash=cash, portfolio_total=total)
    worst = min(lim, key=lambda l: l["shares"])
    return ("Your rules fit no whole share of this plan (the %s limit allows %d: %s). nse-budget will refuse it: "
            "plan a closer stop, or change the rule." % (worst["name"], worst["shares"], worst["how"]))


def break_even(qty, entry, symbol=None, cfg=None):
    """The lowest sell price on the tick grid that pays back the round trip's charges."""
    e = D(entry)
    q = D(int(qty))

    def net(p):
        return q * (p - e) - nc.dec(nc.intraday_charges(qty, e, p, cfg)["total"])
    est = e + nc.dec(nc.intraday_charges(qty, e, e, cfg)["total"]) / q
    p = on_grid(est, "up", symbol, cfg)
    for _ in range(100000):
        if net(p) >= 0:
            return p
        p = p + tick_of(p, symbol, cfg)
    raise PlanError("could not work out a break-even price")


def charges_for(c, qty, qty_source, symbol=None, cfg=None):
    """What the round trip costs at the stop and at the target, on qty shares, and the loss at the
    worst fill of the stop leg (its SL limit; the trigger with "loss_basis": "trigger")."""
    if qty < 1:
        raise PlanError("--qty must be 1 or more")
    e, s, t = c["entry"], c["stop"], c["target"]
    cs = nc.intraday_charges(qty, e, s, cfg)
    ct = nc.intraday_charges(qty, e, t, cfg)
    q = D(int(qty))
    lose = q * (e - s) + nc.dec(cs["total"])
    win = q * (t - e) - nc.dec(ct["total"])
    rr_net = (win / lose).quantize(CENT, rounding=ROUND_HALF_UP)
    be = break_even(qty, e, symbol, cfg)
    lw = nc.loss_at_stop(qty, e, s, symbol, cfg)
    lose_worst = lw["gross"] + lw["charges"]
    rr_worst = (win / lose_worst).quantize(CENT, rounding=ROUND_HALF_UP) if lose_worst > 0 else rr_net
    return {"quantity": int(qty), "quantity_from": qty_source,
            "at_stop": cs["total"], "at_target": ct["total"],
            "per_share_at_stop": float((nc.dec(cs["total"]) / q).quantize(CENT, rounding=ROUND_HALF_UP)),
            "per_share_at_target": float((nc.dec(ct["total"]) / q).quantize(CENT, rounding=ROUND_HALF_UP)),
            "max_loss": float(q * (e - s)), "max_loss_with_charges": float(lose),
            "worst_exit": float(lw["exit_price"]), "loss_basis": loss_basis(cfg),
            "risk_per_share_worst": float(lw["per_share"]), "max_loss_worst": float(lw["gross"]),
            "charges_at_worst": float(lw["charges"]), "max_loss_worst_with_charges": float(lose_worst),
            "reward_risk_after_charges": float(rr_net), "reward_risk_after_charges_worst": float(rr_worst),
            "break_even": float(be),
            "lines_at_target": {k: v for k, v in ct.items() if k not in ("total", "turnover")},
            "rates": "Zerodha intraday equity (nse_common.intraday_charges); \"charges\" in the config overrides"}


# ---------------------------------------------------------------- bars for ATR (Yahoo .NS)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(sym, rng, interval):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = nc.yahoo_chart_url(sym, rng, interval, host)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None, now_utc=None):
    ysym = nc.yahoo_symbol(sym)
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, nc.nse_symbol(sym) + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (nc.to_ist(now_utc or nc.utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get(sym, rng, interval)
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), ysym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None, cfg=None):
    """Closed, on-grid bars of one NSE session (09:15-15:30 IST), oldest first.
    The live tick and bars not closed by now are dropped."""
    step = INTERVALS[interval]
    o, c = nc.session_times(day, cfg)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = nc.to_ist(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        ist = nc.ist_from_epoch(b["t"])
        if ist.date() != day or not (open_dt <= ist < close_dt):
            continue
        if local and ist + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((ist - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    try:
        cfg = nc.load_config(path)
    except ValueError as e:
        raise PlanError("%s is not a usable config: %s" % (path, e))
    if not cfg.get("flat_by"):
        cfg["flat_by"] = DEFAULT_FLAT_BY
    return cfg


def state_dir_for(config_path):
    return os.path.join(os.path.dirname(config_path or "") or ".", "state")


def resolve_flat_by(day, explicit, cfg, entry_time, holidays=None):
    """(HH:MM, note). The NSE normal market closes at 15:30 IST. On a special
    session (config "special_sessions") the config's flat-by keeps the same
    distance before that session's close."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULT_FLAT_BY, "--flat-by" if explicit else "flat_by")
    note = ""
    where = "--flat-by %s" % hm(t) if explicit else "flat_by %s in the config" % hm(t)
    if day and nc.trading_day(day, holidays, cfg)[0]:
        o, c = nc.session_times(day, cfg)
        sp = nc.special_session(day, cfg)
        at = datetime.datetime.combine(day, t)
        open_dt, close_dt = datetime.datetime.combine(day, o), datetime.datetime.combine(day, c)
        if not open_dt < at < close_dt:
            if sp and not explicit:
                moved = close_dt - (datetime.datetime.combine(day, nc.SESSION_CLOSE) - at)
                if not open_dt < moved < close_dt:
                    raise PlanError("flat_by %s does not fit the %s (%s-%s IST) on %s: give --flat-by"
                                    % (hm(t), sp[2], hm(o), hm(c), day))
                t = moved.time()
                note = "moved into the %s (%s-%s IST), the same distance before its close" % (sp[2], hm(o), hm(c))
            elif at >= close_dt:
                raise PlanError("%s is at or after the %s IST close on %s" % (where, hm(c), day))
            else:
                raise PlanError("%s is at or before the %s IST open on %s" % (where, hm(o), day))
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (hm(entry_time), hm(t)))
    return hm(t), note


def age_words(minutes):
    m = float(minutes)
    if m < 120:
        return "%d minute%s" % (int(m), "" if int(m) == 1 else "s")
    if m < 48 * 60:
        return "%.1f hours" % (m / 60.0)
    return "%d days" % int(m / 1440)


def trigger_time(doc):
    """When entry.json's trigger happened, naive UTC: its triggered_at (UTC, the trigger bar's close), else its
    date and time read as IST. None when neither is there or readable."""
    when = doc.get("triggered_at")
    if when:
        try:
            return nc.parse_now(str(when))
        except ValueError:
            pass
    if doc.get("date") and doc.get("time"):
        try:
            day = datetime.date.fromisoformat(str(doc["date"]))
            return nc.to_utc(datetime.datetime.combine(day, parse_clock(doc["time"], "the entry time")))
        except (ValueError, PlanError):
            pass
    return None


def check_trigger_age(doc, path, now_utc, cfg, allow_late):
    """A trigger older than trigger_max_age_minutes is not planned from: the price has moved since, and a
    plan built on the old price sizes and stops the wrong trade. Returns a note when --allow-late let an old
    one through (else ""); raises PlanError when it is too old, or cannot be dated, and nothing overrides it."""
    v = cfg.get("trigger_max_age_minutes")
    limit = 30.0 if v is None else float(v)
    sym = doc.get("symbol") or "the stock"
    when = trigger_time(doc)
    how = ("Give --entry PRICE to plan from the price now, or --allow-late to plan from the old trigger price "
           "anyway (the price has probably moved).")
    if when is None:
        if allow_late:
            return "%s does not say when %s triggered, so its age is unknown: planned from it because of --allow-late." % (path, sym)
        raise PlanError("%s does not say when %s triggered (no triggered_at, and no date and time), so it may be "
                        "old. %s" % (path, sym, how))
    age = (now_utc - when).total_seconds() / 60.0
    if age <= limit:
        return ""
    ist = nc.to_ist(when)
    text = ("the %s trigger is %s old (it fired at %s IST on %s; the limit is %s minutes, \"trigger_max_age_minutes\")"
            % (sym, age_words(age), ist.strftime("%H:%M"), ist.date().isoformat(), "%g" % limit))
    if allow_late:
        return "Planned from a trigger that is %s old (fired %s IST on %s) because of --allow-late: the price has probably moved." % (
            age_words(age), ist.strftime("%H:%M"), ist.date().isoformat())
    raise PlanError("%s: %s. %s" % (path, text, how))


def gather(a, now_utc=None, cfg=None):
    """The entry facts, from trader/entry.json or the flags. Returns (info, note); the note is a warning to
    print ("" when there is none)."""
    note = ""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("nse-exit plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": nc.nse_symbol(a.symbol), "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or nc.tpath("entry.json")
        doc = read_json(path, "entry (run /nse-entry first, or give --symbol and --entry)")
        if not isinstance(doc, dict):
            raise PlanError("%s is not an entry" % path)
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: nse-exit plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        if a.entry is None:         # the trigger's price is the entry: it must be recent. --entry PRICE is the user's own.
            note = check_trigger_age(doc, path, now_utc or nc.utc_now(), cfg or {}, a.allow_late)
        orr = doc.get("opening_range") or {}
        info = {"symbol": nc.nse_symbol(doc.get("symbol") or ""),
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if not nc.is_nse_symbol(info["symbol"]):
        raise PlanError("%r is not an NSE symbol (bare, like RELIANCE or M&M)" % info["symbol"])
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info, note


def build_plan(a, now_utc):
    cfg = read_config(a.config)
    info, late_note = gather(a, now_utc, cfg)
    holidays, years = nc.load_holidays(cfg, state_dir_for(a.config))
    sym = info["symbol"]
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = nc.to_ist(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(sym, info["interval"], day, a.offline, now_utc)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None, cfg)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], sym, day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, sym, cfg, a.spread)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time, holidays)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(on_grid(info["or_high"], "nearest", sym, cfg)),
               "low": float(on_grid(info["or_low"], "nearest", sym, cfg)),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "nse-exit", "symbol": sym, "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "created_at": nc.utc_iso(now_utc), "note": NOTE,
            "exchange": "NSE", "tick": tick_text(c["tick"])}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    cash, total, acct_note = account_files(a.config, a.portfolio)
    if a.qty:
        est, qsrc = a.qty, "--qty"
    else:
        est, qsrc = estimate_qty(c, sym, cfg, cash, total), "config caps"
    charges = charges_for(c, max(1, est), qsrc, sym, cfg)
    worst_exit = nc.worst_exit_price(c["stop"], sym, cfg)
    plan["quantity"] = int(est)
    plan["quantity_from"] = qsrc
    plan["risk_per_share_worst"] = float(c["entry"] - worst_exit)
    plan["worst_exit"] = float(worst_exit)
    warnings = []
    if late_note:
        warnings.append(late_note)
    if acct_note:
        warnings.append(acct_note)
    if est < 1 and not a.qty:
        warnings.append(no_size_note(c, sym, cfg, cash, total))
    if abs(float(D(info["entry"])) - float(c["entry"])) > 1e-6:
        warnings.append("The entry %s is off the ₹%s tick: planned at %s." % (
            money_raw(info["entry"]), tick_text(c["tick"]), money(c["entry"])))
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    if parse_hhmm(flat_by, "flat_by") > LATE_FLAT and not nc.special_session(day, cfg):
        warnings.append("Flat-by %s leaves little room before Zerodha's %s MIS auto square-off (₹50 + 18%% GST "
                        "an order). Other brokers differ: check yours." % (flat_by, hm(SQUARE_OFF)))
    ok, why = nc.trading_day(day, holidays, cfg)
    if not ok:
        warnings.append("NSE is closed on %s (%s): there is no session to trade this plan in." % (day.isoformat(), why))
    elif not nc.holidays_known(day, years):
        warnings.append("NSE's %d holiday list is not known here yet (NSE publishes it around December; run "
                        "alerts.py holidays --refresh). %s is treated as a trading day." % (day.year, day.isoformat()))
    mrr = cfg.get("min_reward_risk")
    use_net = bool(cfg.get("include_charges", True))
    rr_check = D(charges["reward_risk_after_charges"]) if use_net else c["reward_risk"]
    try:
        if mrr is not None and rr_check < D(mrr):
            warnings.append("Reward:risk %s%s is under your min_reward_risk %s in the config: nse-risk checks that."
                            % (ratio(rr_check), " after charges" if use_net else "", num(D(mrr))))
    except PlanError:
        pass
    today = nc.to_ist(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s IST)." % (day.isoformat(), today.isoformat()))
    return plan, warnings, charges


def describe(plan, charges=None):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    tick = D(plan.get("tick") or tick_of(e))
    when = ""
    if plan.get("entry_time"):
        when = " at %s IST" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long on NSE, %s, %s on %s bars (tick ₹%s)" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute",
        tick_text(tick))]
    out.append("  Entry         %s%s" % (money(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry, %s)" % (money(plan["risk_per_share"]), pct,
                                                              ticks_words(risk, tick)))
    rr = "  Reward:risk   %s" % ratio(plan["reward_risk"])
    if charges:
        rr += " (%s after charges)" % ratio(charges["reward_risk_after_charges"])
    out.append(rr)
    if charges:
        n = charges["quantity"]
        if charges["loss_basis"] == "trigger":
            out.append("  Worst fill    the stop trigger %s (\"loss_basis\": \"trigger\": a gap below it can lose more): "
                       "%s on %d share%s with charges" % (money(charges["worst_exit"]),
                                                          money(charges["max_loss_worst_with_charges"]), n,
                                                          "" if n == 1 else "s"))
        else:
            out.append("  Worst fill    the SL limit %s, %s a share under the entry: %s on %d share%s with charges "
                       "(reward:risk %s after charges if it fills there)" % (
                           money(charges["worst_exit"]), money(charges["risk_per_share_worst"]),
                           money(charges["max_loss_worst_with_charges"]), n, "" if n == 1 else "s",
                           ratio(charges["reward_risk_after_charges_worst"])))
        out.append("  Charges       about %s a share at the target, %s at the stop (%s and %s a round trip on %d "
                   "share%s, Zerodha intraday rates)" % (
                       money(charges["per_share_at_target"]), money(charges["per_share_at_stop"]),
                       money(charges["at_target"]), money(charges["at_stop"]), n, "" if n == 1 else "s"))
        out.append("  Break-even    %s: the sell price that pays back the charges on %d share%s%s" % (
            money(charges["break_even"]), n, "" if n == 1 else "s",
            " (sized from your config's caps; nse-budget sets the real size)" if charges["quantity_from"] == "config caps" else ""))
    out.append("  Flat by       %s IST, win or lose: no overnight hold (Zerodha squares off MIS at %s for ₹50 + GST)"
               % (plan["flat_by"], hm(SQUARE_OFF)))
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(KITE_NOTE)
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings, charges = build_plan(a, now)
    if a.write:
        nc.write_json(a.write, plan)
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings, charges=charges), indent=2, ensure_ascii=False))
        return 0
    for line in describe(plan, charges):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /nse-timemachine replays it, then /nse-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    if not isinstance(plan, dict):
        raise PlanError("%s is not a plan: make a new one with exit.py plan --write" % a.plan)
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    cfg = read_config(a.config)
    full = dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)
    c = {"entry": D(plan["entry"]), "stop": D(plan["stop"]), "target": D(plan["target"])}
    risk = c["entry"] - c["stop"]
    charges = None
    if risk > 0 and c["target"] > c["entry"]:
        pq = plan.get("quantity")
        if a.qty:
            qty, qsrc = a.qty, "--qty"
        elif isinstance(pq, int) and not isinstance(pq, bool) and pq >= 1:
            qty, qsrc = pq, plan.get("quantity_from") or "config caps"
        else:
            cash, total, _ = account_files(a.config, a.portfolio)
            qty, qsrc = max(1, estimate_qty(c, plan["symbol"], cfg, cash, total)), "config caps"
        charges = charges_for(c, qty, qsrc, plan["symbol"], cfg)
    for line in describe(full, charges):
        print(line)
    today = nc.to_ist(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s IST)." % (plan["date"], today))
    return 0


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Exit Planner (nse-exit): the stop and target before the entry, NSE.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH",
                   help="entry.json to plan from (default: entry.json in the trader folder)")
    p.add_argument("--symbol", help="NSE symbol, e.g. RELIANCE")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today IST)")
    p.add_argument("--time", help="entry time IST, e.g. 10:05")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:10)")
    p.add_argument("--spread", type=float, help="the bid-ask spread in rupees (Kite market depth); default one tick")
    p.add_argument("--qty", type=int, help="shares for the charges estimate (default: what your rules allow, "
                                           "nse_common.size_qty)")
    p.add_argument("--allow-late", action="store_true",
                   help="plan from an entry.json trigger older than trigger_max_age_minutes (the price has probably moved)")
    p.add_argument("--portfolio", help="portfolio.json for the share estimate (default: the one next to the config)")
    p.add_argument("--config", default=nc.tpath("config.json"), help="your rules (default trader/config.json)")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-13 10:05"')
    p.add_argument("--write", nargs="?", const=nc.tpath("plan.json"), metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default=nc.tpath("plan.json"))
    p.add_argument("--config", default=nc.tpath("config.json"))
    p.add_argument("--portfolio", help="portfolio.json for the share estimate (default: the one next to the config)")
    p.add_argument("--qty", type=int)
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
