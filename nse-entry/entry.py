#!/usr/bin/env python3
"""Entry Timer for nse-entry: watches an NSE setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check RELIANCE                          # opening-range breakout, 5-minute bars
    python3 entry.py check RELIANCE --now "2026-11-13 10:05" --offline RELIANCE.json
    python3 entry.py check RELIANCE --setup level --level 1340  # first close above a price
    python3 entry.py check M&M --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch RELIANCE --every 30 --until 14:30  # one line per closed bar
    python3 entry.py watchlist                                # every hit in trader/watchlist.json

Setups (long side only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute bars,
         or fifteen 1-minute bars). It triggers on the first COMPLETED bar after
         the range that CLOSES above the range high. A wick above is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from nse-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars. Yahoo stamps the 09:15 bar of most
         NSE charts with volume 0 (the pre-open auction's volume is not in it),
         so the average leaves zero-volume bars out.

A trigger's time is the trigger bar's CLOSE time in IST (the 10:00 bar closes at
10:05) and its price is that close. Only bars on the interval grid count (Yahoo's
live tick, e.g. 10:49:45, is dropped) and a bar counts only once it has closed:
now >= its start + the interval.

The NSE session (nse_common): pre-open 09:00-09:15 IST (orders 09:00-09:08),
normal market 09:15-15:30 IST, Monday to Friday, minus NSE's trading holidays
(the 2026 list is built in; other years come from nse-alerts' cache or config
"extra_holidays"). A special session in config "special_sessions" (Muhurat
trading) opens a closed day, and the entry window keeps its distance from that
session's open and close.

Statuses: triggered; waiting (before the open, pre-open, range still forming,
or no close above the level yet); expired (no trigger by no_entries_after,
default 14:30 IST); no-data (the NSE is closed today or Yahoo sent no bars).

trader/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (14:30), IST. Defaults come from nse_common.DEFAULT_CONFIG.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m
  (M&M is M%26M.NS); falls back to query1 on HTTP 429 or 5xx; a 429 gets one
  retry after a pause. --offline PATH reads a saved chart JSON (.json or
  .json.gz) instead, or PATH/<SYMBOL>.json when PATH is a folder. Without
  --now, an offline file is read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
WINDOW_KEYS = ("no_entries_before", "no_entries_after")
EPOCH = datetime.datetime(1970, 1, 1)
REGULAR = (nc.SESSION_OPEN, nc.SESSION_CLOSE)


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def utc_now():
    return nc.utc_now()


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return nc.utc_iso(utc)


def parse_now(text):
    """--now -> naive UTC. "2026-11-13 10:05" (or ...T10:05, or just 10:05 for today)
    is IST; a trailing Z or an offset makes it absolute."""
    if not text:
        return utc_now()
    t = str(text).strip()
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", t):
        t = "%s %s" % (nc.to_ist(utc_now()).date().isoformat(), t.zfill(5) if len(t) <= 5 else t.zfill(8))
    try:
        return nc.parse_now(t)
    except ValueError as e:
        raise EntryError(str(e))


def parse_hhmm(text, what):
    try:
        return nc.parse_hhmm(text, what)
    except ValueError as e:
        raise EntryError(str(e))


def hm(t):
    """10:05, 09:30: the contract's wall-clock form (IST)."""
    return "%02d:%02d" % (t.hour, t.minute)


def money(x):
    """1334.0 -> '₹1,334.00'."""
    return nc.inr(x, keep_zeros=True)


def calendar(cfg, config_path=None):
    """(holidays, known years): the 2026 list, nse-alerts' cache next to the config, extra_holidays."""
    if config_path:
        return nc.load_holidays(cfg, state_dir=os.path.join(os.path.dirname(config_path) or ".", "state"))
    return nc.load_holidays(cfg)


def entry_window(day, cfg):
    """(no entries before, no entries after) as naive IST datetimes. In a special session
    the window keeps its distance from that session's open and close."""
    before = parse_hhmm(cfg.get("no_entries_before") or "09:30", "no_entries_before")
    after = parse_hhmm(cfg.get("no_entries_after") or "14:30", "no_entries_after")
    o, c = nc.session_times(day, cfg)
    b_dt = datetime.datetime.combine(day, before)
    a_dt = datetime.datetime.combine(day, after)
    if (o, c) != REGULAR:
        b_dt = datetime.datetime.combine(day, o) + (b_dt - datetime.datetime.combine(day, nc.SESSION_OPEN))
        a_dt = datetime.datetime.combine(day, c) - (datetime.datetime.combine(day, nc.SESSION_CLOSE) - a_dt)
    return b_dt, a_dt


