#!/usr/bin/env python3
"""Chart Scanner for nse-scanner: scan NSE charts for setups. Standard library only.

    python3 scan.py universe                          # Nifty 500 from NSE archives -> trader/universe.txt
    python3 scan.py universe fno                      # or nifty50, fno, all
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --universe nifty50          # or --symbols RELIANCE,M&M,BAJAJ-AUTO, --watchlist FILE
    python3 scan.py orb                               # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:30 --or-minutes 15    # cut the clock at 10:30 IST
    python3 scan.py orb --offline wall5m.json.gz --at 10:05 --lists tests/fixtures/nse-scanner

universe  Builds the list from NSE's archives (nsearchives.nseindia.com/content/):
            nifty50   indices/ind_nifty50list.csv
            nifty500  indices/ind_nifty500list.csv (the default)
            fno       fo/fo_mktlots.csv, the stock rows only (the index rows
                      such as NIFTY and BANKNIFTY are skipped)
            all       equities/EQUITY_L.csv, series EQ only
          Every list then goes through equities/sec_list.csv (NSE's daily list
          of tradable securities with their price bands):
            out  a symbol that is not in sec_list (suspended, or a placeholder)
            out  no row in an allowed series (config allowed_series, EQ):
                 BE/BZ are trade-for-trade (delivery only, no intraday), SM/ST
                 are SME, RR is a REIT, IV an InvIT
            out  a 2% or 5% circuit band (under config min_circuit_band_pct,
                 10); "No Band" (F&O stocks, dynamic bands) passes
            out  graded surveillance, a Remarks like "GSM STAGE - 0" (config
                 block_surveillance, true)
          It says how many it dropped and why. Files are cached for the day in
          trader/cache/ (--refresh downloads them again).

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity, in rupees:
            --min-price 20      the last close, in rupees
            --min-value-cr 10   traded value in crore (1 crore = 1,00,00,000):
                                the last session's price x volume from the spark
                                meta, or the 20-session average when the chart
                                has daily volumes (a saved v8 chart)
          Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 15:30 IST close.
          A breakout_20d hit carries "level", the prior 20-day closing high (the
          close it just beat), and every hit carries "name" when the company
          name is known (nse-entry's `watchlist --setup level` reads "level";
          nse-news reads "name"). Names come from EQUITY_L.csv (the --lists
          folder, or today's cache/EQUITY_L-<IST date>.csv; never downloaded
          just for this), else the universe list, else sec_list.csv.
          The traded-value floor needs a COMPLETE session's volume. Before
          09:15, after 15:30 and on a day the NSE is closed, Yahoo's spark
          volume is the last complete session's, and the floor uses it. While
          the session is running that volume is today's partial count, so a
          liquid stock would fail the floor early in the day: the floor is then
          checked on each hit's own daily chart (complete sessions only, the
          average of the last 20) and a hit that cannot be checked is kept and
          marked "value_checked": false, never dropped for a partial volume.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session: the 09:15, 09:20 and 09:25 bars.
          Stage 1: spark closes for every symbol; a chart goes on only if a
          completed bar after the range closed at or above the highest close of
          the range (a necessary condition), or when spark dropped some of its
          bars (it does that on busy NSE charts; not a halt), so the v8 chart
          decides. Stage 2: each candidate's v8 chart
          (real highs and lows): range high = the highest high of the range
          bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time (the 10:00 bar closes at 10:05). Stage 2 fetches at
          most --max-confirm (200) charts, the most traded first, and says so
          when it stops short. A chart is usable only when every 5-minute bar
          from 09:15 to the cutoff is there (a missing bar is a halt or no
          trades), except the last bars of the session from 15:15: Yahoo
          leaves them empty on most NSE charts. Floors: --min-price 20,
          --min-value-cr 0 (traded so far). No new hits after config
          no_entries_after (14:30), or --until.

Bars: null closes are dropped, a bar off the 5-minute grid (Yahoo's live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete (start + 5
minutes <= the clock). Times are IST (UTC+05:30). --now pins the clock
("2026-11-13 10:05", IST; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. A 429 or 5xx on one host
goes to the other (query1 / query2), one retry after a pause on a 429. A polite
pause between requests (--pause). Partial results are reported as partial.

--offline PATH reads Yahoo files instead of the network: a folder of
<SYMBOL>.json v8 chart files or spark answers, or one file that is a spark
answer, a v8 chart, or a bundle {"RELIANCE": <v8 chart or chart result>, ...}.
.json.gz is fine. --lists DIR reads the NSE list files (ind_nifty50list.csv,
ind_nifty500list.csv, fo_mktlots.csv, EQUITY_L.csv, sec_list.csv, .gz fine)
from a folder; --sec-list FILE gives sec_list alone.

A scan is a list of charts that match a rule, not a list of stocks to buy.

Exit codes: 0 the scan found something; 1 it ran and found nothing, or could not
start (bad flags, no list); 3 the scan FAILED to read prices: more than half of
the symbols (or of the stage-2 candidate charts) could not be fetched. That is
not "no setups": it prints "scan failed: could not read prices (N of M symbols
failed)", leaves the existing watchlist.json untouched and writes nothing. When
a scan does write, watchlist.json records "scanned" and "errors" (the symbols
that could not be read, with up to five reasons in "error_samples").

Files live in the trader folder (nse_common.trader_home: $NSE_TRADER_HOME, else the
nearest trader/ with a config.json above the working folder, else
~/.day-trader-india, else ./trader). Each run says which config file it used, or
that the built-in defaults are in use.
"""

import argparse
import csv
import datetime
import gzip
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

