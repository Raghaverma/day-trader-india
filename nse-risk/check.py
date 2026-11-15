#!/usr/bin/env python3
"""Pre-trade risk check for nse-risk. Standard library only.

Reads the proposed orders (trader/orders.json, Kite-shaped: an entry leg, a
stop leg and a target alert) and checks every one against your rules
(trader/config.json), your account (trader/portfolio.json), prices
(trader/prices.json), what was placed today (trader/state/broker.json) and
NSE's security list (sec_list.csv: series, price band, surveillance). Each
order gets PASS or BLOCK with the reasons. Orders are checked in file order
as if each PASS had filled, so buys that are fine one by one but break a
limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-13 10:05"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --sec-list sec_list.csv.gz      # NSE's security list from a file (tests, offline)
    python3 check.py --no-write                      # check without recording the verdicts

sec_list.csv comes from NSE's archives
(https://nsearchives.nseindia.com/content/equities/sec_list.csv) and is cached
for the day in trader/cache/sec_list-YYYY-MM-DD.csv. If today's copy can't be
fetched, the newest cached copy is used with a warning; with none at all,
entries are blocked (series, band and surveillance can't be checked).

Entries and exits. An ENTRY opens or adds to a position (a BUY, or a SELL
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        no entry leg; variety not regular; exchange not NSE; quantity
                not a whole number; product not MIS; unknown order_type or
                validity; a LIMIT without a price, an SL without a trigger and
                a price; a price off the NSE tick
  asset         F&O contracts (NFO/BFO, ...FUT, ...CE/PE) unless "allow_fno";
                anything that is not an NSE stock symbol
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  security      not in NSE's sec_list; series not in "allowed_series" (BE/BZ
                are trade-for-trade: delivery only, no intraday); a price band
                under "min_circuit_band_pct" ("No Band" F&O stocks pass);
                GSM/ASM surveillance while "block_surveillance"
  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop leg while "require_stop": true; a stop leg that is not
                the opposite side, the same quantity, SL or SL-M, with its
                trigger on the right side of the entry and its limit beyond
                the trigger
  loss          qty x (entry - stop trigger), plus the estimated charges when
                "include_charges", over max_loss_per_trade_inr
  reward:risk   (target - entry) / (entry - stop), after charges, under
                min_reward_risk
  daily loss    today's entries at their max loss (or their real loss, if
                worse) plus this one, over max_daily_loss_inr
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST), or
                the NSE market is closed (weekend, NSE holiday, outside
                09:15-15:30). A year whose holiday list isn't known is a
                warning, not a block.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json under each order's
nc.order_fingerprint, so nse-broker only shows an order as ready if THIS
exact order passed today.
"""

import argparse
import csv
import glob
import gzip
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
FRESH_MINUTES = 15
VARIETIES = {"regular"}
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALIDITIES = {"DAY", "IOC"}
FNO_EXCHANGES = {"NFO", "BFO", "MCX", "CDS", "BCD"}
FNO_RE = re.compile(r"(\d{2}[A-Z]{3}FUT|\d(CE|PE))$")
SURVEILLANCE_RE = re.compile(r"\b(GSM|ASM|ESM)\b|SURVEILLANCE", re.I)
SERIES_SUFFIX_RE = re.compile(r"^(.+)-([A-Z][A-Z0-9])$")
SERIES_WORDS = {
    "BE": "trade-for-trade: every trade settles by delivery, so no intraday",
    "BZ": "trade-for-trade (BZ: a company that missed listing rules): delivery only, no intraday",
    "SM": "the SME platform", "ST": "the SME platform, trade-for-trade",
    "SZ": "the SME platform, trade-for-trade",
    "IV": "an InvIT", "RR": "a REIT", "IT": "an InvIT",
    "E1": "an ETF-like series", "W1": "warrants",
}
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the trigger price", "stale price": "an old price"}
EPS = 1e-9
PAISA = 0.005


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return nc.inr(x)


