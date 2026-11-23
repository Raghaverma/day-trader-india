#!/usr/bin/env python3
"""Deep pre-market research for the paper autopilot: a research desk before the open, shaped like a fund's
morning meeting, on the Claude account logged in on this Mac. Standard library only.

    python3 deepscan.py tick                    # what launchd runs every 5 minutes (com.nse-autopilot-deep)
    python3 deepscan.py run --phase all         # both phases now (--limit N companies, --out DIR, --no-mail)
    python3 deepscan.py status [--date D]       # what ran, the tokens it used, the sit-outs

Two phases on a trading day (IST, from "deep" in autopilot.json):

  03:30  phase 1, the heavy one.
         data   the world overnight: US, Europe and Asia indices, US index futures, VIX, US Treasury yields,
                the dollar, the rupee, yen and yuan, Brent and WTI, gas, gold, silver, copper, bitcoin,
                emerging-market and India ETFs, the Indian ADRs and the US peers of each Indian sector; the
                Nifty 500 (breadth, sectors, leaders and laggards, 52-week highs and lows, volume surges);
                FII/DII cash flows and the F&O participant open interest (NSE); the market's bulk and block
                deals; India and world macro headlines (Google News); and for every Nifty 50 stock its NSE
                stock file (nse-news/dossier.py), a year of daily bars, the last session's 5-minute bars,
                Yahoo's fundamentals (4 years, 5 quarters, valuation), 30 days of headlines, its Nifty 50
                peers and every past ORB signal on it from the studies and the live days
         desks  a macro desk; a desk per Nifty 50 company (it reads the macro view); a second analyst who
                argues against every company call that would sit a stock out or rank it high; a desk per
                sector that reads its companies
  07:45  phase 2: the world again (Asia is trading, so are the US futures), the headlines since phase 1, then
         the CIO, who reads every desk and writes the morning note, the stocks to sit out today and a
         conviction rank for every stock. The note is Markdown, a PDF on the Desktop beside the P&L journal,
         and an email.

Live, when "apply" is true: run_day.py's pre-market adds the CIO's sit-outs to Claude's veto list (source
"deep"), and the ranks go to the rank file, which tags each signal "top half" or "bottom half" so the
scorecard can tell whether the ranks mean anything. The end-of-day report replays every deep veto. Nothing
here places an order, sizes a trade or touches config.json.

Claude gets no tools (as in claude_run.py): everything it needs is in the prompt and it only answers.
"max_tokens" stops new calls once that many tokens are used, and the first usage-limit error stops the day's
remaining calls, so the 08:40 pre-market and the 15:35 analysis keep their share. Every step writes its file
as it finishes, so a crash or a reboot means the next tick carries on where it stopped. Not financial advice.
"""

import argparse
import concurrent.futures
import csv
import datetime
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autopilot_common as ac  # noqa: E402
import claude_run  # noqa: E402
import market  # noqa: E402

nc = ac.nc
REPO = os.path.dirname(HERE)
PROMPTS = os.path.join(HERE, "prompts")
VETO_CATEGORIES = ("RESULTS", "CORPORATE_ACTION", "NEGATIVE_NEWS", "REGULATORY_OR_BAN", "OTHER")

DEFAULTS = {"enabled": False, "apply": False, "start": "03:30", "refresh_at": "07:45", "done_by": "08:35",
            "max_tokens": 6000000, "parallel": 3, "desk_model": "sonnet", "cio_model": "opus",
            "max_vetoes": 8, "mail": True, "pdf": True, "rank": True, "challenge": True,
            "call_timeout_s": 900, "dossier_budget_s": 1500, "breadth_workers": 2}

# ------------------------------------------------------------------ what the desks look at

GLOBAL = [
    ("^GSPC", "S&P 500", "US"), ("^IXIC", "Nasdaq Composite", "US"), ("^DJI", "Dow Jones", "US"),
    ("^RUT", "Russell 2000", "US"), ("^VIX", "CBOE VIX", "US"),
    ("ES=F", "S&P 500 futures", "US futures"), ("NQ=F", "Nasdaq 100 futures", "US futures"),
    ("YM=F", "Dow futures", "US futures"),
    ("^STOXX50E", "Euro Stoxx 50", "Europe"), ("^FTSE", "FTSE 100", "Europe"), ("^GDAXI", "DAX", "Europe"),
    ("^FCHI", "CAC 40", "Europe"),
    ("^N225", "Nikkei 225", "Asia"), ("^HSI", "Hang Seng", "Asia"), ("000001.SS", "Shanghai Composite", "Asia"),
    ("^KS11", "KOSPI", "Asia"), ("^TWII", "Taiwan Weighted", "Asia"), ("^AXJO", "ASX 200", "Asia"),
    ("^STI", "Straits Times", "Asia"),
    ("^IRX", "US 13-week bill yield", "Rates"), ("^FVX", "US 5-year yield", "Rates"),
    ("^TNX", "US 10-year yield", "Rates"), ("^TYX", "US 30-year yield", "Rates"),
    ("DX-Y.NYB", "US dollar index", "FX"), ("INR=X", "USD/INR", "FX"), ("EURUSD=X", "EUR/USD", "FX"),
    ("JPY=X", "USD/JPY", "FX"), ("CNY=X", "USD/CNY", "FX"),
    ("BZ=F", "Brent crude", "Commodities"), ("CL=F", "WTI crude", "Commodities"),
    ("NG=F", "Natural gas", "Commodities"), ("GC=F", "Gold", "Commodities"), ("SI=F", "Silver", "Commodities"),
    ("HG=F", "Copper", "Commodities"), ("BTC-USD", "Bitcoin", "Crypto"),
    ("EEM", "iShares MSCI Emerging Markets", "EM and India ETFs"), ("INDA", "iShares MSCI India", "EM and India ETFs"),
    ("EPI", "WisdomTree India Earnings", "EM and India ETFs"),
    ("^NSEI", "Nifty 50", "India"), ("^NSEBANK", "Nifty Bank", "India"), ("^INDIAVIX", "India VIX", "India"),
    ("NIFTY_FIN_SERVICE.NS", "Nifty Financial Services", "India sectors"), ("^CNXIT", "Nifty IT", "India sectors"),
    ("^CNXAUTO", "Nifty Auto", "India sectors"), ("^CNXPHARMA", "Nifty Pharma", "India sectors"),
    ("^CNXFMCG", "Nifty FMCG", "India sectors"), ("^CNXMETAL", "Nifty Metal", "India sectors"),
    ("^CNXENERGY", "Nifty Energy", "India sectors"), ("^CNXREALTY", "Nifty Realty", "India sectors"),
    ("^CNXPSUBANK", "Nifty PSU Bank", "India sectors"), ("^CNXINFRA", "Nifty Infrastructure", "India sectors"),
    ("^CNXPSE", "Nifty PSE", "India sectors"), ("^CNXCONSUM", "Nifty India Consumption", "India sectors"),
]
# US listings of Indian companies: (ADR, NSE symbol, NSE shares per ADR)
ADRS = [("INFY", "INFY", 1), ("WIT", "WIPRO", 1), ("HDB", "HDFCBANK", 3), ("IBN", "ICICIBANK", 2),
        ("RDY", "DRREDDY", 1)]
PEERS = [("ACN", "Accenture"), ("CTSH", "Cognizant"), ("EPAM", "EPAM Systems"), ("MSFT", "Microsoft"),
         ("NVDA", "Nvidia"), ("TSM", "TSMC"), ("XOM", "ExxonMobil"), ("CVX", "Chevron"), ("SHEL", "Shell"),
         ("BHP", "BHP"), ("RIO", "Rio Tinto"), ("VALE", "Vale"), ("FCX", "Freeport-McMoRan"),
         ("JPM", "JPMorgan"), ("XLF", "US financials ETF"), ("TM", "Toyota"), ("TSLA", "Tesla"),
         ("TEVA", "Teva"), ("VTRS", "Viatris"), ("UL", "Unilever"), ("PM", "Philip Morris"),
         ("CAT", "Caterpillar"), ("NEE", "NextEra Energy"), ("VZ", "Verizon"), ("WMT", "Walmart"),
         ("SHW", "Sherwin-Williams"), ("HD", "Home Depot")]