ARCHIVES = "https://nsearchives.nseindia.com/content/"
LISTS = {
    "nifty50": ("indices/ind_nifty50list.csv", "Nifty 50"),
    "nifty500": ("indices/ind_nifty500list.csv", "Nifty 500"),
    "fno": ("fo/fo_mktlots.csv", "F&O stocks"),
    "all": ("equities/EQUITY_L.csv", "every NSE equity in series EQ"),
}
SEC_LIST = "equities/sec_list.csv"
DEFAULT_UNIVERSE = "nifty500"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
LATE_GAP_MIN = 15   # Yahoo leaves an NSE session's last 5-minute bars (15:15, 15:20) empty
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
VALUE_DAYS = 20     # sessions in the average traded value, when volumes exist
CRORE = 10000000.0
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
SERIES_NOTE = {"BE": "trade-for-trade, no intraday", "BZ": "trade-for-trade, no intraday",
               "SM": "SME", "ST": "SME", "SZ": "SME", "RR": "REIT", "IV": "InvIT"}

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


class ScanFailed(ScanError):
    """The scan could not read prices (not "no setups"): exit code 3, nothing written."""
    exit_code = 3

    def __init__(self, message, failed=0, total=0, samples=None):
        ScanError.__init__(self, message)
        self.failed, self.total, self.samples = failed, total, samples or {}


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """--now (IST wall clock, or ...Z for UTC) -> naive UTC. None -> the real clock."""
    try:
        return nc.parse_now(text)
    except ValueError as e:
        raise ScanError(str(e))


def parse_hhmm(text, flag="--at"):
    try:
        return nc.parse_hhmm(text, flag)
    except ValueError as e:
        raise ScanError(str(e))


def hm(t):
    """datetime or time -> '10:05' (24-hour IST)."""
    return "%02d:%02d" % (t.hour, t.minute)


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def today_ist():
    """The real IST date (cache files are per day). Tests swap this out."""
    return nc.to_ist(nc.utc_now()).date()


def calendar(cfg, config_path=None):
    """(holidays, known years) from nse_common: the 2026 list, nse-alerts' cache, extra_holidays."""
    state = os.path.join(os.path.dirname(config_path) or ".", "state") if config_path else None
    return nc.load_holidays(cfg, state_dir=state) if state else nc.load_holidays(cfg)


def config_line(path):
    """Which config this run used: nse-scanner does not guard money, so it runs on the built-in
    defaults without one, and says so."""
    if path and os.path.isfile(path):
        return "Config: %s." % path
    return "Config: none at %s, so the built-in defaults are in use." % path


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", "replace")


def _other_host(url):
    if "://query1." in url:
        return url.replace("://query1.", "://query2.", 1)
    return url.replace("://query2.", "://query1.", 1)


