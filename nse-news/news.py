#!/usr/bin/env python3
"""News monitor for nse-news. Standard library only.

Watches NSE corporate announcements (the filings companies make to the
exchange) and Google News headlines for the stocks you watch or hold, and
prints only what is new since the last check, newest first, in IST, tagged,
with how far each stock is from its previous close and any board meeting or
results date coming up on NSE's event calendar.

    python3 news.py                                   # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE TCS
    python3 news.py --tickers TCS --all --hours 168   # everything in the last 7 days, seen or not
    python3 news.py --no-google                       # NSE filings only
    python3 news.py --no-moves --no-calendar

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(nse-scanner), the symbol in trader/plan.json (nse-exit) and the positions in
trader/portfolio.json. Symbols are bare NSE symbols (RELIANCE, never RELIANCE.NS).
"trader/" is the one trader folder nse_common.trader_home() finds
($NSE_TRADER_HOME, else the nearest trader/ with a config.json, ...), wherever
Claude Code was started.

Sources (no keys, no accounts):
  - NSE corporate announcements, the official exchange filings: results,
    board meetings, order wins, acquisitions, credit ratings, pledges,
    resignations and so on. One request per symbol for the window only:
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
  - Google News RSS, searched by company name (the name comes from NSE: sm_name
    in the announcements, else EQUITY_L.csv, cached for the day), for
    personal, non-commercial use:
    https://news.google.com/rss/search?q=%22Reliance+Industries%22+when:3d&hl=en-IN&gl=IN&ceid=IN:en
    Price-quote and "prediction" pages are skipped.
  - NSE event calendar (upcoming board meetings, so results dates), once a run:
    https://www.nseindia.com/api/event-calendar?index=equities
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against the previous session's close (5 daily bars of <SYM>.NS).

Times print in IST. State: trader/state/news.json remembers what you have seen
(30 days, so --hours is capped at 720 unless --all) and each symbol's company
name, so an item shows once. Only items inside the window are remembered, and
nothing is remembered when no news source could be read. The window is 72 hours,
or further back when a symbol's NSE filings or headlines were last read in full
longer ago than that (a long weekend, a few days of NSE failures): the run says
"looking back N hours". An explicit --hours is used exactly and never widened.

Every source is tracked. When one fails (NSE filings, Google News, the NSE
calendar or Yahoo prices, for some or all symbols) a "checked:" line next to
the result names it. Exit codes: 0 something new, 1 nothing new in what could
be checked, 2 usage (no symbols, a bad symbol or --now), 3 every news source
asked for failed: nothing was checked (never reported as "Nothing new").

Offline: --offline DIR reads nse-announcements-<SYM>.json, gnews-<SYM>.xml,
chart-<SYM>.json, event-calendar.json and (if there) EQUITY_L.csv from DIR.
The tests use it.

Headlines are leads, not facts: open the filing or the story before acting,
and treat nothing here as advice.
"""

import argparse
import csv
import datetime as _dt
import email.utils
import glob
import gzip
import hashlib
import http.client
import http.cookiejar
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

NSE_HOME = "https://www.nseindia.com/"
NSE_ANN = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
           "&from_date=%s&to_date=%s")
NSE_CAL = "https://www.nseindia.com/api/event-calendar?index=equities"
EQUITY_L = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
KEEP_DAYS = 30
MAX_HOURS = KEEP_DAYS * 24  # a wider window could show an item again after its "seen" mark expired
MOVE_PCT = 3.0      # a stock this far from its previous close is flagged as moving
HOURS = 72          # Friday evening filings still show at Monday's pre-open
EQUITY_L_MIN_ROWS = 1000  # NSE lists over 2,000 equities; fewer means a cut-off or wrong file
EQUITY_L_COLUMNS = ("SYMBOL", "NAME OF COMPANY", "SERIES")  # nse-scanner reads SYMBOL and SERIES
# Each source a run can read: key -> label in the "checked:" line. NSE filings and Google
# News are the news; the calendar and Yahoo prices are context.
SOURCES = (("nse", "NSE filings"), ("google", "Google News"), ("calendar", "NSE calendar"),
           ("prices", "Yahoo prices"))
NEWS_FEEDS = ("nse", "google")
FEED_SHORT = {"nse": "NSE", "google": "Google News", "calendar": "NSE's calendar", "prices": "Yahoo"}