# NSE industry (the Nifty lists' "Industry" column) -> its sector index and its global links
INDUSTRY_LINKS = {
    "Financial Services": (["NIFTY_FIN_SERVICE.NS", "^NSEBANK", "^CNXPSUBANK"], ["JPM", "XLF", "HDB", "IBN", "^TNX", "INR=X"]),
    "Information Technology": (["^CNXIT"], ["ACN", "CTSH", "EPAM", "MSFT", "INFY", "WIT", "^IXIC", "NQ=F", "INR=X"]),
    "Oil Gas & Consumable Fuels": (["^CNXENERGY"], ["BZ=F", "CL=F", "NG=F", "XOM", "CVX", "SHEL", "INR=X"]),
    "Power": (["^CNXENERGY", "^CNXPSE"], ["NG=F", "NEE", "^TNX"]),
    "Metals & Mining": (["^CNXMETAL"], ["HG=F", "BHP", "RIO", "VALE", "FCX", "CNY=X", "000001.SS"]),
    "Automobile and Auto Components": (["^CNXAUTO"], ["TM", "TSLA", "BZ=F", "JPY=X"]),
    "Healthcare": (["^CNXPHARMA"], ["RDY", "TEVA", "VTRS", "INR=X"]),
    "Fast Moving Consumer Goods": (["^CNXFMCG"], ["UL", "PM", "WMT"]),
    "Consumer Durables": (["^CNXCONSUM"], ["GC=F", "SI=F", "WMT"]),
    "Consumer Services": (["^CNXCONSUM"], ["WMT", "BZ=F"]),
    "Construction": (["^CNXINFRA"], ["CAT", "^TNX"]),
    "Construction Materials": (["^CNXINFRA"], ["CAT", "HD"]),
    "Capital Goods": (["^CNXINFRA"], ["CAT", "^TNX"]),
    "Telecommunication": (["^CNXINFRA"], ["VZ"]),
    "Services": (["^CNXINFRA"], ["BZ=F", "CAT"]),
    "Realty": (["^CNXREALTY"], ["^TNX", "HD"]),
    "Chemicals": (["^CNXCONSUM"], ["SHW", "BZ=F"]),
}
MACRO_QUERIES = [
    ("Sensex Nifty outlook this week", "IN"), ("GIFT Nifty", "IN"), ("FII DII flows India equities", "IN"),
    ("RBI monetary policy repo rate", "IN"), ("India inflation CPI WPI", "IN"), ("India GDP growth economy", "IN"),
    ("rupee dollar forex RBI", "IN"), ("SEBI circular new rules", "IN"), ("India fiscal deficit government", "IN"),
    ("India quarterly results earnings season", "IN"), ("India IPO market listing", "IN"),
    ("mutual fund inflows SIP India", "IN"), ("India US trade deal tariffs", "IN"),
    ("Wall Street stocks close", "US"), ("Federal Reserve interest rates", "US"), ("US Treasury yields", "US"),
    ("US economy jobs inflation data", "US"), ("Nvidia AI stocks", "US"), ("oil prices OPEC", "US"),
    ("gold price", "US"), ("China economy stimulus markets", "US"), ("Japan Nikkei Bank of Japan yen", "US"),
    ("European stocks ECB", "US"), ("Middle East geopolitics oil", "US"), ("Russia Ukraine", "US"),
    ("emerging markets stocks dollar", "US"), ("global markets week ahead", "US"),
]
FUNDAMENTAL_TYPES = (
    ["annual" + t for t in ("TotalRevenue", "GrossProfit", "OperatingIncome", "EBITDA", "NetIncome", "DilutedEPS",
                            "OperatingCashFlow", "CapitalExpenditure", "FreeCashFlow", "TotalDebt",
                            "CashAndCashEquivalents", "StockholdersEquity", "TotalAssets", "InterestExpense",
                            "NetInterestIncome", "DilutedAverageShares")] +
    ["quarterly" + t for t in ("TotalRevenue", "OperatingIncome", "EBITDA", "NetIncome", "DilutedEPS",
                               "NetInterestIncome", "TotalDebt", "StockholdersEquity")] +
    ["trailing" + t for t in ("PeRatio", "ForwardPeRatio", "PegRatio", "PsRatio", "PbRatio",
                              "EnterprisesValueEBITDARatio", "MarketCap", "EnterpriseValue")])
ARCHIVES = "https://nsearchives.nseindia.com/content/"
UA = getattr(nc, "UA", "Mozilla/5.0")


class DeepError(Exception):
    pass


def deep_settings(settings):
    d = dict(DEFAULTS)
    d.update((settings or {}).get("deep") or {})
    return d


def deep_dir(home, day):
    return os.path.join(ac.day_dir(home, day), "deep")


def minutes(text):
    h, m = str(text).split(":")
    return int(h) * 60 + int(m)


def ist_hm(utc):
    return nc.to_ist(utc).strftime("%H:%M")


def stamp():
    return nc.to_ist(nc.utc_now()).strftime("%H:%M:%S")


def rpath(path):
    return os.path.join(*path) if isinstance(path, (list, tuple)) else path


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------ HTTP

def http_get(url, accept="application/json", timeout=25, tries=2):
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if data[:2] == b"\x1f\x8b":
                    import gzip
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
        time.sleep(1.5 * (i + 1))
    raise DeepError("%s: %s" % (url.split("?")[0], last))


def yahoo_chart(ticker, rng="1y", interval="1d"):
    """A Yahoo v8 chart doc for a raw Yahoo ticker (no .NS added)."""
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?%s" % (
        urllib.parse.quote(ticker, safe=""), urllib.parse.urlencode({"range": rng, "interval": interval}))
    doc = json.loads(http_get(url).decode("utf-8"))
    if not (doc.get("chart") or {}).get("result"):
        raise DeepError("Yahoo %s: no result" % ticker)
    return doc


def pct(a, b):
    try:
        return round((float(a) / float(b) - 1) * 100, 2) if b else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def summarize(rows, meta=None):
    """Facts from daily bars [(epoch, o, h, l, c, v)]: the last close and its moves, the 52-week range, the
    moving averages, the 20-day range and volume."""
    if not rows:
        return None
    closes = [r[4] for r in rows]
    last = closes[-1]

    def back(n):
        return pct(last, closes[-1 - n]) if len(closes) > n else None

    def sma(n):
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None
    hi = max((r[2] or r[4]) for r in rows[-252:])
    lo = min((r[3] or r[4]) for r in rows[-252:])
    rng = [((r[2] or r[4]) - (r[3] or r[4])) / r[4] * 100 for r in rows[-20:] if r[4]]
    vols = [r[5] or 0 for r in rows[-21:-1]]
    avg_v = sum(vols) / len(vols) if vols else 0
    out = {"last": round(last, 4), "bar_ist": nc.ist_from_epoch(rows[-1][0]).strftime("%Y-%m-%d %H:%M"),
           "chg_1d": back(1), "chg_5d": back(5), "chg_1m": back(21), "chg_3m": back(63), "chg_1y": back(250),
           "high_52w": round(hi, 4), "low_52w": round(lo, 4), "from_high": pct(last, hi), "from_low": pct(last, lo),
           "sma20": sma(20), "sma50": sma(50), "sma200": sma(200),
           "avg_range_20d_pct": round(sum(rng) / len(rng), 2) if rng else None,
           "volume_x_20d": round((rows[-1][5] or 0) / avg_v, 2) if avg_v else None}
    if meta:
        t = meta.get("regularMarketTime")
        out["price_now"] = meta.get("regularMarketPrice")
        out["price_time_ist"] = nc.ist_from_epoch(t).strftime("%Y-%m-%d %H:%M") if t else None
        out["currency"] = meta.get("currency")
        if out["price_now"] and len(closes) >= 2:
            # the move of the price now against the close before the last daily bar's session
            out["chg_now_vs_prev_close"] = pct(out["price_now"], closes[-2])
    return out


def fetch_global(pause=0.15):
    """Every instrument in GLOBAL, ADRS and PEERS: {"instruments": [...], "errors": {...}}."""
    seen, out, errors = set(), [], {}
    items = list(GLOBAL) + [(a, "%s ADR (%s, %s NSE shares per ADR)" % (a, s, k), "Indian ADRs") for a, s, k in ADRS] + \
        [(t, n, "Global peers") for t, n in PEERS]
    for tick, label, group in items:
        if tick in seen:
            continue
        seen.add(tick)
        try:
            doc = yahoo_chart(tick)
            rows = market.rows(doc)
            meta = doc["chart"]["result"][0].get("meta") or {}
            s = summarize(rows, meta)
            out.append({"ticker": tick, "label": label, "group": group, "summary": s,
                        "closes_30d": [[nc.ist_from_epoch(r[0]).strftime("%m-%d"), round(r[4], 4)] for r in rows[-30:]]})
        except Exception as e:
            errors[tick] = ac.tail(str(e), 160)
        time.sleep(pause)
    return {"fetched_at": nc.utc_iso(), "fetched_ist": stamp(), "instruments": out, "errors": errors}


def global_index(g):
    return {i["ticker"]: i for i in (g or {}).get("instruments") or []}