def yahoo_get(url):
    """GET a Yahoo Finance URL, then the other host (query1 / query2) on a 429 or 5xx,
    and one more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    served = nc.kite_answer(url)
    if served is not None:
        return served
    last = "no answer"
    path = url.split("finance.yahoo.com")[-1].split("?")[0]
    for attempt in (0, 1):
        saw_429 = False
        for u in (url, _other_host(url)):
            try:
                return http_get(u)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path))
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                last = str(getattr(e, "reason", e)) or e.__class__.__name__
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path))


def spark_url(symbols, rng, interval):
    return nc.yahoo_spark_url(symbols, rng, interval)


def chart_url(sym, rng, interval):
    return nc.yahoo_chart_url(sym, rng, interval)


# ---------------------------------------------------------------- Yahoo parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYMBOL: series}. Symbols Yahoo does not know are simply missing
    from the answer. RELIANCE.NS comes back as RELIANCE, M&M.NS as M&M."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), nc.nse_symbol(ysym))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8-sig") as fh:
            return fh.read()
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _is_result(doc):
    return isinstance(doc, dict) and "meta" in doc and ("timestamp" in doc or "indicators" in doc)


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc or _is_result(doc):
        sym = name_hint
        if not sym:
            try:
                meta = doc["meta"] if _is_result(doc) else (doc["chart"].get("result") or [{}])[0]["meta"]
                sym = meta["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = nc.nse_symbol(sym)
        if _is_result(doc):
            data[sym] = series_from_result(doc)
            return
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % (name_hint or "?"))


def load_offline(path):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({SYMBOL: series}, {SYMBOL: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc or _is_result(doc)):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- NSE lists

def _rows(text):
    """CSV text -> list of dicts with stripped keys and values (NSE pads both)."""
    rdr = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = None
    out = []
    for row in rdr:
        if not row or not any(c.strip() for c in row):
            continue
        if head is None:
            head = [h.strip() for h in row]
            continue
        out.append({h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(head)})
    if head is None:
        raise ScanError("an empty list file")
    return out, head


def parse_index_list(text):
    """ind_nifty50list.csv / ind_nifty500list.csv -> [(SYMBOL, company name)]."""
    rows, head = _rows(text)
    if "Symbol" not in head:
        raise ScanError("not an NSE index list (no Symbol column)")
    return [(r["Symbol"].upper(), r.get("Company Name", "")) for r in rows if r.get("Symbol")]


def parse_fo_mktlots(text):
    """fo_mktlots.csv -> [(SYMBOL, name)] of the stock underlyings. The first rows are
    indices (NIFTY, BANKNIFTY, ...); the stocks start after the row that says
    "Derivatives on Individual Securities"."""
    rows, head = _rows(text)
    if "SYMBOL" not in head:
        raise ScanError("not NSE's fo_mktlots.csv (no SYMBOL column)")
    out, stocks = [], False
    marker = any(r.get("UNDERLYING", "").lower().startswith("derivatives on individual") for r in rows)
    for r in rows:
        und, sym = r.get("UNDERLYING", ""), r.get("SYMBOL", "").upper()
        if und.lower().startswith("derivatives on individual"):
            stocks = True
            continue
        if marker and not stocks:
            continue
        if not marker and (und.upper().startswith("NIFTY") or sym.startswith("NIFTY") or sym.endswith("NIFTY")):
            continue
        if sym and sym != "SYMBOL":
            out.append((sym, und))
    return out


def parse_equity_l(text, series="EQ"):
    """EQUITY_L.csv (headers with leading spaces) -> [(SYMBOL, name)] of one series."""
    rows, head = _rows(text)
    if "SYMBOL" not in head or "SERIES" not in head:
        raise ScanError("not NSE's EQUITY_L.csv (no SYMBOL / SERIES columns)")
    return [(r["SYMBOL"].upper(), r.get("NAME OF COMPANY", "")) for r in rows
            if r.get("SYMBOL") and r.get("SERIES", "").upper() == series]


def parse_sec_list(text):
    """sec_list.csv -> {SYMBOL: [{series, band, remarks, name}, ...]} (a symbol can have several series)."""
    rows, head = _rows(text)
    if "Symbol" not in head or "Band" not in head:
        raise ScanError("not NSE's sec_list.csv (no Symbol / Band columns)")
    out = {}
    for r in rows:
        sym = r.get("Symbol", "").upper()
        if not sym:
            continue
        out.setdefault(sym, []).append({"series": r.get("Series", "").upper(), "band": r.get("Band", ""),
                                        "remarks": r.get("Remarks", "").strip(), "name": r.get("Security Name", "")})
    return out


def band_pct(text):
    """'5' -> 5.0, 'No Band' -> None (dynamic bands: F&O stocks)."""
    try:
        return float(str(text).strip().rstrip("%"))
    except ValueError:
        return None


def surveillance(remarks):
    """'GSM STAGE - 0' -> 'GSM STAGE - 0'; '-' -> None."""
    r = " ".join(str(remarks or "").split())
    return r if re.search(r"\b(GSM|ASM)\b", r.upper()) else None


def screen(symbols, sec, cfg):
    """Drop what sec_list says is not for intraday. -> (kept, {SYMBOL: reason key})."""
    allowed = [str(s).upper() for s in (cfg.get("allowed_series") or ["EQ"])]
    min_band = float(cfg.get("min_circuit_band_pct") or 0)
    block = bool(cfg.get("block_surveillance", True))
    kept, dropped = [], {}
    for sym in symbols:
        rows = sec.get(sym)
        if not rows:
            dropped[sym] = "not in sec_list"
            continue
        ok = [r for r in rows if r["series"] in allowed]
        if not ok:
            dropped[sym] = "series " + "/".join(sorted({r["series"] for r in rows}))
            continue
        r = ok[0]
        b = band_pct(r["band"])
        if b is not None and b < min_band:
            dropped[sym] = "%s%% band" % ("%g" % b)
            continue
        if block and surveillance(r["remarks"]):
            dropped[sym] = "surveillance (%s)" % surveillance(r["remarks"]).split()[0]
            continue
        kept.append(sym)
    return kept, dropped


def reason_counts(dropped):
    out = {}
    for why in dropped.values():
        out[why] = out.get(why, 0) + 1
    return out


def reason_words(key, n):
    if key.startswith("series "):
        s = key.split(" ", 1)[1]
        note = SERIES_NOTE.get(s)
        return "%s in series %s%s" % (_c(n), s, " (%s)" % note if note else "")
    if key.endswith("% band"):
        return "%s in a %s circuit band" % (_c(n), key[:-5])
    if key.startswith("surveillance"):
        return "%s under %s surveillance" % (_c(n), key[key.index("(") + 1:-1])
    if key == "not in sec_list":
        return "%s not in sec_list (suspended or a placeholder)" % _c(n)
    return "%s %s" % (_c(n), key)


def drop_summary(total, dropped, cfg):
    if not dropped:
        return "sec_list.csv: none of %s dropped (series %s, bands %s%%+ or No Band%s)." % (
            _c(total), "/".join(cfg.get("allowed_series") or ["EQ"]), _n(cfg.get("min_circuit_band_pct") or 0),
            ", no GSM" if cfg.get("block_surveillance", True) else "")
    counts = reason_counts(dropped)
    parts = [reason_words(k, v) for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return "sec_list.csv dropped %s of %s: %s." % (_c(len(dropped)), _c(total), ", ".join(parts))


def _archive_file(rel, a):
    """The NSE archive file `rel` as text: from --lists DIR, today's cache, or the network."""
    base = os.path.basename(rel)
    if getattr(a, "lists", None):
        for name in (base, base + ".gz"):
            p = os.path.join(a.lists, name)
            if os.path.isfile(p):
                return read_maybe_gz(p), p
        raise ScanError("no %s in %s" % (base, a.lists))
    stem, ext = os.path.splitext(base)
    day = today_ist().isoformat()
    cache = getattr(a, "cache", None)
    path = os.path.join(cache, "%s-%s%s" % (stem, day, ext)) if cache else None
    if path and os.path.isfile(path) and not getattr(a, "refresh", False):
        return read_maybe_gz(path), path
    url = ARCHIVES + rel
    try:
        text = http_get(url, timeout=60)
    except urllib.error.HTTPError as e:
        raise ScanError("NSE archives answered HTTP %d for %s" % (e.code, base))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise ScanError("could not reach nsearchives.nseindia.com for %s: %s" % (base, getattr(e, "reason", e)))
    if text.lstrip().startswith("<"):
        raise ScanError("NSE archives sent a web page, not %s: try again in a minute" % base)
    if path:
        os.makedirs(cache, exist_ok=True)
        for old in os.listdir(cache):
            if old.startswith(stem + "-") and old.endswith(ext) and old != os.path.basename(path):
                try:
                    os.remove(os.path.join(cache, old))
                except OSError:
                    pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text, url


def load_list(name, a):
    """'nifty500' -> ([(SYMBOL, name)], source)."""
    rel, _ = LISTS[name]
    text, src = _archive_file(rel, a)
    if name == "fno":
        return parse_fo_mktlots(text), src
    if name == "all":
        return parse_equity_l(text), src
    return parse_index_list(text), src


def load_sec(a):
    """{SYMBOL: rows} from --sec-list, --lists DIR, the cache or the network; None when offline
    without one (the screen is then skipped and the scan says so)."""
    if getattr(a, "sec_list", None):
        if not os.path.isfile(a.sec_list):
            raise ScanError("no sec_list file at %s" % a.sec_list)
        return parse_sec_list(read_maybe_gz(a.sec_list))
    if getattr(a, "lists", None):
        return parse_sec_list(_archive_file(SEC_LIST, a)[0])
    if getattr(a, "offline", None):
        cache = getattr(a, "cache", None)
        p = os.path.join(cache, "sec_list-%s.csv" % today_ist().isoformat()) if cache else None
        return parse_sec_list(read_maybe_gz(p)) if p and os.path.isfile(p) else None  # today's copy, no network
    return parse_sec_list(_archive_file(SEC_LIST, a)[0])


