#!/usr/bin/env python3
"""Stock file for nse-news: everything public on one NSE stock, in one place. Standard library only.

    python3 dossier.py RELIANCE                      # the whole file, as text
    python3 dossier.py RELIANCE TCS --json
    python3 dossier.py RELIANCE TCS --out DIR        # DIR/<SYM>.md, DIR/<SYM>.json and DIR/index.json
    python3 dossier.py RELIANCE --no-results         # skip the quarterly numbers (nse-analyst)

What goes in (each section names its source, and says so when the source failed):
  flags      the short list worth reading before a trade, in words: results or a board meeting
             close, an ex-date close, the F&O ban, a big move or volume, a 52-week high or low,
             insider or promoter selling, a pledge, a bulk or block deal, important filings
  price      Yahoo daily bars for a year: last close, moves over 1 day, 1 week, 1 month, 3 months
             and 1 year, the 52-week high and low, the 20, 50 and 200-day averages, the average
             daily range and the last session's volume against its 20-day average
  band       the circuit band and series from NSE's sec_list.csv
  events     NSE board meetings (coming and recent) and corporate actions (dividends, splits,
             bonus issues, record and ex-dates)
  results    the latest quarters from nse-analyst's results.py (NSE XBRL): revenue, net profit,
             EPS, margins, growth, the next results date it expects
  filings    NSE corporate announcements in the last 30 days, tagged like news.py, "!" = important
  headlines  Google News by company name, the last 7 days (quote pages dropped)
  owners     NSE shareholding pattern: promoter and public %, the last 4 quarters
  pledge     NSE pledge data: promoter shares pledged, and all pledged shares
  insiders   SEBI insider-trading (PIT) disclosures on NSE in the last 90 days
  deals      bulk and block deals: nse-deals' history (trader/state/deals.json) and NSE's latest files
  F&O ban    whether NSE's ban list for the next session names the stock

Sources (no keys, no accounts; NSE's /api/ with nc.nse_headers(), as news.py):
  https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=<SYM>
  https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol=<SYM>
  https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=<SYM>
  https://www.nseindia.com/api/corporate-pledgedata?index=equities&symbol=<SYM>
  https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYM>
  https://www.nseindia.com/api/corporate-announcements (news.NSE_ANN)
  https://nsearchives.nseindia.com/content/fo/fo_secban.csv
  https://nsearchives.nseindia.com/content/equities/sec_list.csv
  https://nsearchives.nseindia.com/content/equities/bulk.csv and block.csv
  Google News RSS (news.GNEWS) and Yahoo's chart endpoint (nc.yahoo_chart_url)

With --out, each stock is written as soon as it is built and index.json after each stock, so a
run cut short still leaves every finished file. --budget-s stops starting new stocks after that
many seconds (the rest are listed as "not built: out of time").

Exit codes: 0 every stock built (a source may still have failed: see "failed"), 1 a stock had
every source fail, 2 usage. Facts from public sources, not advice: open the filing before acting.
"""

import argparse
import csv
import datetime as _dt
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402
import news  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYST = os.path.join(os.path.dirname(HERE), "nse-analyst", "results.py")

NSE_BM = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=%s"
NSE_CA = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol=%s"
NSE_SHP = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=%s"
NSE_PLEDGE = "https://www.nseindia.com/api/corporate-pledgedata?index=equities&symbol=%s"
NSE_PIT = "https://www.nseindia.com/api/corporates-pit?index=equities&symbol=%s"
FO_BAN = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
SEC_LIST = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
DEALS = (("bulk", "https://nsearchives.nseindia.com/content/equities/bulk.csv"),
         ("block", "https://nsearchives.nseindia.com/content/equities/block.csv"))

FILING_DAYS = 30
NEWS_DAYS = 7
INSIDER_DAYS = 90
DEAL_DAYS = 90
SOON_DAYS = 14             # a board meeting or ex-date this close is a flag
BIG_MOVE_PCT = 3.0         # the last session's move that is a flag
BIG_VOLUME_X = 2.0         # the last session's volume against its 20-day average
NEAR_EXTREME_PCT = 2.0     # this close to the 52-week high or low is a flag
INSIDER_FLAG_INR = 1e7     # ₹1 crore of insider market buying or selling in 90 days
PLEDGE_FLAG_PCT = 5.0      # promoter shares pledged, % of the promoters' holding
PROMOTER_MOVE_PTS = 1.0    # promoter holding change, percentage points, over the last quarter
SECTIONS = ("price", "band", "events", "results", "filings", "headlines", "owners", "pledge", "insiders",
            "deals", "fo_ban")
LABELS = {"price": "Yahoo prices", "band": "NSE sec_list", "events": "NSE board meetings and corporate actions",
          "results": "nse-analyst results", "filings": "NSE filings", "headlines": "Google News",
          "owners": "NSE shareholding", "pledge": "NSE pledge data", "insiders": "NSE insider trades",
          "deals": "NSE bulk and block deals", "fo_ban": "NSE F&O ban list"}
INSIDER_PEOPLE = re.compile(r"promoter|director|key managerial|kmp|designated|immediate relative", re.I)


class DossierError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v