def compact_global(g, keep_closes=False):
    out = []
    for i in (g or {}).get("instruments") or []:
        row = {"ticker": i["ticker"], "label": i["label"], "group": i["group"]}
        row.update(i.get("summary") or {})
        if keep_closes:
            row["closes_30d"] = i.get("closes_30d")
        out.append(row)
    return out


# ------------------------------------------------------------------ the Nifty 500

def nifty_list(which="nifty500"):
    """[(symbol, name, industry)] from NSE's index list file."""
    text = http_get(ARCHIVES + "indices/ind_%slist.csv" % which, accept="text/csv").decode("utf-8", "replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if row.get("Symbol"):
            out.append((row["Symbol"], row.get("Company Name") or "", row.get("Industry") or ""))
    if not out:
        raise DeepError("NSE's %s list came back empty" % which)
    return out


def fetch_breadth(members, bars_dir, keep, workers=2, pause=0.2):
    """Daily bars for every Nifty 500 stock; keeps the year of bars for `keep` (the Nifty 50) in bars_dir.
    Returns {"stocks": {sym: summary + industry}, "errors": {...}}."""
    stocks, errors = {}, {}
    lock = threading.Lock()

    def one(item):
        sym, name, ind = item
        try:
            doc = market.fetch_chart(sym, "1d", rng="1y")
            rows = market.rows(doc)
            s = summarize(rows)
            if s:
                s.update({"name": name, "industry": ind})
                with lock:
                    stocks[sym] = s
            if sym in keep:
                ac.write_json(os.path.join(bars_dir, sym.replace("&", "_") + ".json"),
                              {"symbol": sym, "rows": rows})
        except Exception as e:
            with lock:
                errors[sym] = ac.tail(str(e), 120)
        time.sleep(pause)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        list(ex.map(one, members))
    return {"stocks": stocks, "errors": errors}


def breadth_stats(br):
    st = (br or {}).get("stocks") or {}
    vals = list(st.values())
    if not vals:
        return {"error": "no Nifty 500 bars"}

    def share(pred):
        xs = [v for v in vals if pred(v) is not None]
        return round(100.0 * sum(1 for v in xs if pred(v)) / len(xs), 1) if xs else None

    def above(k):
        return lambda v: (v["last"] > v[k]) if v.get(k) else None
    out = {"stocks": len(vals), "advancers_1d": sum(1 for v in vals if (v.get("chg_1d") or 0) > 0),
           "decliners_1d": sum(1 for v in vals if (v.get("chg_1d") or 0) < 0),
           "pct_above_sma20": share(above("sma20")), "pct_above_sma50": share(above("sma50")),
           "pct_above_sma200": share(above("sma200")),
           "near_52w_high": sorted([s for s, v in st.items() if v.get("from_high") is not None and v["from_high"] > -1.0]),
           "near_52w_low": sorted([s for s, v in st.items() if v.get("from_low") is not None and v["from_low"] < 1.0]),
           "median_chg_1d": round(statistics.median([v.get("chg_1d") or 0 for v in vals]), 2),
           "median_chg_5d": round(statistics.median([v.get("chg_5d") or 0 for v in vals]), 2),
           "median_chg_1m": round(statistics.median([v.get("chg_1m") or 0 for v in vals]), 2)}

    def top(key, rev, n=15):
        xs = [(s, v) for s, v in st.items() if v.get(key) is not None]
        xs.sort(key=lambda kv: kv[1][key], reverse=rev)
        return [{"symbol": s, "industry": v.get("industry"), key: v[key], "last": v["last"]} for s, v in xs[:n]]
    out["leaders_1d"], out["laggards_1d"] = top("chg_1d", True), top("chg_1d", False)
    out["leaders_5d"], out["laggards_5d"] = top("chg_5d", True), top("chg_5d", False)
    out["leaders_1m"], out["laggards_1m"] = top("chg_1m", True, 10), top("chg_1m", False, 10)
    out["volume_surges"] = top("volume_x_20d", True, 20)
    sectors = {}
    for s, v in st.items():
        sectors.setdefault(v.get("industry") or "?", []).append(v)
    out["sectors"] = {}
    for ind, xs in sorted(sectors.items()):
        def med(k):
            ys = [x.get(k) for x in xs if x.get(k) is not None]
            return round(statistics.median(ys), 2) if ys else None
        out["sectors"][ind] = {"stocks": len(xs), "median_chg_1d": med("chg_1d"), "median_chg_5d": med("chg_5d"),
                               "median_chg_1m": med("chg_1m"), "median_chg_3m": med("chg_3m"),
                               "pct_above_sma50": round(100.0 * sum(1 for x in xs if x.get("sma50") and x["last"] > x["sma50"]) / len(xs), 1),
                               "advancers_1d": sum(1 for x in xs if (x.get("chg_1d") or 0) > 0)}
    return out


# ------------------------------------------------------------------ NSE flows and deals

def nse_fetcher():
    sys.path.insert(0, os.path.join(REPO, "nse-news"))
    import news
    return news.Fetcher(pause=0.6)


def previous_session(day, cfg):
    d = day - datetime.timedelta(days=1)
    for _ in range(10):
        if nc.trading_day(d, cfg=cfg)[0]:
            return d
        d -= datetime.timedelta(days=1)
    return d


def fetch_flows(fetcher, prev):
    out = {"errors": {}}
    try:
        out["fii_dii_cash"] = json.loads(fetcher.get("https://www.nseindia.com/api/fiidiiTradeReact", "fiidii",
                                                     nse=True).decode("utf-8"))
    except Exception as e:
        out["errors"]["fii_dii_cash"] = ac.tail(str(e), 160)
    try:
        text = fetcher.get(ARCHIVES + "nsccl/fao_participant_oi_%s.csv" % prev.strftime("%d%m%Y"),
                           "partoi").decode("utf-8", "replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        rows = list(csv.reader(lines[1:]))
        head = [h.strip() for h in rows[0]]
        out["participant_oi"] = {"title": lines[0].strip('", '),
                                 "rows": [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if r and r[0].strip()]}
    except Exception as e:
        out["errors"]["participant_oi"] = ac.tail(str(e), 160)
    return out


def fetch_deals(fetcher, n500):
    out, errors = [], {}
    for kind in ("bulk", "block"):
        try:
            text = fetcher.get(ARCHIVES + "equities/%s.csv" % kind, kind).decode("utf-8", "replace")
            for row in csv.DictReader(io.StringIO(text)):
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                try:
                    qty = float(row.get("Quantity Traded", "0").replace(",", ""))
                    px = float((row.get("Trade Price / Wght. Avg. Price") or "0").replace(",", ""))
                except ValueError:
                    continue
                out.append({"kind": kind, "date": row.get("Date"), "symbol": row.get("Symbol"),
                            "client": row.get("Client Name"), "side": row.get("Buy/Sell"), "qty": qty,
                            "price": px, "value_cr": round(qty * px / 1e7, 2),
                            "in_nifty500": row.get("Symbol") in n500})
        except Exception as e:
            errors[kind] = ac.tail(str(e), 160)
    out.sort(key=lambda d: -d["value_cr"])
    return {"deals": out[:60], "count": len(out), "errors": errors}


# ------------------------------------------------------------------ headlines

def rss_items(data, limit=40):
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        when = it.findtext("pubDate") or ""
        try:
            t = datetime.datetime.strptime(when.strip(), "%a, %d %b %Y %H:%M:%S %Z")
            when = nc.to_ist(t).strftime("%Y-%m-%d %H:%M IST")
        except ValueError:
            pass
        if src and title.endswith(" - " + src):
            title = title[:-len(src) - 3]
        if title:
            out.append({"title": title[:260], "source": src[:60], "time": when})
        if len(out) >= limit:
            break
    return out


def gnews(query, region="IN", limit=40):
    hl, gl, ceid = ("en-IN", "IN", "IN:en") if region == "IN" else ("en-US", "US", "US:en")
    url = "https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s" % (urllib.parse.quote(query), hl, gl, ceid)
    return rss_items(http_get(url, accept="application/rss+xml"), limit)


def fetch_macro_news(window="3d", pause=0.4):
    out, errors, seen = [], {}, set()
    for q, region in MACRO_QUERIES:
        try:
            for it in gnews("%s when:%s" % (q, window), region, 25):
                key = re.sub(r"\W+", " ", it["title"].lower()).strip()
                if key in seen:
                    continue
                seen.add(key)
                it["query"] = q
                out.append(it)
        except Exception as e:
            errors[q] = ac.tail(str(e), 120)
        time.sleep(pause)
    out.sort(key=lambda i: i.get("time") or "", reverse=True)
    return {"items": out, "errors": errors, "fetched_ist": stamp()}


# ------------------------------------------------------------------ one company

def fetch_fundamentals(sym):
    url = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s?%s" % (
        urllib.parse.quote(nc.yahoo_symbol(sym), safe=""),
        urllib.parse.urlencode({"type": ",".join(FUNDAMENTAL_TYPES), "period1": "1483228800",
                                "period2": str(int(time.time()) + 86400)})))
    doc = json.loads(http_get(url).decode("utf-8"))
    out = {}
    for r in ((doc.get("timeseries") or {}).get("result") or []):
        t = ((r.get("meta") or {}).get("type") or [None])[0]
        pts = [[p.get("asOfDate"), (p.get("reportedValue") or {}).get("raw")] for p in (r.get(t) or []) if p]
        pts = [p for p in pts if p[1] is not None]
        if t and pts:
            out[t] = pts[-6:]
    return {"source": "Yahoo fundamentals-timeseries (currency as reported, usually INR)", "series": out}


