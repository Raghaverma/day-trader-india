#!/usr/bin/env python3
"""Price alerts and the NSE clock for nse-alerts. Standard library only.

    python3 alerts.py clock                          # NSE open, pre-open or closed; next open; holidays
    python3 alerts.py holidays                       # this year's NSE trading holidays, with names
    python3 alerts.py holidays --refresh             # fetch NSE's holiday-master -> trader/state/nse-holidays.json
    python3 alerts.py holidays --refresh --if-stale  # only when the list is stale; silent when fresh, always exit 0
    python3 alerts.py holidays --year 2027           # says so plainly when a year's list is not known
    python3 alerts.py quote RELIANCE --write         # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by (15:10) from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1328.80
    python3 alerts.py add --symbol RELIANCE --target 1344.40
    python3 alerts.py add --at 15:10 --label "flat by" --before-close
    python3 alerts.py add --symbol INFY --pct 3
    python3 alerts.py add --opening-bell             # rings at 09:15 IST on each trading day
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-11-13 15:10" --offline tests/fixtures/nse-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price". A target alert says what to
                  do on Kite: there is no OCO for MIS orders, so cancel the
                  resting stop first, then sell.
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE normal market opened today (09:15 IST; rings once per
                  trading day)
  time            the IST clock passed "at" (HH:MM) on a trading day (rings once
                  per trading day); "before_close": true keeps it the same
                  distance before the close on a special session (Muhurat
                  trading from "special_sessions" in trader/config.json)

A rule with "date" is only watched on that IST date (rules from a plan are for
the plan's day only). An above / below alert rings ONCE and then stays quiet
until you reset it, unless the rule has "rearm": true (it re-arms when the
price crosses back). A pct_move alert rings at most once per day.

Between two checks: a price can spike through a level and come back before the
next check. So an above / target alert also rings when the high of any
completed 5-minute bar since the previous check reached it, and a below / stop
alert when a bar's low did; the message then names the bar and its IST time
and says it was a bar high / low, not the last price (the ring in --json has
"source": "bar"). A bar counts when it has ended by now, ended after the
symbol's previous quote, and started after the rule was added (`add` stamps
it) or re-armed (`reset`, or a "rearm" rule that fell back), so a morning low
from before the trade never rings its stop. A rule first seen by `check` with
no stamp starts counting from that check, and with no previous quote and no
stamp the last price alone decides. trader/state/alerts.json keeps the last
check time ("last_check", and per symbol in "quoted") and when each rule was
armed ("armed").

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m
  (falls back to query1 on a 429 or a 5xx; one retry with a pause on a 429; one
  request per symbol, half a second apart). --offline DIR reads
  DIR/yahoo-<SYMBOL>.json (or .json.gz) instead.

Files live in the trader folder (nse_common.trader_home: $NSE_TRADER_HOME, else
the nearest trader/ with a config.json, else ~/.day-trader-india, else
./trader). Every command that reads the config says which file it used, or that
the built-in defaults are in use.

The NSE clock: normal market 09:15-15:30 IST, pre-open 09:00-09:15 (orders
09:00-09:08), closed on weekends and NSE trading holidays. The 2026 list is
built in (nse_common.NSE_HOLIDAYS). Other years come from
`holidays --refresh` (https://www.nseindia.com/api/holiday-master?type=trading,
segment CM) or "extra_holidays" in trader/config.json. `clock` and `holidays`
refresh the list on their own when it is stale (a year not loaded, next year's
list missing in December, or a cache older than "holiday_refresh_days", 30);
that is best effort and never stops them. A year with no list is reported,
never guessed: weekdays then count as trading days.
"""

import argparse
import datetime
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2", "query1")
HOLIDAY_URL = nc.HOLIDAY_URL
NSE_HOME = nc.NSE_HOME_URL
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
UPCOMING_DAYS = 60
SQUARE_OFF = "15:25"   # Zerodha's MIS auto square-off
NORMAL = ("09:15", "15:30")
BAR_SECONDS = {"1m": 60, "2m": 120, "5m": 300, "15m": 900}