def _date(text):
    """'30-Oct-2026', '30-OCT-2026 16:25:15', '30-10-2026' or ISO -> date, else None."""
    t = str(text or "").strip()
    for fmt, n in (("%d-%b-%Y", 11), ("%d-%m-%Y", 10), ("%Y-%m-%d", 10)):
        try:
            return _dt.datetime.strptime(t[:n].title() if "b" in fmt else t[:n], fmt).date()
        except ValueError:
            continue
    return None


def _json(data, what):
    return news._json(data, what)


def _rows(doc, what):
    return news._rows(doc, what)


def band_words(band):
    v = _f(band)
    if v is not None:
        return "circuit band %g%%" % v
    if re.search(r"no band", str(band or ""), re.I):
        return "no fixed circuit band (an F&O stock: NSE's dynamic price bands apply)"
    return "circuit band %s" % (band or "unknown")


def crore(x):
    return nc.crore(x) if x is not None else "n/a"


def pct(x, signed=True):
    if x is None:
        return "n/a"
    return ("%+.1f%%" if signed else "%.1f%%") % x


# ---------------------------------------------------------------- price (Yahoo, a year of daily bars)

def parse_daily(data, sym):
    """Yahoo chart JSON (daily) -> [{"date", "open", "high", "low", "close", "volume"}], oldest first."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        res = doc["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        ts = res.get("timestamp") or []
    except (ValueError, KeyError, IndexError, TypeError):
        raise DossierError("Yahoo's chart for %s is not usable" % sym)
    out = []
    for i, t in enumerate(ts):
        c = (q.get("close") or [None] * len(ts))[i]
        if c is None:
            continue
        g = lambda k: ((q.get(k) or [None] * len(ts))[i])
        out.append({"date": nc.ist_from_epoch(t).date().isoformat(), "open": g("open"), "high": g("high") or c,
                    "low": g("low") or c, "close": float(c), "volume": g("volume") or 0})
    if not out:
        raise DossierError("Yahoo's chart for %s has no closes" % sym)
    return out


def price_facts(bars):
    last = bars[-1]
    closes = [b["close"] for b in bars]

    def back(n):
        return round(100 * (closes[-1] / closes[-1 - n] - 1), 2) if len(closes) > n else None

    def sma(n):
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    yr = bars[-252:]
    hi = max(b["high"] for b in yr)
    lo = min(b["low"] for b in yr)
    rng = [(b["high"] - b["low"]) / b["close"] * 100 for b in bars[-20:] if b["close"]]
    vols = [b["volume"] for b in bars[-21:-1] if b["volume"]]
    avg_vol = sum(vols) / len(vols) if vols else None
    out = {"date": last["date"], "close": round(last["close"], 2), "change_1d_pct": back(1),
           "change_1w_pct": back(5), "change_1m_pct": back(21), "change_3m_pct": back(63),
           "change_1y_pct": back(min(251, len(closes) - 1)) if len(closes) > 200 else None,
           "high_52w": round(hi, 2), "low_52w": round(lo, 2),
           "from_high_pct": round(100 * (last["close"] / hi - 1), 2) if hi else None,
           "from_low_pct": round(100 * (last["close"] / lo - 1), 2) if lo else None,
           "sma20": sma(20), "sma50": sma(50), "sma200": sma(200),
           "avg_range_20d_pct": round(sum(rng) / len(rng), 2) if rng else None,
           "volume": last["volume"], "avg_volume_20d": round(avg_vol) if avg_vol else None,
           "volume_x": round(last["volume"] / avg_vol, 2) if avg_vol and last["volume"] else None,
           "sessions": len(bars)}
    return out


# ---------------------------------------------------------------- NSE: events, owners, pledge, insiders

def parse_board_meetings(data, sym, today):
    rows = _rows(_json(data, "NSE board meetings for %s" % sym), "NSE board meetings for %s" % sym)
    seen, out = set(), []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _date(r.get("bm_date"))
        if not d:
            continue
        purpose = news._clean(r.get("bm_purpose")) or "Board meeting"
        key = (d, purpose.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": d.isoformat(), "days": (d - today).days, "purpose": purpose,
                    "desc": news._clean(r.get("bm_desc"))[:400], "name": news._clean(r.get("sm_name")) or None,
                    "industry": news._clean(r.get("sm_indusrty")).strip("- ") or None,
                    "results": bool(re.search(r"result", purpose + " " + (r.get("bm_desc") or ""), re.I))})
    dates = {}
    for e in out:
        dates.setdefault(e["date"], []).append(e)
    out = [e for e in out if not (re.search(r"intimation", e["purpose"], re.I) and len(dates[e["date"]]) > 1)]
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


def parse_actions(data, sym, today):
    rows = _rows(_json(data, "NSE corporate actions for %s" % sym), "NSE corporate actions for %s" % sym)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _date(r.get("exDate"))
        if not d:
            continue
        out.append({"ex_date": d.isoformat(), "days": (d - today).days, "subject": news._clean(r.get("subject")),
                    "record_date": (_date(r.get("recDate")) or d).isoformat(), "name": news._clean(r.get("comp"))})
    out.sort(key=lambda e: e["ex_date"], reverse=True)
    return out


def parse_shareholding(data, sym):
    doc = _json(data, "NSE shareholding for %s" % sym)
    rows = _rows(doc, "NSE shareholding for %s" % sym)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _date(r.get("date"))
        prom, pub = _f(r.get("pr_and_prgrp")), _f(r.get("public_val"))
        if not d or prom is None:
            continue
        out.append({"quarter_end": d.isoformat(), "promoter_pct": prom, "public_pct": pub,
                    "filed": news._clean(r.get("broadcastDate")) or None, "name": news._clean(r.get("name"))})
    out.sort(key=lambda e: e["quarter_end"], reverse=True)
    dedup, seen = [], set()
    for e in out:
        if e["quarter_end"] not in seen:
            seen.add(e["quarter_end"])
            dedup.append(e)
    return dedup[:4]


def parse_pledge(data, sym):
    rows = _rows(_json(data, "NSE pledge data for %s" % sym), "NSE pledge data for %s" % sym)
    for r in rows:
        if isinstance(r, dict):
            as_of = _date(r.get("shp")) or _date(r.get("broadcastDt"))
            return {"as_of": as_of.isoformat() if as_of else None,
                    "promoter_pledged_pct": _f(r.get("percPromoterShares")),
                    "promoter_pledged_of_total_pct": _f(r.get("percTotShares")),
                    "all_pledged_pct": _f(r.get("percSharesPledged")),
                    "promoter_holding_pct": _f(r.get("percPromoterHolding"))}
    return None          # no pledge filing: not the same as 0%


def parse_insiders(data, sym, today, days=INSIDER_DAYS):
    rows = _rows(_json(data, "NSE insider trades for %s" % sym), "NSE insider trades for %s" % sym)
    out, latest = [], None
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _date(r.get("acqfromDt")) or _date(r.get("date"))
        if not d:
            continue
        latest = max(latest, d) if latest else d
        if (today - d).days > days:
            continue
        side = news._clean(r.get("tdpTransactionType")) or "?"
        out.append({"date": d.isoformat(), "who": news._clean(r.get("acqName")),
                    "category": news._clean(r.get("personCategory")), "side": side,
                    "mode": news._clean(r.get("acqMode")), "shares": _f(r.get("secAcq")),
                    "value": _f(r.get("secVal")), "security": news._clean(r.get("secType"))})
    out.sort(key=lambda e: e["date"], reverse=True)
    market = [e for e in out if re.search(r"market", e["mode"] or "", re.I) and not
              re.search(r"off", e["mode"] or "", re.I) and INSIDER_PEOPLE.search(e["category"] or "")]
    bought = sum(e["value"] or 0 for e in market if e["side"].lower().startswith("buy"))
    sold = sum(e["value"] or 0 for e in market if e["side"].lower().startswith("sell"))
    return {"trades": out, "market_bought": round(bought, 2), "market_sold": round(sold, 2),
            "latest_filing": latest.isoformat() if latest else None}


# ---------------------------------------------------------------- shared files: F&O ban, sec_list, deals

def parse_fo_ban(text):
    """fo_secban.csv -> (date text, {SYMBOL}). The file is a title line, then '1,SYMBOL' rows."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or "ban" not in lines[0].lower():
        raise DossierError("NSE's F&O ban file came back in another shape")
    m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", lines[0])
    syms = set()
    for l in lines[1:]:
        parts = [p.strip() for p in l.split(",")]
        if len(parts) >= 2 and nc.is_nse_symbol(parts[1]):
            syms.add(parts[1].upper())
    return (m.group(1) if m else None), syms