def clean_company_name(name):
    name = re.sub(r"\b(Ltd|Limited|Ltd\.|Corporation|Corp|Company|Co)\b\.?", "", name or "", flags=re.I)
    return " ".join(name.replace("&", "and").split())


def orb_history(home):
    """{SYMBOL: [past ORB signals]} from the studies (₹10 lakh rows) and the live reports' trades."""
    out = {}
    for fname, label in (("sizes-study-5m.csv", "replay, 5-minute bars"), ("sizes-study.csv", "replay, 1-minute bars")):
        p = os.path.join(ac.reports_dir(home), fname)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("budget") != "₹10 lakh":
                    continue
                out.setdefault(row["symbol"], []).append(
                    {"day": row.get("day"), "at": row.get("at"), "result": row.get("result"), "net": row.get("net"),
                     "r": row.get("r"), "source": label})
    rd = ac.reports_dir(home)
    for f in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
        if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", f):
            doc = ac.read_json(os.path.join(rd, f)) or {}
            for t in doc.get("trades") or []:
                out.setdefault(t.get("symbol"), []).append(
                    {"day": f[:10], "at": t.get("fill_time"), "result": t.get("exit_reason"), "net": t.get("net"),
                     "r": t.get("r"), "source": "live paper trade"})
    return out


def last_session_bars(home, sym, prev):
    doc = market.read_day(market.archive_path(home, "5m", prev.isoformat(), sym))
    rows = (doc or {}).get("bars") if isinstance(doc, dict) else doc
    out = []
    for r in rows or []:
        try:
            out.append([nc.ist_from_epoch(r[0]).strftime("%H:%M")] + [round(x, 2) if isinstance(x, float) else x
                                                                    for x in r[1:6]])
        except (TypeError, IndexError):
            continue
    return out


# ------------------------------------------------------------------ Claude

LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit|limit will reset|weekly limit|"
                      r"out of (extra )?usage|credit balance|rate_limit|quota", re.I)


class Desk(object):
    """Every Claude call of the day: the token tally, the budget, the stop on a usage limit, the saved files."""

    def __init__(self, home, ddir, settings, deep, runner=None, now=None):
        self.home, self.ddir, self.settings, self.deep = home, ddir, settings, deep
        self.runner = runner or claude_run.run_claude
        self.now = now or nc.utc_now
        self.lock = threading.Lock()
        self.usage = ac.read_json(os.path.join(ddir, "usage.json")) or {"calls": [], "tokens": 0, "cost_usd": 0.0}
        self.stopped = self.usage.get("stopped")

    def csettings(self, model):
        c = dict((self.settings or {}).get("claude") or {})
        c["deep_model"] = model
        return {"claude": c}

    def save_usage(self):
        with self.lock:
            self.usage["stopped"] = self.stopped
            ac.write_json(os.path.join(self.ddir, "usage.json"), self.usage)

    def call(self, name, kind, payload, model, deadline=None, timeout=None):
        """{"ok", "json", "text", "error"} for desk `name`; never raises. `payload` may be a function."""
        try:
            return self._call(name, kind, payload, model, deadline, timeout)
        except Exception as e:
            return {"ok": False, "error": "crashed: %s: %s" % (type(e).__name__, ac.tail(str(e), 200))}

    def _call(self, name, kind, payload, model, deadline=None, timeout=None):
        path = os.path.join(self.ddir, "desks", name + ".json")
        old = ac.read_json(path)
        if old and old.get("ok"):
            return old
        if self.stopped:
            return {"ok": False, "error": "not run: %s" % self.stopped}
        with open(os.path.join(PROMPTS, "deep-%s.md" % kind), encoding="utf-8") as fh:
            head = fh.read().strip()
        if callable(payload):
            payload = payload()
        prompt = "%s\n\n<data>\n%s\n</data>\n" % (head, json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                                                    default=str))
        est = int(len(prompt) / 3.2) + 4000
        with self.lock:
            if self.usage["tokens"] + est > int(self.deep["max_tokens"]):
                return {"ok": False, "error": "not run: the token budget (%s) is used up" % self.deep["max_tokens"]}
        left = None
        if deadline is not None:
            left = (deadline - self.now()).total_seconds()
            if left < 90:
                return {"ok": False, "error": "not run: out of time"}
        tmo = int(min(timeout or self.deep["call_timeout_s"], left if left is not None else 1e9))
        out = None
        for attempt in (1, 2):
            ok, env, err = self.runner(self.csettings(model), "deep", prompt, tmo)
            self.record(name, model, len(prompt), ok, env, err)
            if ok:
                text = env["result"].strip()
                doc = claude_run.last_json_object(text)
                body = re.split(r"```(?:json)?\s*\{", text)[0].strip() if "```" in text else text
                out = {"ok": isinstance(doc, dict), "json": doc if isinstance(doc, dict) else None, "text": body,
                       "error": None if isinstance(doc, dict) else "no JSON block in the answer", "model": model,
                       "at": nc.utc_iso()}
                if out["ok"]:
                    break
            else:
                out = {"ok": False, "error": err, "model": model, "at": nc.utc_iso()}
                if LIMIT_RE.search(str(err or "")) or LIMIT_RE.search(str((env or {}).get("result") or "")):
                    self.stopped = "Claude's usage limit at %s IST: %s" % (stamp(), ac.tail(str(err), 160))
                    self.save_usage()
                    break
                if "timed out" in str(err):
                    break
            if attempt == 1:
                time.sleep(15)
        ac.write_json(path, out)
        return out

    def record(self, name, model, chars, ok, env, err):
        u = (env or {}).get("usage") or {}
        toks = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens", "output_tokens"))
        with self.lock:
            self.usage["calls"].append({"name": name, "model": model, "ok": bool(ok), "error": None if ok else ac.tail(str(err), 200),
                                        "prompt_chars": chars, "tokens": toks,
                                        "input": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0),
                                        "output": int(u.get("output_tokens") or 0),
                                        "cost_usd": (env or {}).get("total_cost_usd"),
                                        "seconds": (env or {}).get("_seconds"), "at": stamp()})
            self.usage["tokens"] += toks
            self.usage["cost_usd"] = round(self.usage["cost_usd"] + float((env or {}).get("total_cost_usd") or 0), 4)
        self.save_usage()


# ------------------------------------------------------------------ the day