# ---------------------------------------------------------------- Yahoo

def chart_url(sym, rng, interval, host="query2"):
    return nc.yahoo_chart_url(sym, rng, interval, host)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(sym, rng, interval):
    """GET one chart: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
        url = chart_url(sym, rng, interval, host)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except (OSError, http.client.HTTPException) as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": nc.nse_symbol(meta.get("symbol") or sym), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym, nc.yahoo_symbol(sym)):
            for ext in (".json", ".json.gz"):
                p = os.path.join(path, name + ext)
                if os.path.exists(p):
                    return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None):
    """Bars for one NSE symbol, from Yahoo (range=1d for today) or a saved file."""
    sym = nc.nse_symbol(sym)
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
    else:
        today = today or nc.to_ist(utc_now()).date()
        text = yahoo_get(sym, chart_range(day or today, today, interval), interval)
    ch = parse_chart(text, sym)
    if ch["symbol"] != sym:
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    """trader/config.json over nse_common's defaults. The window times must be HH:MM."""
    try:
        cfg = nc.load_config(path)
    except (ValueError, OSError) as e:
        raise EntryError("%s is not valid JSON: %s" % (path, e))
    for k in WINDOW_KEYS:
        parse_hhmm(cfg.get(k), k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, holidays=None, years=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    cfg = cfg if cfg is not None else nc.load_config(None)
    if holidays is None:
        holidays, years = nc.load_holidays(cfg, state_dir=None)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = nc.to_ist(now_utc)
    day = local.date()
    res = {"symbol": chart["symbol"], "exchange": "NSE", "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"),
           "status": "waiting", "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "calendar_known": day.year in set(years or nc.HOLIDAY_YEARS),
           "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = nc.ist_from_epoch(dt).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = nc.trading_day(day, holidays, cfg)
    if not ok:
        nd = nc.next_trading_day(day, holidays, cfg)
        res.update(status="no-data", phase="closed", reason="the NSE is closed today (%s)" % why,
                   next_open="%s %s" % (nd.isoformat(), hm(nc.session_times(nd, cfg)[0])))
        return res
    o, c = nc.session_times(day, cfg)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    sp = nc.special_session(day, cfg)
    before, cutoff = entry_window(day, cfg)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), special_session=sp[2] if sp else None,
               no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        et = nc.ist_from_epoch(b["t"])
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (nc.ist_from_epoch(dt).strftime("%H:%M:%S"),
                                                                       hm(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        rng = " and the %d-minute range is set at %s" % (or_minutes, hm(or_end)) if setup == "orb" else ""
        if (o, c) == REGULAR and local.time() >= nc.PREOPEN_OPEN:
            res.update(phase="pre-open", reason="pre-open call auction (09:00-09:15 IST, orders until 09:08): "
                                                "the first bar starts at 09:15%s" % rng)
        else:
            res.update(phase="before-open", reason="the NSE opens at %s IST%s%s" % (
                hm(o), " (pre-open from 09:00)" if (o, c) == REGULAR else "", rng))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    traded = [b["v"] for b in or_bars if b["v"] > 0]
    avg_vol = (sum(traded) / float(len(traded))) if traded else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end,
                                "avg_volume": int(round(avg_vol)) if avg_vol is not None else None}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, hm(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)
    res["tick"] = format(nc.tick_size(lvl, chart["symbol"], cfg), "f")

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (hm(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": hm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < nc.to_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a tick or two"
                                % nc.ist_from_epoch(dt).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    what = "close above the range high %s" % money(lvl) if setup == "orb" else "close above %s" % money(lvl)
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, hm(cutoff), " (%s, %s-%s)" % (sp[2], hm(o), hm(c)) if sp else ""))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            hm(last["et"]), money(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            hm(last["et"]), money(last["c"]), hm(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
            money(last["c"]), hm(last["et"]), money(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % money(lvl) if setup == "orb" else "the level %s" % money(lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason). Times are IST HH:MM."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "nse-entry", "exchange": "NSE", "symbol": res["symbol"], "setup": res["setup"],
            "side": "buy", "date": res["date"], "status": res["status"], "time": t.get("time"),
            "bar_start": t.get("bar_start"), "bar_index": t.get("bar_index"), "price": t.get("price"),
            "level": res.get("level"), "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"before-open": "NOT OPEN YET", "pre-open": "PRE-OPEN", "forming": "RANGE FORMING",
               "closed": "MARKET CLOSED"}
SHORT = {"before-open": "not open", "pre-open": "pre-open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % money(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s IST  %s on %s bars (Yahoo, free, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            hm(start), hm(end), money(orr["high"]), money(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed above %s %s (%+.2f%%%s)." % (
            t["time"], money(t["price"]), t["bar_start"], t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", money(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], money(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /nse-exit "
                   "(exit.py plan --from trader/entry.json). Tick at this price: %s." % money(res["tick"]))
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"], money(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("special_session"):
        out.append("Special session today: %s, %s-%s IST; entries %s to %s." % (
            res["special_session"], res["opens"], res["closes"], res["no_entries_before"], res["no_entries_after"]))
    if not res.get("calendar_known"):
        out.append("NSE's %s holiday list is not loaded (nse-alerts: alerts.py holidays --refresh): weekdays "
                   "are treated as trading days." % res["date"][:4])
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = hm(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (money(min(x["l"] for x in seen)), money(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (money(orr["high"]), money(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s, %s" % (
            money(lvl), t["pct_over"], t["time"], money(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % money(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (money(lvl - b["c"]), money(lvl))
    return "%5s bar  closed %s  %s" % (start, money(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    cfg = opts.get("cfg") if opts.get("cfg") is not None else nc.load_config(None)
    holidays = opts.get("holidays")
    if holidays is None:
        holidays, _ = nc.load_holidays(cfg, state_dir=None)
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = nc.to_ist(now)
        day = local.date()
        ok, why = nc.trading_day(day, holidays, cfg)
        if not ok:
            emit("%s: the NSE is closed today (%s). Nothing to watch." % (day.isoformat(), why))
            return 1, None
        o, c = nc.session_times(day, cfg)
        open_dt = datetime.datetime.combine(day, o)
        close_dt = datetime.datetime.combine(day, c)
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                if local >= open_dt:
                    where = "open, no bar closed yet"
                elif (o, c) == REGULAR and local.time() >= nc.PREOPEN_OPEN:
                    where = "pre-open call auction"
                else:
                    where = "not open yet"
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    hm(local), where, hm(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = nc.to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (hm(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or "14:30", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _symbol(text):
    sym = nc.nse_symbol(text)
    if not nc.is_nse_symbol(sym):
        raise EntryError("%r is not an NSE symbol (like RELIANCE, M&M or BAJAJ-AUTO)" % text)
    return sym


def _opts(a, cfg, holidays, years):
    if a.side in ("sell", "short"):
        raise EntryError("nse-entry plans longs only: no short entries (and no intraday shorts in this pack "
                         "unless your config allows them). Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg, "holidays": holidays, "years": years}


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = nc.to_ist(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, nc.to_ist(now).date(), a.offline, real_today), now


def _setup(a):
    cfg = read_config(a.config)
    holidays, years = calendar(cfg, a.config)
    return cfg, _opts(a, cfg, holidays, years)


def cmd_check(a):
    _, opts = _setup(a)
    sym = _symbol(a.symbol)
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        nc.write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2, ensure_ascii=False))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    _, opts = _setup(a)
    sym = _symbol(a.symbol)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = nc.to_ist(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, nc.to_ist(now).date(), a.offline, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(nc.to_ist(now_fn()).date(), t)

    def on_trigger(res, now):
        nc.write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    _, opts = _setup(a)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /nse-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <SYMBOL>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = nc.nse_symbol(h["symbol"])
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2, ensure_ascii=False))
    else:
        print("%-10s  %-9s  %-5s  %-11s  %-11s  %-11s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST",
                                                              "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-10s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-10s  %-9s  %-5s  %-11s  %-11s  %-11s  %s" % (
                s, status[:9], t.get("time") or "-", money(t["price"]) if t else "-",
                money(r["level"]) if r.get("level") is not None else "-",
                money(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /nse-exit." % (n, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Entry Timer (nse-entry): watches an NSE setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15, from 09:15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-13 10:05"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default=os.path.join("trader", "config.json"))

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const=os.path.join("trader", "entry.json"), metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default=os.path.join("trader", "entry.json"), help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default=os.path.join("trader", "watchlist.json"))
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