def parse_sec_list(text):
    """sec_list.csv -> {SYMBOL: {"series", "band"}} (EQ preferred when a symbol has two rows)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        sym, series = r.get("symbol"), r.get("series")
        if not sym:
            continue
        if sym in out and out[sym]["series"] == "EQ":
            continue
        out[sym] = {"series": series, "band": r.get("band"), "remarks": r.get("remarks") if r.get("remarks") not in
                    ("", "-") else None}
    if len(out) < 500:
        raise DossierError("NSE's sec_list.csv has only %d rows" % len(out))
    return out


def parse_deal_file(text, kind):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {re.sub(r"\s+", " ", (k or "")).strip().lower(): (v or "").strip() for k, v in r.items()}
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        qty = _f(r.get("quantity traded"))
        px = _f(next((v for k, v in r.items() if k.startswith("trade price")), None))
        d = _date(r.get("date"))
        rows.append({"kind": kind, "date": d.isoformat() if d else r.get("date"), "symbol": sym,
                     "client": r.get("client name"), "side": (r.get("buy/sell") or "").upper(), "qty": qty,
                     "price": px, "value": round(qty * px, 2) if qty and px else None, "source": "NSE file"})
    return rows


def deals_history(path):
    doc = nc.read_json(path) if path and os.path.isfile(path) else None
    out = []
    for d in ((doc or {}).get("deals") or {}).values():
        if not isinstance(d, dict) or not d.get("symbol"):
            continue
        qty, px = _f(d.get("qty") or d.get("quantity")), _f(d.get("price"))
        out.append({"kind": d.get("kind") or d.get("type"), "date": d.get("date"), "symbol": d["symbol"],
                    "client": d.get("client"), "side": (d.get("side") or "").upper(), "qty": qty, "price": px,
                    "value": _f(d.get("value")) or (round(qty * px, 2) if qty and px else None),
                    "source": "nse-deals history"})
    return out


class Shared(object):
    """What every stock's file reads once a run: the F&O ban list, sec_list and the deal files."""

    def __init__(self, fetcher, deals_state=None):
        self.fetcher = fetcher
        self.errors = {}
        self.ban_date, self.ban = None, None
        self.band = None
        self.deals = None
        try:
            self.ban_date, self.ban = parse_fo_ban(fetcher.get(FO_BAN, "fo_secban.csv").decode("utf-8", "replace"))
        except (news.NewsError, DossierError) as exc:
            self.errors["fo_ban"] = str(exc)
        try:
            self.band = parse_sec_list(fetcher.get(SEC_LIST, "sec_list.csv").decode("utf-8", "replace"))
        except (news.NewsError, DossierError) as exc:
            self.errors["band"] = str(exc)
        deals, failed = deals_history(deals_state), []
        for kind, url in DEALS:
            try:
                deals.extend(parse_deal_file(fetcher.get(url, "%s.csv" % kind).decode("utf-8", "replace"), kind))
            except news.NewsError as exc:
                failed.append("%s: %s" % (kind, exc))
        seen, self.deals = set(), []
        for d in deals:
            key = (d["kind"], d["date"], d["symbol"], (d["client"] or "").lower(), d["side"], d["qty"])
            if key not in seen:
                seen.add(key)
                self.deals.append(d)
        if failed and len(failed) == len(DEALS) and not self.deals:
            self.errors["deals"] = "; ".join(failed)
        elif failed:
            self.errors["deals_partial"] = "; ".join(failed)