class Day(object):
    def __init__(self, home, day, settings, cfg, out=None, limit=None, runner=None, mail_on=True, log=None):
        self.home, self.day, self.settings, self.cfg = home, day, settings, cfg
        self.deep = deep_settings(settings)
        self.d = out or deep_dir(home, day)
        os.makedirs(os.path.join(self.d, "desks"), exist_ok=True)
        self.limit = limit
        self.mail_on = mail_on
        self.desk = Desk(home, self.d, settings, self.deep, runner)
        self.st = ac.read_json(self.p("state.json")) or {"date": day, "phase1": {}, "phase2": {}}
        self.log = log or (lambda msg: print("%s %s" % (stamp(), msg), flush=True))
        self.prev = previous_session(datetime.date.fromisoformat(day), cfg)

    def p(self, *parts):
        return os.path.join(self.d, *parts)

    def save(self):
        ac.write_json(self.p("state.json"), self.st)

    def at(self, hhmm):
        """naive UTC for hh:mm IST on the day."""
        d = datetime.date.fromisoformat(self.day)
        return nc.to_utc(datetime.datetime(d.year, d.month, d.day, *[int(x) for x in hhmm.split(":")]))

    def step(self, phase, name, fn):
        """Run a data step once (its result file is the marker); record the error and carry on."""
        rec = self.st[phase].setdefault("steps", {})
        if (rec.get(name) or {}).get("ok"):
            return
        t0 = time.time()
        try:
            note = fn()
            rec[name] = {"ok": True, "seconds": round(time.time() - t0, 1), "note": note}
        except Exception as e:
            rec[name] = {"ok": False, "seconds": round(time.time() - t0, 1),
                         "error": "%s: %s" % (type(e).__name__, ac.tail(str(e), 300))}
        self.log("%s %s: %s" % (phase, name, "ok" if rec[name]["ok"] else rec[name]["error"]))
        self.save()

    # ---------------------------------------------------------- universe

    def universe(self):
        u = ac.read_json(self.p("data", "universe.json"))
        if u:
            return u
        n500 = nifty_list("nifty500")
        try:
            n50 = nifty_list("nifty50")
        except DeepError:
            n50 = None
        ind = {s: i for s, _, i in n500}
        if n50:
            ind.update({s: i for s, _, i in n50})
            syms = [(s, n) for s, n, _ in n50]
        else:
            syms = [(s, n) for s, n, _ in n500][:50]
        u = {"nifty50": [{"symbol": s, "name": n, "industry": ind.get(s) or ""} for s, n in syms],
             "nifty500": [[s, n, i] for s, n, i in n500]}
        ac.write_json(self.p("data", "universe.json"), u)
        return u

    def companies(self):
        cs = self.universe()["nifty50"]
        return cs[:self.limit] if self.limit else cs

    # ---------------------------------------------------------- phase 1

    def phase1(self, stop_at):
        ph = self.st["phase1"]
        ph.setdefault("started", nc.utc_iso())
        self.save()
        self.step("phase1", "universe", lambda: "%d Nifty 50, %d Nifty 500" % (
            len(self.universe()["nifty50"]), len(self.universe()["nifty500"])))
        u = self.universe()
        n50 = [c["symbol"] for c in u["nifty50"]]

        def g():
            doc = fetch_global()
            ac.write_json(self.p("data", "global.json"), doc)
            return "%d instruments, %d failed" % (len(doc["instruments"]), len(doc["errors"]))
        self.step("phase1", "global", g)

        def b():
            br = fetch_breadth([tuple(x) for x in u["nifty500"]], self.p("data", "bars"), set(n50),
                               self.deep["breadth_workers"])
            ac.write_json(self.p("data", "breadth-raw.json"), br)
            ac.write_json(self.p("data", "breadth.json"), breadth_stats(br))
            return "%d stocks, %d failed" % (len(br["stocks"]), len(br["errors"]))
        self.step("phase1", "breadth", b)
        fetcher = [None]

        def f():
            fetcher[0] = fetcher[0] or nse_fetcher()
            doc = fetch_flows(fetcher[0], self.prev)
            ac.write_json(self.p("data", "flows.json"), doc)
            return "errors: %s" % (doc["errors"] or "none")
        self.step("phase1", "flows", f)

        def dl():
            fetcher[0] = fetcher[0] or nse_fetcher()
            doc = fetch_deals(fetcher[0], {x[0] for x in u["nifty500"]})
            ac.write_json(self.p("data", "deals.json"), doc)
            return "%d deals" % doc["count"]
        self.step("phase1", "deals", dl)

        def mn():
            doc = fetch_macro_news()
            ac.write_json(self.p("data", "macro-news.json"), doc)
            return "%d headlines" % len(doc["items"])
        self.step("phase1", "macro_news", mn)
        self.step("phase1", "dossiers", lambda: self.dossiers([c["symbol"] for c in self.companies()]))
        self.step("phase1", "company_data", self.company_data)

        # the desks
        macro = self.desk.call("macro", "macro", self.macro_payload, self.deep["desk_model"], stop_at)
        self.log("macro desk: %s" % ("ok" if macro.get("ok") else macro.get("error")))
        cs = self.companies()
        hist = orb_history(self.home)
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(self.deep["parallel"])) as ex:
            futs = {ex.submit(self.desk.call, "company-" + c["symbol"].replace("&", "_"), "company",
                              (lambda c=c: self.company_payload(c, macro, hist)), self.deep["desk_model"], stop_at): c["symbol"]
                    for c in cs}
            for fu in concurrent.futures.as_completed(futs):
                r = fu.result()
                self.log("company %s: %s" % (futs[fu], "ok" if r.get("ok") else r.get("error")))
        if self.deep.get("challenge"):
            todo = []
            for c in cs:
                j = self.company_json(c["symbol"])
                try:
                    hot = bool(j) and (j.get("sit_out") is True or float(j.get("orb_conviction") or 0) >= 4)
                except (TypeError, ValueError, AttributeError):
                    hot = False
                if hot:
                    todo.append(c)
            with concurrent.futures.ThreadPoolExecutor(max_workers=int(self.deep["parallel"])) as ex:
                futs = {ex.submit(self.desk.call, "challenge-" + c["symbol"].replace("&", "_"), "challenge",
                                  (lambda c=c: self.challenge_payload(c, macro, hist)), self.deep["desk_model"], stop_at): c["symbol"]
                        for c in todo}
                for fu in concurrent.futures.as_completed(futs):
                    r = fu.result()
                    self.log("challenge %s: %s" % (futs[fu], "ok" if r.get("ok") else r.get("error")))
        groups = {}
        for c in cs:
            groups.setdefault(c["industry"] or "Other", []).append(c)
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(self.deep["parallel"])) as ex:
            futs = {ex.submit(self.desk.call, "sector-" + slug(ind), "sector", (lambda ind=ind, members=members: self.sector_payload(ind, members, macro)),
                              self.deep["desk_model"], stop_at): ind for ind, members in sorted(groups.items())}
            for fu in concurrent.futures.as_completed(futs):
                r = fu.result()
                self.log("sector %s: %s" % (futs[fu], "ok" if r.get("ok") else r.get("error")))
        ph["done"] = nc.utc_iso()
        ph["tokens"] = self.desk.usage["tokens"]
        self.save()

    def dossiers(self, syms):
        out = self.p("stocks")
        have = (ac.read_json(os.path.join(out, "index.json")) or {}).get("stocks") or {}
        todo = [s for s in syms if s not in have or have[s].get("failed")]
        if not todo:
            return "all %d built already" % len(syms)
        names = self.p("data", "names.txt")
        write_text(names, "".join("%s|%s\n" % (c["symbol"], c["name"]) for c in self.universe()["nifty50"]))
        cmd = [sys.executable, os.path.join(REPO, "nse-news", "dossier.py"), "--tickers"] + todo + \
            ["--out", out, "--budget-s", str(int(self.deep["dossier_budget_s"])), "--names", names]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=int(self.deep["dossier_budget_s"]) + 600)
        idx = (ac.read_json(os.path.join(out, "index.json")) or {}).get("stocks") or {}
        if not idx:
            raise DeepError("dossier.py built nothing (exit %s): %s" % (p.returncode, ac.tail(p.stderr, 300)))
        return "%d stock files (exit %s)" % (len(idx), p.returncode)

    def company_data(self):
        done = 0
        for c in self.companies():
            sym = c["symbol"]
            fp = self.p("data", "fund", sym.replace("&", "_") + ".json")
            if not os.path.isfile(fp):
                try:
                    ac.write_json(fp, fetch_fundamentals(sym))
                except Exception as e:
                    ac.write_json(fp, {"error": ac.tail(str(e), 200)})
                time.sleep(0.3)
            npth = self.p("data", "news30", sym.replace("&", "_") + ".json")
            if not os.path.isfile(npth):
                try:
                    items = gnews('"%s" when:30d' % clean_company_name(c["name"]), "IN", 60)
                    ac.write_json(npth, {"query": clean_company_name(c["name"]), "items": items})
                except Exception as e:
                    ac.write_json(npth, {"error": ac.tail(str(e), 200), "items": []})
                time.sleep(0.4)
            done += 1
        return "%d companies" % done

    # ---------------------------------------------------------- payloads

    def rules(self):
        cfg = self.cfg
        acc = (self.settings.get("accounts") or {})
        return {"strategy": "15-minute opening-range breakout (09:15-09:30 IST), LONG only, intraday MIS, entry on "
                            "the first 5-minute close above the range high from 09:30 to 14:30, stop at the range "
                            "midpoint, target 2R, flat by 15:10. Paper money only.",
                "main_account": {"max_trades_per_day": cfg.get("max_trades_per_day"),
                                 "max_loss_per_trade_inr": cfg.get("max_loss_per_trade_inr"),
                                 "max_daily_loss_inr": cfg.get("max_daily_loss_inr"),
                                 "filter": "opening volume (09:20-09:30) at least 1.5x its 20-day median"},
                "comparison_accounts": "the same signals at %s trades a day" % (acc.get("caps") or []),
                "trade_cap": "first come first served: the first signals of the day take the slots",
                "universe": "Nifty 50"}

    def macro_payload(self):
        g = ac.read_json(self.p("data", "global.json"))
        cal = []
        idx = (ac.read_json(self.p("stocks", "index.json")) or {}).get("stocks") or {}
        for s, v in sorted(idx.items()):
            fl = [f for f in v.get("flags") or [] if not f.startswith("Could not read")]
            if fl:
                cal.append({"symbol": s, "flags": fl})
        return {"date": self.day, "weekday": datetime.date.fromisoformat(self.day).strftime("%A"),
                "previous_nse_session": self.prev.isoformat(), "rules": self.rules(),
                "global": compact_global(g, keep_closes=True), "global_errors": (g or {}).get("errors"),
                "nifty500_breadth": ac.read_json(self.p("data", "breadth.json")),
                "flows": ac.read_json(self.p("data", "flows.json")),
                "bulk_block_deals": ac.read_json(self.p("data", "deals.json")),
                "macro_headlines": (ac.read_json(self.p("data", "macro-news.json")) or {}).get("items"),
                "nifty50_flags": cal}

    def macro_view(self, macro):
        if not (macro or {}).get("ok"):
            return {"status": "the macro desk did not answer"}
        return {"view": macro.get("json"), "note": (macro.get("text") or "")[:6000]}

    def company_json(self, sym):
        d = ac.read_json(self.p("desks", "company-%s.json" % sym.replace("&", "_"))) or {}
        return d.get("json") if d.get("ok") else None

    def company_payload(self, c, macro, hist):
        sym = c["symbol"]
        fs = sym.replace("&", "_")
        dossier = ac.read_json(self.p("stocks", fs + ".json")) or {"status": "no NSE stock file"}
        for k in ("sources", "headlines_left_out"):
            dossier.pop(k, None)
        bars = (ac.read_json(self.p("data", "bars", fs + ".json")) or {}).get("rows") or []
        gi = global_index(ac.read_json(self.p("data", "global.json")))
        idx_t, links = INDUSTRY_LINKS.get(c["industry"], ([], []))
        br = ac.read_json(self.p("data", "breadth-raw.json")) or {}
        peers = [dict({"symbol": o["symbol"], "name": o["name"]}, **{k: (br.get("stocks") or {}).get(o["symbol"], {}).get(k)
                                                                    for k in ("last", "chg_1d", "chg_5d", "chg_1m", "chg_3m", "from_high")})
                 for o in self.universe()["nifty50"] if o["industry"] == c["industry"] and o["symbol"] != sym]
        adr = [dict({"adr": a, "nse_shares_per_adr": k}, **((gi.get(a) or {}).get("summary") or {}))
               for a, s, k in ADRS if s == sym]
        news30 = ac.read_json(self.p("data", "news30", fs + ".json")) or {}
        return {"date": self.day, "previous_nse_session": self.prev.isoformat(), "rules": self.rules(),
                "company": c, "nse_stock_file": dossier,
                "fundamentals": ac.read_json(self.p("data", "fund", fs + ".json")),
                "daily_bars_1y": {"columns": ["date", "open", "high", "low", "close", "volume"],
                                  "rows": [[nc.ist_from_epoch(r[0]).strftime("%Y-%m-%d")] + [round(x, 2) if isinstance(x, float) else x for x in r[1:6]]
                                           for r in bars[-260:]]},
                "last_session_5m_bars": {"date": self.prev.isoformat(), "columns": ["time", "open", "high", "low", "close", "volume"],
                                         "rows": last_session_bars(self.home, sym, self.prev)},
                "past_orb_signals_on_this_stock": (hist.get(sym) or [])[-80:],
                "nifty50_peers": peers, "indian_adr": adr,
                "sector_indices": [dict({"ticker": t}, **((gi.get(t) or {}).get("summary") or {})) for t in idx_t],
                "global_links": [dict({"ticker": t, "label": (gi.get(t) or {}).get("label")}, **((gi.get(t) or {}).get("summary") or {}))
                                 for t in links],
                "sector_breadth": ((ac.read_json(self.p("data", "breadth.json")) or {}).get("sectors") or {}).get(c["industry"]),
                "headlines_30d": news30.get("items") or [], "macro_desk": self.macro_view(macro)}

    def challenge_payload(self, c, macro, hist):
        p = self.company_payload(c, macro, hist)
        d = ac.read_json(self.p("desks", "company-%s.json" % c["symbol"].replace("&", "_"))) or {}
        p["first_analyst"] = {"view": d.get("json"), "note": d.get("text")}
        return p

    def sector_payload(self, ind, members, macro):
        gi = global_index(ac.read_json(self.p("data", "global.json")))
        idx_t, links = INDUSTRY_LINKS.get(ind, ([], []))
        notes = []
        for c in members:
            d = ac.read_json(self.p("desks", "company-%s.json" % c["symbol"].replace("&", "_"))) or {}
            ch = ac.read_json(self.p("desks", "challenge-%s.json" % c["symbol"].replace("&", "_"))) or {}
            notes.append({"symbol": c["symbol"], "name": c["name"], "desk": d.get("json"),
                          "desk_note": (d.get("text") or "")[:5000], "challenge": ch.get("json")})
        return {"date": self.day, "sector": ind, "rules": self.rules(), "companies": notes,
                "sector_indices": [dict({"ticker": t, "closes_30d": (gi.get(t) or {}).get("closes_30d")},
                                        **((gi.get(t) or {}).get("summary") or {})) for t in idx_t],
                "global_links": [dict({"ticker": t, "label": (gi.get(t) or {}).get("label")}, **((gi.get(t) or {}).get("summary") or {}))
                                 for t in links],
                "nifty500_sector": ((ac.read_json(self.p("data", "breadth.json")) or {}).get("sectors") or {}).get(ind),
                "macro_desk": self.macro_view(macro)}

    # ---------------------------------------------------------- phase 2

    def phase2(self, deadline):
        ph = self.st["phase2"]
        ph.setdefault("started", nc.utc_iso())
        self.save()

        def g():
            doc = fetch_global()
            ac.write_json(self.p("data", "global-am.json"), doc)
            return "%d instruments" % len(doc["instruments"])
        self.step("phase2", "global_refresh", g)

        def mn():
            old = {re.sub(r"\W+", " ", i["title"].lower()).strip()
                   for i in (ac.read_json(self.p("data", "macro-news.json")) or {}).get("items") or []}
            doc = fetch_macro_news("1d")
            doc["items"] = [i for i in doc["items"] if re.sub(r"\W+", " ", i["title"].lower()).strip() not in old]
            ac.write_json(self.p("data", "macro-news-am.json"), doc)
            return "%d new headlines" % len(doc["items"])
        self.step("phase2", "news_refresh", mn)
        cio = self.desk.call("cio", "cio", self.cio_payload, self.deep["cio_model"], deadline,
                             timeout=int(self.deep["call_timeout_s"]))
        if not cio.get("ok") and self.deep["cio_model"] != self.deep["desk_model"] and not self.desk.stopped:
            os.path.exists(self.p("desks", "cio.json")) and os.remove(self.p("desks", "cio.json"))
            cio = self.desk.call("cio", "cio", self.cio_payload, self.deep["desk_model"], deadline)
        self.log("CIO: %s" % ("ok" if cio.get("ok") else cio.get("error")))
        self.finish(cio)
        ph["done"] = nc.utc_iso()
        ph["tokens"] = self.desk.usage["tokens"]
        self.save()

    def cio_payload(self):
        cs = self.universe()["nifty50"]
        if self.limit:
            cs = cs[:self.limit]
        companies = []
        for c in cs:
            d = ac.read_json(self.p("desks", "company-%s.json" % c["symbol"].replace("&", "_"))) or {}
            ch = ac.read_json(self.p("desks", "challenge-%s.json" % c["symbol"].replace("&", "_"))) or {}
            companies.append({"symbol": c["symbol"], "industry": c["industry"], "desk": d.get("json"),
                              "desk_status": "ok" if d.get("ok") else (d.get("error") or "not run"),
                              "challenge": ch.get("json")})
        sectors = []
        for f in sorted(os.listdir(self.p("desks"))):
            if f.startswith("sector-"):
                d = ac.read_json(self.p("desks", f)) or {}
                if d.get("ok"):
                    sectors.append({"view": d.get("json"), "note": (d.get("text") or "")[:3000]})
        macro = ac.read_json(self.p("desks", "macro.json")) or {}
        idx = (ac.read_json(self.p("stocks", "index.json")) or {}).get("stocks") or {}
        card = ac.read_json(os.path.join(ac.reports_dir(self.home), "scorecard.json")) or {}
        return {"date": self.day, "now_ist": stamp(), "rules": self.rules(), "max_vetoes": int(self.deep["max_vetoes"]),
                "veto_record_so_far": (card.get("totals") or {}).get("vetoes"),
                "macro_desk": self.macro_view(macro),
                "world_now": compact_global(ac.read_json(self.p("data", "global-am.json"))
                                            or ac.read_json(self.p("data", "global.json"))),
                "headlines_since_phase1": ((ac.read_json(self.p("data", "macro-news-am.json")) or {}).get("items") or [])[:120],
                "nifty500_breadth": {k: v for k, v in (ac.read_json(self.p("data", "breadth.json")) or {}).items()
                                     if k not in ("near_52w_high", "near_52w_low")},
                "flows": ac.read_json(self.p("data", "flows.json")),
                "sector_desks": sectors, "company_desks": companies,
                "nse_flags": {s: [f for f in (v.get("flags") or []) if not f.startswith("Could not read")]
                              for s, v in sorted(idx.items())}}

    # ---------------------------------------------------------- the outputs

    def finish(self, cio):
        syms = [c["symbol"] for c in self.universe()["nifty50"]]
        j = cio.get("json") if cio.get("ok") else None
        vetoes = clean_vetoes((j or {}).get("vetoes"), syms)[:int(self.deep["max_vetoes"])]
        doc = {"date": self.day, "ok": bool(j), "at": nc.utc_iso(), "vetoes": vetoes,
               "error": None if j else cio.get("error"), "regime": (j or {}).get("regime"),
               "expected_open": (j or {}).get("expected_open"), "summary": (j or {}).get("summary"),
               "favoured": [f for f in (j or {}).get("favoured") or [] if isinstance(f, dict)][:15]}
        ac.write_json(self.p("vetoes.json"), doc)
        self.st["phase2"]["applied"] = self.apply_now(vetoes)
        ranks = self.ranks(j)
        doc["ranks"] = ranks
        ac.write_json(self.p("ranks.json"), {"as_of": self.day, "ranks": ranks, "source": "deep pre-market"})
        if self.deep.get("rank") and ranks and not self.limit and self.d == deep_dir(self.home, self.day):
            ac.write_json(os.path.join(self.home, "deep-ranks.json"),
                          {"as_of": self.day, "ranks": ranks, "source": "deep pre-market (deepscan.py)"})
        md = self.note(cio, doc)
        write_text(self.p("note.md"), md)
        self.st["phase2"]["note"] = self.p("note.md")
        paths = {"md": self.p("note.md")}
        if self.deep.get("pdf"):
            try:
                import pdf
                html_path = self.p("note.html")
                write_text(html_path, pdf.md_to_html(md, "NSE deep pre-market %s" % self.day))
                pdf.render(self.home, html_path, self.p("note.pdf"))
                paths["pdf"] = self.p("note.pdf")
                jdir = os.path.expanduser(str((self.settings.get("journal") or {}).get("dir") or ""))
                if jdir and os.path.isdir(jdir) and not self.limit:
                    shutil.copyfile(self.p("note.pdf"), os.path.join(jdir, "%s pre-market research.pdf" % self.day))
                self.st["phase2"]["pdf"] = "ok"
            except Exception as e:
                self.st["phase2"]["pdf"] = "failed: %s" % ac.tail(str(e), 200)
        if self.mail_on and self.deep.get("mail") and (self.settings.get("mail") or {}).get("enabled"):
            ok, why = self.send(md, doc, paths)
            self.st["phase2"]["mail"] = "sent" if ok else "failed: %s" % why
        self.save()

    def apply_now(self, vetoes):
        """When run_day.py's veto already ran (the deep pre-market finished late), add the sit-outs to the day's
        vetoes.json itself: every signal after this sees them. Before that, run_day.py merges them."""
        if not (self.deep.get("apply") and vetoes) or self.limit or self.d != deep_dir(self.home, self.day):
            return "not applied here"
        path = os.path.join(ac.day_dir(self.home, self.day), "vetoes.json")
        doc = ac.read_json(path)
        if not doc:
            return "left to run_day.py's pre-market"
        have = {v.get("symbol") for v in doc.get("vetoes") or []}
        added = []
        for v in vetoes:
            if v["symbol"] not in have:
                doc.setdefault("vetoes", []).append(dict(v, reason="Deep pre-market: %s" % v["reason"], source="deep"))
                added.append(v["symbol"])
        doc["deep"] = "%d applied at %s IST" % (len(added), stamp())
        ac.write_json(path, doc)
        try:
            ac.log_event(self.home, self.day, {"event": "deep_vetoes", "at": nc.utc_iso(), "added": added})
        except Exception:
            pass
        return doc["deep"]

    def ranks(self, j):
        out = {}
        for s, v in ((j or {}).get("ranks") or {}).items():
            try:
                out[nc.nse_symbol(s)] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
        if out:
            return out
        for c in self.universe()["nifty50"]:
            cj = self.company_json(c["symbol"]) or {}
            try:
                out[c["symbol"]] = round((float(cj.get("orb_conviction")) - 1) / 4.0, 3)
            except (TypeError, ValueError):
                continue
        return out

    def note(self, cio, doc):
        u = self.desk.usage
        L = ["# NSE deep pre-market research: %s" % self.day, "",
             "Paper trading only. Written by Claude before the open from public data; not financial advice.", ""]
        if cio.get("ok"):
            L += [cio.get("text") or "", ""]
        else:
            L += ["**The CIO did not answer** (%s), so no deep sit-outs apply today; Claude's normal pre-market "
                  "veto still runs. The desks that did answer are below." % cio.get("error"), ""]
            m = ac.read_json(self.p("desks", "macro.json")) or {}
            if m.get("ok"):
                L += ["## Macro desk", "", m.get("text") or "", ""]
        L += ["## Sit-outs today%s" % ("" if self.deep.get("apply") else " (shadow: not applied)"), ""]
        if doc["vetoes"]:
            L += ["| Stock | Category | Why |", "|---|---|---|"] + \
                ["| %s | %s | %s |" % (v["symbol"], v["category"], v["reason"].replace("|", "/")) for v in doc["vetoes"]]
        else:
            L.append("None.")
        L += ["", "## Every Nifty 50 desk", "",
              "| Stock | Stance | ORB conviction (1-5) | Sit out | Gap bias | One line |", "|---|---|---|---|---|---|"]
        for c in self.universe()["nifty50"]:
            j = self.company_json(c["symbol"])
            if not j:
                L.append("| %s | not run | | | | |" % c["symbol"])
                continue
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                c["symbol"], j.get("stance") or "", j.get("orb_conviction") or "", "yes" if j.get("sit_out") else "",
                j.get("gap_bias") or "", str(j.get("one_line") or "").replace("|", "/")[:200]))
        steps = []
        for ph in ("phase1", "phase2"):
            for k, v in (self.st.get(ph, {}).get("steps") or {}).items():
                steps.append("%s %s: %s" % (ph, k, v.get("note") if v.get("ok") else "FAILED " + str(v.get("error"))))
        n_ok = sum(1 for c in u["calls"] if c["ok"])
        L += ["", "## How this was made", "",
              "%d Claude calls answered (%d tried), %s tokens in all (input incl. cache, plus output), "
              "estimated at $%.2f at API prices (it runs on the Claude login on this Mac)." % (
                  n_ok, len(u["calls"]), "{:,}".format(u["tokens"]), u.get("cost_usd") or 0)]
        if self.desk.stopped:
            L.append("Stopped early: %s." % self.desk.stopped)
        L += [""] + ["- " + s for s in steps]
        return "\n".join(L) + "\n"

    def send(self, md, doc, paths):
        import mail
        m = mail._cfg(self.settings)
        msg = EmailMessage()
        head = (doc.get("summary") or "").strip().split(". ")[0][:110] if doc.get("ok") else "the CIO did not answer"
        msg["Subject"] = "[PAPER] NSE deep pre-market %s: %d sit-out%s. %s" % (
            self.day, len(doc["vetoes"]), "" if len(doc["vetoes"]) == 1 else "s", head)
        msg["From"] = m["from"]
        msg["To"] = ", ".join(m["to"])
        msg.set_content(md)
        if os.path.isfile(self.p("note.html")):
            with open(self.p("note.html"), encoding="utf-8") as fh:
                msg.add_alternative(fh.read(), subtype="html")
        for k in ("pdf", "md"):
            if paths.get(k) and os.path.isfile(paths[k]):
                with open(paths[k], "rb") as fh:
                    main, sub = ("application", "pdf") if k == "pdf" else ("text", "markdown")
                    msg.add_attachment(fh.read(), maintype=main, subtype=sub,
                                       filename="%s-deep-premarket.%s" % (self.day, k))
        return mail.deliver(m, msg)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "other").lower()).strip("-") or "other"


