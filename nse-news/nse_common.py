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
  config     trader/config.json with defaults. Every tool finds the same trader
             folder (trader_home) no matter where Claude Code was started, and the
             tools that guard money refuse to run without a config.json.
  loss       The worst-case loss at the stop (the SL limit, not the trigger), the
             shared share-count rules and the order tick, so nse-exit,
             nse-timemachine, nse-budget and nse-risk all size the same way.
  kite data  Kite Connect as the bar and quote source when today's login is valid
             (autopilot/kite.py login): kite_answer(yahoo_url) returns the same
             Yahoo-shaped JSON from Kite, or None and the caller asks Yahoo as before.
"""

import csv
import datetime
import decimal
import gzip
import hashlib
import http.cookiejar
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

COMMON_VERSION = "1.2.0"

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


HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_HOME_URL = "https://www.nseindia.com/"


class HolidayError(Exception):
    pass


DEFAULT_DIR = object()     # "use the standard trader folder" (None means "none at all")


def _http_get(url, timeout, headers, opener=None):
    """(status, body text). HTTP errors come back as a status, not an exception."""
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)
        with resp:
            code, raw, enc = resp.status, resp.read(), resp.headers.get("Content-Encoding")
    except urllib.error.HTTPError as e:
        code, raw, enc = e.code, e.read() or b"", e.headers.get("Content-Encoding") if e.headers else None
    except (urllib.error.URLError, OSError) as e:
        raise HolidayError("could not reach NSE: %s" % e)
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            pass
    return code, raw.decode("utf-8", "replace")


def fetch_holiday_master(timeout=20, get=None):
    """NSE's holiday-master JSON. On a 401 or 403 it visits nseindia.com once with a
    cookie jar (what a browser does first) and asks again. `get(url, timeout, headers,
    opener)` is for tests."""
    get = get or _http_get
    code, body = get(HOLIDAY_URL, timeout, nse_headers(), None)
    if code in (401, 403):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        get(NSE_HOME_URL, timeout, nse_headers({"Accept": "text/html,application/xhtml+xml,*/*"}), opener)
        code, body = get(HOLIDAY_URL, timeout, nse_headers(), opener)
    if code != 200:
        raise HolidayError("NSE answered HTTP %d for its holiday list" % code)
    try:
        return json.loads(body)
    except ValueError:
        raise HolidayError("NSE's holiday list came back as something other than JSON")


def update_holiday_cache(doc, state_dir, now_utc=None, source=HOLIDAY_URL):
    """holiday-master JSON -> <state_dir>/nse-holidays.json, merged with the years already
    there: {"fetched_at", "source", "holidays": {iso: name}, "years": [...]}.
    Returns (cache, years in this fetch)."""
    if not isinstance(doc, dict):
        raise HolidayError("NSE's holiday list is not the holiday-master shape")
    hol, years = parse_holiday_master(doc, "CM")
    if not hol:
        raise HolidayError("NSE's holiday list has no capital-market (CM) dates")
    path = os.path.join(state_dir, HOLIDAY_CACHE)
    try:
        old = read_json(path, {}) or {}
    except ValueError:
        old = {}
    if not isinstance(old, dict):
        old = {}
    merged = {k: v for k, v in (old.get("holidays") or {}).items()
              if str(k)[:4].isdigit() and int(str(k)[:4]) not in years}
    merged.update(hol)
    all_years = sorted({int(y) for y in (old.get("years") or []) if str(y).isdigit()} | set(years))
    cache = {"fetched_at": utc_iso(now_utc), "source": source,
             "holidays": dict(sorted(merged.items())), "years": all_years}
    write_json(path, cache)
    return cache, years


def holidays_stale(state_dir, today, cfg=None, now_utc=None, max_age_days=None):
    """(stale, why). True when the holiday list cannot be trusted for `today` (an IST date): the
    year is not known, or in December next year's list is still missing, or the cache NSE
    fetched is older than "holiday_refresh_days" (30)."""
    hol, years = load_holidays(cfg, state_dir)
    if today.year not in years:
        return True, "NSE's %d holiday list is not loaded" % today.year
    if today.month == 12 and today.year + 1 not in years:
        return True, "NSE's %d holiday list is not loaded yet (NSE publishes it around December)" % (today.year + 1)
    days = max_age_days if max_age_days is not None else float((cfg or {}).get("holiday_refresh_days") or 30)
    cache = None
    try:
        cache = read_json(os.path.join(state_dir, HOLIDAY_CACHE)) if state_dir else None
    except ValueError:
        cache = None
    if cache is None:
        # only the built-in list: fine for its own years, refreshed once a month or so is NSE's own cadence
        return False, ""
    fetched = str((cache or {}).get("fetched_at") or "")
    try:
        when = datetime.datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True, "the holiday cache has no fetch time"
    if ((now_utc or utc_now()) - when).total_seconds() > days * 86400:
        return True, "the holiday cache is older than %d days" % days
    return False, ""


def ensure_holidays(cfg=None, state_dir=None, today=None, now_utc=None, fetch=None):
    """load_holidays, but first refreshes NSE's list when it is stale (see holidays_stale).
    Best effort: a failed refresh never raises. Returns (holidays, years, note) where note is
    "" or one plain sentence saying what happened (refreshed, or could not refresh)."""
    state_dir = state_dir if state_dir is not None else tpath("state")
    now_utc = now_utc or utc_now()
    today = today or to_ist(now_utc).date()
    note = ""
    stale, why = holidays_stale(state_dir, today, cfg, now_utc)
    if stale:
        try:
            doc = (fetch or fetch_holiday_master)()
            update_holiday_cache(doc, state_dir, now_utc)
            note = "refreshed NSE's holiday list (%s)" % why
        except (HolidayError, OSError, ValueError) as e:
            note = "could not refresh NSE's holiday list (%s): %s" % (why, e)
    hol, years = load_holidays(cfg, state_dir)
    return hol, years, note


def load_holidays(cfg=None, state_dir=DEFAULT_DIR):
    """Built-in 2026 list + nse-alerts' cache + config "extra_holidays".

    Returns (holidays {iso: name}, known_years set). A year counts as known only
    when NSE's full list for it is loaded (built in, or cached by `alerts.py
    holidays --refresh`), or when config "holiday_years" names it; a single
    extra_holidays date does not make its year known.
    """
    hol = dict(NSE_HOLIDAYS)
    years = set(HOLIDAY_YEARS)
    if state_dir is DEFAULT_DIR:
        state_dir = tpath("state")      # None means "no cache file at all"
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


def order_tick(price, symbol=None, cfg=None):
    """The tick to put every order price on for this stock. NSE sets each stock's tick from
    an earlier close (reviewed monthly), not from today's price, so near a slab edge the
    price's own slab can be the wrong one: a stop at 997.97 on the 0.05 grid is rejected if
    the stock's tick is 0.10. This takes the coarsest tick found within "tick_band_pct"
    (10) of the price either way. The coarser tick is a multiple of the finer ones, so a
    price on this grid is valid under every tick the stock could have. A per-symbol
    "tick_sizes" override in the config wins."""
    cfg = cfg or {}
    over = (cfg.get("tick_sizes") or {}).get((symbol or "").upper())
    if over:
        return dec(over)
    pct = cfg.get("tick_band_pct")
    band = dec(10 if pct is None else pct) / 100
    p = dec(price)
    return max(tick_size(p * (1 - band)), tick_size(p), tick_size(p * (1 + band)))


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


# ---------------------------------------------------------------- loss at the stop, sizing

def stop_buffer_pct(cfg=None):
    v = (cfg or {}).get("stop_limit_buffer_pct")
    return 0.5 if v is None else float(v)


def stop_limit_price(stop, symbol=None, cfg=None, long=True):
    """The limit of an SL stop leg: the trigger less "stop_limit_buffer_pct" (0.5), rounded
    away from the trigger onto the tick (down for a sell stop, up for a buy stop)."""
    s = dec(stop)
    buf = dec(stop_buffer_pct(cfg)) / 100
    raw, mode = (s * (1 - buf), "down") if long else (s * (1 + buf), "up")
    return round_to_tick(raw, order_tick(s, symbol, cfg), mode)


def worst_exit_price(stop, symbol=None, cfg=None, long=True):
    """Where a stop that gaps through its trigger can fill at worst. The stop leg is an SL
    order, so it fills no worse than its limit: that is the price the max loss is measured
    at. "loss_basis": "trigger" measures at the trigger instead (the old behaviour: a stop
    that does not slip)."""
    if str((cfg or {}).get("loss_basis") or "limit").lower() == "trigger":
        return dec(stop)
    return stop_limit_price(stop, symbol, cfg, long)


def shares_for(inr, per_share):
    """Whole shares that fit in `inr` at `per_share`, exactly."""
    u, p = dec(inr), dec(per_share)
    if p <= 0 or u <= 0:
        return 0
    return int((u / p).to_integral_value(rounding=decimal.ROUND_FLOOR))


def charges_total(qty, buy, sell, cfg=None):
    if qty <= 0:
        return D(0)
    return dec(intraday_charges(qty, buy, sell, cfg)["total"])


def loss_at_stop(qty, entry, stop, symbol=None, cfg=None, long=True, with_charges=None):
    """The worst-case loss if the stop is hit, in rupees (Decimals):
    {"exit_price", "per_share", "gross", "charges", "total"}. total includes the round-trip
    charges unless "include_charges" is false (or with_charges=False)."""
    cfg = cfg or {}
    if with_charges is None:
        with_charges = bool(cfg.get("include_charges", True))
    exit_px = worst_exit_price(stop, symbol, cfg, long)
    e = dec(entry)
    per = (e - exit_px) if long else (exit_px - e)
    gross = per * int(qty)
    buy, sell = (e, exit_px) if long else (exit_px, e)
    ch = charges_total(qty, buy, sell, cfg)
    return {"exit_price": exit_px, "per_share": per, "gross": gross, "charges": ch,
            "total": gross + (ch if with_charges else D(0))}


def max_loss_shares(cap, entry, stop, symbol=None, cfg=None, long=True, with_charges=None):
    """The most shares whose worst-case loss at the stop (loss_at_stop) fits in `cap`."""
    exit_px = worst_exit_price(stop, symbol, cfg, long)
    risk = (dec(entry) - exit_px) if long else (exit_px - dec(entry))
    hi = shares_for(cap, risk)
    if hi <= 0:
        return 0
    capd = dec(cap)
    lo = 0
    while lo < hi:                      # the loss only grows with n, so binary search
        mid = (lo + hi + 1) // 2
        if loss_at_stop(mid, entry, stop, symbol, cfg, long, with_charges)["total"] <= capd:
            lo = mid
        else:
            hi = mid - 1
    return lo


def account_base(cfg, portfolio_total=None, budget=None):
    """The rupee base the position cap and the cash buffer are measured on, the same in nse-budget
    and nse-risk: the configured budget, or the account's real value (cash + holdings in
    portfolio.json) when that is smaller, so a losing day shrinks the next order too."""
    b = float(budget if budget is not None else ((cfg or {}).get("budget") or 0))
    if portfolio_total is not None and portfolio_total > 0:
        return min(b, float(portfolio_total)) if b > 0 else float(portfolio_total)
    return b


def portfolio_total(portfolio, prices=None):
    """Cash plus holdings of a portfolio.json doc, in rupees; None when there is no portfolio.
    A holding is valued at its price in `prices` ({SYMBOL: price}) when there is one, else at
    the cost of its lots. This is the value nse-budget and nse-risk both size against."""
    if not isinstance(portfolio, dict):
        return None
    try:
        total = float(portfolio.get("cash"))
    except (TypeError, ValueError):
        return None
    prices = prices or {}
    for p in portfolio.get("positions") or []:
        sym = nse_symbol(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol"))
        if not sym:
            continue
        n = _num_or_none(p.get("shares", p.get("qty", p.get("quantity"))))
        if n is None:
            n = sum(_num_or_none(l.get("shares")) or 0 for l in p.get("lots") or [])
        px = _num_or_none(prices.get(sym))
        if px:
            total += n * px
        else:
            total += sum(_num_or_none(l.get("cost")) or 0 for l in p.get("lots") or [])
    return total


def size_limits(entry, stop, symbol=None, cfg=None, budget=None, cash=None, portfolio_total=None, long=True):
    """The four limits nse-budget sizes a trade to: max loss (at the SL limit, with charges),
    position cap, max order, cash after the buffer. Returns a list of
    {"name", "inr", "shares", "how"}; the trade's size is the smallest "shares". `budget` is the
    configured budget (config "budget" if None); `cash` and `portfolio_total` come from
    portfolio.json when there is one."""
    cfg = cfg or {}
    base = account_base(cfg, portfolio_total, budget)
    ref = dec(entry)
    out = []
    cap = cfg.get("max_loss_per_trade_inr")
    if cap:
        n = max_loss_shares(cap, ref, stop, symbol, cfg, long)
        lo = loss_at_stop(max(n, 1), ref, stop, symbol, cfg, long)
        out.append({"name": "max loss", "inr": float(cap), "shares": n,
                    "how": "%s at the SL limit %s: %d x %s = %s%s" % (
                        inr(cap), lo["exit_price"], max(n, 1), lo["per_share"], inr(lo["gross"]),
                        (" + %s charges = %s" % (inr(lo["charges"]), inr(lo["total"])))
                        if cfg.get("include_charges", True) else "")})
    pct = cfg.get("max_position_pct")
    if pct:
        c = (dec(base) * dec(pct) / 100).quantize(D("0.01"))
        out.append({"name": "position cap", "inr": float(c), "shares": shares_for(c, ref),
                    "how": "%s%% of %s = %s" % (pct, inr(base), inr(c))})
    mo = cfg.get("max_order_inr")
    if mo:
        out.append({"name": "max order", "inr": float(mo), "shares": shares_for(mo, ref),
                    "how": "%s max order" % inr(mo)})
    buf = (dec(base) * dec(cfg.get("cash_buffer_pct") or 0) / 100).quantize(D("0.01"))
    avail = dec(base) - buf
    if cash is not None and dec(cash) - buf < avail:
        avail = dec(cash) - buf
    avail = max(avail, D(0))
    out.append({"name": "cash", "inr": float(avail), "shares": shares_for(avail, ref),
                "how": "%s available after the %s cash buffer" % (inr(avail), inr(buf))})
    return out


def size_qty(entry, stop, symbol=None, cfg=None, budget=None, cash=None, portfolio_total=None, long=True):
    """Whole shares the rules allow (the smallest of size_limits). May be 0."""
    return max(0, min(l["shares"] for l in size_limits(entry, stop, symbol, cfg, budget, cash,
                                                       portfolio_total, long)))


# ---------------------------------------------------------------- prices.json freshness

def _num_or_none(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _utc_or_none(text):
    if not text:
        return None
    try:
        return parse_now(str(text).strip())
    except ValueError:
        return None


def price_time(doc, sym):
    """When the quote for `sym` in a prices.json doc was struck (naive UTC): the last trade's
    time, else when it was fetched, else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = _utc_or_none(det.get(key))
        if t:
            return t
    return _utc_or_none((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=None, cfg=None):
    """(price, time_utc, fresh) for sym from a prices.json doc (nse-alerts writes it). Fresh means
    struck within `minutes` (default cfg "price_fresh_minutes", 5) and not from the future."""
    if minutes is None:
        minutes = float((cfg or {}).get("price_fresh_minutes") or 5)
    if not isinstance(doc, dict):
        return None, None, False
    p = _num_or_none((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


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
    "loss_basis": "limit",              # measure the max loss at the SL limit ("limit") or the trigger ("trigger")
    "tick_band_pct": 10,                # order_tick: coarsest tick within this % of the price
    "risk_pass_minutes": 5,             # a risk-check PASS is good for this long
    "price_fresh_minutes": 5,           # a quote older than this is not "fresh"
    "order_max_age_minutes": 120,       # an orders.json older than this is not traded
    "trigger_max_age_minutes": 30,      # an entry trigger older than this is not planned from
    "holiday_refresh_days": 30,         # refresh NSE's holiday list when the cache is older
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


HOME_ENV = "NSE_TRADER_HOME"
GLOBAL_HOME = os.path.join("~", ".day-trader-india")
TEST_ENV = "NSE_TEST_MODE"


class ConfigMissing(Exception):
    pass


def test_mode(env=None):
    """True only when NSE_TEST_MODE=1: the switch that lets tests pin the clock or the day
    on commands that must not take them in real use (nse-broker's --today and --now)."""
    return (os.environ if env is None else env).get(TEST_ENV) == "1"


def trader_home(cwd=None, env=None):
    """The one folder that holds config.json, orders.json, state/ and the rest, wherever
    Claude Code was started. In order: $NSE_TRADER_HOME; the nearest `trader/` folder with a
    config.json in the working folder or any folder above it; ~/.day-trader-india if it has a
    config.json; otherwise ./trader (where a first run creates it)."""
    env = os.environ if env is None else env
    explicit = env.get(HOME_ENV)
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    here = os.path.abspath(cwd or os.getcwd())
    probe = here
    while True:
        cand = os.path.join(probe, "trader")
        if os.path.isfile(os.path.join(cand, "config.json")):
            return cand
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    glob = os.path.expanduser(GLOBAL_HOME)
    if os.path.isfile(os.path.join(glob, "config.json")):
        return glob
    return os.path.join(here, "trader")


def tpath(*parts):
    """A path inside the trader folder: tpath("state", "risk.json")."""
    return os.path.join(trader_home(), *parts)


def require_config(path):
    """Raise ConfigMissing unless the config file exists. Used by the tools that guard money:
    without a config the budget, the loss limits and the daily limit would silently fall back
    to the built-in defaults."""
    if not path or not os.path.isfile(path):
        raise ConfigMissing(
            "no config.json at %s. Your limits (budget, max loss per trade, daily loss limit) live there and "
            "the tools will not guess them. Set your rules with /day-trader-india:nse-budget (it writes the "
            "file), or point %s at the folder that holds your trader files (you may have started Claude "
            "Code in a different folder)." % (path or "(no path)", HOME_ENV))


def load_config(path=None, require=False):
    """The config file over DEFAULT_CONFIG. path=None gives the defaults alone. With
    require=True a missing file raises ConfigMissing; otherwise it is fine. A broken file is
    an error."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if require:
        require_config(path)
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


# ---------------------------------------------------------------- Kite Connect market data
#
# When `autopilot/kite.py login` has left a valid session for today in the trader folder,
# every Yahoo chart or spark request for an NSE symbol is answered from Kite Connect instead,
# in Yahoo's own JSON shape, so no parser changes: callers do
#
#     text = nc.kite_answer(url) or <the Yahoo request they always made>
#
# kite_answer returns None (and the caller asks Yahoo) when there is no session, the session
# has expired (06:00 IST each morning), NSE_DATA_SOURCE=yahoo, the symbol is not on NSE (world
# indices, futures, FX), the interval or range has no Kite equivalent, or Kite fails. A Kite
# failure is printed once per process to stderr; a rejected token marks the session dead so
# the next process does not try again.
#
# The access token lives in the macOS login keychain (service KITE_TOKEN_SERVICE, account =
# the API key); kite-session.json holds only the API key, the user id and the expiry.

KITE_API = "https://api.kite.trade"
KITE_LOGIN = "https://kite.zerodha.com/connect/login?v=3&api_key=%s"
KITE_SESSION_FILE = "kite-session.json"
KITE_SECRET_SERVICE = "nse-kite-api-secret"
KITE_TOKEN_SERVICE = "nse-kite-access-token"
DATA_ENV = "NSE_DATA_SOURCE"
KITE_INDEX = {"^NSEI": "NIFTY 50", "^NSEBANK": "NIFTY BANK", "^INDIAVIX": "INDIA VIX", "^CNXIT": "NIFTY IT",
              "^CNXAUTO": "NIFTY AUTO", "^CNXPHARMA": "NIFTY PHARMA", "^CNXFMCG": "NIFTY FMCG",
              "^CNXMETAL": "NIFTY METAL"}
# Yahoo interval -> (Kite interval, most days Kite serves in one request)
KITE_INTERVAL = {"1m": ("minute", 60), "5m": ("5minute", 100), "15m": ("15minute", 200),
                 "30m": ("30minute", 200), "60m": ("60minute", 400), "1h": ("60minute", 400), "1d": ("day", 2000)}
KITE_GAP = {"hist": 0.35, "quote": 1.05, "other": 0.12}     # seconds between calls (Kite: 3/s, 1/s, 10/s)
KITE_DAILY_DAYS = 400
_KITE_LOCK = threading.Lock()
_KITE = {"last": {}, "session": None, "instruments": None, "daily": {}, "said": False,
         "http": None, "sleep": time.sleep, "error": None, "served": 0}
_EPOCH = datetime.datetime(1970, 1, 1)


class KiteError(Exception):
    pass


def kite_session_path(home=None):
    return os.path.join(home or trader_home(), KITE_SESSION_FILE)


def kite_expiry(login_utc):
    """Kite access tokens die at 06:00 IST the next morning (this morning, for a login before 06:00)."""
    ist = to_ist(login_utc)
    six = ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if ist >= six:
        six += datetime.timedelta(days=1)
    return to_utc(six)


def keychain_get(service, account):
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = p.stdout.strip()
    return v if p.returncode == 0 and v else None


def keychain_set(service, account, secret):
    """Save through `security -i` on stdin, so the secret never shows in a process list."""
    if not secret or any(c in secret for c in '"\\\n\r') or any(c in account for c in '"\\\n\r'):
        raise KiteError("refusing to store a value with quotes, backslashes or line breaks")
    cmd = 'add-generic-password -U -s "%s" -a "%s" -w "%s"\n' % (service, account, secret)
    try:
        subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KiteError("couldn't reach the keychain: %s" % e)
    if keychain_get(service, account) != secret:
        raise KiteError("the keychain did not keep the value (service %s)" % service)


def kite_session(home=None, now_utc=None, env=None):
    """{"api_key", "access_token", "user_id", "expires", "home", "path"} for today's valid Kite
    login, or None. In test mode the token comes from the file's "access_token", never the keychain."""
    env = os.environ if env is None else env
    if (env.get(DATA_ENV) or "").strip().lower() == "yahoo":
        return None
    path = kite_session_path(home)
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns)
    if _KITE["session"] and _KITE["session"][0] == key:
        sess = _KITE["session"][1]
    else:
        sess = None
        try:
            doc = read_json(path)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and doc.get("api_key") and not doc.get("dead"):
            if test_mode(env):
                token = doc.get("access_token")
            else:
                token = keychain_get(KITE_TOKEN_SERVICE, doc["api_key"])
            if token:
                sess = {"api_key": doc["api_key"], "access_token": token, "user_id": doc.get("user_id"),
                        "expires": _utc_or_none(doc.get("expires_at")), "home": os.path.dirname(path), "path": path}
        _KITE["session"] = (key, sess)
    if not sess or not sess["expires"] or (now_utc or utc_now()) >= sess["expires"]:
        return None
    return sess


def kite_mark_dead(sess, reason, now_utc=None):
    """Kite rejected the token: say so in the session file so later processes go straight to Yahoo."""
    try:
        doc = read_json(sess["path"]) or {}
        doc.update(dead=str(reason)[:300], dead_at=utc_iso(now_utc))
        write_json(sess["path"], doc)
        os.chmod(sess["path"], 0o600)
    except (OSError, ValueError):
        pass
    _KITE["session"] = None


def _kite_http(method, url, headers, data, timeout):
    if _KITE["http"]:
        return _KITE["http"](method, url, headers, data, timeout)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read() or b""
        finally:
            e.close()
    except (urllib.error.URLError, OSError) as e:
        raise KiteError("could not reach Kite: %s" % (getattr(e, "reason", None) or e))


def kite_call(sess, path, params=None, method="GET", data=None, kind="other", timeout=20, raw=False):
    """One Kite Connect API call -> its "data" (or the raw bytes with raw=True). Paced to Kite's
    rate limits; a 429 or 5xx is tried twice more. A rejected token marks the session dead."""
    url = KITE_API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    headers = {"X-Kite-Version": "3", "User-Agent": UA, "Accept-Encoding": "gzip"}
    if sess and sess.get("access_token"):
        headers["Authorization"] = "token %s:%s" % (sess["api_key"], sess["access_token"])
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    code, got = 0, b""
    for attempt in range(3):
        with _KITE_LOCK:      # deepscan fetches from threads: one paced queue per kind
            wait = KITE_GAP.get(kind, 0.12) - (time.monotonic() - _KITE["last"].get(kind, -1e9))
            if wait > 0:
                _KITE["sleep"](wait)
            _KITE["last"][kind] = time.monotonic()
        code, got = _kite_http(method, url, headers, body, timeout)
        if got[:2] == b"\x1f\x8b":
            got = gzip.decompress(got)
        if (code == 429 or code >= 500) and attempt < 2:
            _KITE["sleep"](1.0 + attempt)
            continue
        break
    if raw and code == 200:
        return got
    try:
        doc = json.loads(got.decode("utf-8"))
    except ValueError:
        raise KiteError("Kite answered HTTP %d for %s with something that is not JSON" % (code, path))
    if code == 200 and isinstance(doc, dict) and doc.get("status") == "success":
        return doc.get("data")
    doc = doc if isinstance(doc, dict) else {}
    msg = doc.get("message") or "HTTP %d" % code
    if code == 403 or doc.get("error_type") == "TokenException":
        if sess and sess.get("path"):
            kite_mark_dead(sess, msg)
        raise KiteError("Kite rejected the login (%s): run `python3 autopilot/kite.py login`" % msg)
    raise KiteError("Kite %s: %s" % (path, msg))


def kite_instruments(sess, now_utc=None):
    """{tradingsymbol: instrument_token} for NSE equities and indices, refreshed once a day."""
    day = to_ist(now_utc or utc_now()).date().isoformat()
    if _KITE["instruments"] and _KITE["instruments"][0] == day:
        return _KITE["instruments"][1]
    path = os.path.join(sess["home"], "cache", "kite", "instruments-NSE.json")
    try:
        doc = read_json(path)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("day") == day and doc.get("tokens"):
        tokens = doc["tokens"]
    else:
        text = kite_call(sess, "/instruments/NSE", raw=True, timeout=60).decode("utf-8", "replace")
        tokens = {}
        for row in csv.DictReader(io.StringIO(text)):
            seg, kind = row.get("segment"), row.get("instrument_type")
            if seg == "INDICES" or (seg == "NSE" and kind == "EQ"):
                try:
                    tokens[row["tradingsymbol"]] = int(row["instrument_token"])
                except (KeyError, TypeError, ValueError):
                    continue
        if not tokens:
            raise KiteError("Kite's NSE instrument list came back empty")
        write_json(path, {"day": day, "tokens": tokens})
    _KITE["instruments"] = (day, tokens)
    return tokens


def kite_symbol(ysym):
    """Yahoo symbol -> Kite tradingsymbol on NSE, or None (not an NSE instrument Kite is used for)."""
    s = urllib.parse.unquote(str(ysym or "")).strip().upper()
    if s.startswith("^"):
        return KITE_INDEX.get(s)
    if s.endswith(".NS") and is_nse_symbol(s[:-3]):
        return s[:-3]
    return None


def yahoo_request(url):
    """("chart" | "spark", [yahoo symbols], {query}) for a Yahoo chart or spark URL, else None."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not u.netloc.endswith("finance.yahoo.com"):
        return None
    q = dict(urllib.parse.parse_qsl(u.query))
    if u.path.startswith("/v8/finance/chart/"):
        return "chart", [urllib.parse.unquote(u.path[len("/v8/finance/chart/"):])], q
    if u.path.rstrip("/") == "/v7/finance/spark":
        return "spark", [s for s in (q.get("symbols") or "").split(",") if s], q
    return None


def kite_window(q, now_utc):
    """A Yahoo range or period1/period2 -> (from IST, to IST, sessions to keep or None), or None
    for ranges Kite can't mirror ("max", "ytd"). Yahoo's 1d-7d ranges count sessions, not days."""
    now = to_ist(now_utc)
    if q.get("period1"):
        try:
            a = to_ist(_EPOCH + datetime.timedelta(seconds=int(q["period1"])))
            b = to_ist(_EPOCH + datetime.timedelta(seconds=int(q["period2"]))) if q.get("period2") else now
        except (TypeError, ValueError, OverflowError):
            return None
        return a, min(b, now), None
    m = re.match(r"^(\d+)(d|wk|mo|y)$", (q.get("range") or "1d").strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n < 1:
        return None
    if unit == "d" and n <= 7:
        return now - datetime.timedelta(days=2 * n + 10), now, n
    days = {"d": n, "wk": 7 * n, "mo": 31 * n, "y": 366 * n}[unit]
    return now - datetime.timedelta(days=days), now, None


def _kite_ts(text):
    """"2026-11-23T09:15:00+0530" -> naive IST."""
    return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")


def _epoch(ist):
    return int((to_utc(ist) - _EPOCH).total_seconds())


def kite_candles(sess, token, kint, maxdays, a, b):
    """[(naive IST, o, h, l, c, v)] from Kite's historical API, split into requests Kite accepts."""
    out, start = {}, a.replace(microsecond=0)
    b = b.replace(microsecond=0)
    while start <= b:
        end = min(b, start + datetime.timedelta(days=maxdays) - datetime.timedelta(seconds=1))
        data = kite_call(sess, "/instruments/historical/%d/%s" % (int(token), kint),
                         {"from": start.strftime("%Y-%m-%d %H:%M:%S"), "to": end.strftime("%Y-%m-%d %H:%M:%S")},
                         kind="hist")
        for c in (data or {}).get("candles") or []:
            try:
                t = _kite_ts(c[0])
                out[t] = (t, float(c[1]), float(c[2]), float(c[3]), float(c[4]), int(c[5] or 0) if len(c) > 5 else 0)
            except (IndexError, TypeError, ValueError):
                continue
        start = end + datetime.timedelta(seconds=1)
    return [out[k] for k in sorted(out)]


def kite_daily(sess, token, tsym, now_utc):
    """Daily candles before today for the last KITE_DAILY_DAYS days, fetched once a day per symbol
    (in memory and in <trader>/cache/kite/day/). [(date ISO, o, h, l, c, v)]."""
    today = to_ist(now_utc).date()
    k = (tsym, today.isoformat())
    if k in _KITE["daily"]:
        return _KITE["daily"][k]
    path = os.path.join(sess["home"], "cache", "kite", "day", re.sub(r"[^A-Za-z0-9&\-]", "_", tsym) + ".json")
    try:
        doc = read_json(path)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("day") == today.isoformat() and isinstance(doc.get("rows"), list):
        rows = [tuple(r) for r in doc["rows"]]
    else:
        start = datetime.datetime.combine(today - datetime.timedelta(days=KITE_DAILY_DAYS), datetime.time(0, 0))
        end = datetime.datetime.combine(today - datetime.timedelta(days=1), datetime.time(23, 59, 59))
        rows = [(c[0].date().isoformat(),) + c[1:] for c in kite_candles(sess, token, "day", 2000, start, end)
                if c[0].date() < today]
        write_json(path, {"day": today.isoformat(), "symbol": tsym, "rows": [list(r) for r in rows]})
    _KITE["daily"][k] = rows
    return rows


def kite_quotes(sess, tsyms):
    """{tradingsymbol: Kite full quote} (last_price, last_trade_time, ohlc with yesterday's close, volume)."""
    out, tsyms = {}, list(dict.fromkeys(t for t in tsyms if t))
    for i in range(0, len(tsyms), 250):
        chunk = tsyms[i:i + 250]
        data = kite_call(sess, "/quote", {"i": ["NSE:" + t for t in chunk]}, kind="quote") or {}
        for t in chunk:
            if isinstance(data.get("NSE:" + t), dict):
                out[t] = data["NSE:" + t]
    return out


def _live_today(now_utc):
    """True when today is an NSE session and the market has opened (so today's bars exist)."""
    ist = to_ist(now_utc)
    try:
        ok = trading_day(ist.date())[0]
    except Exception:
        ok = ist.weekday() < 5
    return bool(ok) and ist.time() >= SESSION_OPEN


def kite_result(sess, ysym, q, now_utc, quote=None):
    """One Yahoo v8 chart "result" built from Kite, or None when Kite can't serve this request."""
    tsym = kite_symbol(ysym)
    interval = (q.get("interval") or "1d").strip()
    if not tsym or interval not in KITE_INTERVAL:
        return None
    win = kite_window(q, now_utc)
    if not win:
        return None
    token = kite_instruments(sess, now_utc).get(tsym)
    if token is None:
        return None
    a, b, sessions = win
    kint, maxdays = KITE_INTERVAL[interval]
    today = to_ist(now_utc).date()
    daily = kite_daily(sess, token, tsym, now_utc)
    if kint == "day":
        if (today - a.date()).days <= KITE_DAILY_DAYS:
            rows = [r for r in daily if a.date().isoformat() <= r[0] <= b.date().isoformat()]
        else:
            rows = [(c[0].date().isoformat(),) + c[1:] for c in kite_candles(sess, token, "day", maxdays, a, b)
                    if c[0].date() < today]
        rows = [(datetime.datetime.combine(datetime.date.fromisoformat(r[0]), SESSION_OPEN),) + tuple(r[1:])
                for r in rows]
        ohlc = (quote or {}).get("ohlc") or {}
        if (quote and b.date() == today and _live_today(now_utc) and ohlc.get("open")
                and quote.get("last_price") is not None):
            rows.append((datetime.datetime.combine(today, SESSION_OPEN), float(ohlc["open"]), float(ohlc["high"]),
                         float(ohlc["low"]), float(quote["last_price"]), int(quote.get("volume") or 0)))
    else:
        rows = kite_candles(sess, token, kint, maxdays, a, b)
    if sessions:
        keep = sorted({r[0].date() for r in rows})[-sessions:]
        rows = [r for r in rows if r[0].date() in keep]
    if not rows:
        return None
    first, last = rows[0][0].date(), rows[-1][0].date()
    before = [r for r in daily if r[0] < first.isoformat()]
    last_day = [r for r in rows if r[0].date() == last]
    meta = {"currency": "INR", "symbol": urllib.parse.unquote(ysym).upper(), "exchangeName": "NSI",
            "fullExchangeName": "NSE", "instrumentType": "INDEX" if tsym in KITE_INDEX.values() else "EQUITY",
            "dataGranularity": interval, "range": q.get("range") or "", "dataSource": "kite",
            "chartPreviousClose": before[-1][4] if before else None,
            "regularMarketPrice": rows[-1][4], "regularMarketDayHigh": max(r[2] for r in last_day),
            "regularMarketDayLow": min(r[3] for r in last_day),
            "regularMarketTime": _epoch(datetime.datetime.combine(last, SESSION_CLOSE))}
    if quote and last == today and quote.get("last_price") is not None:
        ohlc = quote.get("ohlc") or {}
        meta["regularMarketPrice"] = float(quote["last_price"])
        if ohlc.get("high"):
            meta["regularMarketDayHigh"], meta["regularMarketDayLow"] = float(ohlc["high"]), float(ohlc["low"])
        if ohlc.get("close"):
            meta["previousClose"] = float(ohlc["close"])
        when = quote.get("last_trade_time") or quote.get("timestamp")
        try:
            meta["regularMarketTime"] = _epoch(datetime.datetime.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            meta["regularMarketTime"] = _epoch(min(to_ist(now_utc), rows[-1][0]))
    elif last == today:
        meta["regularMarketTime"] = _epoch(min(to_ist(now_utc), rows[-1][0]))
    if sessions == 1 and "previousClose" not in meta and before:
        meta["previousClose"] = before[-1][4]
    return {"meta": meta, "timestamp": [_epoch(r[0]) for r in rows],
            "indicators": {"quote": [{"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                                      "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                                      "volume": [r[5] for r in rows]}]}}


def kite_answer(url, now_utc=None, home=None):
    """The Yahoo JSON text for a Yahoo chart or spark URL, served from Kite, or None (ask Yahoo).
    A spark batch is all-Kite or all-Yahoo, never mixed."""
    req = yahoo_request(url)
    if not req:
        return None
    now = now_utc or utc_now()
    sess = kite_session(home, now)
    if not sess:
        return None
    kind, ysyms, q = req
    tsyms = [kite_symbol(s) for s in ysyms]
    if not ysyms or not all(tsyms) or (q.get("interval") or "1d") not in KITE_INTERVAL:
        return None
    try:
        quotes = kite_quotes(sess, tsyms) if _live_today(now) else {}
        results = []
        for ysym, tsym in zip(ysyms, tsyms):
            res = kite_result(sess, ysym, q, now, quotes.get(tsym))
            if res is None:
                return None
            results.append(res)
    except (KiteError, ValueError, KeyError, TypeError, OSError) as e:
        _KITE["error"] = str(e)
        if not _KITE["said"]:
            _KITE["said"] = True
            print("Kite data unavailable (%s); using Yahoo" % e, file=sys.stderr)
        return None
    _KITE["served"] += 1
    if kind == "chart":
        return json.dumps({"chart": {"result": results, "error": None}})
    return json.dumps({"spark": {"result": [{"symbol": s, "response": [r]} for s, r in zip(ysyms, results)],
                                 "error": None}})