# ---------------------------------------------------------------- results (nse-analyst)

def results_for(sym, timeout=150, run=None):
    """nse-analyst's results.py --json for one symbol; {"changes", "quarter", "filed", ...} or raise."""
    if not os.path.isfile(ANALYST):
        raise DossierError("nse-analyst is not installed next to nse-news")
    argv = [sys.executable, ANALYST, sym, "--json", "--quarters", "5", "--no-calendar"]
    try:
        p = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DossierError("results.py took longer than %ds" % timeout)
    try:
        doc = json.loads(p.stdout or "null")
    except ValueError:
        doc = None
    if not doc:
        raise DossierError("results.py: %s" % ((p.stderr or "").strip()[-300:] or "no output"))
    d = doc[0]
    rows = (d.get("result") or {}).get("rows") or []
    top = rows[0] if rows else {}
    return {"quarter": top.get("quarter"), "quarter_end": top.get("end"), "filed": top.get("filed"),
            "basis": (d.get("result") or {}).get("basis"), "changes": d.get("changes") or [],
            "results_due": d.get("results_due"), "notes": d.get("notes") or [],
            "revenue": top.get("top_line"), "net_profit": top.get("net_profit"), "eps": top.get("eps_diluted"),
            "ttm": (d.get("result") or {}).get("ttm")}


# ---------------------------------------------------------------- one stock

def build(sym, fetcher, shared, now, name=None, results=True, google=True, run=None):
    """The file for one stock: {"symbol", "name", "built_at", sections..., "flags", "failed"}."""
    today = nc.to_ist(now).date()
    q = urllib.parse.quote(sym, safe="")          # M&M: an unquoted & would end the query string
    doc = {"symbol": sym, "name": name, "built_at": nc.utc_iso(now), "built_ist": nc.ist_text(now),
           "failed": {}, "sources": {}}

    def attempt(key, fn):
        try:
            doc[key] = fn()
            doc["sources"][key] = "ok"
        except (news.NewsError, DossierError) as exc:
            doc[key] = None
            doc["failed"][key] = str(exc)
            doc["sources"][key] = "failed"

    attempt("price", lambda: price_facts(parse_daily(fetcher.get(nc.yahoo_chart_url(sym, "1y", "1d"),
                                                                 "chart1y-%s.json" % sym), sym)))
    if shared.band is not None:
        doc["band"] = shared.band.get(sym)
        doc["sources"]["band"] = "ok"
    else:
        doc["band"], doc["failed"]["band"], doc["sources"]["band"] = None, shared.errors.get("band"), "failed"

    def events():
        """Board meetings and corporate actions, each read on its own: one failing keeps the other."""
        out, errs = {"board_meetings": [], "actions": []}, []
        for key, url, name, parse in (("board_meetings", NSE_BM, "bm", parse_board_meetings),
                                      ("actions", NSE_CA, "ca", parse_actions)):
            try:
                out[key] = parse(fetcher.get(url % q, "%s-%s.json" % (name, sym), nse=True), sym, today)
            except (news.NewsError, DossierError) as exc:
                errs.append("%s: %s" % (key.replace("_", " "), exc))
        if len(errs) == 2:
            raise DossierError("; ".join(errs))
        out["errors"] = errs
        return out

    attempt("events", events)
    ev = doc.get("events") or {}
    for e in ev.get("board_meetings") or []:
        doc["name"] = doc["name"] or e.get("name")
        doc["industry"] = doc.get("industry") or e.get("industry")
    attempt("owners", lambda: parse_shareholding(fetcher.get(NSE_SHP % q, "shp-%s.json" % sym, nse=True), sym))
    for e in doc.get("owners") or []:
        doc["name"] = doc["name"] or e.get("name")
    attempt("pledge", lambda: parse_pledge(fetcher.get(NSE_PLEDGE % q, "pledge-%s.json" % sym, nse=True), sym))
    attempt("insiders", lambda: parse_insiders(fetcher.get(NSE_PIT % q, "pit-%s.json" % sym, nse=True), sym, today))

    def filings():
        frm = (today - _dt.timedelta(days=FILING_DAYS)).strftime("%d-%m-%Y")
        url = news.NSE_ANN % (q, frm, today.strftime("%d-%m-%Y"))
        items = news.parse_announcements(fetcher.get(url, "nse-announcements-%s.json" % sym, nse=True), sym)
        news.tag(items, {sym: doc["name"]})
        return _items(items)

    attempt("filings", filings)
    for it in doc.get("filings") or []:
        doc["name"] = doc["name"] or it.get("company")
    if google:
        def headlines():
            url = news.gnews_query(doc["name"], sym, NEWS_DAYS * 24)
            items, _ = news.parse_gnews(fetcher.get(url, "gnews-%s.xml" % sym), sym)
            cut = nc.utc_now() if now is None else now
            items = [i for i in items if not i.get("time") or (cut - i["time"]).days < NEWS_DAYS]
            news.tag(items, {sym: doc["name"]})
            items = news.dedupe(items)
            mine = [i for i in items if sym in (i.get("touches") or []) and
                    news.mention_patterns({sym: doc["name"]})[sym].search(i["title"])]
            doc["headlines_left_out"] = len(items) - len(mine)
            return _items(mine)[:25]
        attempt("headlines", headlines)
    if results:
        attempt("results", lambda: results_for(sym, run=run))
    if shared.ban is not None:
        doc["fo_ban"] = {"date": shared.ban_date, "banned": sym in shared.ban}
        doc["sources"]["fo_ban"] = "ok"
    else:
        doc["fo_ban"], doc["failed"]["fo_ban"], doc["sources"]["fo_ban"] = None, shared.errors.get("fo_ban"), "failed"
    if shared.deals is not None and "deals" not in shared.errors:
        cut = (today - _dt.timedelta(days=DEAL_DAYS)).isoformat()
        doc["deals"] = sorted([d for d in shared.deals if d["symbol"] == sym and str(d["date"]) >= cut],
                              key=lambda d: str(d["date"]), reverse=True)
        doc["sources"]["deals"] = "ok"
    else:
        doc["deals"], doc["failed"]["deals"], doc["sources"]["deals"] = None, shared.errors.get("deals"), "failed"
    doc["flags"] = flags(doc, today)
    return doc