def config_path():
    """trader/config.json in the trader folder (nse_common.trader_home), found at run time."""
    return nc.tpath("config.json")


def state_dir():
    return nc.tpath("state")


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, via nse_common)

def parse_now(text):
    """--now "2026-11-13 10:05" (IST) -> naive UTC."""
    try:
        return nc.parse_now(text)
    except ValueError as e:
        raise AlertError(str(e))


def _utc(text):
    """A stored UTC stamp "2026-11-13T04:35:00Z" -> naive UTC, or None."""
    try:
        return datetime.datetime.strptime(str(text or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def load_cfg(path):
    try:
        return nc.load_config(path)
    except ValueError as e:
        raise AlertError("%s is not a usable config: %s" % (path, e))


def config_line(path):
    """Which config this run used: the skills that do not guard money run on the defaults
    without one, and say so."""
    if path and os.path.isfile(path):
        return "Config: %s." % path
    return "Config: none at %s, so the built-in defaults are in use." % path


def calendar(a):
    """(cfg, holidays, known years) from the config and the holiday cache."""
    cfg = load_cfg(getattr(a, "config", None) or config_path())
    hol, years = nc.load_holidays(cfg, getattr(a, "state_dir", None) or state_dir())
    return cfg, hol, years


def _holiday_fetch():
    """fetch_holiday_master for nse_common.ensure_holidays: a failure comes back as the
    HolidayError it expects (it never stops clock or holidays)."""
    try:
        return fetch_holiday_master()
    except AlertError as e:
        raise nc.HolidayError(str(e))


def ensured_calendar(a, now_utc):
    """calendar(), but NSE's holiday list is refreshed first when it is stale (a year not
    loaded, next year's list missing in December, or an old cache). Best effort: returns
    (cfg, holidays, years, note) and never raises for the network."""
    cfg = load_cfg(getattr(a, "config", None) or config_path())
    sdir = getattr(a, "state_dir", None) or state_dir()
    try:
        hol, years, note = nc.ensure_holidays(cfg, sdir, nc.to_ist(now_utc).date(), now_utc, fetch=_holiday_fetch)
    except Exception as e:     # never let the refresh stop the clock
        hol, years = nc.load_holidays(cfg, sdir)
        note = "could not refresh NSE's holiday list: %s" % e
    return cfg, hol, years, note


def phase_words(st):
    if st["phase"] == "open":
        return "normal market" if (st["opens"], st["closes"]) == NORMAL else "special session"
    if st["phase"] == "pre-open":
        return "orders 09:00 to 09:08, the normal market opens at %s" % st["opens"]
    if st["phase"] == "before-open":
        return "before the 09:00 pre-open" if (st["opens"], st["closes"]) == NORMAL else \
            "before the %s session" % st["opens"]
    if st["phase"] == "after-close":
        return "after the %s close" % st["closes"]
    return st["reason"] or "closed"


def day_words(iso):
    d = datetime.date.fromisoformat(iso[:10])
    return "%s %s" % (d.strftime("%a"), iso)


def upcoming(today, hol, years, days=UPCOMING_DAYS):
    """Weekday NSE holidays from today for `days` days, and the years in that
    window whose list is not known."""
    end = today + datetime.timedelta(days=days)
    out = []
    for iso in sorted(hol):
        try:
            d = datetime.date.fromisoformat(iso)
        except ValueError:
            continue
        if today <= d <= end and d.weekday() < 5:
            out.append({"date": iso, "weekday": nc.WEEKDAYS[d.weekday()], "name": hol[iso]})
    unknown = sorted({y for y in range(today.year, end.year + 1) if y not in set(years)})
    return out, unknown


def unknown_line(unknown):
    ys = " and ".join(str(y) for y in unknown)
    return ("NSE's %s holiday list is not known here yet (NSE publishes the next year's list around December): "
            "run alerts.py holidays --refresh. Until then every weekday counts as a trading day, so check "
            "nseindia.com before you plan around a date." % ys)


# ---------------------------------------------------------------- network

_urlopen = urllib.request.urlopen   # tests swap these three
_sleep = time.sleep


def _build_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _get(url, timeout=20, headers=None, opener=None):
    """(HTTP status, body). A gzip body is unpacked. A network failure is an AlertError."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": nc.UA, "Accept": "*/*"})
    fn = opener.open if opener is not None else _urlopen

    def text(raw):
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return raw.decode("utf-8", "replace")
    try:
        with fn(req, timeout=timeout) as r:
            return 200, text(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = text(e.read() or b"")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))
    except OSError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e))


# ---------------------------------------------------------------- NSE holidays

HOLIDAY_ADVICE = ": try again in a few minutes, or add the dates to \"extra_holidays\" in trader/config.json"


def fetch_holiday_master(timeout=20):
    """NSE's holiday-master JSON, via nse_common.fetch_holiday_master (a 401 or 403 makes it
    visit nseindia.com once with a cookie jar and ask again). The requests go through this
    module's _get, _sleep and _build_opener, so a network failure is an AlertError and the
    three can be swapped in tests. A refusal from NSE is an AlertError too."""
    jar = {}

    def get(url, tmo, headers, opener=None):
        if opener is None:
            return _get(url, tmo, headers)
        if "opener" not in jar:
            jar["opener"] = _build_opener()     # nse_common's own opener is not used
        if url == HOLIDAY_URL:
            _sleep(PAUSE)                       # a pause between the home page and the retry
        return _get(url, tmo, headers, jar["opener"])
    try:
        return nc.fetch_holiday_master(timeout, get=get)
    except nc.HolidayError as e:
        raise AlertError(str(e) + HOLIDAY_ADVICE)


def refresh_holidays(doc, state_dir, now_utc, source=HOLIDAY_URL):
    """holiday-master JSON -> trader/state/nse-holidays.json, merged with the years
    already there: {"fetched_at", "source", "holidays": {iso: name}, "years": [...]}.
    A thin wrapper over nse_common.update_holiday_cache (a bad list is an AlertError)."""
    try:
        return nc.update_holiday_cache(doc, state_dir, now_utc, source)
    except nc.HolidayError as e:
        raise AlertError(str(e))


# ---------------------------------------------------------------- quotes (Yahoo .NS)

def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return nc.yahoo_chart_url(sym, "1d", "5m", host)


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (nc.yahoo_symbol(sym), err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    msym = m.get("symbol")
    if msym and nc.nse_symbol(msym) != sym:
        raise AlertError("Yahoo sent %s when asked for %s" % (msym, nc.yahoo_symbol(sym)))
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % nc.yahoo_symbol(sym))
    t = m.get("regularMarketTime")
    when = nc.utc_iso(nc.to_utc(nc.ist_from_epoch(t))) if t else ""
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym, "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo",
            "bar_seconds": BAR_SECONDS.get(m.get("dataGranularity"), 300),
            "bars": parse_bars(res[0].get("timestamp"), q)}


def parse_bars(stamps, q):
    """Yahoo's 5-minute bars -> [{"t": UTC start "2026-11-13T04:45:00Z", "high", "low"}]. A bar
    with no high or low (Yahoo leaves holes) is skipped. The last bar may still be forming:
    evaluate only counts a bar once it has ended."""
    out = []
    highs, lows = q.get("high") or [], q.get("low") or []
    for i, ts in enumerate(stamps or []):
        try:
            hi, lo = round(float(highs[i]), 4), round(float(lows[i]), 4)     # float32 noise off
            start = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts))
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        out.append({"t": nc.utc_iso(start), "high": hi, "low": lo})
    return out


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, sym):
    base = os.path.join(folder, "yahoo-%s.json" % sym)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def clean_symbols(items):
    out = []
    for s in items:
        sym = nc.nse_symbol(s)
        if not sym:
            continue
        if sym.startswith("^") or not nc.is_nse_symbol(sym):
            raise AlertError("%r is not an NSE symbol (bare, like RELIANCE or M&M)" % s)
        if sym not in out:
            out.append(sym)
    return out


def get_quotes(symbols, offline=None, pause=PAUSE):
    """{SYMBOL: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = nc.nse_symbol(sym)
        try:
            if offline:
                text = _offline_text(offline, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source="yahoo"):
    try:
        old = nc.read_json(path, {}) or {}
    except (ValueError, OSError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = nc.utc_iso(now_utc)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes in rupees for alerts and sizing checks. Your broker's price is the one that "
                    "fills. detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    nc.write_json(path, doc)
    return doc


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:10\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this IST date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return nc.to_ist(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today. "before_close" keeps it the same distance
    before the close on a special session (normal close 15:30)."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and (status.get("opens"), status.get("closes")) != NORMAL:
        base = datetime.date(2000, 1, 1)
        gap = datetime.datetime.combine(base, nc.SESSION_CLOSE) - datetime.datetime.combine(base, at)
        at = (datetime.datetime.combine(base, _hhmm(status["closes"])) - gap).time()
    return at


def rs(x):
    return nc.inr(round(float(x), 2), keep_zeros=True)


def bar_touch(q, price, above, now_utc, prev_quoted, armed):
    """The first 5-minute bar that reached `price` (its high for an above / target rule, its low
    for a below / stop rule) and that nobody looked at yet, else None. A bar counts when it has
    ended by now (a forming bar is never used), ended after the previous quote of the symbol
    (that check already saw anything earlier) and started after the rule was armed (so a low
    from before the trade never rings its stop). With no previous quote and no armed time
    there is nothing to look back to: the last price alone decides."""
    if armed is None:
        return None
    secs = int(q.get("bar_seconds") or 300)
    for b in q.get("bars") or []:
        start = _utc(b.get("t"))
        if start is None:
            continue
        end = start + datetime.timedelta(seconds=secs)
        if end > now_utc or start < armed or (prev_quoted is not None and end <= prev_quoted):
            continue
        v = b.get("high") if above else b.get("low")
        if isinstance(v, (int, float)) and (v >= price if above else v <= price):
            return b
    return None


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}.

    state: {"rung": {id: when it rang}, "armed": {id: when the rule started to count},
    "quoted": {SYMBOL: when its quote was last read}, "last_check"}. An above / target rule
    also rings on a completed bar's high since the previous quote of its symbol, and a below /
    stop rule on a bar's low (see bar_touch); the ring then has "source": "bar" and the bar's
    "bar_time"."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    armed_at = state.setdefault("armed", {})
    quoted = state.setdefault("quoted", {})
    prev_quoted = {s: _utc(t) for s, t in quoted.items()}
    today = status["date"]
    local = nc.to_ist(now_utc)
    stamp = nc.utc_iso(now_utc)
    trading = status["phase"] != "closed-day"
    normal = (status.get("opens"), status.get("closes")) == NORMAL
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST), %s %s to %s." % (
                    status["now_ist"], "normal market" if normal else (status.get("reason") or "special session"),
                    status["opens"], status["closes"])})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if trading and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                            local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    (", the market opens at %s." % status["opens"]) if status["phase"] in ("before-open", "pre-open") \
                    else ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, cancel its stop, then sell: nse-broker's ticket.py flatten "
                            "--symbol %s builds the sell ticket, then nse-risk and your yes." % (sym, sym))
                    if normal:
                        msg += " Zerodha squares off MIS at %s and charges ₹50 + GST an order for it." % SQUARE_OFF
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = nc.nse_symbol(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            above = typ == "above"
            armed = _utc(armed_at.setdefault(rid, stamp))
            hit = p >= r["price"] if above else p <= r["price"]
            bar = None if hit else bar_touch(q, r["price"], above, now_utc, prev_quoted.get(sym), armed)
            if (hit or bar) and rid not in rung:
                word = "at or above" if above else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                note = (" (" + r["note"] + ")") if r.get("note") else ""
                if bar:
                    mins = int(q.get("bar_seconds") or 300) // 60
                    when = nc.to_ist(_utc(bar["t"])).strftime("%H:%M")
                    msg = "%s traded at %s in the %s IST %d-minute bar (bar %s, not the last price): %s your %s %s%s. " \
                          "The last price is %s: check the live quote." % (
                              sym, rs(bar["high" if above else "low"]), when, mins, "high" if above else "low",
                              word, label, rs(r["price"]), note, rs(p))
                else:
                    msg = "%s %s %s: %s your %s %s%s." % (sym, "is" if status["open"] else "last", rs(p), word,
                                                           label, rs(r["price"]), note)
                if r["type"] == "target":
                    msg += " Kite has no OCO for MIS: cancel the stop, then sell."
                elif r["type"] == "stop":
                    msg += " Your SL sell should trigger here: check the order book and record the fill."
                ring = {"id": rid, "symbol": sym, "message": msg}
                if bar:
                    ring.update(source="bar", bar_time=bar["t"], bar_price=bar["high" if above else "low"])
                rings.append(ring)
                rung[rid] = stamp
            elif not (hit or bar) and rid in rung and r.get("rearm"):
                del rung[rid]
                armed_at[rid] = stamp       # only bars from now on count for the next ring
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, rs(p), rs(ref))})
                rung[rid] = stamp
    for sym in quotes:
        quoted[sym] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run nse-exit first)")
    sym = nc.nse_symbol(plan["symbol"])
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run nse-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or nc.DEFAULT_CONFIG["flat_by"]
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not an IST time like 15:10" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        if isinstance(p, dict):
            syms.append(str(p.get("symbol") or p.get("tradingsymbol") or p.get("ticker") or ""))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]))
    return [s for s in syms if s]