def px(x):
    return nc.inr(x, keep_zeros=True).replace("₹", "")


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0").rstrip(".")


def _r2(x):
    return round(float(x) + 0.0, 2)


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        return nc.read_json(path)
    except ValueError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def parse_utc(text):
    if not text:
        return None
    try:
        return nc.parse_now(str(text).strip())
    except ValueError:
        return None


def hhmm(value, key):
    """ "09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    try:
        return nc.parse_hhmm(value, key)
    except ValueError as e:
        raise RiskError(str(e))


def charges(qty, buy, sell, cfg):
    if not qty or qty <= 0:
        return 0.0
    return nc.intraday_charges(int(round(qty)), buy, sell, cfg)["total"]


# ---------------------------------------------------------------- NSE security list

def parse_sec_list(text):
    """sec_list.csv text -> {SYMBOL: [{"series", "name", "band", "remarks"}, ...]}."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip().lower() for h in next(reader)]
    except StopIteration:
        raise RiskError("the security list is empty")
    want = {"symbol": None, "series": None, "security name": None, "band": None, "remarks": None}
    for i, h in enumerate(header):
        if h in want:
            want[h] = i
    if want["symbol"] is None or want["series"] is None or want["band"] is None:
        raise RiskError("the security list has no Symbol / Series / Band columns (got %s)" % ", ".join(header))
    rows = {}
    for r in reader:
        if not r or len(r) <= max(v for v in want.values() if v is not None):
            continue

        def col(k):
            i = want[k]
            return r[i].strip() if i is not None and i < len(r) else ""

        sym = col("symbol").upper()
        if not sym:
            continue
        rows.setdefault(sym, []).append({"series": col("series").upper(), "name": col("security name"),
                                         "band": col("band"), "remarks": col("remarks")})
    if not rows:
        raise RiskError("the security list has no rows")
    return rows