def clean_vetoes(raw, universe):
    allowed = set(universe)
    out, seen = [], set()
    for v in raw or []:
        if not isinstance(v, dict):
            continue
        sym = nc.nse_symbol(v.get("symbol") or "")
        if sym and sym in allowed and sym not in seen:
            seen.add(sym)
            cat = re.sub(r"[^A-Z_]", "", str(v.get("category") or "").upper().replace(" ", "_"))
            out.append({"symbol": sym, "reason": str(v.get("reason") or "")[:300],
                        "category": cat if cat in VETO_CATEGORIES else "OTHER", "source": "deep"})
    return out


# ------------------------------------------------------------------ what run_day.py and report.py ask

def live_vetoes(home, day, settings, universe):
    """(vetoes, status) for run_day.py's pre-market: the CIO's sit-outs when "apply" is on and they are ready."""
    d = deep_settings(settings)
    if not d.get("enabled"):
        return [], "off"
    doc = ac.read_json(os.path.join(deep_dir(home, day), "vetoes.json"))
    if not doc:
        st = ac.read_json(os.path.join(deep_dir(home, day), "state.json")) or {}
        return [], "not ready (phase 2 %s)" % ("running" if (st.get("phase2") or {}).get("started") else "not started")
    if not doc.get("ok"):
        return [], "the CIO did not answer: %s" % ac.tail(str(doc.get("error")), 160)
    vs = clean_vetoes(doc.get("vetoes"), universe)
    if not d.get("apply"):
        return [], "shadow: %d sit-out%s not applied" % (len(vs), "" if len(vs) == 1 else "s")
    return vs, "%d applied" % len(vs)