def cmd_clock(a):
    now = parse_now(a.now)
    cfg, hol, years, note = ensured_calendar(a, now)
    st = nc.market_status(now, cfg, hol, years)
    today = datetime.date.fromisoformat(st["date"])
    up, unknown = upcoming(today, hol, years)
    cfgfile = getattr(a, "config", None) or config_path()
    if a.json:
        print(json.dumps(dict(st, upcoming_holidays=up, known_years=sorted(years), unknown_years=unknown,
                              holiday_note=note, config=cfgfile if os.path.isfile(cfgfile) else None),
                         indent=2, ensure_ascii=False))
        return 0 if st["open"] else 1
    word = {"open": "OPEN", "pre-open": "PRE-OPEN"}.get(st["phase"], "closed")
    print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], word, phase_words(st)))
    if st["phase"] != "closed-day":
        if (st["opens"], st["closes"]) == NORMAL:
            print("Today's session: %s to %s IST (pre-open 09:00 to 09:15, orders 09:00 to 09:08)." % NORMAL)
        else:
            print("Today's session: %s to %s IST (%s)." % (st["opens"], st["closes"], st["reason"] or "special session"))
    if not st["open"]:
        print("Next open: %s IST." % day_words(st["next_open"]))
    if up:
        print("Upcoming NSE holidays: %s." % "; ".join("%s %s %s" % (h["weekday"][:3], h["date"], h["name"]) for h in up))
    else:
        print("No %sNSE holidays on a weekday in the next %d days." % ("known " if unknown else "", UPCOMING_DAYS))
    if unknown:
        print(unknown_line(unknown))
    if note:
        print("Holiday list: %s." % note)
    print(config_line(cfgfile))
    return 0 if st["open"] else 1