def read_sec_list(path):
    """A sec_list.csv or sec_list.csv.gz file -> rows."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        raise RiskError("can't read the security list %s: %s" % (path, e.strerror or e))
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as e:
            raise RiskError("%s is not a readable .gz file: %s" % (path, e))
    return parse_sec_list(raw.decode("utf-8-sig", "replace"))


def fetch_sec_list(timeout=20):
    req = urllib.request.Request(SEC_LIST_URL, headers={"User-Agent": nc.UA, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", "replace")


def load_sec_list(path=None, cache_dir=os.path.join("trader", "cache"), today=None, fetch=fetch_sec_list):
    """(rows or None, info). info: {"source", "date"} and maybe "warning" or "error".

    path   a file (tests, offline use); otherwise today's cache, else NSE's archives
           (cached for the day), else the newest older cache with a warning."""
    if path:
        return read_sec_list(path), {"source": path, "date": None}
    today = today or nc.to_ist(nc.utc_now()).date().isoformat()
    cached = os.path.join(cache_dir, "sec_list-%s.csv" % today)
    if os.path.isfile(cached):
        try:
            return read_sec_list(cached), {"source": cached, "date": today}
        except RiskError:
            pass
    err = None
    try:
        text = fetch()
        rows = parse_sec_list(text)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cached, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        return rows, {"source": SEC_LIST_URL, "date": today}
    except (urllib.error.URLError, OSError, ValueError, RiskError) as e:
        err = getattr(e, "reason", None) or e
    old = sorted(glob.glob(os.path.join(cache_dir, "sec_list-????-??-??.csv")))
    for p in reversed(old):
        try:
            rows = read_sec_list(p)
        except RiskError:
            continue
        day = os.path.basename(p)[9:19]
        return rows, {"source": p, "date": day,
                      "warning": "NSE's security list could not be fetched today (%s): using the copy from %s"
                                 % (err, day)}
    return None, {"source": None, "date": None,
                  "error": "NSE's security list (sec_list.csv) could not be fetched (%s) and there is no cached "
                           "copy" % err}


def sec_lookup(rows, tradingsymbol, allowed):
    """The sec_list row for a Kite tradingsymbol ("RELIANCE", or "ABAN-BE" for series BE), or None."""
    ts = str(tradingsymbol or "").upper()
    if rows is None:
        return None
    if ts in rows:
        cands = rows[ts]
        for r in cands:
            if r["series"] in allowed:
                return dict(r, symbol=ts)
        for r in cands:
            if r["series"] == "EQ":
                return dict(r, symbol=ts)
        return dict(cands[0], symbol=ts)
    m = SERIES_SUFFIX_RE.match(ts)
    if m and m.group(1) in rows:
        for r in rows[m.group(1)]:
            if r["series"] == m.group(2):
                return dict(r, symbol=m.group(1))
    return None


def band_text(band):
    b = str(band or "").strip()
    if b.lower().startswith("no band"):
        return "No Band (an F&O stock: NSE sets a dynamic band)"
    return "%s%% price band" % b if b else "no band listed"


def security_checks(row, sym, c, sec_info):
    """(reasons, warnings, info) for an entry from its sec_list row."""
    reasons, warns, info = [], [], []
    if row is None:
        reasons.append("security: %s is not in NSE's security list (sec_list.csv%s): check the symbol"
                       % (sym, (" of " + sec_info["date"]) if sec_info.get("date") else ""))
        return reasons, warns, info
    allowed = [str(s).upper() for s in (c.get("allowed_series") or ["EQ"])]
    series = row["series"]
    if series not in allowed:
        reasons.append("series: %s trades in series %s (%s); you allow %s"
                       % (sym, series, SERIES_WORDS.get(series, "not a normal equity series"), ", ".join(allowed)))
    band = str(row.get("band") or "").strip()
    min_band = _f(c.get("min_circuit_band_pct"))
    if band.lower().startswith("no band"):
        pass
    else:
        b = _f(band)
        if b is None:
            warns.append("NSE lists %s with band %r: could not read it" % (sym, band))
        elif min_band and b < min_band - EPS:
            reasons.append("band: %s has a %s%% price band, under your %s%% minimum: thin stocks can lock at the "
                           "limit and leave you unable to sell" % (sym, _num(b), _num(min_band)))
    remark = str(row.get("remarks") or "").strip()
    if remark and remark != "-":
        if SURVEILLANCE_RE.search(remark):
            if c.get("block_surveillance", True):
                reasons.append("surveillance: %s is under %s (NSE's surveillance measures: tighter limits, "
                               "higher margins, sometimes trade-for-trade)" % (sym, remark))
            else:
                warns.append("%s is under %s (block_surveillance is off)" % (sym, remark))
        else:
            warns.append("NSE's remark for %s: %s" % (sym, remark))
    info.append("NSE %s: series %s, %s, %s" % (sym, series, band_text(band),
                                              "no surveillance" if remark in ("", "-") else remark))
    return reasons, warns, info


# ---------------------------------------------------------------- the clock

def window_check(now_utc, c, holidays=None, years=None):
    """Reasons an entry can't be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    try:
        st = nc.market_status(now_utc, c, holidays, years)
    except ValueError as e:                 # a malformed special_sessions entry
        raise RiskError("config: %s" % e)
    warns = []
    ist = nc.to_ist(now_utc)
    if not st["holidays_known"]:
        warns.append("NSE's holiday list for %d isn't known here yet (nse-alerts: alerts.py holidays --refresh): "
                     "weekdays are treated as trading days; check nseindia.com" % ist.year)
    if before is None and after is None:
        return [], warns
    stamp = "%s IST" % ist.strftime("%a %Y-%m-%d %H:%M")
    if st["phase"] == "closed-day":
        return ["window: the NSE market is closed today (%s, %s)" % (st["reason"], stamp)], warns
    if not st["open"]:
        return ["window: the NSE market is closed (%s; the session is %s to %s IST)"
                % (stamp, st["opens"], st["closes"])], warns
    sp = nc.special_session(ist.date(), c)
    if sp:
        warns.append("special session today (%s, %s-%s IST): the entry window applies to normal sessions only"
                     % (sp[2], nc.hhmm(sp[0]), nc.hhmm(sp[1])))
        return [], warns
    t = ist.time().replace(second=0, microsecond=0)
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (nc.hhmm(before), nc.hhmm(t))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (nc.hhmm(after), nc.hhmm(t))], warns
    return [], warns