def _items(items):
    out = []
    for i in sorted(items, key=lambda x: x.get("time") or _dt.datetime.min, reverse=True):
        out.append({"time_ist": nc.ist_text(i["time"]) if i.get("time") else None, "source": i.get("source"),
                    "title": i.get("title"), "subject": i.get("subject"), "link": i.get("link"),
                    "tags": i.get("tags") or [], "important": bool(i.get("important"))})
    return out


def flags(doc, today):
    """The few things worth reading before a trade, in words, most pressing first."""
    out = []
    fb = doc.get("fo_ban") or {}
    if fb.get("banned"):
        out.append("In NSE's F&O ban list for %s: no new F&O positions; the cash stock often moves hard"
                   % (fb.get("date") or "the next session"))
    ev = doc.get("events") or {}
    for e in ev.get("board_meetings") or []:
        if 0 <= e["days"] <= SOON_DAYS:
            out.append("Board meeting %s (%s): %s" % ("TODAY" if e["days"] == 0 else "in %d day%s" % (
                e["days"], "" if e["days"] == 1 else "s"), e["date"], e["purpose"]))
    for a in ev.get("actions") or []:
        if 0 <= a["days"] <= SOON_DAYS:
            out.append("Ex-date %s (%s): %s" % ("TODAY" if a["days"] == 0 else "in %d day%s" % (
                a["days"], "" if a["days"] == 1 else "s"), a["ex_date"], a["subject"]))
    res = doc.get("results") or {}
    filed = _date(res.get("filed"))
    if filed and 0 <= (today - filed).days <= 2:
        out.append("Results for %s filed %s" % (res.get("quarter") or "the quarter", res.get("filed")))
    p = doc.get("price") or {}
    if p.get("change_1d_pct") is not None and abs(p["change_1d_pct"]) >= BIG_MOVE_PCT:
        out.append("Moved %s in the last session (%s)" % (pct(p["change_1d_pct"]), p.get("date")))
    if p.get("volume_x") and p["volume_x"] >= BIG_VOLUME_X:
        out.append("Volume %.1fx its 20-day average in the last session" % p["volume_x"])
    if p.get("from_high_pct") is not None and p["from_high_pct"] >= -NEAR_EXTREME_PCT:
        out.append("Within %.1f%% of its 52-week high (%s)" % (abs(p["from_high_pct"]), nc.inr(p["high_52w"])))
    if p.get("from_low_pct") is not None and p["from_low_pct"] <= NEAR_EXTREME_PCT:
        out.append("Within %.1f%% of its 52-week low (%s)" % (abs(p["from_low_pct"]), nc.inr(p["low_52w"])))
    band = doc.get("band") or {}
    if band.get("series") and band["series"] != "EQ":
        out.append("Series %s, not EQ: the autopilot will not trade it" % band["series"])
    if band.get("band") and _f(band["band"]) is not None and _f(band["band"]) < 10:
        out.append("Circuit band %s%%: the price can lock at the limit" % band["band"])
    ins = doc.get("insiders") or {}
    if (ins.get("market_sold") or 0) >= INSIDER_FLAG_INR:
        out.append("Insiders sold %s on the market in the last %d days" % (crore(ins["market_sold"]), INSIDER_DAYS))
    if (ins.get("market_bought") or 0) >= INSIDER_FLAG_INR:
        out.append("Insiders bought %s on the market in the last %d days" % (crore(ins["market_bought"]),
                                                                             INSIDER_DAYS))
    pl = doc.get("pledge") or {}
    if (pl.get("promoter_pledged_pct") or 0) >= PLEDGE_FLAG_PCT:
        out.append("Promoters have pledged %.1f%% of their shares" % pl["promoter_pledged_pct"])
    ow = doc.get("owners") or []
    if len(ow) >= 2:
        d = round(ow[0]["promoter_pct"] - ow[1]["promoter_pct"], 2)
        if abs(d) >= PROMOTER_MOVE_PTS:
            out.append("Promoter holding %s %.2f points in the quarter to %s (%.2f%%)" % (
                "up" if d > 0 else "down", abs(d), ow[0]["quarter_end"], ow[0]["promoter_pct"]))
    for d in (doc.get("deals") or [])[:3]:
        if d.get("date") and (today - (_date(d["date"]) or today)).days <= 7:
            out.append("%s deal %s: %s %s %s shares at %s%s" % (
                (d.get("kind") or "").capitalize(), d["date"], d.get("client") or "?",
                "bought" if d.get("side", "").startswith("B") else "sold", "{:,.0f}".format(d["qty"] or 0),
                nc.inr(d["price"]) if d.get("price") else "?",
                (" (%s)" % crore(d["value"])) if d.get("value") else ""))
    recent = [f for f in doc.get("filings") or [] if f.get("important") and f.get("time_ist") and
              _date(f["time_ist"][:10]) and (today - _date(f["time_ist"][:10])).days <= 3]
    if recent:
        out.append("%d important NSE filing%s in the last 3 days: %s" % (
            len(recent), "" if len(recent) == 1 else "s", "; ".join(news._short(f.get("subject") or f["title"], 70)
                                                                   for f in recent[:3])))
    missing = [LABELS.get(k, k) for k in sorted(doc.get("failed") or {})]
    missing += ["NSE %s" % e.split(":")[0] for e in ev.get("errors") or []]
    if missing:
        out.append("Could not read: %s" % ", ".join(missing))
    return out


