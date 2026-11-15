"""Shared NSE helpers for the day-trader-india skills. Standard library only.

This file is copied byte for byte into every skill folder, so each folder still
works on its own when copied into ~/.claude/skills. tests/test_repo.py checks
that every copy matches. Change it in one place, then copy it everywhere:

    for d in nse-*/; do cp nse-scanner/nse_common.py "$d"; done

What lives here (and nowhere else):
  clock      IST is UTC+05:30 all year (no daylight saving). The NSE capital
             market trades 09:15-15:30 IST; pre-open runs 09:00-09:15 (orders
             09:00-09:08). Times passed with --now are IST wall-clock times.
  holidays   NSE capital-market trading holidays for 2026, as published by NSE
             (holiday-master, segment CM). Other years come from the cache that
             nse-alerts writes (`alerts.py holidays --refresh`) or from
             "extra_holidays" in trader/config.json. A year with no list is
             reported, never guessed.
  ticks      NSE cash-market tick sizes by price slab (NSE circular, effective
             15 April 2025, reviewed monthly on the previous month's close).
  money      Rupees with Indian digit grouping: 123456.5 -> "₹1,23,456.50".
  charges    Intraday (MIS) equity charges, Zerodha's published rates by default:
             brokerage 0.03% or ₹20 per executed order (lower), STT 0.025% on
             the sell side, NSE transaction 0.00307%, SEBI ₹10/crore, IPFT
             ₹0.01/crore, stamp duty 0.003% on the buy side, GST 18% on
             brokerage + transaction + SEBI + IPFT. Override in "charges".
  orders     The Kite Connect place_order fields and the order fingerprint that
             nse-risk records and nse-broker checks.
  config     trader/config.json with defaults.
"""

import datetime
import decimal
import hashlib
import json
import os
import re
import urllib.parse

COMMON_VERSION = "1.0.0"

UA = "Mozilla/5.0 (compatible)"

# ---------------------------------------------------------------- clock

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
PREOPEN_OPEN = datetime.time(9, 0)
PREOPEN_ORDERS_CLOSE = datetime.time(9, 8)
SESSION_OPEN = datetime.time(9, 15)
SESSION_CLOSE = datetime.time(15, 30)