# (tag, case-insensitive phrases, case-sensitive acronyms). Matched on the title and
# the filing text with SEBI's LODR boilerplate removed (every filing cites it).
TAGS = [
    ("results", r"\bresults?\b|\bearnings\b|\bnet profit\b|\bquarterly\b|\bprofit (?:rises|falls|jumps|drops|"
                r"surges|declines|up|down)\b", r"\bPAT\b|\bEBITDA\b|\bQ[1-4]\b|\bQ[1-4]FY\d\d\b"),
    ("board meeting", r"\bboard meeting\b|\bmeeting of (?:the )?board\b|\bboard of directors\b[^.]{0,80}"
                      r"\bmeeting\b|\boutcome of (?:the )?board\b", None),
    ("dividend/bonus/split/buyback", r"\bdividends?\b|\bbonus\b|\bstock split\b|\bsplit\b|\bsub-?division\b|"
                                     r"\bbuy-?backs?\b|\brecord date\b", None),
    ("order win", r"\borders? (?:win|wins|worth|inflow|book)\b|\bbag(?:s|ged)?\b[^.]{0,60}\b(?:order|contract)s?\b|"
                  r"\bwins?\b[^.]{0,60}\b(?:order|contract|tender)s?\b|\bletter of (?:award|intent)\b|"
                  r"\bbagging/receiving\b|\breceipt of (?:an )?orders?\b|\bawarded\b", r"\bLoA\b|\bL1\b"),
    ("deal/merger", r"\bacqui(?:re|res|red|ring|sition|sitions)\b|\bmergers?\b|\bamalgamation\b|\bde-?merger\b|"
                    r"\bscheme of arrangement\b|\btakeover\b|\bopen offer\b|\bjoint venture\b|\bdivest(?:s|ed|ment)?\b|"
                    r"\bslump sale\b|\brestructuring\b|\bbuys? (?:a )?(?:\d+(?:\.\d+)?% )?stake\b", r"\bJV\b"),
    ("analyst/brokerage", r"\btarget price\b|\bprice target\b|\bupgrade[sd]?\b|\bdowngrade[sd]?\b|"
                          r"\binitiates? coverage\b|\b(?:buy|sell|hold|accumulate|reduce|neutral|add|overweight|"
                          r"underweight|outperform|underperform) (?:rating|call|recommendation)\b|"
                          r"\b\d+(?:\.\d+)?% (?:upside|downside)\b|\bbrokerages?\b", None),
    ("regulatory", r"\bshow[- ]cause\b|\bpenalt(?:y|ies)\b|\bfined\b|\bcourt\b|\btribunal\b|\blitigation\b|"
                   r"\btax demand\b|\bGST (?:demand|notice)\b|\braids?\b|\bprobe\b|\binvestigation\b|\bapproval\b|"
                   r"\bsettlement order\b|\binsolvency\b|\bregulator\b",
     r"\bSEBI\b|\bRBI\b|\bCCI\b|\bNCLT\b|\bNCLAT\b|\bIRDAI\b|\bTRAI\b|\bDGCA\b|\bUS ?FDA\b|\bFDA\b|\bED\b"),
    ("promoter/pledge/stake", r"\bpromoters?\b|\bpledge[ds]?\b|\bencumbrances?\b|\binvocation\b|\bstake\b|"
                              r"\boffer for sale\b|\binsider trading\b|\bshareholding pattern\b", r"\bOFS\b|\bSAST\b"),
    ("block deal", r"\bblock deals?\b|\bbulk deals?\b|\bblock trades?\b|\blarge deals?\b", None),
    ("credit rating", r"\bcredit ratings?\b|\bratings? (?:upgrade|downgrade|reaffirm|affirm|revis)\w*|"
                      r"\bCARE Ratings\b|\bCareEdge\b|\bIndia Ratings\b|\bMoody'?s\b|\bFitch\b|\bS&P Global\b|"
                      r"\bAcuite\b|\bInfomerics\b|\bBrickwork\b", r"\bCRISIL\b|\bICRA\b|\bInd-Ra\b"),
    ("management change", r"\bresign(?:s|ed|ation|ations)?\b|\bsteps? down\b|\bappoint(?:s|ed|ment|ments)?\b|"
                          r"\bre-?appoint\w*|\bcessation\b|\bmanaging director\b|\bchief (?:executive|financial|"
                          r"operating|technology) officer\b|\bchair(?:man|person|woman)\b|\bkey managerial\b|"
                          r"\bcompany secretary\b|\bchange in (?:directorate|management|auditors?)\b",
     r"\bCEO\b|\bCFO\b|\bCOO\b|\bCTO\b|\bKMP\b|\bMD\b"),
    ("circuit/halt", r"\b(?:upper|lower) circuit\b|\bcircuit (?:limit|filter|breaker)\b|\bhalt(?:s|ed)?\b|"
                     r"\btrading (?:pause|suspen)\w*|\bsuspen(?:d|ds|ded|sion)\b|\bsurveillance\b|"
                     r"\btrade[- ]for[- ]trade\b|\bdelist(?:ing|ed)?\b|\bprice band\b", r"\bASM\b|\bGSM\b|\bT2T\b"),
    ("macro", r"\bmonetary policy\b|\brepo rate\b|\bunion budget\b|\bbudget 20\d\d\b|\bforeign (?:institutional|"
              r"portfolio) investors?\b|\binflation\b|\btariffs?\b|\bcrude (?:oil )?prices?\b|\bSensex\b|\bNifty\b|"
              r"\brupee (?:falls|rises|hits|slips|gains|weakens|strengthens)\b",
     r"\bRBI (?:policy|MPC|rate)|\bMPC\b|\bFIIs?\b|\bFPIs?\b|\bDIIs?\b|\bCPI\b|\bWPI\b|\bGDP\b|\bIIP\b|\bFed\b|"
     r"\bFOMC\b|\bBudget\b"),
    ("fund raising", r"\bqualified institutions? placement\b|\bpreferential (?:issue|allotment)\b|\brights issue\b|"
                     r"\bnon-convertible debentures?\b|\bdebentures?\b|\bfund ?rais(?:e|es|ing)\b|"
                     r"\binitial public offer(?:ing)?\b|\bcommercial paper\b", r"\bQIP\b|\bNCDs?\b|\bIPOs?\b"),
    ("investor meet", r"\binvestors?'? meet(?:ing)?s?\b|\banalysts?'? meet\b|\binstitutional investors?'?\b|"
                      r"\bcon(?:\.|ference)? call\b|\bearnings call\b|\binvestor presentation\b|"
                      r"\binvestor conference\b", None),
]
_TAGS = [(t, re.compile(ci, re.I), re.compile(cs) if cs else None) for t, ci, cs in TAGS]

# NSE filing subjects (the announcement's "desc") that deserve a "!".
IMPORTANT_DESC = re.compile(
    r"financial results?|outcome of board meeting|credit rating|resignation|cessation|change in (?:director|"
    r"management|auditor|key managerial)|acquisition|amalgamation|merger|demerger|scheme of arrangement|"
    r"bagging|orders?/contracts|pledge|encumbrance|suspension|insolvency|default|fraud|litigation|"
    r"action\(?s?\)? (?:taken|initiated)|buy ?back|dividend|bonus|split|sub-division|delisting|open offer|"
    r"restructuring", re.I)