def review(home, day, rep):
    """The deep pre-market against the day, for the report: its sit-outs replayed and its favoured stocks'
    signals. None when it did not run."""
    dd = deep_dir(home, day)
    if not os.path.isdir(dd):
        return None
    st = ac.read_json(os.path.join(dd, "state.json")) or {}
    usage = ac.read_json(os.path.join(dd, "usage.json")) or {}
    v = ac.read_json(os.path.join(dd, "vetoes.json")) or {}
    trades = {t.get("symbol"): t for t in rep.get("trades") or []}
    shadows = {}
    for r in (rep.get("shadows") or {}).get("rows") or []:
        if (r.get("side") or "long") == "long":
            shadows[r["symbol"]] = r
    cands = {c.get("symbol"): c for c in rep.get("candidates") or [] if (c.get("side") or "long") == "long"}

    def outcome(sym):
        if sym in trades:
            return {"what": "traded", "net": trades[sym].get("net"), "r": trades[sym].get("r")}
        if sym in shadows:
            s = shadows[sym]
            return {"what": "%s (replayed)" % s.get("why"), "net": s.get("net"), "r": s.get("r"),
                    "result": s.get("result")}
        if sym in cands:
            return {"what": cands[sym].get("outcome") or "signal", "net": None}
        return {"what": "no signal", "net": None}
    applied = {x["symbol"] for x in rep.get("vetoes") or [] if x.get("source") == "deep"}
    vrows = [dict(x, applied=x["symbol"] in applied, **{"outcome": outcome(x["symbol"])}) for x in v.get("vetoes") or []]
    frows = []
    for f in v.get("favoured") or []:
        sym = nc.nse_symbol(f.get("symbol") or "")
        if sym:
            frows.append({"symbol": sym, "conviction": f.get("conviction"), "why": str(f.get("why") or "")[:200],
                          "outcome": outcome(sym)})
    nets = [x["outcome"]["net"] for x in vrows if x["outcome"].get("net") is not None]
    fnets = [x["outcome"]["net"] for x in frows if x["outcome"].get("net") is not None]
    return {"ran": bool(st.get("phase1")), "cio_ok": bool(v.get("ok")), "error": v.get("error"),
            "regime": v.get("regime"), "expected_open": v.get("expected_open"), "summary": v.get("summary"),
            "vetoes": vrows, "vetoes_net_if_taken": round(sum(nets), 2) if nets else None,
            "favoured": frows, "favoured_net": round(sum(fnets), 2) if fnets else None,
            "tokens": usage.get("tokens"), "cost_usd": usage.get("cost_usd"),
            "calls": len(usage.get("calls") or []), "calls_ok": sum(1 for c in usage.get("calls") or [] if c.get("ok")),
            "stopped": usage.get("stopped"), "note": (st.get("phase2") or {}).get("note")}