# ---------------------------------------------------------------- account and log

def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = nc.nse_symbol(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol"))
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty", p.get("quantity"))))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """nse-broker logs the kind nse-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    return [r for r in (state or {}).get("orders", [])
            if r.get("status") == "placed" and rec_day(r) == today]


def realised_today(state, today):
    """{SYMBOL: net P&L of today's closed trades} from nse-broker's log (paper-check, fill)."""
    out = {}
    for r in (state or {}).get("orders", []):
        if r.get("status") == "closed" and rec_day(r) == today and _f(r.get("pnl_net")) is not None:
            s = nc.nse_symbol(r.get("symbol"))
            out[s] = out.get(s, 0.0) + _f(r["pnl_net"])
    return out


def load_prices(pdoc):
    """prices.json (nse-alerts) -> ({SYM: price}, {SYM: naive UTC time or None})."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = nc.nse_symbol(s)
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


# ---------------------------------------------------------------- one leg

def leg_format(leg, what, sym, c, warns):
    """Format reasons for one Kite leg."""
    reasons = []
    if not isinstance(leg, dict):
        return ["format: no %s leg" % what]
    v = str(leg.get("variety") or "").lower()
    if v not in VARIETIES:
        reasons.append("format: the %s leg's variety must be \"regular\" (no AMO, cover, iceberg or auction "
                       "orders), got %r" % (what, leg.get("variety")))
    ex = str(leg.get("exchange") or "").upper()
    if ex in FNO_EXCHANGES:
        pass  # the asset check says it
    elif ex != "NSE":
        reasons.append("exchange: the %s leg is on %r; this pack trades the NSE cash market only"
                       % (what, leg.get("exchange")))
    ts = str(leg.get("tradingsymbol") or "").upper()
    m = SERIES_SUFFIX_RE.match(ts)
    if ts != sym and not (m and m.group(1) == sym):
        reasons.append("format: the %s leg's tradingsymbol %r doesn't match the order's symbol %s"
                       % (what, leg.get("tradingsymbol"), sym))
    tt = str(leg.get("transaction_type") or "").upper()
    if tt not in ("BUY", "SELL"):
        reasons.append("format: the %s leg's transaction_type must be BUY or SELL, got %r"
                       % (what, leg.get("transaction_type")))
    q = _f(leg.get("quantity"))
    if q is None or q <= 0 or abs(q - round(q)) > EPS:
        reasons.append("format: the %s leg's quantity must be a whole number of shares, got %r"
                       % (what, leg.get("quantity")))
    elif isinstance(leg.get("quantity"), str):
        warns.append("send the %s leg's quantity as a number (%s), the way Kite's place_order types it"
                     % (what, leg.get("quantity")))
    prod = str(leg.get("product") or "").upper()
    want = str(c.get("product") or "MIS").upper()
    if prod != want:
        words = {"CNC": "delivery", "NRML": "F&O / overnight", "MTF": "margin trading"}.get(prod, "not intraday")
        reasons.append("product: the %s leg is %s (%s); this pack day-trades %s (intraday) only"
                       % (what, prod or "missing", words, want))
    ot = str(leg.get("order_type") or "").upper()
    if ot not in ORDER_TYPES:
        reasons.append("format: unknown order_type %r in the %s leg" % (leg.get("order_type"), what))
    val = str(leg.get("validity") or "DAY").upper()
    if val not in VALIDITIES:
        reasons.append("format: the %s leg's validity must be DAY or IOC, got %r" % (what, leg.get("validity")))
    price, trig = leg.get("price"), leg.get("trigger_price")
    for k, v in (("price", price), ("trigger_price", trig)):
        if v is None:
            continue
        f = _f(v)
        if f is None or f < 0:
            reasons.append("format: the %s leg's %s must be a number, got %r" % (what, k, v))
        elif isinstance(v, str):
            warns.append("send the %s leg's %s as a number (%s), the way Kite's place_order types it"
                         % (what, k, v))
    if ot == "LIMIT" and not (_f(price) or 0) > 0:
        reasons.append("format: a LIMIT %s leg needs a price" % what)
    if ot == "MARKET" and (_f(price) or 0) > 0:
        reasons.append("format: a MARKET %s leg takes no price (it has %s)" % (what, price))
    if ot in ("SL", "SL-M") and not (_f(trig) or 0) > 0:
        reasons.append("format: an %s %s leg needs a trigger_price" % (ot, what))
    if ot == "SL" and not (_f(price) or 0) > 0:
        reasons.append("format: an SL %s leg needs a price (the limit once triggered)" % what)
    for k, v in (("price", price), ("trigger_price", trig)):
        f = _f(v)
        if f and f > 0:
            t = nc.tick_size(f, sym, c)
            if not nc.on_tick(v, t):
                reasons.append("tick: the %s leg's %s %s is not on the %s tick" % (what, k, _num(f), t))
    tag = leg.get("tag")
    if tag is not None and not re.match(r"^[A-Za-z0-9]{1,20}$", str(tag)):
        warns.append("the %s leg's tag %r should be letters and digits, at most 20" % (what, tag))
    return reasons


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          sec=None, sec_info=None, holidays=None, years=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    sec          rows from load_sec_list / read_sec_list; None = not loaded (entries are blocked)
    holidays     {iso: name} and years (default nc.load_holidays(cfg))"""
    c = json.loads(json.dumps(nc.DEFAULT_CONFIG))
    c.update(cfg or {})
    sec_info = sec_info or {}
    prices = {nc.nse_symbol(k): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or nc.utc_now()
    now_ist = nc.to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    if holidays is None:
        holidays, years = nc.load_holidays(c)
    acct, top_warn = account(c, portfolio, prices)
    blocked = {nc.nse_symbol(b) for b in c.get("blocked") or []}
    allowed = [str(s).upper() for s in (c.get("allowed_series") or ["EQ"])]
    with_charges = bool(c.get("include_charges", True))
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    planned = {}
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
            s = nc.nse_symbol(r.get("symbol"))
            planned[s] = planned.get(s, 0.0) + ml
    realised = realised_today(state, today)
    for s, pnl in realised.items():
        extra = -pnl - planned.get(s, 0.0)
        if pnl < 0 and extra > PAISA:
            loss_today += extra
            top_warn.append("%s lost %s today, %s more than its planned max loss: the extra counts in the "
                            "daily loss" % (s, money(-pnl), money(extra)))
    placed_keys = {(nc.nse_symbol(r.get("symbol")), str(r.get("side", "")).lower()) for r in done_today
                   if r.get("kind") != "stop"}
    win_reasons, win_warns = window_check(now_utc, c, holidays, years)
    broker = str(c.get("broker") or "paper").lower()
    if broker in ("kite", "manual"):
        top_warn.append("broker \"%s\": these orders are for your real Zerodha account, real money" % broker)
    top_warn.extend(win_warns)
    if sec is None and sec_info.get("error"):
        top_warn.append(sec_info["error"] + ": entries are blocked until it loads (or pass --sec-list FILE)")
    if sec_info.get("warning"):
        top_warn.append(sec_info["warning"])
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        o = o if isinstance(o, dict) else {}
        e = o.get("entry") if isinstance(o.get("entry"), dict) else None
        sl = o.get("stop") if isinstance(o.get("stop"), dict) else None
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        sym = nc.nse_symbol(o.get("symbol") or (e or {}).get("tradingsymbol"))
        m = SERIES_SUFFIX_RE.match(sym)
        side = str((e or {}).get("transaction_type") or "").lower()
        ot = str((e or {}).get("order_type") or "").upper()
        q = _f((e or {}).get("quantity"))
        value = None

        # format and asset
        if not sym:
            reasons.append("format: no symbol")
        if e is None:
            reasons.append("format: no entry leg (the Kite order to place first)")
        else:
            reasons.extend(leg_format(e, "entry", sym, c, warns))
        ex = str((e or {}).get("exchange") or "").upper()
        ts = str((e or {}).get("tradingsymbol") or sym).upper()
        fno = ex in FNO_EXCHANGES or bool(FNO_RE.search(ts))
        if fno:
            if not c.get("allow_fno"):
                reasons.append("asset: %s looks like an F&O contract%s; F&O is off (\"allow_fno\": false)"
                               % (ts, (" on " + ex) if ex in FNO_EXCHANGES else ""))
            else:
                warns.append("F&O order: futures and options can lose more than you expect")
        elif sym and (sym.startswith("^") or not nc.is_nse_symbol(sym)):
            reasons.append("asset: %r is not an NSE stock symbol; this pack trades NSE cash equity only"
                           % (o.get("symbol") or ts))

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if ot == "LIMIT":
            ref, ref_from = _f(e.get("price")), "limit"
        elif ot == "SL":
            ref, ref_from = _f(e.get("price")), "limit"
        elif ot == "SL-M":
            ref, ref_from = _f(e.get("trigger_price")), "stop price"
        if ref is None or ref <= 0:
            ref = None
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, px(ref), px(plan_entry), px(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and ot == "MARKET":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > EPS and q is not None:
            exiting = q <= held + EPS
        elif side == "buy" and held < -EPS and q is not None:
            exiting = q <= -held + EPS
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to value a %s-share order; add it to trader/prices.json "
                         "(nse-alerts: alerts.py quote %s --write)" % (sym, _num(q), sym))

        # NSE's security list: series, band, surveillance
        sec_row = None
        if sym and not fno and nc.is_nse_symbol(sym):
            if sec is None:
                later.append("security: NSE's security list isn't loaded, so the series, price band and "
                             "surveillance can't be checked")
            else:
                sec_row = sec_lookup(sec, ts, allowed)
                sr, sw, si = security_checks(sec_row, (sec_row or {}).get("symbol") or sym, c, sec_info)
                later.extend(sr)
                if kind == "entry":
                    warns.extend(sw)
                info.extend(si)

        # blocked list and size
        if sym in blocked or (m and m.group(1) in blocked):
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                later.append("size: %s is over your %s max order" % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -PAISA and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (MIS leverage is "
                             "borrowed money, \"allow_margin\": false)" % (money(value), money(cash)))
            elif after_cash < buffer_inr - PAISA and not c.get("allow_margin"):
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _num(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _num(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = (q > held + EPS) if q is not None else held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop leg, loss, reward:risk, daily loss, trades, window
        max_loss = loss_gross = ch_stop = ch_target = risk_ps = rr = stop = target = stop_limit = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop = _f((sl or {}).get("trigger_price"))
            plan_stop = _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            tg = o.get("target")
            target = _f(tg.get("price")) if isinstance(tg, dict) else _f(tg)
            target = target or _f(plan.get("target"))
            if sl is not None:
                sr = leg_format(sl, "stop", sym, c, warns)
                want_side = "SELL" if long else "BUY"
                if str(sl.get("transaction_type") or "").upper() != want_side:
                    sr.append("stop: the stop leg must be a %s for a %s entry" % (want_side, side.upper()))
                sq = _f(sl.get("quantity"))
                if q is not None and sq is not None and abs(sq - q) > EPS:
                    sr.append("stop: the stop leg is %s shares, the entry %s: they must match" % (_num(sq), _num(q)))
                if str(sl.get("order_type") or "").upper() not in ("SL", "SL-M"):
                    sr.append("stop: the stop leg must be an SL or SL-M order, got %r" % sl.get("order_type"))
                if str(sl.get("product") or "").upper() != str((e or {}).get("product") or "").upper():
                    sr.append("stop: the stop leg's product %s differs from the entry's %s"
                              % (sl.get("product"), (e or {}).get("product")))
                stop_limit = _f(sl.get("price")) if str(sl.get("order_type") or "").upper() == "SL" else None
                if stop_limit and leg_stop:
                    if long and stop_limit > leg_stop + EPS:
                        sr.append("stop: the SL limit %s is above its trigger %s; a sell stop's limit goes at or "
                                  "under the trigger" % (px(stop_limit), px(leg_stop)))
                    if not long and stop_limit < leg_stop - EPS:
                        sr.append("stop: the SL limit %s is under its trigger %s; a buy stop's limit goes at or "
                                  "above the trigger" % (px(stop_limit), px(leg_stop)))
                if str(sl.get("order_type") or "").upper() == "SL-M":
                    warns.append("SL-M is a market order once triggered: Kite can reject API market orders "
                                 "without market protection; SL is the safer default")
                later.extend(sr)
                if plan_stop and leg_stop and abs(leg_stop - plan_stop) > PAISA:
                    warns.append("the stop leg's trigger %s differs from the plan's stop %s: the leg is the one "
                                 "the broker uses" % (px(leg_stop), px(plan_stop)))
            elif stop:
                warns.append("the stop %s is in the plan only, not an order: place an SL leg after the fill, "
                             "or set a stop alert (nse-alerts: alerts.py add --from-plan)" % px(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): an SL stop leg "
                                 "from nse-exit and nse-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (px(stop), "below" if long else "above", px(ref), side.upper()))
                else:
                    risk_ps = round(abs(ref - stop), 2)
                    shares = q if q is not None else 0
                    loss_gross = round(shares * risk_ps, 2)
                    ch_stop = charges(shares, ref if long else stop, stop if long else ref, c)
                    max_loss = round(loss_gross + (ch_stop if with_charges else 0), 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + PAISA:
                        if with_charges:
                            later.append("loss: %s shares x %s risk a share = %s at the stop, plus %s charges = "
                                         "%s, over your %s max loss per trade"
                                         % (_num(shares), px(risk_ps), money(loss_gross), money(ch_stop),
                                            money(max_loss), money(cap)))
                        else:
                            later.append("loss: %s shares x %s risk a share = %s at the stop, over your %s max "
                                         "loss per trade" % (_num(shares), px(risk_ps), money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (px(target), "above" if long else "below", px(ref)))
                        else:
                            ch_target = charges(shares, ref if long else target, target if long else ref, c)
                            reward = abs(target - ref)
                            if with_charges and shares:
                                rr = round((shares * reward - ch_target) / (shares * risk_ps + ch_stop), 2)
                            else:
                                rr = round(reward / risk_ps, 2)
                            tt = nc.tick_size(target, sym, c)
                            if not nc.on_tick(target, tt):
                                warns.append("the target %s is not on the %s tick (it is an alert, so it still "
                                             "works)" % (_num(target), tt))
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f%s (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, " after charges" if with_charges else "",
                                                             px(target), px(ref), px(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s%s, target %s: risk %s a share"
                            % (px(ref), REF_WORDS.get(ref_from, ref_from), px(stop),
                               (" (SL limit %s)" % px(stop_limit)) if stop_limit else "",
                               px(target) if target else "none", px(risk_ps)))
                cap = _f(c.get("max_loss_per_trade_inr"))
                if with_charges:
                    loss_txt = "max loss %s with charges (%s + %s%s)" % (
                        money(max_loss), money(loss_gross), money(ch_stop),
                        ("; limit %s" % money(cap)) if cap else "")
                else:
                    loss_txt = "max loss %s%s" % (money(max_loss), (" (limit %s)" % money(cap)) if cap else "")
                info.append("%s, reward:risk %s%s%s"
                            % (loss_txt, ("%.2f" % rr) if rr is not None else "n/a",
                               " after charges" if (with_charges and rr is not None) else "",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side.upper(), sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side.upper(), sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d orders placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if value is not None and q is not None:
                if side == "buy":
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                    acct["shares"][sym] = held + q
                elif side == "sell":
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                    acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": "%s shares" % (_num(q) if q is not None else "?"),
            "value_inr": _r2(value) if value is not None else None,
            "entry_ref": _r2(ref) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "stop_limit": stop_limit, "target": target, "risk_per_share": risk_ps,
            "max_loss": max_loss, "max_loss_gross": loss_gross, "charges_at_stop": ch_stop,
            "charges_at_target": ch_target, "include_charges": with_charges, "reward_risk": rr,
            "series": (sec_row or {}).get("series"), "band": (sec_row or {}).get("band"),
            "remarks": (sec_row or {}).get("remarks"),
            "fingerprint": nc.order_fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "broker": broker,
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": _r2(acct["cash"]),
        "sec_list": {"source": sec_info.get("source"), "date": sec_info.get("date")},
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": _r2(loss_today), "realised": {k: _r2(v) for k, v in realised.items()}},
    }


BROKER_WORDS = {"paper": "paper, no broker", "manual": "manual: you type it into Kite, real money",
                "kite": "Kite MCP, real money"}


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"].upper(), r["symbol"],
                                                r["amount"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    BROKER_WORDS.get(res["broker"], res["broker"])))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. nse-broker still asks you yes or no for each one.")
    lines.append("Not financial advice. A PASS is a rules check, not a recommendation, and nothing is placed "
                 "without your explicit yes.")
    return "\n".join(lines)


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Check proposed NSE orders against your limits (nse-risk).")
    ap.add_argument("--orders", default=os.path.join("trader", "orders.json"))
    ap.add_argument("--config", default=os.path.join("trader", "config.json"))
    ap.add_argument("--portfolio", default=os.path.join("trader", "portfolio.json"),
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default=os.path.join("trader", "prices.json"))
    ap.add_argument("--state", default=os.path.join("trader", "state", "broker.json"),
                    help="what nse-broker placed today")
    ap.add_argument("--sec-list", help="NSE's sec_list.csv (or .csv.gz) from a file instead of the daily download")
    ap.add_argument("--cache-dir", default=os.path.join("trader", "cache"), help="where sec_list is cached")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-13 10:05\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default=os.path.join("trader", "state", "risk.json"),
                    help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        try:
            now = nc.parse_now(a.now)
        except ValueError as e:
            raise RiskError(str(e))
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-13")
        doc = read_json(a.orders, "orders file (run nse-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        try:
            cfg = nc.load_config(a.config)
        except ValueError as e:
            raise RiskError("%s: %s" % (a.config, e))
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        holidays, years = nc.load_holidays(cfg, os.path.dirname(a.state) or ".")
        # the cache is named for the day it was downloaded (the real clock, even when --now is pinned)
        sec, sec_info = load_sec_list(a.sec_list, a.cache_dir, nc.to_ist(nc.utc_now()).date().isoformat())
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, sec, sec_info, holidays, years)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            rec = dict(res)
            rec["checked_at"] = nc.utc_iso()
            rec["orders_file"] = a.orders
            nc.write_json(a.out, rec)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