# A filing with one of these tags also gets the "!".
IMPORTANT_TAGS = {"deal/merger", "order win", "management change", "circuit/halt", "promoter/pledge/stake",
                  "regulatory", "credit rating", "dividend/bonus/split/buyback"}
# Every NSE filing cites the listing rules; that is not regulatory news.
BOILERPLATE = re.compile(
    r"(?:Securities and Exchange Board of India|SEBI)\s*\(\s*(?:Listing Obligations?|LODR|Prohibition of Insider"
    r"|PIT|Substantial Acquisition|SAST|Depositories|Issue of Capital|ICDR)[^)]*\)\s*(?:Regulations?)?,?\s*"
    r"(?:\d{4})?|(?:Securities and Exchange Board of India|SEBI)\s+(?:Listing Obligations and Disclosure "
    r"Requirements|LODR)\s+Regulations?,?\s*(?:\d{4})?|\bSEBI\s*\(?LODR\)?", re.I)
# Google News search results that are quote pages, not news.
QUOTE_PAGE = re.compile(
    r"\bshare price\s*(?:$|-|:|\||today\b|live\b|prediction\b)|\bstock price\b.{0,40}\b(?:live|chart)\b|"
    r"\boption chain\b|\breturns? chart\b|\btop (?:gainers|losers)\b|\bstock analysis\b|\bstock forecast\b|"
    r"\bprediction for\b", re.I)
WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def _sym(text):
    return nc.nse_symbol(text)


def watch_from_files(portfolio=nc.DEFAULT_DIR, watchlist=nc.DEFAULT_DIR, plan=nc.DEFAULT_DIR, problems=None):
    """{SYMBOL: company name or None}: the scanner's hits, the planned trade and your holdings.
    Defaults are in the trader folder (nc.tpath); None skips a file; a file that is not there is
    fine. A file that is there but cannot be read (broken JSON) is not: it goes into `problems`
    (a list), so a held stock is never dropped from the check without a word. A hit's "name"
    is used when the scanner wrote one; otherwise company_names() looks it up (NSE's sm_name,
    then the EQUITY_L cache, else the symbol is used)."""
    portfolio = nc.tpath("portfolio.json") if portfolio is nc.DEFAULT_DIR else portfolio
    watchlist = nc.tpath("watchlist.json") if watchlist is nc.DEFAULT_DIR else watchlist
    plan = nc.tpath("plan.json") if plan is nc.DEFAULT_DIR else plan
    out = {}
    for path, key in ((watchlist, "hits"), (plan, None), (portfolio, "positions")):
        try:
            doc = nc.read_json(path) if path else None
        except (OSError, ValueError) as exc:
            doc = None
            if problems is not None:
                problems.append("could not read %s, so its symbols are not checked: %s" % (path, exc))
        if not isinstance(doc, dict):
            continue
        rows = [doc] if key is None else doc.get(key) or []
        for h in rows:
            if not isinstance(h, dict):
                continue
            sym = h.get("symbol") or h.get("tradingsymbol") or h.get("ticker")
            if sym:
                s = _sym(sym)
                out[s] = h.get("name") or out.get(s)
    return out


def clean_name(name):
    """ "Reliance Industries Limited" -> "Reliance Industries" (what headlines say)."""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    s = re.sub(r"(?i)[\s,]+(?:limited|ltd\.?)$", "", s).strip()
    return s or None


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """GETs with nc.UA, gzip, a polite pause, and NSE's cookie dance on 401/403."""

    def __init__(self, offline=None, pause=0.5):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
        self._primed = False
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _open(self, url, headers):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers=headers)
        with self._opener.open(req, timeout=30) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            for p in (path, path + ".gz"):
                if os.path.isfile(p):
                    with open(p, "rb") as fh:
                        data = fh.read()
                    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
            raise NewsError("offline: no file %s" % path)
        served = None if nse else nc.kite_answer(url)
        if served is not None:
            return served.encode("utf-8")
        headers = nc.nse_headers() if nse else {"User-Agent": nc.UA, "Accept-Encoding": "gzip"}
        try:
            try:
                return self._open(url, headers)
            except urllib.error.HTTPError as exc:
                if not (nse and exc.code in (401, 403) and not self._primed):
                    raise
                self._primed = True
                try:
                    self._open(NSE_HOME, {"User-Agent": nc.UA, "Accept": "text/html", "Accept-Encoding": "gzip"})
                except (urllib.error.URLError, OSError, http.client.HTTPException):
                    pass
                return self._open(url, headers)
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise NewsError("could not reach %s: %s" % (url.split("?")[0], getattr(exc, "reason", exc)))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> naive UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return d


def parse_nse_time(value):
    """NSE's "13-Nov-2026 18:25:01" or "2026-11-13 18:25:01" (IST) -> naive UTC."""
    s = _clean(value)
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            return nc.to_utc(_dt.datetime.strptime(s, fmt))
        except ValueError:
            continue
    return None


def _json(data, what):
    try:
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError):
        raise NewsError("%s is not JSON (NSE may be blocking the request)" % what)


def _rows(doc, what):
    """NSE's list, or {"data": [...]}. Anything else (an error object, {}) is not "no rows"."""
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and isinstance(doc.get("data"), list):
        return doc["data"]
    raise NewsError("%s came back in an unexpected shape (%s), not a list" % (
        what, ", ".join(sorted(doc)[:5]) if isinstance(doc, dict) and doc else type(doc).__name__))