# ------------------------------------------------------------------ the clock

class Lock(object):
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                with open(self.path) as fh:
                    pid = int(fh.read().strip() or 0)
                os.kill(pid, 0)
                return None
            except (ValueError, OSError):
                os.remove(self.path)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


def tick(home, now=None, runner=None):
    """Whatever the clock says is due. 0 always, unless something crashed."""
    now = now or nc.utc_now()
    settings = ac.load_settings(home)
    d = deep_settings(settings)
    if not d.get("enabled"):
        return 0
    cfg = ac.load_cfg(home)
    ist = nc.to_ist(now)
    day = ist.date()
    if not nc.trading_day(day, cfg=cfg)[0]:
        return 0
    hm = ist.strftime("%H:%M")
    if not (minutes(d["start"]) <= minutes(hm) < minutes(d["done_by"])):
        return 0
    D = Day(home, day.isoformat(), settings, cfg, runner=runner)
    if D.st["phase1"].get("done") and D.st["phase2"].get("done"):
        return 0
    with Lock(os.path.join(D.d, "lock")) as got:
        if not got:
            return 0
        refresh = D.at(d["refresh_at"])
        if not D.st["phase1"].get("done"):
            D.phase1(refresh - datetime.timedelta(minutes=5))
        if minutes(ist_hm(nc.utc_now())) >= minutes(d["refresh_at"]) and not D.st["phase2"].get("done"):
            D.phase2(D.at(d["done_by"]) + datetime.timedelta(minutes=8))
    return 0


def status(home, day):
    dd = deep_dir(home, day)
    st = ac.read_json(os.path.join(dd, "state.json"))
    if not st:
        print("No deep pre-market for %s (folder %s)." % (day, dd))
        return 0
    u = ac.read_json(os.path.join(dd, "usage.json")) or {}
    print("Deep pre-market %s" % day)
    for ph in ("phase1", "phase2"):
        p = st.get(ph) or {}
        print("  %s: started %s, done %s" % (ph, p.get("started"), p.get("done")))
        for k, v in (p.get("steps") or {}).items():
            print("    %-14s %s" % (k, v.get("note") if v.get("ok") else "FAILED: %s" % v.get("error")))
    calls = u.get("calls") or []
    print("  Claude: %d calls, %d ok, %s tokens, ~$%.2f at API prices%s" % (
        len(calls), sum(1 for c in calls if c.get("ok")), "{:,}".format(u.get("tokens") or 0), u.get("cost_usd") or 0,
        ("; stopped: %s" % u["stopped"]) if u.get("stopped") else ""))
    v = ac.read_json(os.path.join(dd, "vetoes.json"))
    if v:
        print("  CIO: %s; sit-outs: %s" % ("ok" if v.get("ok") else "failed: %s" % v.get("error"),
                                         ", ".join("%s (%s)" % (x["symbol"], x["category"]) for x in v["vetoes"]) or "none"))
    return 0


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Deep pre-market research for the paper autopilot.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tick", help="what launchd runs every 5 minutes")
    p = sub.add_parser("run", help="run a phase now")
    p.add_argument("--phase", choices=("1", "2", "all"), default="all")
    p.add_argument("--date", help="YYYY-MM-DD (default today, IST)")
    p.add_argument("--limit", type=int, help="only the first N Nifty 50 companies (a test)")
    p.add_argument("--out", help="write here instead of the day's deep/ folder (a test)")
    p.add_argument("--no-mail", action="store_true")
    p.add_argument("--phase1-until", help="HH:MM IST: no new phase-1 desk calls after this")
    p.add_argument("--cio-by", help="HH:MM IST: the CIO must answer by then")
    p = sub.add_parser("status")
    p.add_argument("--date")
    a = ap.parse_args(argv)
    home = ac.trader_home()
    if a.cmd == "tick":
        return tick(home)
    if a.cmd == "status":
        return status(home, a.date or nc.to_ist(nc.utc_now()).date().isoformat())
    if a.cmd == "run":
        settings = ac.load_settings(home)
        day = a.date or nc.to_ist(nc.utc_now()).date().isoformat()
        D = Day(home, day, settings, ac.load_cfg(home), out=a.out, limit=a.limit, mail_on=not a.no_mail)
        far = nc.utc_now() + datetime.timedelta(hours=3)
        with Lock(os.path.join(D.d, "lock")) as got:        # the same lock as tick: never two runs on one folder
            if not got:
                print("deepscan.py: another run holds %s" % os.path.join(D.d, "lock"), file=sys.stderr)
                return 1
            if a.phase in ("1", "all"):
                D.phase1(D.at(a.phase1_until) if a.phase1_until else far)
            if a.phase in ("2", "all"):
                D.phase2(D.at(a.cio_by) if a.cio_by else far)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