def holidays_if_stale(a, now, sdir):
    """holidays --refresh --if-stale: refresh NSE's list only when nse_common.holidays_stale says
    so. Silent when it is fresh. One line when it refreshed or could not. Always exit 0."""
    cfg = load_cfg(getattr(a, "config", None) or config_path())
    stale, why = nc.holidays_stale(sdir, nc.to_ist(now).date(), cfg, now)
    if not stale:
        return 0
    try:
        if a.offline:
            doc, source = _read(a.offline, None), a.offline
        else:
            doc, source = fetch_holiday_master(), HOLIDAY_URL
        cache, got = refresh_holidays(doc, sdir, now, source)
        print("Refreshed NSE's holiday list (%s): %s, wrote %s." % (
            why, " and ".join(str(y) for y in got), os.path.join(sdir, nc.HOLIDAY_CACHE)))
    except AlertError as e:
        print("Could not refresh NSE's holiday list (%s): %s" % (why, e))
    return 0


def cmd_holidays(a):
    now = parse_now(a.now)
    sdir = a.state_dir or state_dir()
    cfgfile = a.config or config_path()
    if a.if_stale:
        return holidays_if_stale(a, now, sdir)
    note = ""
    if a.refresh:
        if a.offline:
            try:
                with open(a.offline, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError) as e:
                raise AlertError("could not read %s: %s" % (a.offline, e))
            source = a.offline
        else:
            doc = fetch_holiday_master()
            source = HOLIDAY_URL
        cache, got = refresh_holidays(doc, sdir, now, source)
        path = os.path.join(sdir, nc.HOLIDAY_CACHE)
        print("Fetched NSE's capital-market holiday list for %s: %d date(s). Wrote %s." % (
            " and ".join(str(y) for y in got), sum(1 for k in cache["holidays"] if int(k[:4]) in got), path))
        cfg, hol, years = calendar(a)
    else:
        cfg, hol, years, note = ensured_calendar(a, now)
    want = a.year or nc.to_ist(now).year
    try:
        cache = nc.read_json(os.path.join(sdir, nc.HOLIDAY_CACHE), {}) or {}
    except ValueError:
        cache = {}
    rows = [(k, v) for k, v in sorted(hol.items()) if k[:4] == str(want)]
    known = want in years
    if a.json:
        print(json.dumps({"year": want, "known": known, "holidays": dict(rows), "known_years": sorted(years),
                          "holiday_note": note, "config": cfgfile if os.path.isfile(cfgfile) else None},
                         indent=2, ensure_ascii=False))
        return 0 if known else 1
    if note:
        print("Holiday list: %s." % note)
    if not known:
        print("NSE's %d holiday list is not known here yet. NSE publishes the next year's list around December: "
              "run alerts.py holidays --refresh then, or put the dates in \"extra_holidays\" and the year in "
              "\"holiday_years\" in trader/config.json. "
              "Until then every weekday of %d counts as a trading day." % (want, want))
        for k, v in rows:
            print("  %s  %s  (from extra_holidays)" % (day_words(k), v))
        print(config_line(cfgfile))
        return 1
    if isinstance(cache, dict) and want in (cache.get("years") or []):
        where = "fetched %s from NSE's holiday-master" % str(cache.get("fetched_at") or "")[:10]
    elif want in nc.HOLIDAY_YEARS:
        where = "built in, from NSE's holiday-master"
    else:
        where = "from \"extra_holidays\" and \"holiday_years\" in your config"
    print("NSE trading holidays %d (capital market, %s):" % (want, where))
    for k, v in rows:
        d = datetime.date.fromisoformat(k)
        print("  %s  %s%s" % (day_words(k), v, "  (a weekend: closed anyway)" if d.weekday() >= 5 else ""))
    print("Weekends are closed too. Special sessions (Muhurat trading) come from \"special_sessions\" in "
          "trader/config.json.")
    print(config_line(cfgfile))
    return 0