def equity_l_names(text):
    """EQUITY_L.csv -> {SYMBOL: company name}, every series (the first row of a symbol wins)."""
    rows, head = _rows(text)
    if "SYMBOL" not in head or "NAME OF COMPANY" not in head:
        raise ScanError("not NSE's EQUITY_L.csv (no SYMBOL / NAME OF COMPANY columns)")
    out = {}
    for r in rows:
        sym, name = r.get("SYMBOL", "").upper(), r.get("NAME OF COMPANY", "").strip()
        if sym and name and sym not in out:
            out[sym] = name
    return out


def local_equity_l(a):
    """EQUITY_L.csv text if the scanner already has it, else None. It looks in --lists DIR and in
    today's cache file (cache/EQUITY_L-<IST date>.csv, the file `universe all` writes and nse-news
    shares), and never goes to the network for it."""
    paths = []
    if getattr(a, "lists", None):
        paths += [os.path.join(a.lists, "EQUITY_L.csv"), os.path.join(a.lists, "EQUITY_L.csv.gz")]
    cache = getattr(a, "cache", None)
    if cache:
        paths.append(os.path.join(cache, "EQUITY_L-%s.csv" % today_ist().isoformat()))
    for p in paths:
        if os.path.isfile(p):
            try:
                return read_maybe_gz(p)
            except (OSError, ValueError):
                continue
    return None


