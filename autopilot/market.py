"""Market data for the paper autopilot's measurements: the regime tags on every signal and the daily bar
archive. Yahoo's public chart endpoint, standard library only, polite (one request at a time).

Nothing here decides a trade. The tags are recorded on each signal so the report can split the results by
them ("did ORB do better when the Nifty was above its 50-day average?"); the archive keeps the bars Yahoo
drops after 30 days (1-minute) and 60 days (5-minute), so the setups can be tested over years later.
"""

import datetime
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import autopilot_common as ac

nc = ac.nc
HOSTS = ("query2", "query1")
NIFTY, VIX = "^NSEI", "^INDIAVIX"


class MarketError(Exception):
    pass


def chart_url(sym, interval, rng=None, period=None, host="query2"):
    """rng like "5d", or period=(start_epoch, end_epoch) for a window (Yahoo serves 1-minute bars 7 days at a
    time, from the last 30 days)."""
    q = {"interval": interval}
    if period:
        q["period1"], q["period2"] = str(int(period[0])), str(int(period[1]))
    else:
        q["range"] = rng or "1d"
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?%s" % (
        host, urllib.parse.quote(nc.yahoo_symbol(sym), safe=""), urllib.parse.urlencode(q))


def fetch_chart(sym, interval="5m", rng=None, period=None, timeout=20):
    """A Yahoo v8 chart doc (from Kite when logged in, see nse_common.kite_answer). Tries the other
    Yahoo host once; raises MarketError with the reason."""
    served = nc.kite_answer(chart_url(sym, interval, rng, period))
    if served is not None:
        return json.loads(served)
    last = None
    for host in HOSTS:
        req = urllib.request.Request(chart_url(sym, interval, rng, period, host),
                                     headers={"User-Agent": nc.UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.loads(r.read().decode("utf-8"))
            if (doc.get("chart") or {}).get("result"):
                return doc
            last = ((doc.get("chart") or {}).get("error") or {}).get("description") or "no result"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = getattr(e, "reason", None) or e
    raise MarketError("Yahoo chart %s %s: %s" % (sym, interval, last))


def rows(doc):
    """[(epoch, open, high, low, close, volume)] from a chart doc, bars without a close dropped."""
    try:
        r = doc["chart"]["result"][0]
        ts = r.get("timestamp") or []
        q = r["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for i, t in enumerate(ts):
        def at(k):
            v = (q.get(k) or [None] * len(ts))
            return v[i] if i < len(v) else None
        c = at("close")
        if c is None:
            continue
        out.append((int(t), at("open"), at("high"), at("low"), float(c), at("volume") or 0))
    return out


# ---------------------------------------------------------------- the regime tags

def daily_closes(fetch, sym, before_day):
    """Daily closes of `sym` for sessions before `before_day` (a date): today's half-formed bar never counts."""
    doc = fetch(sym, "1d", "1y")
    out = []
    for t, _o, _h, _l, c, _v in rows(doc or {}):
        if nc.ist_from_epoch(t).date() < before_day:
            out.append(c)
    return out


def regime(fetch, day):
    """{"nifty_trend": "up"|"down", "nifty_vs_50d_pct", "vix", "vix_pctile", "vix_band": "low"|"mid"|"high"}
    from the sessions before `day`. A part that cannot be fetched is left out and named in "errors"."""
    out, errors = {}, []
    try:
        closes = daily_closes(fetch, NIFTY, day)
        if len(closes) >= 50:
            sma = sum(closes[-50:]) / 50.0
            out["nifty_close"] = round(closes[-1], 2)
            out["nifty_vs_50d_pct"] = round(100 * (closes[-1] / sma - 1), 2)
            out["nifty_trend"] = "up" if closes[-1] > sma else "down"
        else:
            errors.append("Nifty 50: %d daily closes, need 50" % len(closes))
    except Exception as e:                           # a tag is never worth stopping the day for
        errors.append("Nifty 50: %s" % e)
    try:
        vix = daily_closes(fetch, VIX, day)
        if len(vix) >= 60:
            last = vix[-1]
            pct = sum(1 for v in vix if v <= last) / float(len(vix))
            out["vix"] = round(last, 2)
            out["vix_pctile"] = round(pct, 3)
            out["vix_band"] = "high" if pct >= 0.7 else ("low" if pct <= 0.3 else "mid")
        else:
            errors.append("India VIX: %d daily closes, need 60" % len(vix))
    except Exception as e:
        errors.append("India VIX: %s" % e)
    if errors:
        out["errors"] = errors
    return out


def index_move(fetch, day, upto_hhmm):
    """The Nifty 50 from the day's first 5-minute bar's open to the close of the bar that closed at
    `upto_hhmm` (IST "HH:MM", the scan's bar close), as {"pct", "tag": "up"|"down"}; None without data."""
    doc = fetch(NIFTY, "5m", "1d")
    bars = [(nc.ist_from_epoch(t), o, c) for t, o, _h, _l, c, _v in rows(doc or {})]
    bars = [b for b in bars if b[0].date() == day]
    if not bars or bars[0][1] is None:
        return None
    end = datetime.datetime.combine(day, nc.parse_hhmm(upto_hhmm))
    done = [b for b in bars if b[0] + datetime.timedelta(minutes=5) <= end]
    if not done:
        return None
    pct = done[-1][2] / float(bars[0][1]) - 1
    return {"pct": round(100 * pct, 3), "tag": "up" if pct > 0 else "down"}


# ---------------------------------------------------------------- the bar archive

def archive_path(home, interval, day, sym):
    return os.path.join(home, "archive", interval, day, nc.nse_symbol(sym).replace("^", "_") + ".json.gz")


def split_by_day(sym, interval, doc):
    """{date: [(epoch, o, h, l, c, v)]} for the bars of a chart doc, by IST session date."""
    days = {}
    for r in rows(doc):
        days.setdefault(nc.ist_from_epoch(r[0]).date().isoformat(), []).append(r)
    return days


def write_day(home, sym, interval, day, bars, fetched_at):
    """Keep the fuller copy: a day is (re)written only when this fetch has more bars than the file."""
    path = archive_path(home, interval, day, sym)
    old = read_day(path)
    if old is not None and len(old.get("bars") or []) >= len(bars):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"symbol": nc.nse_symbol(sym), "interval": interval, "date": day, "source": "yahoo",
           "fetched_at": fetched_at, "columns": ["epoch", "open", "high", "low", "close", "volume"],
           "bars": [list(b) for b in bars]}
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return True


def or_volume(rows):
    """Volume of the opening range's 5-minute bars (09:15-09:30) from archive rows. Yahoo's 09:15 bar carries no
    volume, so in practice this is 09:20-09:30, the same every day: a ratio of two of them is fair."""
    return sum((r[5] or 0) for r in rows if "09:15" <= nc.ist_from_epoch(r[0]).strftime("%H:%M") < "09:30"
               and None not in r[1:5])


def opening_rvol(home, fetch, sym, day, history=20, min_days=5):
    """Today's opening-range volume over its median for the `history` archived days before `day` (None with fewer
    than `min_days` of them, or no volume today). research.py found the signals with 1.5x or more the best."""
    import statistics
    root = os.path.join(home, "archive", "5m")
    past = []
    for d in sorted(x for x in (os.listdir(root) if os.path.isdir(root) else []) if x < day)[::-1]:
        doc = read_day(archive_path(home, "5m", d, sym))
        v = or_volume((doc or {}).get("bars") or [])
        if v:
            past.append(v)
        if len(past) >= history:
            break
    if len(past) < min_days:
        return None
    today = [r for r in rows(fetch(sym, "5m", "1d")) if nc.ist_from_epoch(r[0]).date().isoformat() == day]
    v = or_volume(today)
    return round(v / statistics.median(past), 2) if v else None


def rvol_band(x):
    if x is None:
        return None
    return "1.5x or more" if x >= 1.5 else ("0.8x to 1.5x" if x >= 0.8 else "under 0.8x")


def read_day(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, EOFError):
        return None


def archive(home, symbols, fetch, plan, pause=0.4, sleep=time.sleep, now=None):
    """Fetch each symbol for each (interval, rng-or-period) in `plan` and file the bars by day. Returns
    {"symbols", "requests", "failed": {sym: why}, "written": n day-files, "days": [dates]}."""
    stamp = nc.utc_iso(now)
    out = {"symbols": len(symbols), "requests": 0, "failed": {}, "written": 0, "days": set()}
    for sym in symbols:
        for interval, rng, period in plan:
            if out["requests"]:
                sleep(pause)
            out["requests"] += 1
            try:
                doc = fetch(sym, interval, rng, period)
            except Exception as e:
                out["failed"][sym] = ac.tail(str(e), 160)
                continue
            for day, bars in split_by_day(sym, interval, doc).items():
                if write_day(home, sym, interval, day, bars, stamp):
                    out["written"] += 1
                out["days"].add(day)
    out["days"] = sorted(out["days"])
    return out


def daily_plan():
    """What the end of day fetches: the last 5 sessions, so a day missed (the Mac was off) is filled later."""
    return [("1m", "5d", None), ("5m", "5d", None)]


def backfill_plan(now=None):
    """Everything Yahoo still has: 60 days of 5-minute bars, and 1-minute bars over the last 28 days in four
    windows just under 7 days each (Yahoo's limit for one 1-minute request)."""
    end = int((now or nc.utc_now()).replace(tzinfo=datetime.timezone.utc).timestamp())
    week = 7 * 86400 - 60
    plan = [("5m", "60d", None)]
    for k in range(3, -1, -1):
        plan.append(("1m", None, (end - (k + 1) * week, end - k * week)))
    return plan