def cmd_quote(a):
    syms = clean_symbols(list(a.symbols) + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        slim = {s: {k: v for k, v in q.items() if k != "bars"} for s, q in quotes.items()}   # the bars are long
        print(json.dumps({"quotes": slim, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1), rs(q["prev_close"]))
                when = nc.ist_text(datetime.datetime.strptime(q["time"], "%Y-%m-%dT%H:%M:%SZ")) if q.get("time") else ""
                print("%-10s %s%s  %s  [%s]" % (s, rs(q["price"]), chg, when, q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    cfg, hol, years = calendar(a)
    status = nc.market_status(now, cfg, hol, years)
    watched = [r for r in rules if active(r, status["date"])]
    syms = []
    for r in watched:
        if r.get("type") not in NO_QUOTE:
            s = nc.nse_symbol(r.get("symbol", ""))
            if s and s not in syms:
                syms.append(s)
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    if not isinstance(state, dict):
        state = {}
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        nc.write_json(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        cfgfile = getattr(a, "config", None) or config_path()
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()},
                          "config": cfgfile if os.path.isfile(cfgfile) else None}, indent=2, ensure_ascii=False))
    else:
        word = {"open": "OPEN", "pre-open": "PRE-OPEN"}.get(status["phase"], "closed")
        print("NSE %s (%s IST, %s)." % (word, status["now_ist"], phase_words(status)))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, rs(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        print(config_line(getattr(a, "config", None) or config_path()))
        if rings:
            print("An alert is information, not an order. On Kite the stop is a resting SL sell and the target is "
                  "only this alert (no OCO for MIS): at the target, cancel the stop, then sell. Record any fill "
                  "with nse-broker (ticket.py fill). To close a position yourself: ticket.py flatten, then nse-risk "
                  "and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    stamp = nc.utc_iso(parse_now(a.now))

    def arm(ids):
        """Rules count bars from the moment they are added (evaluate's "armed")."""
        state = _read(a.state, {})
        if not isinstance(state, dict):
            state = {}
        for i in ids:
            state.setdefault("rung", {}).pop(i, None)
            state.setdefault("armed", {})[i] = stamp
        nc.write_json(a.state, state)

    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run nse-exit first)" % a.plan)
        cfg = load_cfg(a.config)
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        arm(ids)
        nc.write_json(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r, ensure_ascii=False)))
        print("The target alert is the exit order's trigger: Kite has no OCO for MIS, so when it rings, cancel "
              "the stop, then sell. Run alerts.py check every few minutes while the trade is open (a scheduled "
              "task or /loop): a check also looks at the 5-minute bars since the previous one, so a spike "
              "through the stop or the target between two checks still rings. Free quotes are delayed; the SL "
              "order at the broker is what actually stops you out.")
        print(config_line(a.config))
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = clean_symbols([a.symbol])[0]
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = clean_symbols([a.symbol])[0]
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    nc.write_json(a.rules, doc)
    arm([rule["id"]])
    print("Added %s: %s" % (rule["id"], json.dumps(rule, ensure_ascii=False)))
    if rule["type"] == "target":
        print("When it rings: Kite has no OCO for MIS, so cancel the stop, then sell.")
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}, ensure_ascii=False),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    state.setdefault("armed", {})[a.id] = nc.utc_iso(parse_now(a.now))     # bars from now on only
    nc.write_json(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (nse-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    rules_default = nc.tpath("alerts.json")                 # one trader folder, wherever Claude Code started
    state_default = nc.tpath("state", "alerts.json")
    prices_default = nc.tpath("prices.json")

    def cal(p):
        p.add_argument("--config", default=nc.tpath("config.json"),
                       help="special_sessions and extra_holidays (default: the trader folder's config.json)")
        p.add_argument("--state-dir", default=nc.tpath("state"),
                       help="where nse-holidays.json lives (default: the trader folder's state/)")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-13 10:05"')
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open? next open, holidays")
    cal(p)
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("holidays", help="NSE trading holidays; --refresh fetches NSE's list")
    cal(p)
    p.add_argument("--refresh", action="store_true", help="fetch NSE's holiday-master into the cache")
    p.add_argument("--if-stale", action="store_true",
                   help="with --refresh: only when the list is stale; silent when fresh; always exit 0")
    p.add_argument("--offline", metavar="FILE", help="a saved holiday-master JSON instead of the network")
    p.add_argument("--year", type=int)
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    p.add_argument("--plan", default=nc.tpath("plan.json"))
    p.add_argument("--write", nargs="?", const=prices_default, metavar="PATH",
                   help="save to prices.json (default: the trader folder's prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    cal(p)
    p.add_argument("--rules", default=rules_default)
    p.add_argument("--state", default=state_default)
    p.add_argument("--prices-out", default=prices_default)
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default=rules_default)
    p.add_argument("--state", default=state_default)
    p.add_argument("--now", help="the time the rule starts to count bars from (default: the real clock)")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default=nc.tpath("plan.json"))
    p.add_argument("--config", default=nc.tpath("config.json"), help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that keeps its distance before the close on a special session")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default=rules_default)
    p.add_argument("--state", default=state_default)
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default=state_default)
    p.add_argument("--now", help="the time the rule starts to count bars from (default: the real clock)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "holidays": cmd_holidays, "quote": cmd_quote, "check": cmd_check,
                "add": cmd_add, "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