def company_names(a, universe_names=None, sec=None):
    """{SYMBOL: company name}: EQUITY_L.csv when it is on hand, else the universe list's names
    (Company Name in the index lists, UNDERLYING in fo_mktlots), else sec_list's Security Name."""
    names = {}
    for sym, rows in (sec or {}).items():
        pick = [r for r in rows if r.get("name")]
        if pick:
            names[sym] = pick[0]["name"].strip()
    names.update({k.upper(): v for k, v in (universe_names or {}).items() if v})
    try:
        text = local_equity_l(a)
        if text:
            names.update(equity_l_names(text))
    except ScanError:
        pass        # a broken copy is no reason to lose the other sources
    return names


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc, cfg=None, holidays=None, with_volume=False):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes (15:30 IST), anything dated after the clock dropped.
    with_volume -> [(date, close, volume or None)]."""
    now = nc.to_ist(now_utc)
    today = now.date()
    ok, _ = nc.trading_day(today, holidays, cfg)
    today_done = (not ok) or now.time() >= nc.session_times(today, cfg)[1]
    vols = series.get("v")
    out = {}
    for i, (ts, c) in enumerate(zip(series["t"], series["c"])):
        if c is None:
            continue
        d = nc.ist_from_epoch(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = (float(c), vols[i] if vols and i < len(vols) else None)  # a late duplicate (the live tick) wins
    if with_volume:
        return [(d, c, v) for d, (c, v) in sorted(out.items())]
    return [(d, c) for d, (c, _) in sorted(out.items())]


def session_in_progress(now_utc, cfg=None, holidays=None):
    """True while today's NSE session is running (09:15 to 15:30 on a trading day). Then Yahoo's
    meta volume is today's PARTIAL count, not a complete session's."""
    now = nc.to_ist(now_utc)
    ok, _ = nc.trading_day(now.date(), holidays, cfg)
    if not ok:
        return False
    o, c = nc.session_times(now.date(), cfg)
    return o <= now.time() < c


def traded_value(series, bars, partial_meta=False):
    """Rupees traded a session: the average close x volume of the last 20 COMPLETE sessions when
    the chart has daily volumes (bars come from daily_closes, which drops today's bar until the
    close), else the last session's price x volume from the spark meta. With partial_meta (the
    session is running) the meta volume is today's partial count and is never used: the answer is
    (None, "partial") when there are no complete sessions' volumes to average."""
    vb = [(c, v) for _, c, v in bars[-VALUE_DAYS:] if v]
    if vb:
        return sum(c * v for c, v in vb) / len(vb), "average of %d sessions" % len(vb)
    if partial_meta:
        return None, "partial"
    meta = series.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None and bars:
        price = bars[-1][1]
    vol = meta.get("regularMarketVolume")
    if price is None or vol is None:
        return None, "no volume"
    return float(price) * float(vol), "last session"


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_value_cr, cfg=None, holidays=None, names=None,
               volume_of=None):
    """Pure. data = {SYMBOL: series}. Returns (hits, counts, last_date).

    names: {SYMBOL: company name}, written to each hit that has one. A breakout_20d hit gets "level",
    the prior 20-day closing high.

    The traded-value floor is measured on complete sessions only. While the session is running
    Yahoo's spark volume is partial, so a symbol with no complete volumes is not judged in stage 1:
    its hit goes on to volume_of(symbol) (a daily chart with volumes; None when there is none), and
    is dropped only when the complete sessions' value is under the floor. A hit that cannot be
    checked is kept with "value_checked": false."""
    hits, dates, value = [], {}, {}
    names = names or {}
    live = session_in_progress(now_utc, cfg, holidays)
    deferred = {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc, cfg, holidays, with_volume=True)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        val, how = traded_value(s, bars, partial_meta=live)
        defer = min_value_cr > 0 and how == "partial"
        if price is None or price < min_price or (
                min_value_cr > 0 and not defer and (val is None or val < min_value_cr * CRORE)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c, _ in bars]
        tags, nums = find_setups(closes)
        if tags:
            vol = meta.get("regularMarketVolume")
            if vol is None and bars[-1][2] is not None:
                vol = bars[-1][2]
            order_val = val
            if order_val is None and price is not None and meta.get("regularMarketVolume") is not None:
                order_val = float(price) * float(meta["regularMarketVolume"])   # only for the order: "so far"
            value[sym] = order_val or 0.0
            hit = {"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                   "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                   "volume": int(vol) if vol is not None else None}
            if sym in names:
                hit["name"] = names[sym]
            if "breakout_20d" in tags:
                hit["level"] = round(nums["high20"], 2)    # the prior 20-day closing high it just beat
            hits.append(hit)
            if defer:
                deferred[sym] = hit
    if deferred:
        counts["deferred"] = len(deferred)
        unchecked = 0
        for sym, hit in list(deferred.items()):
            s2 = None
            if volume_of is not None:
                try:
                    s2 = volume_of(sym)
                except ScanError:
                    s2 = None
            val2 = None
            if s2:
                bars2 = daily_closes(s2, now_utc, cfg, holidays, with_volume=True)
                val2, _ = traded_value(s2, bars2, partial_meta=True)       # complete sessions only
            if val2 is None:
                hit["value_checked"] = False
                unchecked += 1
            elif val2 < min_value_cr * CRORE:
                hits.remove(hit)
                value.pop(sym, None)
                counts["illiquid"] += 1
                counts["scanned"] -= 1
            else:
                value[sym] = val2
                last_vol = [v for _, _, v in bars2 if v]
                hit["volume"] = int(last_vol[-1]) if last_vol else None
        if unchecked:
            counts["unchecked"] = unchecked
    hits.sort(key=lambda h: (-value[h["symbol"]], h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def session_dt(day, cfg=None):
    o, c = nc.session_times(day, cfg)
    return datetime.datetime.combine(day, o), datetime.datetime.combine(day, c)


def slot(day, k, cfg=None):
    return session_dt(day, cfg)[0] + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, cfg=None):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST), on the grid,
    inside the session, with a close. Sorted, one per slot."""
    open_dt, close_dt = session_dt(day, cfg)
    out = {}
    hs, ls, vs = series.get("h"), series.get("l"), series.get("v")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = nc.ist_from_epoch(ts)
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et < open_dt or et >= close_dt:
            continue  # pre-open, or after the close
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        out[et] = {"start": et, "slot": int((et - open_dt).total_seconds()) // (BAR_MIN * 60), "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (vs[i] or 0) if vs else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, cfg=None):
    """Every slot from 09:15 to the cutoff is there, and at least the range. A missing bar
    from 15:15 (LATE_GAP_MIN before the close) is Yahoo's, not a halt, and is let through."""
    open_dt, close_dt = session_dt(day, cfg)
    end = min(cutoff, close_dt)
    want = int((end - open_dt).total_seconds() // (BAR_MIN * 60))
    if want < n_range:
        return False
    have = {b["slot"] for b in bars}
    gap_from = close_dt - datetime.timedelta(minutes=LATE_GAP_MIN)
    for k in range(want):
        if k not in have and slot(day, k, cfg) < gap_from:
            return False
    return True


SIDES = ("long", "short", "both")


def sides_of(side):
    return ("long", "short") if side == "both" else (side,)


def orb_candidate(bars, n_range, side="long"):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close).
    side="short" is the mirror: some close at or below the lowest close of the range."""
    if side == "short":
        bottom = min(b["c"] for b in bars if b["slot"] < n_range)
        return any(b["c"] <= bottom for b in bars if b["slot"] >= n_range)
    top = max(b["c"] for b in bars if b["slot"] < n_range)
    return any(b["c"] >= top for b in bars if b["slot"] >= n_range)


def orb_hit(bars, n_range, side="long"):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high.
    side="short": the first completed bar to CLOSE under the range low (a breakdown); the hit says
    "side": "short" and pct_over is negative (how far under the low). A long hit has no "side" key."""
    rng = [b for b in bars if b["slot"] < n_range]
    if len(rng) < n_range or any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for b in bars:
        if b["slot"] < n_range:
            continue
        if (b["c"] < ol) if side == "short" else (b["c"] > oh):
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            edge = ol if side == "short" else oh
            hit = {"price": round(b["c"], 2), "at": hm(closes_at),
                   "bar_start": hm(b["start"]), "bar_index": b["slot"],
                   "or_high": round(oh, 2), "or_low": round(ol, 2),
                   "pct_over": round((b["c"] / edge - 1) * 100, 2) + 0.0, "_t": closes_at}
            if side == "short":
                hit["side"] = "short"
            return hit
    return None