# ---------------------------------------------------------------- text

def render(doc):
    L = []
    name = doc.get("name") or ""
    L.append("# %s%s" % (doc["symbol"], (": " + name) if name else ""))
    L.append("")
    L.append("Built %s. Public sources only; facts, not advice: open the filing before acting.%s" % (
        doc.get("built_ist") or doc["built_at"], (" Industry: %s." % doc["industry"]) if doc.get("industry") else ""))
    L.append("")
    L.append("## Flags")
    L.extend(["- " + f for f in doc["flags"]] or ["- Nothing that stands out."])
    p = doc.get("price")
    L += ["", "## Price (Yahoo, daily)"]
    if p:
        L.append("- Last close %s on %s: %s on the day, %s over a week, %s a month, %s three months, %s a year." % (
            nc.inr(p["close"]), p["date"], pct(p["change_1d_pct"]), pct(p["change_1w_pct"]), pct(p["change_1m_pct"]),
            pct(p["change_3m_pct"]), pct(p["change_1y_pct"])))
        L.append("- 52-week range %s to %s (%s from the high, %s from the low)." % (
            nc.inr(p["low_52w"]), nc.inr(p["high_52w"]), pct(p["from_high_pct"]), pct(p["from_low_pct"])))
        avg = []
        for k, lab in (("sma20", "20-day"), ("sma50", "50-day"), ("sma200", "200-day")):
            if p.get(k):
                avg.append("%s %s (%s)" % (lab, nc.inr(p[k]), "above" if p["close"] > p[k] else "below"))
        if avg:
            L.append("- Averages: %s." % ", ".join(avg))
        L.append("- Average daily range (20 days) %s; last volume %s, %s its 20-day average." % (
            pct(p["avg_range_20d_pct"], signed=False), "{:,}".format(int(p["volume"] or 0)),
            ("%.1fx" % p["volume_x"]) if p.get("volume_x") else "n/a"))
    else:
        L.append("- Not available: %s" % doc["failed"].get("price"))
    b = doc.get("band")
    if b:
        L.append("- NSE series %s, %s%s." % (b.get("series"), band_words(b.get("band")),
                                            ("; " + b["remarks"]) if b.get("remarks") else ""))
    fb = doc.get("fo_ban")
    if fb is not None:
        L.append("- F&O ban list for %s: %s." % (fb.get("date") or "the next session",
                                                  "IN THE BAN" if fb["banned"] else "not in it"))
    L += ["", "## Events"]
    ev = doc.get("events")
    if ev is None:
        L.append("- Not available: %s" % doc["failed"].get("events"))
    else:
        bm = ev.get("board_meetings") or []
        ahead = [e for e in bm if e["days"] >= 0]
        past = [e for e in bm if e["days"] < 0][:3]
        for e in sorted(ahead, key=lambda e: e["date"]):
            L.append("- Board meeting %s (in %d days): %s" % (e["date"], e["days"], e["purpose"]))
        for e in past:
            L.append("- Board meeting held %s: %s" % (e["date"], e["purpose"]))
        acts = ev.get("actions") or []
        for a in [a for a in acts if a["days"] >= 0] + [a for a in acts if a["days"] < 0][:3]:
            L.append("- %s %s: %s" % ("Ex-date" if a["days"] >= 0 else "Went ex", a["ex_date"], a["subject"]))
        if not bm and not acts and not ev.get("errors"):
            L.append("- No board meetings or corporate actions on NSE's lists.")
        for e in ev.get("errors") or []:
            L.append("- Not available: %s" % e)
    L += ["", "## Latest results (NSE XBRL, via nse-analyst)"]
    r = doc.get("results")
    if r:
        L.append("- %s (%s), filed %s." % (r.get("quarter") or "?", r.get("basis") or "?", r.get("filed") or "?"))
        L.extend("- " + c for c in r.get("changes") or [])
        if r.get("results_due"):
            L.append("- Next results expected between %s and %s (the listing rules' deadline)." % tuple(
                r["results_due"][:2]) if len(r["results_due"]) >= 2 else "- Next results due: %s" % r["results_due"])
    elif "results" in doc["failed"]:
        L.append("- Not available: %s" % doc["failed"]["results"])
    else:
        L.append("- Not read in this run.")
    L += ["", "## NSE filings, last %d days" % FILING_DAYS]
    fl = doc.get("filings")
    if fl is None:
        L.append("- Not available: %s" % doc["failed"].get("filings"))
    elif not fl:
        L.append("- None.")
    for f in (fl or [])[:20]:
        L.append("- %s%s %s%s" % ("! " if f["important"] else "", f.get("time_ist") or "", f.get("subject") or
                                   f["title"], (" [%s]" % ", ".join(f["tags"])) if f["tags"] else ""))
    if "headlines" in doc or "headlines" in doc["failed"]:
        L += ["", "## Headlines, last %d days (Google News)" % NEWS_DAYS]
        hl = doc.get("headlines")
        if hl is None:
            L.append("- Not available: %s" % doc["failed"].get("headlines"))
        elif not hl:
            L.append("- None that name the stock.")
        for h in (hl or [])[:15]:
            L.append("- %s %s (%s)%s" % (h.get("time_ist") or "", h["title"], h.get("source") or "",
                                         (" [%s]" % ", ".join(h["tags"])) if h["tags"] else ""))
        if doc.get("headlines_left_out"):
            L.append("- (%d market-wide stories that do not name it in the title left out)" % doc["headlines_left_out"])
    L += ["", "## Who owns it (NSE shareholding pattern)"]
    ow = doc.get("owners")
    if ow is None:
        L.append("- Not available: %s" % doc["failed"].get("owners"))
    elif not ow:
        L.append("- No shareholding pattern on NSE's list.")
    for o in ow or []:
        L.append("- %s: promoters %.2f%%, public %s" % (o["quarter_end"], o["promoter_pct"],
                                                        ("%.2f%%" % o["public_pct"]) if o["public_pct"] is not None
                                                        else "n/a"))
    pl = doc.get("pledge")
    if "pledge" in doc["failed"]:
        L.append("- Pledge data not available: %s" % doc["failed"]["pledge"])
    elif pl is None:
        L.append("- No pledge filing on NSE.")
    else:
        L.append("- Pledged (%s): promoters %s of their shares; all pledged shares %s of the company." % (
            pl.get("as_of") or "?", pct(pl.get("promoter_pledged_pct"), signed=False),
            pct(pl.get("all_pledged_pct"), signed=False)))
    L += ["", "## Insider trades, last %d days (SEBI PIT disclosures on NSE)" % INSIDER_DAYS]
    ins = doc.get("insiders")
    if ins is None:
        L.append("- Not available: %s" % doc["failed"].get("insiders"))
    else:
        if not ins["trades"]:
            L.append("- None%s." % ((" (the latest disclosure is from %s)" % ins["latest_filing"])
                                    if ins.get("latest_filing") else ""))
        else:
            L.append("- On the market, promoters, directors and officers: bought %s, sold %s." % (
                crore(ins["market_bought"]), crore(ins["market_sold"])))
        for t in ins["trades"][:12]:
            L.append("- %s %s (%s) %s %s %s%s" % (t["date"], t["who"], t["category"], t["side"].lower(),
                                                   "{:,.0f}".format(t["shares"] or 0), t["security"] or "shares",
                                                   (", %s, %s" % (crore(t["value"]), t["mode"])) if t.get("value")
                                                   else ", " + (t["mode"] or "")))
    L += ["", "## Bulk and block deals, last %d days" % DEAL_DAYS]
    dl = doc.get("deals")
    if dl is None:
        L.append("- Not available: %s" % doc["failed"].get("deals"))
    elif not dl:
        L.append("- None on file (NSE publishes only the latest day; nse-deals keeps the history when it runs).")
    for d in (dl or [])[:10]:
        L.append("- %s %s: %s %s %s at %s%s" % (d["date"], d.get("kind") or "", d.get("client") or "?",
                                                "bought" if (d.get("side") or "").startswith("B") else "sold",
                                                "{:,.0f}".format(d["qty"] or 0),
                                                nc.inr(d["price"]) if d.get("price") else "?",
                                                (" (%s)" % crore(d["value"])) if d.get("value") else ""))
    L += ["", "## Sources", "- " + "; ".join("%s: %s" % (LABELS.get(k, k), v) for k, v in sorted(doc["sources"].items()))]
    return "\n".join(L) + "\n"