# NSE capital market (CM) trading holidays, 2026, from NSE's holiday-master.
# Weekend dates are listed by NSE too and kept here as published.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
HOLIDAY_YEARS = (2026,)
HOLIDAY_CACHE = "nse-holidays.json"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def utc_now():
    """The real clock, naive UTC, whole seconds."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def to_ist(utc):
    """Naive UTC -> naive IST wall clock."""
    return utc + IST_OFFSET


def to_utc(ist):
    """Naive IST wall clock -> naive UTC."""
    return ist - IST_OFFSET


def ist_from_epoch(ts):
    """Unix seconds (Yahoo timestamps) -> naive IST."""
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)) + IST_OFFSET


_NOW_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*(Z|[+-]\d{2}:?\d{2}|IST)?$")


def parse_now(text):
    """--now "2026-11-13 10:05" (IST wall clock) -> naive UTC. None/"" -> the real clock.

    Also takes a date alone (09:15 IST that day), a trailing Z (UTC) or an
    explicit offset like +05:30.
    """
    if text is None or str(text).strip() == "":
        return utc_now()
    s = str(text).strip()
    if re.match(r"^\d{1,2}:\d{2}$", s):
        # "10:05" alone: that time today, IST
        t = parse_hhmm(s, "--now")
        return to_utc(datetime.datetime.combine(to_ist(utc_now()).date(), t))
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        d = datetime.date.fromisoformat(s)
        return to_utc(datetime.datetime.combine(d, SESSION_OPEN))
    m = _NOW_RE.match(s)
    if not m:
        raise ValueError('--now wants "YYYY-MM-DD HH:MM" (IST), got %r' % text)
    d = datetime.date.fromisoformat(m.group(1))
    h, mi, se = int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)
    if h > 23 or mi > 59 or se > 59:
        raise ValueError("--now has an impossible time: %r" % text)
    wall = datetime.datetime.combine(d, datetime.time(h, mi, se))
    tz = m.group(5)
    if tz is None or tz == "IST":
        return to_utc(wall)
    if tz == "Z":
        return wall
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    off = datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return wall - sign * off


def parse_hhmm(text, what="time"):
    """ "15:10" -> datetime.time(15, 10). Raises ValueError naming `what`."""
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(text or "").strip())
    if not m:
        raise ValueError("%s wants HH:MM (24-hour, IST), got %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hhmm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def utc_iso(utc=None):
    return (utc or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_text(utc):
    """Naive UTC -> "2026-11-13 10:05 IST"."""
    return to_ist(utc).strftime("%Y-%m-%d %H:%M IST")


def parse_holiday_master(doc, segment="CM"):
    """NSE /api/holiday-master?type=trading JSON -> ({iso_date: name}, sorted years)."""
    out = {}
    for row in (doc or {}).get(segment) or []:
        try:
            d = datetime.datetime.strptime(str(row.get("tradingDate", "")).strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        name = re.sub(r"\s*\*+\s*$", "", str(row.get("description") or "").strip())
        out[d.isoformat()] = name or "NSE holiday"
    years = sorted({int(k[:4]) for k in out})
    return out, years


def load_holidays(cfg=None, state_dir=os.path.join("trader", "state")):
    """Built-in 2026 list + nse-alerts' cache + config "extra_holidays".

    Returns (holidays {iso: name}, known_years set). A year counts as known only
    when NSE's full list for it is loaded (built in, or cached by `alerts.py
    holidays --refresh`), or when config "holiday_years" names it; a single
    extra_holidays date does not make its year known.
    """
    hol = dict(NSE_HOLIDAYS)
    years = set(HOLIDAY_YEARS)
    path = os.path.join(state_dir, HOLIDAY_CACHE) if state_dir else None
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cache = json.load(fh)
            hol.update({str(k): str(v) for k, v in (cache.get("holidays") or {}).items()})
            years.update(int(y) for y in cache.get("years") or [])
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    extra = (cfg or {}).get("extra_holidays") or {}
    if isinstance(extra, dict):
        hol.update({str(k): str(v) for k, v in extra.items()})
    for y in (cfg or {}).get("holiday_years") or []:
        try:
            years.add(int(y))
        except (TypeError, ValueError):
            pass
    return hol, years


def holidays_known(d, years):
    return d.year in set(years)


def special_session(d, cfg=None):
    """A special session on date d from config, e.g. Muhurat trading:
    "special_sessions": {"2026-11-08": {"open": "18:15", "close": "19:15", "label": "Muhurat trading"}}
    Returns (open time, close time, label) or None.
    """
    sp = ((cfg or {}).get("special_sessions") or {}).get(d.isoformat())
    if not sp:
        return None
    return (parse_hhmm(sp.get("open"), "special_sessions open"),
            parse_hhmm(sp.get("close"), "special_sessions close"),
            str(sp.get("label") or "special session"))


def trading_day(d, holidays=None, cfg=None):
    """(is_trading_day, reason). Weekends and NSE holidays are closed; a configured
    special session (Muhurat trading) opens a day that is otherwise closed."""
    if special_session(d, cfg):
        return True, special_session(d, cfg)[2]
    hol = NSE_HOLIDAYS if holidays is None else holidays
    if d.weekday() >= 5:
        return False, WEEKDAYS[d.weekday()]
    name = hol.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session_times(d, cfg=None):
    """(open, close) IST times for a trading day: 09:15-15:30, or a special session."""
    sp = special_session(d, cfg)
    if sp:
        return sp[0], sp[1]
    return SESSION_OPEN, SESSION_CLOSE


def next_trading_day(d, holidays=None, cfg=None, include_today=False):
    day = d if include_today else d + datetime.timedelta(days=1)
    for _ in range(400):
        if trading_day(day, holidays, cfg)[0]:
            return day
        day += datetime.timedelta(days=1)
    raise ValueError("no NSE trading day within 400 days of %s" % d.isoformat())


def market_status(utc, cfg=None, holidays=None, years=None):
    """Where the NSE session stands at naive-UTC `utc`.

    phase: closed-day | before-open | pre-open | open | after-close
    """
    if holidays is None:
        holidays, years = load_holidays(cfg)
    ist = to_ist(utc)
    d = ist.date()
    ok, reason = trading_day(d, holidays, cfg)
    o, c = session_times(d, cfg)
    st = {"now_ist": ist.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[d.weekday()], "date": d.isoformat(),
          "open": False, "phase": "", "reason": reason, "opens": hhmm(o), "closes": hhmm(c),
          "holidays_known": holidays_known(d, years if years is not None else HOLIDAY_YEARS)}
    t = ist.time()
    if not ok:
        st["phase"] = "closed-day"
    elif t < PREOPEN_OPEN and (o, c) == (SESSION_OPEN, SESSION_CLOSE):
        st["phase"] = "before-open"
    elif t < o:
        st["phase"] = "pre-open" if (o, c) == (SESSION_OPEN, SESSION_CLOSE) else "before-open"
    elif t < c:
        st["phase"], st["open"] = "open", True
    else:
        st["phase"] = "after-close"
    if st["phase"] in ("closed-day", "after-close"):
        nd = next_trading_day(d, holidays, cfg)
    else:
        nd = d
    no, _ = session_times(nd, cfg)
    st["next_open"] = "%s %s" % (nd.isoformat(), hhmm(no))
    return st


# ---------------------------------------------------------------- money

D = decimal.Decimal


def dec(x):
    """float/int/str -> Decimal without binary noise (1334.0 -> Decimal('1334.0'))."""
    if isinstance(x, decimal.Decimal):
        return x
    return D(repr(x)) if isinstance(x, float) else D(str(x))


def _group_indian(digits):
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def inr(x, decimals=2, keep_zeros=False):
    """Rupees, Indian grouping: 123456.5 -> "₹1,23,456.50"; 56000 -> "₹56,000";
    keep_zeros=True keeps "₹56,000.00". Negative -> "-₹500"."""
    v = dec(x).quantize(D(1).scaleb(-decimals), rounding=decimal.ROUND_HALF_UP) if decimals else \
        dec(x).quantize(D(1), rounding=decimal.ROUND_HALF_UP)
    neg = v < 0
    ip, _, fp = format(abs(v), "f").partition(".")
    s = _group_indian(ip)
    if decimals and (keep_zeros or fp.strip("0")):
        s += "." + fp
    return ("-" if neg else "") + "₹" + s


def crore(x, decimals=None):
    """Rupees -> "₹3,32,861 cr" (1 crore = 1,00,00,000). With decimals=None,
    whole crores from ₹100 cr up and two decimals below: "₹7.26 cr"."""
    v = dec(x) / D(10000000)
    if decimals is None:
        decimals = 0 if abs(v) >= 100 else 2
    return inr(v, decimals=decimals, keep_zeros=bool(decimals)) + " cr"


def lakh(x, decimals=2):
    v = dec(x) / D(100000)
    return inr(v, decimals=decimals) + " lakh"


# ---------------------------------------------------------------- ticks

# (price below this, tick). NSE CM tick sizes from 15 April 2025; ETFs excluded.
# A price exactly on a slab edge takes the coarser tick, which is a multiple of
# the finer one, so a rounded price is valid either way.
TICK_SLABS = ((D("250"), D("0.01")), (D("1000"), D("0.05")), (D("5000"), D("0.10")),
              (D("10000"), D("0.50")), (D("20000"), D("1.00")), (None, D("5.00")))


def tick_size(price, symbol=None, cfg=None):
    """NSE cash-market tick for a price, or a per-symbol override from config
    "tick_sizes": {"RELIANCE": 0.1}. NSE resets each stock's tick monthly from
    the previous month's close, so near a slab edge check your broker."""
    over = ((cfg or {}).get("tick_sizes") or {}).get((symbol or "").upper())
    if over:
        return dec(over)
    p = dec(price)
    for edge, tick in TICK_SLABS:
        if edge is None or p < edge:
            return tick
    return TICK_SLABS[-1][1]


def round_to_tick(price, tick, mode="nearest"):
    """Decimal price on the tick grid. mode: nearest | down | up."""
    # quantize first so float noise (1324.2999999999998) can't cost a tick
    p = dec(price).quantize(D("0.000001"), rounding=decimal.ROUND_HALF_UP)
    t = dec(tick)
    rounding = {"nearest": decimal.ROUND_HALF_UP, "down": decimal.ROUND_FLOOR, "up": decimal.ROUND_CEILING}[mode]
    n = (p / t).quantize(D(1), rounding=rounding)
    return (n * t).quantize(t)


def on_tick(price, tick):
    return dec(price) % dec(tick) == 0


# ---------------------------------------------------------------- symbols

NSE_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def nse_symbol(text):
    """ "nse:reliance", "RELIANCE.NS", " reliance " -> "RELIANCE". Indices ("^NSEI") pass through."""
    s = str(text or "").strip().upper()
    if s.startswith("^"):
        return s
    if s.startswith("NSE:"):
        s = s[4:]
    if s.endswith(".NS"):
        s = s[:-3]
    return s


def is_nse_symbol(text):
    return bool(NSE_SYMBOL_RE.match(str(text or "")))


def yahoo_symbol(sym):
    s = nse_symbol(sym)
    return s if s.startswith("^") else s + ".NS"


def yahoo_chart_url(sym, rng="1d", interval="5m", host="query2"):
    """M&M -> .../v8/finance/chart/M%26M.NS?range=1d&interval=5m

    Careful: meta.chartPreviousClose is the close before the WHOLE range, so with
    range=5d it is five sessions back, not yesterday. For yesterday's close use
    the second-to-last daily bar."""
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
        host, urllib.parse.quote(yahoo_symbol(sym), safe=""), rng, interval)


def yahoo_spark_url(symbols, rng="3mo", interval="1d", host="query1"):
    """Up to 20 symbols a request (Yahoo answers 400 above that)."""
    return "https://%s.finance.yahoo.com/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        host, ",".join(urllib.parse.quote(yahoo_symbol(s), safe="") for s in symbols), rng, interval)


def nse_headers(extra=None):
    """Headers NSE's www.nseindia.com/api endpoints accept from a script."""
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip",
         "Referer": "https://www.nseindia.com/"}
    h.update(extra or {})
    return h


# ---------------------------------------------------------------- charges

DEFAULT_CHARGES = {
    "brokerage_pct": 0.03,       # % of order value per executed order ...
    "brokerage_cap": 20.0,       # ... or this many rupees, whichever is lower
    "stt_sell_pct": 0.025,       # intraday equity STT, sell side only
    "exchange_pct": 0.00307,     # NSE transaction charge, both sides
    "sebi_per_crore": 10.0,      # SEBI turnover fee
    "ipft_per_crore": 0.01,      # NSE investor protection fund trust
    "stamp_buy_pct": 0.003,      # stamp duty, buy side only (intraday)
    "gst_pct": 18.0,             # on brokerage + exchange + SEBI + IPFT
}


def charge_rates(cfg=None):
    rates = dict(DEFAULT_CHARGES)
    rates.update({k: float(v) for k, v in ((cfg or {}).get("charges") or {}).items() if k in DEFAULT_CHARGES})
    return rates


def _r2(x):
    return float(dec(x).quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP))


def intraday_charges(qty, buy_price, sell_price, cfg=None):
    """Estimated charges for one round trip (buy qty at buy_price, sell at sell_price),
    intraday MIS equity on NSE. Rupees, each line rounded to the paisa."""
    r = charge_rates(cfg)
    q = D(int(qty))
    buy_v = q * dec(buy_price)
    sell_v = q * dec(sell_price)
    turnover = buy_v + sell_v
    pct = lambda p: D(repr(float(p))) / D(100)
    cap = D(repr(float(r["brokerage_cap"])))
    brokerage = min(cap, buy_v * pct(r["brokerage_pct"])) + min(cap, sell_v * pct(r["brokerage_pct"]))
    if qty <= 0:
        brokerage = D(0)
    stt = sell_v * pct(r["stt_sell_pct"])
    exchange = turnover * pct(r["exchange_pct"])
    sebi = turnover * D(repr(float(r["sebi_per_crore"]))) / D(10000000)
    ipft = turnover * D(repr(float(r["ipft_per_crore"]))) / D(10000000)
    stamp = buy_v * pct(r["stamp_buy_pct"])
    gst = (brokerage + exchange + sebi + ipft) * pct(r["gst_pct"])
    lines = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "ipft": ipft,
             "stamp": stamp, "gst": gst}
    out = {k: _r2(v) for k, v in lines.items()}
    out["total"] = _r2(sum(lines.values()))
    out["turnover"] = _r2(turnover)
    return out


# ---------------------------------------------------------------- orders

# Kite Connect place_order arguments (what the Kite MCP server's place_order takes).
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "tag")
ORDER_TAG = "dtindia"


def _core(leg):
    if not isinstance(leg, dict):
        return None
    out = {}
    for k in KITE_FIELDS:
        v = leg.get(k)
        if v is None or k == "tag":
            continue
        if k == "quantity":
            try:
                f = float(v)
                v = int(f) if f == int(f) else f
            except (TypeError, ValueError):
                pass
        if k in ("price", "trigger_price"):
            v = format(dec(v).normalize(), "f")
        out[k] = v
    return out


def _num_text(v):
    try:
        return format(dec(v).normalize(), "f")
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None


def order_target(order):
    """The target price of an order: {"target": {"price": X}}, a bare number, or plan.target."""
    t = order.get("target")
    if isinstance(t, dict):
        t = t.get("price")
    if t is None:
        t = (order.get("plan") or {}).get("target")
    return t


def order_fingerprint(order):
    """The exact order nse-risk checked: its entry leg, stop leg (Kite fields), target
    price and flat-by time. Changing any of them after the check (a further target
    worsens reward:risk) changes the fingerprint, so nse-broker only shows an order
    whose fingerprint has a PASS from today."""
    t = order_target(order)
    core = {"entry": _core(order.get("entry")), "stop": _core(order.get("stop")),
            "target": _num_text(t) if t is not None else None,
            "flat_by": order.get("flat_by") or (order.get("plan") or {}).get("flat_by")}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- files and config

DEFAULT_CONFIG = {
    "budget": 100000,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "include_charges": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:30",
    "flat_by": "15:10",
    "broker": "paper",
    "product": "MIS",
    "stop_order_type": "SL",
    "stop_limit_buffer_pct": 0.5,
    "allowed_series": ["EQ"],
    "min_circuit_band_pct": 10,
    "block_surveillance": True,
    "blocked": [],
    "allow_margin": False,
    "allow_short": False,
    "allow_fno": False,
    "charges": {},
    "tick_sizes": {},
    "special_sessions": {},
    "extra_holidays": {},
}


def utf8_stdout():
    """Print ₹ safely even when the terminal or a pipe is not UTF-8."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path, doc):
    """Atomic write, pretty, trailing newline, ₹ kept as ₹."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path=os.path.join("trader", "config.json")):
    """trader/config.json over DEFAULT_CONFIG. A missing file is fine; a broken one is an error."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            raise ValueError("%s is not a JSON object" % path)
        for k, v in user.items():
            if k in ("charges", "tick_sizes", "special_sessions", "extra_holidays") and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