def shifted_until(day, until, cfg=None):
    """no_entries_after on `day`. In a special session (config special_sessions, like Muhurat
    trading) it keeps its distance from the close: 14:30 is an hour before 15:30."""
    o, c = nc.session_times(day, cfg)
    if (o, c) == (nc.SESSION_OPEN, nc.SESSION_CLOSE) or until is None:
        return until
    gap = datetime.datetime.combine(day, nc.SESSION_CLOSE) - datetime.datetime.combine(day, until)
    return (datetime.datetime.combine(day, c) - gap).time()


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, cfg=None, holidays=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = nc.to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = nc.trading_day(day, holidays, cfg)
    if not ok:
        raise ScanError("No session on %s (%s): the NSE is closed. Run orb on a trading day after the "
                        "opening range, or replay a past day with --offline." % (day_label(day), why))
    open_dt, close_dt = session_dt(day, cfg)
    range_end = open_dt + datetime.timedelta(minutes=or_minutes)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    if first_close > close_dt:
        raise ScanError("the %d-minute range does not fit the %s-%s session on %s" % (
            or_minutes, hm(open_dt), hm(close_dt), day_label(day)))
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (hm(at), hm(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (hm(at), hm(close_dt), day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The NSE closed at %s IST on %s. Replay the session with --at 10:30."
                            % (hm(close_dt), day_label(day)))
    if cutoff < first_close:
        if cutoff < open_dt:
            regular = (open_dt.time(), close_dt.time()) == (nc.SESSION_OPEN, nc.SESSION_CLOSE)
            if regular and cutoff.time() >= nc.PREOPEN_OPEN:
                where = ("It is %s IST, the pre-open call auction (09:00-09:15, orders until 09:08): "
                         "no 5-minute bars yet." % hm(cutoff))
            else:
                where = "It is %s IST: the NSE opens at %s." % (hm(cutoff), hm(open_dt))
        else:
            where = "It is %s IST." % hm(cutoff)
        raise ScanError("%s The %d-minute opening range runs %s to %s and the first bar after it closes at "
                        "%s. Run orb again after %s." % (where, or_minutes, hm(open_dt), hm(range_end),
                                                         hm(first_close), hm(first_close)))
    until = shifted_until(day, until, cfg)
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:30 (or --until)." % (hm(cutoff), hm(until), hm(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_value_cr=0.0, confirm=None,
             max_confirm=None, cfg=None, names=None, side="long"):
    """stage1 = {SYMBOL: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (rupees traded so far) are confirmed; the rest are counted as unconfirmed.
    names: {SYMBOL: company name}, written to each hit that has one.
    side: "long" (breakouts, the default), "short" (breakdowns) or "both"; with both a stock can be a hit
    on each side. Returns (hits, counts, failed {sym: why})."""
    names = names or {}
    sides = sides_of(side)
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "gappy": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff, cfg)
        complete = usable(bars, day, cutoff, n_range, cfg)
        # Yahoo's spark drops 5-minute bars of busy NSE stocks at random (ITC lost 9 of 75 on
        # 13 Nov 2026), so with a stage 2 a gap is not a halt: the v8 chart decides.
        if not complete and (confirm is None or not bars):
            counts["unusable"] += 1
            continue
        if s.get("v"):
            vol = sum(b["v"] for b in bars)
        else:
            vol = (s.get("meta") or {}).get("regularMarketVolume") or 0
        value = bars[-1]["c"] * vol
        if bars[-1]["c"] < min_price or value < min_value_cr * CRORE:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if not complete:
            counts["gappy"] += 1
            cands.append((sym, bars, value))
        elif any(orb_candidate(bars, n_range, s) for s in sides):
            cands.append((sym, bars, value))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff, cfg)
            if not usable(full, day, cutoff, n_range, cfg):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        for s in sides:
            h = orb_hit(full, n_range, s)
            if h:
                hit = {"symbol": sym, "setups": ["orb"]}
                if sym in names:
                    hit["name"] = names[sym]
                hits.append(dict(hit, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYMBOL: series}, {SYMBOL: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {nc.yahoo_symbol(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_url(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 10 and (k + 1) % 10 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [nc.nse_symbol(x.get("symbol") if isinstance(x, dict) else x) for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(nc.nse_symbol(ln.split("|")[0]))
    return out


def pipe_names(path):
    """{SYMBOL: name} from a SYMBOL|Name text file (what `universe` writes); {} for other files."""
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError:
        return out
    if text.lstrip().startswith(("{", "[")):
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "|" in ln:
            sym, _, name = ln.partition("|")
            if name.strip():
                out[nc.nse_symbol(sym)] = name.strip()
    return out


def resolve_universe(a, offline_syms=None, names=None):
    """-> (symbols, label, source). --symbols, --watchlist, --universe NAME|FILE; offline with
    none of those: every chart in the file; online: Nifty 500. `names`, if given, is filled in
    with {SYMBOL: company name} from the list that was read (a SYMBOL|Name file, an NSE list)."""
    if a.symbols:
        syms = [nc.nse_symbol(s) for s in re.split(r"[,\s]+", a.symbols) if s.strip()]
        label, src = "your symbols", "--symbols"
    elif a.watchlist:
        syms, label, src = symbols_from_file(a.watchlist), "watchlist", a.watchlist
    elif a.universe and a.universe.lower() not in LISTS:
        if not os.path.exists(a.universe):
            raise ScanError("--universe is nifty50, nifty500, fno, all or a file; no file at %s" % a.universe)
        syms, label, src = symbols_from_file(a.universe), os.path.basename(a.universe), a.universe
        if names is not None:
            names.update(pipe_names(a.universe))
    elif offline_syms is not None and not a.universe:
        syms, label, src = sorted(offline_syms), "every chart in the offline file", a.offline
    else:
        name = (a.universe or DEFAULT_UNIVERSE).lower()
        rows, src = load_list(name, a)
        syms, label = [s for s, _ in rows], LISTS[name][1]
        if names is not None:
            names.update(dict(rows))
    bad = [s for s in syms if not nc.is_nse_symbol(s)]
    if bad:
        raise ScanError("not NSE symbols: %s" % ", ".join(bad[:5]))
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    return out, label, src


def build_scan_list(a, cfg, offline_syms=None):
    """Universe, then the sec_list screen, then --limit. -> (symbols, info)."""
    uni_names = {}
    syms, label, src = resolve_universe(a, offline_syms, uni_names)
    sec = load_sec(a)
    if sec is None:
        kept, dropped = list(syms), {}
        screened = "sec_list.csv not checked (offline: pass --sec-list FILE or --lists DIR to screen series, bands and GSM)."
    else:
        kept, dropped = screen(syms, sec, cfg)
        screened = drop_summary(len(syms), dropped, cfg)
    if a.limit:
        kept = kept[:a.limit]
    if not kept:
        raise ScanError("no symbols to scan" + (" (%s)" % screened if dropped else ""))
    names = company_names(a, uni_names, sec)
    return kept, {"label": label, "source": src, "listed": len(syms), "dropped": dropped,
                  "screened": screened, "sec_list": sec is not None,
                  "names": {s: names[s] for s in kept if s in names}}


def holiday_note(day, years):
    if day.year in set(years):
        return None
    return ("NSE's %d holiday list is not loaded (nse-alerts: alerts.py holidays --refresh): weekdays are "
            "treated as trading days." % day.year)


# ---------------------------------------------------------------- commands

def load_cfg(a):
    try:
        return nc.load_config(a.config)
    except (ValueError, OSError) as e:
        raise ScanError("%s is not valid JSON: %s" % (a.config, e))


def unread_symbols(symbols, data, failed):
    """(unread symbols, {symbol: why}): the symbols that have no chart in `data`."""
    unread = [s for s in symbols if s not in (data or {})]
    why = {s: (failed or {}).get(s) or "no chart in the offline files" for s in unread}
    return unread, why


def require_read(symbols, unread, why, what="symbols", of=None):
    """Raise ScanFailed (exit code 3) when more than half could not be read: that is a data
    failure, not "no setups", and it must not overwrite the watchlist the last good scan wrote."""
    total = of if of is not None else len(symbols)
    if total and len(unread) * 2 > total:
        raise ScanFailed("scan failed: could not read prices (%d of %d %s failed)" % (len(unread), total, what),
                         failed=len(unread), total=total, samples=dict(list(why.items())[:5]))


def error_fields(unread_why):
    """The "errors" and "error_samples" a written watchlist.json records."""
    return {"errors": len(unread_why), "error_samples": dict(list(unread_why.items())[:5])}


def cmd_universe(a):
    t0 = time.time()
    cfg = load_cfg(a)
    name = a.name.lower()
    if name not in LISTS:
        raise ScanError("universe is nifty50, nifty500, fno or all")
    rows, src = load_list(name, a)
    names = dict(rows)
    syms = [s for s, _ in rows]
    kept, dropped = screen(syms, load_sec(a), cfg)
    screened = drop_summary(len(syms), dropped, cfg)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym in kept:
                fh.write("%s|%s\n" % (sym, names.get(sym, "")))
    if a.json:
        print(json.dumps({"universe": name, "listed": len(syms), "count": len(kept),
                          "dropped": reason_counts(dropped), "dropped_symbols": dropped,
                          "source": src, "out": None if a.no_write else a.out,
                          "config": a.config if os.path.isfile(a.config) else None,
                          "seconds": round(time.time() - t0, 1)}, indent=2, ensure_ascii=False))
    else:
        print("%s from NSE (%s): %s symbols." % (LISTS[name][1], os.path.basename(src), _c(len(syms))))
        print(screened)
        print("Kept %s for intraday scans." % _c(len(kept)))
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
        print(config_line(a.config))
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    cfg = load_cfg(a)
    holidays, years = calendar(cfg, a.config)
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols, info = build_scan_list(a, cfg, offline_syms)
    volume_of = None
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % (_c(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)

        def volume_of(sym):
            """A hit's own daily chart (volumes of complete sessions), for the value floor while the
            session is running and spark's volume is only today's partial count."""
            if a.pause:
                SLEEP(a.pause)
            return parse_chart(yahoo_get(chart_url(sym, "3mo", "1d")), sym)
    # More than half unread is a data failure, not "no setups": stop before anything is written.
    unread, why = unread_symbols(symbols, data, failed)
    require_read(symbols, unread, why)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_value_cr, cfg, holidays,
                                         names=info["names"], volume_of=volume_of)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "nse-scanner", "created_at": nc.utc_iso(now), "mode": "daily", "exchange": "NSE",
           "as_of": as_of, "universe": len(symbols), "universe_name": info["label"],
           "scanned": counts["scanned"], "failed": nfailed, "source": "yahoo",
           "filters": {"min_price": a.min_price, "min_value_cr": a.min_value_cr},
           "dropped": reason_counts(info["dropped"]), "dropped_symbols": info["dropped"],
           "note": NOTE, "hits": hits}
    doc.update(error_fields(why))
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        nc.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, config=a.config if os.path.isfile(a.config) else None,
                              seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    local = nc.to_ist(now)
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Universe: %s, %s symbols. %s" % (info["label"], _c(info["listed"]), info["screened"]))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s+ and %s+ traded a day)%s; "
          "%s scanned." % (_c(counts["fetched"]), _c(len(symbols)),
                           (" (%s failed or had no data)" % _c(nfailed)) if nfailed else "",
                           _c(counts["fetched"] - counts["illiquid"]), nc.inr(a.min_price),
                           cr_text(a.min_value_cr),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           _c(counts["scanned"])))
    note = holiday_note(local.date(), years)
    if note:
        print(note)
    if counts.get("deferred"):
        print("The session is running, so Yahoo's spark volume is only today's partial count: the %s floor was "
              "checked on each hit's own daily chart (complete sessions, %d hits)%s." % (
                  cr_text(a.min_value_cr), counts["deferred"],
                  (", %d of them could not be checked and are marked value_checked false" % counts["unchecked"])
                  if counts.get("unchecked") else ""))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (rupees traded):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-10s %12s  RSI %5.1f  %s" % (h["symbol"], nc.inr(h["price"], keep_zeros=True), h["rsi"],
                                               ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(config_line(a.config))
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _c(n):
    """Whole numbers with Indian grouping: 2391 -> 2,391; 123456 -> 1,23,456."""
    return nc.inr(n, decimals=0)[1:]


def cr_text(x):
    """10 -> '₹10 cr'; 2.5 -> '₹2.50 cr'."""
    return "₹%s cr" % _n(x)


def _p(x):
    return nc.inr(x, keep_zeros=True)[1:]


def cmd_orb(a):
    t0 = time.time()
    cfg = load_cfg(a)
    holidays, years = calendar(cfg, a.config)
    now = parse_now(a.now)
    at = parse_hhmm(a.at, "--at") if a.at else None
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:30", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [nc.ist_from_epoch(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\" (IST)")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, cfg, holidays)
    symbols, info = build_scan_list(a, cfg, offline_syms)
    confirm = None
    if not a.offline:
        today = today_ist()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % (_c(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_url(sym, rng, "%dm" % BAR_MIN)), sym)
    # More than half unread is a data failure, not "no setups": stop before anything is written.
    unread, why = unread_symbols(symbols, data, failed)
    require_read(symbols, unread, why)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_value_cr,
                                     confirm, a.max_confirm, cfg, names=info["names"], side=a.side)
    # The same for stage 2: every candidate's v8 chart failing leaves hits missing, not absent.
    asked = counts["confirmed"] + len(failed2)
    if confirm is not None and asked:
        require_read(symbols, list(failed2), dict(list(failed2.items())[:5]), "candidate charts", of=asked)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    errors_why = dict(why)
    errors_why.update(failed2)
    open_dt, _ = session_dt(day, cfg)
    as_of = "%s %s IST" % (day.isoformat(), hm(cutoff))
    doc = {"created_by": "nse-scanner", "created_at": nc.utc_iso(now), "mode": "orb", "exchange": "NSE",
           "as_of": as_of, "universe": len(symbols), "universe_name": info["label"],
           "scanned": counts["scanned"], "failed": nfailed, "unconfirmed": counts["unconfirmed"],
           "source": "yahoo", "or_minutes": a.or_minutes, "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_value_cr": a.min_value_cr},
           "dropped": reason_counts(info["dropped"]), "dropped_symbols": info["dropped"],
           "note": NOTE, "hits": hits}
    if a.side != "long":
        doc["side"] = a.side
    doc.update(error_fields(errors_why))
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        nc.write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, config=a.config if os.path.isfile(a.config) else None,
                              seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (%s to %s), 5-minute bars, Yahoo."
          % (day_label(day), hm(cutoff), a.or_minutes, hm(open_dt),
             hm(open_dt + datetime.timedelta(minutes=a.or_minutes))))
    print("Universe: %s, %s symbols. %s" % (info["label"], _c(info["listed"]), info["screened"]))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates%s, %s confirmed with real highs and lows."
          % (_c(counts["fetched"]), _c(len(symbols)),
             (" (%s failed or had no data)" % _c(nfailed)) if nfailed else "",
             _c(counts["scanned"]),
             (" (%s under the price / value floor)" % _c(counts["illiquid"])) if counts["illiquid"] else "",
             _c(counts["candidates"]),
             (" (%s because Yahoo's spark dropped bars)" % _c(counts["gappy"])) if counts["gappy"] else "",
             _c(counts["confirmed"])))
    note = holiday_note(day, years)
    if note:
        print(note)
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % (_c(counts["unconfirmed"]), _c(a.max_confirm)))
    where = {"long": "above their opening-range high", "short": "under their opening-range low",
             "both": "outside their opening range"}[a.side]
    if hits:
        print("%d hit%s: of %s charts, these closed %s by %s IST:"
              % (len(hits), "" if len(hits) == 1 else "s",
                 ("the %s checked" % _c(counts["confirmed"])) if counts["unconfirmed"] else _c(counts["scanned"]),
                 where, hm(cutoff)) if a.side != "long" else
              "%d of %s charts closed above their opening-range high by %s IST:"
              % (len(hits), ("the %s checked" % _c(counts["confirmed"])) if counts["unconfirmed"]
                 else _c(counts["scanned"]), hm(cutoff)))
        for h in hits[:a.show]:
            short = h.get("side") == "short"
            print("  %5s  %-10s %12s  range %s to %s  %+.2f%% %s%s" % (
                h["at"], h["symbol"], nc.inr(h["price"], keep_zeros=True), _p(h["or_low"]), _p(h["or_high"]),
                h["pct_over"], "under" if short else "over", "  SHORT (breakdown)" if short else ""))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed %s by %s IST." % (where if a.side != "long" else "above its opening-range high",
                                                 hm(cutoff)))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(config_line(a.config))
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL and /nse-entry SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Chart Scanner (nse-scanner): daily setups and opening-range "
                                             "breakouts on NSE charts.")
    sub = ap.add_subparsers(dest="cmd")

    def lists_args(p):
        p.add_argument("--lists", metavar="DIR", help="a folder with NSE's list files instead of the network")
        p.add_argument("--sec-list", metavar="FILE", help="NSE's sec_list.csv (.gz fine) instead of the network")
        p.add_argument("--cache", default=nc.tpath("cache"),
                       help="where the day's NSE files are kept (default: cache/ in the trader folder)")
        p.add_argument("--refresh", action="store_true", help="download the NSE files again")
        p.add_argument("--config", default=nc.tpath("config.json"),
                       help="default: config.json in the trader folder (nse_common.trader_home)")

    p = sub.add_parser("universe", help="an NSE list, screened by sec_list -> trader/universe.txt")
    p.add_argument("name", nargs="?", default=DEFAULT_UNIVERSE, help="nifty50, nifty500 (default), fno or all")
    p.add_argument("--out", default=nc.tpath("universe.txt"))
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    lists_args(p)

    def common(p, min_price, min_value_cr):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols (RELIANCE,M&M,BAJAJ-AUTO)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        g.add_argument("--universe", help="nifty50, nifty500 (default), fno, all, or a SYMBOL|Name file")
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (default %s)" % _n(min_price))
        p.add_argument("--min-value-cr", type=float, default=min_value_cr,
                       help="traded value a session, in crore of rupees (default %s)" % _n(min_value_cr))
        p.add_argument("--offline", metavar="PATH", help="Yahoo fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-11-13 10:05" IST, or ...Z for UTC)')
        p.add_argument("--out", default=nc.tpath("watchlist.json"))
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")
        lists_args(p)

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 20.0, 10.0)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 20.0, 0.0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM IST (default: config no_entries_after, 14:30)")
    p.add_argument("--side", choices=SIDES, default="long",
                   help="long: breakouts (the default). short: breakdowns, the first close under the range low, "
                        "for research (the order path is long-only). both: each")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanFailed as e:
        # exit 3: the prices could not be read. Not "no setups", and nothing was written.
        print(e, file=sys.stderr)
        for sym, why in e.samples.items():
            print("  %s: %s" % (sym, why), file=sys.stderr)
        print("The existing watchlist.json was left as it was and nothing was written.", file=sys.stderr)
        if getattr(a, "json", False):
            print(json.dumps({"error": str(e), "failed": e.failed, "universe": e.total, "wrote": False,
                              "error_samples": e.samples}, indent=2, ensure_ascii=False))
        return e.exit_code
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