def summary_row(doc):
    p = doc.get("price") or {}
    return {"name": doc.get("name"), "flags": doc.get("flags") or [], "failed": sorted(doc.get("failed") or {}),
            "close": p.get("close"), "change_1d_pct": p.get("change_1d_pct"), "from_high_pct": p.get("from_high_pct"),
            "fo_ban": bool((doc.get("fo_ban") or {}).get("banned")),
            "next_board_meeting": next(({"date": e["date"], "purpose": e["purpose"]}
                                        for e in sorted((doc.get("events") or {}).get("board_meetings") or [],
                                                        key=lambda e: e["date"]) if e["days"] >= 0), None),
            "results": (doc.get("results") or {}).get("changes", [None])[0] if doc.get("results") else None}


# ---------------------------------------------------------------- the run

def read_names(path):
    """universe.txt lines 'SYMBOL|Company name' -> {SYMBOL: name}."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                sym, _, name = line.strip().partition("|")
                if sym:
                    out[sym.strip()] = name.strip() or None
    except OSError:
        pass
    return out


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Everything public on an NSE stock, in one file.")
    ap.add_argument("symbols", nargs="*", help="NSE symbols")
    ap.add_argument("--tickers", nargs="*", default=[], help="more symbols (same as the positional ones)")
    ap.add_argument("--names", help="a universe.txt (SYMBOL|Company name per line) for the news search")
    ap.add_argument("--out", help="write <SYM>.md, <SYM>.json and index.json here")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-results", action="store_true", help="skip the quarterly numbers (nse-analyst)")
    ap.add_argument("--no-google", action="store_true", help="skip Google News")
    ap.add_argument("--deals-state", default=None, help="nse-deals history (default trader/state/deals.json)")
    ap.add_argument("--budget-s", type=float, default=None, help="stop starting new stocks after this many seconds")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='"YYYY-MM-DD HH:MM" IST, for reproducible runs')
    a = ap.parse_args(argv)
    syms = [nc.nse_symbol(s) for s in list(a.symbols) + list(a.tickers or [])]
    bad = [s for s in syms if not nc.is_nse_symbol(s)]
    if not syms or bad:
        print("dossier: %s" % ("not an NSE symbol: %s" % ", ".join(bad) if bad else "name at least one symbol"),
              file=sys.stderr)
        return 2
    try:
        now = nc.parse_now(a.now) if a.now else nc.utc_now()
    except ValueError as exc:
        print("dossier: --now: %s" % exc, file=sys.stderr)
        return 2
    syms = list(dict.fromkeys(syms))
    names = read_names(a.names) if a.names else {}
    fetcher = news.Fetcher(offline=a.offline, pause=a.pause)
    deals_state = a.deals_state if a.deals_state is not None else (None if a.offline else nc.tpath("state",
                                                                                                    "deals.json"))
    shared = Shared(fetcher, deals_state)
    start, docs, index, worst = time.time(), [], {}, 0
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    for i, sym in enumerate(syms):
        if a.budget_s is not None and time.time() - start > a.budget_s:
            for rest in syms[i:]:
                index[rest] = {"name": names.get(rest), "flags": [], "failed": ["not built: out of time"]}
            break
        doc = build(sym, fetcher, shared, now, name=names.get(sym), results=not a.no_results,
                    google=not a.no_google)
        if doc["failed"] and len([k for k in doc["sources"] if doc["sources"][k] == "ok"]) == 0:
            worst = 1
        docs.append(doc)
        index[sym] = summary_row(doc)
        if a.out:
            with open(os.path.join(a.out, "%s.md" % sym.replace("&", "_")), "w", encoding="utf-8") as fh:
                fh.write(render(doc))
            nc.write_json(os.path.join(a.out, "%s.json" % sym.replace("&", "_")), doc)
            nc.write_json(os.path.join(a.out, "index.json"), {"built_at": nc.utc_iso(now), "stocks": index,
                                                              "shared_errors": shared.errors})
    if a.out:                      # once more: the stocks left out of time are in the index too
        nc.write_json(os.path.join(a.out, "index.json"), {"built_at": nc.utc_iso(now), "stocks": index,
                                                          "shared_errors": shared.errors})
    if a.json:
        print(json.dumps({"built_at": nc.utc_iso(now), "stocks": docs, "index": index,
                          "shared_errors": shared.errors}, indent=1, ensure_ascii=False, default=str))
    elif not a.out:
        for d in docs:
            sys.stdout.write(render(d) + "\n")
    else:
        print("dossier: %d of %d stock files in %s%s" % (len(docs), len(syms), a.out,
                                                         (" (shared sources failed: %s)" % ", ".join(shared.errors))
                                                         if shared.errors else ""))
    return worst


if __name__ == "__main__":
    sys.exit(main())