def parse_announcements(data, ticker):
    """NSE corporate-announcements JSON -> items (kind "filing")."""
    rows = _rows(_json(data, "NSE announcements for %s" % ticker), "NSE announcements for %s" % ticker)
    out = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        desc = _clean(a.get("desc")) or "Announcement"
        name = _clean(a.get("sm_name"))
        seq = _clean(a.get("seq_id")) or hashlib.sha1(json.dumps(a, sort_keys=True).encode()).hexdigest()[:12]
        out.append({"id": "nse:" + seq, "source": "NSE", "feed": "nse", "kind": "filing",
                    "ticker": _sym(a.get("symbol") or ticker), "company": name or None,
                    "subject": desc, "title": "%s: %s" % (name or ticker, desc),
                    "summary": _clean(a.get("attchmntText")),
                    "link": _clean(a.get("attchmntFile")) or "https://www.nseindia.com/companies-listing/"
                            "corporate-filings-announcements",
                    "time": parse_nse_time(a.get("an_dt") or a.get("sort_date"))})
    return out


def parse_gnews(data, ticker):
    """Google News RSS -> items (kind "headline"). Quote pages are dropped; returns (items, skipped)."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise NewsError("Google News for %s is not valid XML: %s" % (ticker, exc))
    if _local(root.tag) != "rss" or root.find("channel") is None:
        # a well-formed consent or "unusual traffic" page would otherwise read as "no headlines"
        raise NewsError("Google News for %s sent <%s>, not the RSS feed" % (ticker, _local(root.tag)))
    out, skipped = [], 0
    for it in root.iter("item"):
        title = _clean(_text(it, "title"))
        src = it.find("source")
        publisher = _clean(src.text) if src is not None and src.text else ""
        if publisher and title.endswith(" - " + publisher):
            title = title[:-len(" - " + publisher)].rstrip()
        if re.match(r"^https?://", publisher) or (not publisher and src is not None and src.get("url")):
            host = urllib.parse.urlparse(publisher or src.get("url")).netloc
            publisher = re.sub(r"^www\.", "", host)
        if QUOTE_PAGE.search(title):
            skipped += 1
            continue
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "gnews:" + guid, "source": publisher or "Google News", "feed": "google",
                    "kind": "headline", "ticker": ticker, "title": title, "summary": "",
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out, skipped


# Names a news search confuses with something else (the exchange BSE, the footballer Trent, the word "Titan"):
# the search for these is spelled out. Every other stock is searched by its company name.
QUERY_OVERRIDES = {
    "BSE": '"BSE Ltd" OR "BSE shares" OR "BSE stock"',
    "ITC": '"ITC Ltd" OR "ITC shares" OR "ITC stock"',
    "TRENT": '"Trent Ltd" OR "Trent shares" OR "Trent stock"',
    "TITAN": '"Titan Company" OR "Titan shares"',
    "BEL": '"Bharat Electronics"',
    "HAL": '"Hindustan Aeronautics"',
    "LT": '"Larsen & Toubro" OR "L&T shares"',
    "M&M": '"Mahindra & Mahindra" OR "M&M shares"',
}


def gnews_query(name, symbol, hours):
    days = max(1, min(30, int(math.ceil(hours / 24.0))))
    if symbol in QUERY_OVERRIDES:
        return GNEWS % urllib.parse.quote_plus("(%s) when:%dd" % (QUERY_OVERRIDES[symbol], days))
    core = clean_name(name)
    q = ('"%s" when:%dd' % (core, days)) if core else ("%s NSE share when:%dd" % (symbol, days))
    return GNEWS % urllib.parse.quote_plus(q)


def names_from_equity_l(text):
    """EQUITY_L.csv (headers with leading spaces) -> {SYMBOL: NAME OF COMPANY}."""
    out = {}
    rows = csv.reader(io.StringIO(str(text or "").lstrip("﻿")))
    head = [h.strip().upper() for h in next(rows, [])]
    if "SYMBOL" not in head or "NAME OF COMPANY" not in head:
        return out
    i, j = head.index("SYMBOL"), head.index("NAME OF COMPANY")
    for r in rows:
        if len(r) > max(i, j) and r[i].strip():
            out[r[i].strip().upper()] = r[j].strip()
    return out


def check_equity_l(text):
    """Why `text` is not NSE's EQUITY_L.csv (None when it is): it must have the SYMBOL,
    NAME OF COMPANY and SERIES columns (nse-scanner needs SYMBOL and SERIES from the same
    cached file) and at least EQUITY_L_MIN_ROWS companies. A block page, an empty body or a
    cut-off download fails, so it never lands in the cache the scanner reads all day."""
    text = str(text or "").lstrip("﻿")
    if not text.strip():
        return "it is empty"
    if text.lstrip()[:1] == "<":
        return "NSE sent a web page, not the CSV"
    rows = csv.reader(io.StringIO(text))
    head = [h.strip().upper() for h in next(rows, [])]
    missing = [c for c in EQUITY_L_COLUMNS if c not in head]
    if missing:
        return "no %s column" % " / ".join(missing)
    i = head.index("SYMBOL")
    n = sum(1 for r in rows if len(r) > i and r[i].strip())
    if n < EQUITY_L_MIN_ROWS:
        return "only %d companies in it (NSE lists over 2,000)" % n
    return None


def _read_text(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def _good_cache(paths):
    """The text of the first file in `paths` that is a usable EQUITY_L.csv, else None."""
    for p in paths:
        try:
            text = _read_text(p)
        except (OSError, EOFError, ValueError):
            continue
        if check_equity_l(text) is None:
            return text
    return None


def equity_list(fetcher, now, cache_dir=nc.DEFAULT_DIR):
    """Every listed equity's name, cached for the IST day in the trader folder's cache/
    (EQUITY_L-<date>.csv, the same file nse-scanner reads). Nothing is cached unless it
    passes check_equity_l; a bad file for today is deleted and fetched again; when NSE fails
    or sends something unusable, the newest good earlier day's file is used (names barely
    change) and that file is left alone."""
    if cache_dir is nc.DEFAULT_DIR:
        cache_dir = nc.tpath("cache")
    if fetcher.offline or not cache_dir:
        return names_from_equity_l(fetcher.get(EQUITY_L, "EQUITY_L.csv").decode("utf-8", "replace"))
    day = nc.to_ist(now).date().isoformat()
    path = os.path.join(cache_dir, "EQUITY_L-%s.csv" % day)
    if os.path.isfile(path):
        text = _good_cache([path])
        if text is not None:
            return names_from_equity_l(text)
        try:
            os.remove(path)  # a block page or a cut-off file: useless to the scanner too
        except OSError:
            pass
    older = sorted((p for p in glob.glob(os.path.join(cache_dir, "EQUITY_L-*.csv")) if p != path), reverse=True)
    try:
        text = fetcher.get(EQUITY_L, "EQUITY_L.csv").decode("utf-8", "replace")
        bad = check_equity_l(text)
        if bad:
            raise NewsError("EQUITY_L.csv is not usable (%s), so it was not cached" % bad)
    except NewsError:
        text = _good_cache(older)
        if text is None:
            raise
        return names_from_equity_l(text)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)  # whole file or nothing: the scanner may read it at the same moment
        for old in older:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass
    return names_from_equity_l(text)


def parse_calendar(data):
    """NSE event-calendar JSON -> [{symbol, company, purpose, desc, date}] (date ISO).
    Raises NewsError when it is not the calendar or lists nothing at all: NSE's calendar
    always has the coming weeks' board meetings, so empty means it did not answer properly,
    not that no company has a results date."""
    rows = _rows(_json(data, "NSE event calendar"), "NSE event calendar")
    if not rows:
        raise NewsError("NSE's event calendar came back empty, so results dates are unknown")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            d = _dt.datetime.strptime(_clean(r.get("date")), "%d-%b-%Y").date()
        except ValueError:
            continue
        out.append({"symbol": _sym(r.get("symbol")), "company": _clean(r.get("company")),
                    "purpose": _clean(r.get("purpose")), "desc": _clean(r.get("bm_desc")),
                    "date": d.isoformat()})
    if not out:
        # a new date format would otherwise read as "no board meeting for any of your stocks"
        raise NewsError("none of the %d rows in NSE's event calendar had a readable date, so results "
                        "dates are unknown" % len(rows))
    return out


def upcoming(events, watch, now):
    """Calendar entries for your symbols from today (IST) on, soonest first."""
    today = nc.to_ist(now).date().isoformat()
    out = [dict(e) for e in events if e["symbol"] in watch and e["date"] >= today]
    out.sort(key=lambda e: (e["date"], e["symbol"]))
    for e in out:
        e["days"] = (_dt.date.fromisoformat(e["date"]) - _dt.date.fromisoformat(today)).days
        e["results"] = bool(re.search(r"result", e["purpose"] + " " + e["desc"], re.I))
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise.

    With daily bars the previous close is the last bar before the last session
    (meta.chartPreviousClose is the close before the whole range); with a
    one-day range it is meta.chartPreviousClose."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        res = doc["chart"]["result"][0]
        meta = res["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    when = meta.get("regularMarketTime")
    prev = None
    if when and meta.get("dataGranularity") in ("1d", "5d", "1wk"):
        last_day = nc.ist_from_epoch(when).date()
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        bars = [(t, c) for t, c in zip(res.get("timestamp") or [], quote.get("close") or [])
                if c is not None and nc.ist_from_epoch(t).date() < last_day]
        if bars:
            prev = bars[-1][1]
    if prev is None and meta.get("range") in (None, "1d"):
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if prev is None:
        prev = meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": nc.to_utc(nc.ist_from_epoch(when)) if when else None}


def _source(stats, key):
    """stats["sources"][key]: {"ok": [symbols or "all"], "failed": {symbol or "all": why}}."""
    return stats.setdefault("sources", {}).setdefault(key, {"ok": [], "failed": {}})


def _offline_missing(fetcher, exc):
    """Offline, a fixture that is not there means "not asked", not a failure."""
    return bool(fetcher.offline) and "no file" in str(exc)


def collect_moves(fetcher, watch, errors=None, stats=None):
    errors = [] if errors is None else errors
    stats = {} if stats is None else stats
    src = _source(stats, "prices")
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(nc.yahoo_chart_url(t, "5d", "1d"), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(nc.yahoo_chart_url(t, "5d", "1d", host="query1"), name)
            out.append(parse_chart(data, t))
            src["ok"].append(t)
        except NewsError as exc:
            if not _offline_missing(fetcher, exc):
                errors.append("price for %s: %s" % (t, exc))
                src["failed"][t] = str(exc)
    return out


# ---------------------------------------------------------------- tagging

# First words too common to stand for one company on their own (groups, places, banks).
GENERIC_FIRST = {"tata", "bharat", "india", "indian", "hindustan", "state", "bank", "the", "national", "adani",
                 "bajaj", "mahindra", "aditya", "birla", "godrej", "hdfc", "icici", "sbi", "kotak", "jsw",
                 "jindal", "larsen", "dr", "dr.", "sun", "power", "union", "punjab", "central", "oil", "gas",
                 "steel", "coal", "life", "general", "new", "united", "max", "motilal", "shree", "shri", "sri",
                 "great", "global", "first", "asian", "eastern", "western", "southern", "northern"}


def aliases(symbol, name):
    """Ways a headline names a stock: the symbol, the name without Limited, a
    distinctive first word ("Reliance") and the initials ("RIL", "TCS", "HUL")."""
    out = {"symbol": symbol}
    core = clean_name(name)
    if core and len(core) >= 4:
        out["name"] = core
        words = re.findall(r"[A-Za-z&][\w&.'-]*", core)
        if words and len(words[0]) >= 4 and words[0].lower() not in GENERIC_FIRST:
            out["first"] = words[0]
        caps = "".join(w[0] for w in words if w[0].isupper())
        full = caps + ("L" if re.search(r"(?i)\b(?:limited|ltd)\.?$", str(name or "").strip()) else "")
        out["initials"] = sorted({a for a in (caps, full) if len(a) >= 3 and a != symbol})
    return out


def mention_patterns(watch):
    """{SYMBOL: regex} matching (RELIANCE), NSE: RELIANCE, RELIANCE as a word, its name,
    a distinctive first word and its initials."""
    pats = {}
    for t, name in watch.items():
        a = aliases(t, name)
        alts = [r"\(%s\)" % re.escape(t), r"\bNSE\s*:\s*%s\b" % re.escape(t)]
        if len(t) >= 3:
            alts.append(r"(?<![\w&])%s(?![\w&])" % re.escape(t))
        if a.get("name"):
            alts.append(r"(?i:\b%s\b)" % re.escape(a["name"]).replace(r"\ ", r"[\s-]+"))
        if a.get("first"):
            alts.append(r"(?i:\b%s\b)" % re.escape(a["first"]))
        for ini in a.get("initials", []):
            alts.append(r"(?<![\w&])%s(?![\w&])" % re.escape(ini))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag_text(text):
    hay = BOILERPLATE.sub(" ", text or "")
    out = []
    for label, ci, cs in _TAGS:
        if ci.search(hay) or (cs is not None and cs.search(hay)):
            out.append(label)
    return out


def tag(items, watch):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = tag_text(hay)
        if it["kind"] == "filing":
            it["important"] = bool(IMPORTANT_DESC.search(it.get("subject") or "")) or \
                bool(IMPORTANT_TAGS & set(it["tags"])) or {"results", "board meeting"} <= set(it["tags"])
        else:
            it["important"] = False
    return items


def dedupe(items):
    """One item per story: same id, or same link, or the same words the same day.
    NSE sometimes posts one filing twice (two PDFs, one text): those merge too."""
    seen, out = {}, []
    for it in items:
        words = it["title"] + (" " + it["summary"][:300] if it.get("kind") == "filing" else "")
        keys = [it["id"], it["link"] if it.get("kind") != "filing" else it["link"] + "|" + it["id"],
                "%s|%s" % (re.sub(r"\W+", " ", words.lower()).strip(), it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            hit.setdefault("also", []).append(it["id"])
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(item_id):
    return hashlib.sha1(item_id.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def company_names(fetcher, watch, state, now, errors, need):
    """Every symbol's company name: the watchlist's "name" when the scanner wrote one, the
    state cache (NSE's sm_name from earlier runs), then EQUITY_L.csv (the day's cache, else
    NSE). A symbol with none stays None: search and tagging fall back to the symbol."""
    names = state.setdefault("names", {})
    missing = [t for t in need if not (watch.get(t) or names.get(t))]
    if missing:
        try:
            listed = equity_list(fetcher, now)
        except NewsError as exc:
            listed = {}
            if not _offline_missing(fetcher, exc):
                errors.append("NSE equity list: %s" % exc)
        for t in missing:
            if listed.get(t):
                names[t] = listed[t]
    return {t: watch.get(t) or names.get(t) for t in need}


def collect(fetcher, watch, state, now, hours=HOURS, nse=True, google=True, errors=None, stats=None):
    """Items from NSE filings and Google News. Every symbol's result per feed goes into
    stats["sources"] (see _source), the names used into stats["names"]."""
    errors = [] if errors is None else errors
    stats = {} if stats is None else stats
    items = []
    names = state.setdefault("names", {})
    start = nc.to_ist(now - _dt.timedelta(hours=hours)).strftime("%d-%m-%Y")
    end = nc.to_ist(now).strftime("%d-%m-%Y")
    if nse:
        src = _source(stats, "nse")
        for t in sorted(watch):
            try:
                got = parse_announcements(fetcher.get(NSE_ANN % (urllib.parse.quote(t), start, end),
                                                      "nse-announcements-%s.json" % t, nse=True), t)
            except NewsError as exc:
                errors.append("NSE filings for %s: %s" % (t, exc))
                src["failed"][t] = str(exc)
                continue
            items += got
            src["ok"].append(t)
            for it in got:
                if it.get("company") and it["ticker"] == t:
                    names[t] = it["company"]
                    break
    found = company_names(fetcher, watch, state, now, errors, sorted(watch))
    stats["names"] = found
    if google:
        src = _source(stats, "google")
        skipped = off = 0
        for t in sorted(watch):
            if not found.get(t):
                errors.append("no company name for %s: searched Google News by symbol" % t)
            about = mention_patterns({t: found.get(t)})[t]
            try:
                got, sk = parse_gnews(fetcher.get(gnews_query(found.get(t), t, hours), "gnews-%s.xml" % t), t)
            except NewsError as exc:
                errors.append("headlines for %s: %s" % (t, exc))
                src["failed"][t] = str(exc)
                continue
            keep = [it for it in got if about.search(it["title"])]
            items += keep
            skipped += sk
            off += len(got) - len(keep)
            src["ok"].append(t)
        stats["quote_pages_skipped"] = skipped
        stats["off_topic_skipped"] = off
    return items


def calendar(fetcher, watch, now, errors=None, stats=None):
    errors = [] if errors is None else errors
    src = _source({} if stats is None else stats, "calendar")
    try:
        events = parse_calendar(fetcher.get(NSE_CAL, "event-calendar.json", nse=True))
    except NewsError as exc:
        if not _offline_missing(fetcher, exc):
            errors.append("NSE event calendar: %s" % exc)
            src["failed"]["all"] = str(exc)
        return []
    src["ok"].append("all")
    return upcoming(events, watch, now)


def in_window(it, now, hours=HOURS):
    """Inside the last `hours` up to now (10 minutes of clock slack; with a pinned --now,
    nothing from after it). An item with no time counts as inside."""
    if it["time"] is None:
        return True
    return now - _dt.timedelta(hours=hours) <= it["time"] <= now + _dt.timedelta(minutes=10)


def select(items, state, now, hours=HOURS, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    out = []
    for it in items:
        if not in_window(it, now, hours):
            continue
        it["new"] = fingerprint(it["id"]) not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now, hours=None):
    """Mark items seen. With `hours`, only items inside that window (the ones select could
    show): an item stamped after a pinned --now, or a clock slip, is not marked seen
    without ever having been shown."""
    seen = state.setdefault("seen", {})
    stamp = nc.utc_iso(now)
    for it in items:
        if hours is not None and not in_window(it, now, hours):
            continue
        for i in [it["id"]] + it.get("also", []):
            seen.setdefault(fingerprint(i), stamp)
    cutoff = nc.utc_iso(now - _dt.timedelta(days=KEEP_DAYS))
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def _stamp_time(text):
    try:
        return _dt.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def window_hours(state, now, symbols, feeds, default=HOURS):
    """How far back to look. At least `default` (72 h: Friday evening's filings still show at
    Monday's pre-open), and far enough to reach back to the last time each of `symbols` was
    read in full from each of `feeds` (state["checked"]), so a long weekend, a holiday week or a
    few days of NSE failures cannot push a filing out of the window before it was ever shown.
    Never more than MAX_HOURS (the "seen" marks are kept that long)."""
    checked = state.get("checked") if isinstance(state.get("checked"), dict) else {}
    need = default
    for t in symbols:
        per = checked.get(t) if isinstance(checked.get(t), dict) else {}
        for f in feeds:
            last = _stamp_time(per.get(f))
            if last is not None and last < now:
                need = max(need, int(math.ceil((now - last).total_seconds() / 3600.0)) + 1)
    return min(need, MAX_HOURS)


def mark_checked(state, stats, now, feeds):
    """Record, per symbol and feed, that this run read the feed in full (a symbol whose fetch
    failed keeps its older mark, so the next run reaches back to it)."""
    stamp = nc.utc_iso(now)
    checked = state.get("checked") if isinstance(state.get("checked"), dict) else {}
    for f in feeds:
        for t in ((stats.get("sources") or {}).get(f) or {}).get("ok", []):
            if t != "all":
                checked.setdefault(t, {})[f] = stamp
    cutoff = nc.utc_iso(now - _dt.timedelta(days=KEEP_DAYS))
    state["checked"] = {t: {f: v for f, v in per.items() if v >= cutoff}
                        for t, per in checked.items() if isinstance(per, dict)}
    state["checked"] = {t: per for t, per in state["checked"].items() if per}


def _status_of(src):
    if not src or (not src["ok"] and not src["failed"]):
        return "skipped"  # not asked, or offline with no file for it
    if src["failed"] and not src["ok"]:
        return "failed"
    return "partial" if src["failed"] else "ok"


def run_status(stats, asked):
    """How the run went, per source. asked: the source keys this run tried, in SOURCES order.
    -> {"status": "ok" | "partial" | "failed", "line": "checked: ...", "headline": "...",
        "sources": {key: {"label", "status", "ok", "failed"}}}.
    "failed" means every news feed asked for (NSE filings, Google News) failed for every
    symbol, so nothing was checked; with no news feed asked, every source that was tried."""
    srcs = stats.get("sources") or {}
    labels = dict(SOURCES)
    per = {k: _status_of(srcs.get(k)) for k in asked}
    news = [k for k in asked if k in NEWS_FEEDS]
    tried = [k for k in asked if per[k] != "skipped"]
    if news:
        failed = all(per[k] == "failed" for k in news)
        dead = news
    else:
        failed = bool(tried) and all(per[k] == "failed" for k in tried)
        dead = tried
    bits = []
    for k in tried:
        s = srcs[k]
        if per[k] == "partial":
            bits.append("%s failed for %s (ok for %s)" % (labels[k], ", ".join(sorted(s["failed"])),
                                                          ", ".join(sorted(s["ok"]))))
        else:
            bits.append("%s %s" % (labels[k], per[k]))
    status = "failed" if failed else ("ok" if all(per[k] in ("ok", "skipped") for k in asked) else "partial")
    return {"status": status, "line": "checked: " + "; ".join(bits) if bits else "",
            "headline": "could not reach %s: nothing was checked" % " or ".join(FEED_SHORT[k] for k in dead),
            "sources": {k: {"label": labels[k], "status": per[k], "ok": list((srcs.get(k) or {}).get("ok", [])),
                            "failed": dict((srcs.get(k) or {}).get("failed", {}))} for k in asked}}


# ---------------------------------------------------------------- output

def _px(v):
    return nc.inr(v, 2, keep_zeros=True)


def _day(iso):
    d = _dt.date.fromisoformat(iso)
    return "%s %d %s %d" % (WEEKDAY[d.weekday()], d.day, d.strftime("%b"), d.year)


def render_moves(moves, now, move_pct=MOVE_PCT):
    out = []
    today = nc.to_ist(now).date()
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = ""
        if m.get("time"):
            ist = nc.to_ist(m["time"])
            when = (" at %s IST" % ist.strftime("%H:%M")) if ist.date() == today else \
                (" (%s %s IST)" % (_day(ist.date().isoformat()), ist.strftime("%H:%M")))
        out.append("%-10s %s %+.2f%% vs previous close %s%s  %s" % (
            m["ticker"], _px(m["price"]), m["pct"], _px(m["prev_close"]), when, flag))
    return [l.rstrip() for l in out]


def render_calendar(events):
    out = []
    for e in events:
        soon = {0: "today", 1: "tomorrow"}.get(e["days"], "in %d days" % e["days"])
        if e["results"]:
            line = "%-10s reports on %s (%s): board meeting for %s" % (e["symbol"], _day(e["date"]), soon,
                                                                       e["purpose"] or "results")
        else:
            line = "%-10s board meeting on %s (%s): %s" % (e["symbol"], _day(e["date"]), soon,
                                                         e["purpose"] or "purpose not given")
        out.append(line)
        if e["desc"]:
            out.append("      " + _short(e["desc"], 160))
    return out


def _short(text, n):
    text = _clean(text)
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def render(items, errors, now, moves=None, events=None, move_pct=MOVE_PCT, status=None):
    """The text report. `status` (run_status): when every news feed failed the first line
    says nothing was checked (never "Nothing new."); when a source failed the "checked:"
    line next to the result names it."""
    state = (status or {}).get("status", "ok")
    out = []
    if state == "failed":
        out += ["News check FAILED: %s." % status["headline"], ""]
    moves_out = render_moves(moves or [], now, move_pct)
    out += moves_out + ([""] if moves_out else [])
    cal = render_calendar(events or [])
    if cal:
        out.append("Coming up (NSE event calendar):")
        out += ["  " + l for l in cal]
        out.append("")
    if state == "partial" and status.get("line"):
        out.append(status["line"] + ".")
    if not items and state != "failed":
        out.append("Nothing new in what could be checked." if state == "partial" else "Nothing new.")
    for it in items:
        when = nc.ist_text(it["time"]) if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"][:13], it["title"]))
        if it["kind"] == "filing" and it.get("summary") and \
                _clean(it["summary"]).lower() != it["title"].lower():
            out.append("      " + _short(it["summary"], 200))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Filings are what the company told NSE; headlines are leads, not facts. "
               "Open the filing or the story before acting. Not financial advice." % nc.ist_text(now))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = nc.utc_iso(it["time"]) if it["time"] else None
        d["time_ist"] = nc.to_ist(it["time"]).strftime("%Y-%m-%d %H:%M") if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default=nc.tpath("portfolio.json"))
    ap.add_argument("--watchlist", default=nc.tpath("watchlist.json"))
    ap.add_argument("--plan", default=nc.tpath("plan.json"))
    ap.add_argument("--state", default=nc.tpath("state", "news.json"))
    ap.add_argument("--hours", type=float, default=None,
                    help="ignore items older than this (default 72, or back to the last full check when "
                         "that was longer ago; at most 720 unless --all)")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="skip NSE filings (headlines only)")
    ap.add_argument("--no-google", action="store_true", help="skip Google News (NSE filings only)")
    ap.add_argument("--no-calendar", action="store_true", help="skip NSE's board meeting calendar")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='"YYYY-MM-DD HH:MM" IST, for reproducible runs')
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    errors, stats = [], {}
    if args.tickers:
        watch = {_sym(t): None for t in args.tickers if _sym(t)}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan, errors)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan, errors)
    bad = [t for t in watch if not nc.is_nse_symbol(t)]
    if bad:
        print("news: not an NSE symbol: %s" % ", ".join(sorted(bad)), file=sys.stderr)
        return 2
    if not watch:
        print("news: no symbols. Pass --tickers, or run /nse-scanner first (it writes %s; none there "
              "with hits, and nothing in %s or %s)." % (args.watchlist, args.plan, args.portfolio),
              file=sys.stderr)
        for e in errors:
            print("news: %s" % e, file=sys.stderr)
        return 2
    try:
        now = nc.parse_now(args.now)
    except ValueError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    try:
        state = nc.read_json(args.state, {}) or {}
        if not isinstance(state, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        # not silently: with the seen marks gone, everything in the window shows as new again
        errors.append("could not read %s (%s): starting with nothing marked as seen, so items shown before "
                      "may show again" % (args.state, exc))
        state = {}
    asked = [k for k, off in (("nse", args.no_nse), ("google", args.no_google), ("calendar", args.no_calendar),
                              ("prices", args.no_moves)) if not off]
    feeds = [k for k in asked if k in NEWS_FEEDS]
    explicit = args.hours is not None
    if explicit:
        if args.hours > MAX_HOURS and not args.all:
            errors.append("--hours %g is capped at %d: seen items are kept %d days, so a wider window could "
                          "show one again (--all shows older items, seen or not)" % (args.hours, MAX_HOURS,
                                                                                   KEEP_DAYS))
            args.hours = MAX_HOURS
    else:
        args.hours = window_hours(state, now, sorted(watch), feeds)
        if args.hours > HOURS:
            errors.append("looking back %d hours (not %d): the last full check of some feed was that long ago"
                          % (args.hours, HOURS))
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, now, hours=args.hours, nse=not args.no_nse,
                    google=not args.no_google, errors=errors, stats=stats)
    # a hit with no "name" (nse-scanner may not write one) is tagged by the name NSE gives
    # (sm_name, EQUITY_L), else by its symbol alone
    named = {t: watch.get(t) or (stats.get("names") or {}).get(t) for t in watch}
    items = dedupe(tag(items, named))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    events = [] if args.no_calendar else calendar(fetcher, watch, now, errors, stats)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors, stats)
    status = run_status(stats, asked)
    if args.json:
        mv = [dict(m, time=nc.utc_iso(m["time"]) if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": nc.utc_iso(now), "checked_ist": nc.ist_text(now),
                          "status": status["status"], "checked_line": status["line"],
                          "failure": status["headline"] if status["status"] == "failed" else None,
                          "sources": status["sources"], "names": named, "moves": mv,
                          "calendar": events, "items": _json_ready(shown), "notes": errors,
                          "quote_pages_skipped": stats.get("quote_pages_skipped", 0),
                          "off_topic_skipped": stats.get("off_topic_skipped", 0)},
                         indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, events, args.move_pct, status))
    if status["status"] == "failed":
        # nothing was checked: leave the state (seen items, last_check) as it was, and say so
        # with its own exit code rather than "nothing new"
        return 3
    if not args.dry_run:
        remember(items, state, now, args.hours)
        if not explicit:
            mark_checked(state, stats, now, feeds)  # an explicit --hours may not reach back far enough
        try:
            nc.write_json(args.state, state)
        except OSError as exc:
            print("news: could not save %s: %s" % (args.state, exc), file=sys.stderr)
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
