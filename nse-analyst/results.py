#!/usr/bin/env python3
"""Results reader for nse-analyst. Standard library only.

Reads a company's quarterly results straight from the XBRL it files with NSE
(the "Integrated Filing - Financials") and lays out what changed, quarter by
quarter, in rupees crore: revenue from operations, total income, expenses,
PBT, net profit attributable to owners, diluted EPS, the margins the filing
supports (EBITDA, PBT, net), growth against the same quarter a year earlier
(from that year's own filing), against the quarter before, and the last four
quarters. Banks get their own lines: interest earned, net interest income,
operating profit, provisions, gross and net NPA %.

    python3 results.py RELIANCE
    python3 results.py RELIANCE TCS --quarters 7
    python3 results.py HDFCBANK --standalone          # NPA ratios are in the standalone filing
    python3 results.py RELIANCE --json
    python3 results.py RELIANCE INFY --metrics trader/metrics.json

Sources (no keys; NSE's /api/ with nc.nse_headers()):
  https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=<SYM>&type=Integrated%20Filing-%20Financials
      every quarter's filing (consolidated and standalone) with its XBRL link
  https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_..._WEB.xml
      one quarter: context OneD is the quarter, FourD the year to date.
      Cached under trader/cache/nse-xbrl/ (a filing never changes; a revision
      gets a new file).
  https://www.nseindia.com/api/event-calendar?index=equities
      upcoming board meetings: does it report this week?
  Yahoo Finance fundamentals-timeseries (unofficial, consolidated, has gaps):
      only when NSE fails, and every row says where it came from.

How the quarters are built:
  - One filing per quarter: consolidated by default (--standalone for the
    company alone). When a quarter was filed twice (a revision), the later
    broadcast wins. A company with no consolidated filings is shown
    standalone, and the report says so.
  - Year-on-year compares a quarter with the quarter ending a year earlier,
    read from that quarter's own filing (each XBRL has only the quarter and
    the year to date). Quarter-on-quarter compares with the quarter before.
    Many businesses are seasonal: read the year-on-year first.
  - EBITDA = profit before exceptional items and tax + finance costs +
    depreciation and amortisation - other income, only when the filing has
    all four. It is derived, not reported.
  - Ratios NSE stores as fractions (NPA, CET1) are shown in percent; a zero in
    a consolidated bank filing means "not reported there" (banks report NPA
    ratios in the standalone filing), so it is shown blank.

--metrics FILE merges one row per symbol into a JSON file (TTM revenue, net
profit and EPS, margins, growth). Offline: --offline DIR reads
integrated-filing-<SYM>.json, xbrl-<SYM>-<DD-MON-YYYY>-<consolidated|standalone>.xml[.gz],
yahoo-timeseries-<SYM>.json and event-calendar.json from DIR (or --calendar FILE).

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import gzip
import http.client
import http.cookiejar
import json
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
FILINGS_URL = ("https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=%s"
               "&type=Integrated%%20Filing-%%20Financials")
CAL_URL = "https://www.nseindia.com/api/event-calendar?index=equities"
YAHOO_URL = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
             "?type=%s&period1=%d&period2=%d")
YAHOO_TYPES = ("quarterlyTotalRevenue", "quarterlyNetIncome", "quarterlyDilutedEPS", "quarterlyOperatingIncome",
               "quarterlyGrossProfit", "quarterlyBasicEPS")
XBRL_CACHE = os.path.join("trader", "cache", "nse-xbrl")
SRC_NSE = "NSE XBRL"
SRC_YAHOO = "Yahoo"

# Tags (namespace in-capmkt), first one present wins.
COMPANY_TAGS = {
    "revenue": ["RevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income"],
    "expenses": ["Expenses"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense"],
    "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
    "exceptional": ["ExceptionalItemsBeforeTax"],
    "pbt": ["ProfitBeforeTax"],
    "tax": ["TaxExpense"],
    "profit": ["ProfitLossForPeriod"],
    "profit_owners": ["ProfitOrLossAttributableToOwnersOfParent"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsLossPerShareFromContinuingOperations"],
}
BANK_TAGS = {
    "interest_earned": ["InterestEarned"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income"],
    "interest_expended": ["InterestExpended"],
    "operating_expenses": ["OperatingExpenses"],
    "operating_profit": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "exceptional": ["ExceptionalItems"],
    "pbt": ["ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "tax": ["TaxExpense"],
    "profit": ["ProfitLossForThePeriod"],
    "profit_owners": ["ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"],
    "eps_diluted": ["DilutedEarningsPerShareAfterExtraordinaryItems",
                    "DilutedEarningsPerShareBeforeExtraordinaryItems"],
    "gross_npa_pct": ["PercentageOfGrossNpa"],
    "net_npa_pct": ["PercentageOfNpa"],
    "cet1_pct": ["CET1Ratio"],
}
RATIOS = ("gross_npa_pct", "net_npa_pct", "cet1_pct")
META_TAGS = ("Symbol", "NameOfTheCompany", "NameOfBank", "NatureOfReportStandaloneConsolidated",
             "ReportingQuarter", "WhetherResultsAreAuditedOrUnaudited", "DateOfStartOfFinancialYear",
             "DateOfEndOfFinancialYear", "DateOfBoardMeetingWhenFinancialResultsWereApproved", "LevelOfRounding")
QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.6, cache_dir=XBRL_CACHE):
        self.offline, self.pause, self.cache_dir = offline, pause, cache_dir
        self._last, self._primed = 0.0, False
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _open(self, url, headers):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        with self._opener.open(urllib.request.Request(url, headers=headers), timeout=45) as resp:
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
            raise ResultsError("offline: no file %s" % path)
        headers = nc.nse_headers() if nse else {"User-Agent": nc.UA, "Accept-Encoding": "gzip",
                                                "Accept": "*/*"}
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
            raise ResultsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ResultsError("could not reach %s: %s" % (url.split("?")[0], getattr(exc, "reason", exc)))

    def xbrl(self, url, name):
        """An XBRL file, from the cache when we have it (filings never change)."""
        if self.offline:
            return self.get(url, name)
        cached = os.path.join(self.cache_dir, os.path.basename(urllib.parse.urlparse(url).path) + ".gz") \
            if self.cache_dir else None
        if cached and os.path.isfile(cached):
            try:
                with gzip.open(cached, "rb") as fh:
                    return fh.read()
            except (OSError, EOFError):
                pass
        data = self.get(url, name)
        if cached and data.lstrip()[:1] == b"<":
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(cached, "wb") as raw:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                        gz.write(data)
            except OSError:
                pass
        return data


def _json(data, what):
    try:
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError):
        raise ResultsError("%s is not JSON (NSE may be blocking the request)" % what)


# ---------------------------------------------------------------- the filing list

def _qe_date(text):
    """ "30-SEP-2026" -> date."""
    try:
        return _dt.datetime.strptime(str(text or "").strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _broadcast(text):
    try:
        return _dt.datetime.strptime(str(text or "").strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return _dt.datetime(1970, 1, 1)


def parse_filings(doc):
    """integrated-filing-results JSON -> [{end, basis, audited, broadcast, xbrl, ixbrl, name, qe}]."""
    rows = (doc or {}).get("data") if isinstance(doc, dict) else doc
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        end = _qe_date(r.get("qe_Date"))
        basis = str(r.get("consolidated") or "").strip().lower()
        if not end or basis not in ("consolidated", "standalone") or not r.get("xbrl"):
            continue
        out.append({"end": end.isoformat(), "qe": str(r.get("qe_Date")).strip().upper(), "basis": basis,
                    "audited": str(r.get("audited") or "").strip(), "broadcast": str(r.get("broadcast_Date") or ""),
                    "xbrl": str(r["xbrl"]).strip(), "ixbrl": str(r.get("ixbrl") or "").strip() or None,
                    "name": str(r.get("smName") or r.get("cmName") or "").strip() or None,
                    "revision": str(r.get("type_Sub") or "").strip()})
    return out


def pick_filings(filings, basis="consolidated", quarters=5):
    """One filing per quarter (the latest broadcast), newest first. Returns (rows, basis used)."""
    use = basis
    rows = [f for f in filings if f["basis"] == basis]
    if not rows and basis == "consolidated":
        rows = [f for f in filings if f["basis"] == "standalone"]
        use = "standalone"
    best = {}
    for f in rows:
        if f["end"] not in best or _broadcast(f["broadcast"]) > _broadcast(best[f["end"]]["broadcast"]):
            best[f["end"]] = f
    picked = sorted(best.values(), key=lambda f: f["end"], reverse=True)[:max(1, quarters)]
    return picked, use


# ---------------------------------------------------------------- one XBRL

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _num(text):
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) and abs(v) >= 1000 else v


def parse_xbrl(data):
    """One quarter's integrated-filing XBRL -> {"bank", "start", "end", "ytd_start", "meta", "q", "ytd"}.

    q and ytd map our keys (COMPANY_TAGS or BANK_TAGS) to rupees (EPS in rupees
    per share, ratios in percent)."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ResultsError("XBRL is not valid XML: %s" % exc)
    contexts = {}
    for c in root:
        if _local(c.tag) != "context":
            continue
        if any(_local(e.tag) in ("scenario", "segment") for e in c.iter()):
            continue
        dates = {_local(e.tag): (e.text or "").strip() for e in c.iter()
                 if _local(e.tag) in ("startDate", "endDate", "instant")}
        if "startDate" in dates and "endDate" in dates:
            try:
                contexts[c.get("id")] = (_dt.date.fromisoformat(dates["startDate"]),
                                         _dt.date.fromisoformat(dates["endDate"]))
            except ValueError:
                continue
    facts, meta = {}, {}
    for el in root:
        ref = el.get("contextRef")
        if ref not in contexts:
            continue
        name = _local(el.tag)
        if name in META_TAGS and name not in meta:
            meta[name] = (el.text or "").strip()
        facts.setdefault(ref, {})[name] = (el.text or "").strip()
    if not contexts:
        raise ResultsError("XBRL has no reporting period")
    end = max(e for _, e in contexts.values())
    quarter = [k for k, (s, e) in contexts.items() if e == end and 80 <= (e - s).days <= 100]
    ytd = [k for k, (s, e) in contexts.items() if e == end and (e - s).days > 100]
    qid = "OneD" if "OneD" in quarter else (quarter[0] if quarter else None)
    yid = "FourD" if "FourD" in ytd else (max(ytd, key=lambda k: (end - contexts[k][0]).days) if ytd else None)
    if qid is None:
        raise ResultsError("XBRL has no three-month period ending %s" % end)
    names = set(facts.get(qid, {}))
    bank = "InterestEarned" in names or "NameOfBank" in meta
    tags = BANK_TAGS if bank else COMPANY_TAGS

    def pull(ref):
        got = facts.get(ref, {})
        out = {}
        for key, cands in tags.items():
            for t in cands:
                if t in got and got[t] != "":
                    v = _num(got[t])
                    if v is not None and key in RATIOS:
                        v = round(v * 100, 2) if v else None  # NSE stores 1.11% as 0.0111; 0 = not reported
                    out[key] = v
                    break
        return out

    return {"bank": bank, "start": contexts[qid][0].isoformat(), "end": end.isoformat(),
            "ytd_start": contexts[yid][0].isoformat() if yid else None, "meta": meta,
            "q": pull(qid), "ytd": pull(yid) if yid else {}}


def fiscal_label(end, meta=None):
    """Quarter end -> "Q2 FY27" (Indian fiscal year April-March unless the filing says otherwise)."""
    d = _dt.date.fromisoformat(end)
    meta = meta or {}
    fy_end = None
    try:
        fy_end = _dt.date.fromisoformat(meta.get("DateOfEndOfFinancialYear", "")).year
    except ValueError:
        pass
    words = str(meta.get("ReportingQuarter") or "").split()
    q = QUARTER_WORDS.get(words[0].lower()) if words else None
    if q is None or fy_end is None:
        q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
        fy_end = d.year + 1 if d.month >= 4 else d.year
        if q is None:
            return d.strftime("%b %Y")
    return "Q%d FY%02d" % (q, fy_end % 100)


# ---------------------------------------------------------------- Yahoo fallback

def parse_yahoo(doc):
    """fundamentals-timeseries JSON -> {end: {"revenue", "profit_owners", "eps_diluted"}} (consolidated)."""
    keys = {"quarterlyTotalRevenue": "revenue", "quarterlyNetIncome": "profit_owners",
            "quarterlyDilutedEPS": "eps_diluted"}
    out = {}
    for r in ((doc or {}).get("timeseries") or {}).get("result") or []:
        t = ((r.get("meta") or {}).get("type") or [None])[0]
        if t not in keys:
            continue
        for v in r.get(t) or []:
            if not v or v.get("periodType") != "3M" or (v.get("currencyCode") or "INR") != "INR":
                continue
            raw = (v.get("reportedValue") or {}).get("raw")
            if raw is None:
                continue
            out.setdefault(v["asOfDate"], {})[keys[t]] = _num(raw) if keys[t] != "eps_diluted" else float(raw)
    return out


def yahoo_quarters(fetcher, symbol, now):
    end = int((now - _dt.datetime(1970, 1, 1)).total_seconds())
    url = YAHOO_URL % (urllib.parse.quote(nc.yahoo_symbol(symbol), safe=""), ",".join(YAHOO_TYPES),
                       end - 3 * 366 * 86400, end)
    return parse_yahoo(_json(fetcher.get(url, "yahoo-timeseries-%s.json" % symbol), "Yahoo timeseries"))


# ---------------------------------------------------------------- the analysis

def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def _prior_end(end, months):
    """The month-end `months` months before a month-end date (negative: after)."""
    d = _dt.date.fromisoformat(end)
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    first_next = _dt.date(y + 1, 1, 1) if m == 12 else _dt.date(y, m + 1, 1)
    return (first_next - _dt.timedelta(days=1)).isoformat()


def build_row(end, start, values, source, bank, basis, label, filing=None, meta=None):
    v = dict(values)
    net = v.get("profit_owners") if v.get("profit_owners") is not None else v.get("profit")
    row = {"end": end, "start": start, "quarter": label, "source": source, "bank": bank, "basis": basis,
           "values": v, "net_profit": net, "eps_diluted": v.get("eps_diluted"),
           "audited": (meta or {}).get("WhetherResultsAreAuditedOrUnaudited") or (filing or {}).get("audited"),
           "filed": (filing or {}).get("broadcast"), "xbrl": (filing or {}).get("xbrl"),
           "ixbrl": (filing or {}).get("ixbrl")}
    if bank:
        ie, ix = v.get("interest_earned"), v.get("interest_expended")
        row["top_line"] = v.get("total_income")
        row["nii"] = ie - ix if ie is not None and ix is not None else None
        row["net_margin"] = margin(net, v.get("total_income"))
        row["pbt_margin"] = margin(v.get("pbt"), v.get("total_income"))
        row["ebitda"] = row["ebitda_margin"] = None
    else:
        rev = v.get("revenue")
        row["top_line"] = rev
        base = v.get("pbt_before_exceptional")
        if base is None and v.get("pbt") is not None:
            base = v["pbt"] - (v.get("exceptional") or 0)
        parts = (base, v.get("finance_costs"), v.get("depreciation"), v.get("other_income"))
        row["ebitda"] = (parts[0] + parts[1] + parts[2] - parts[3]) if None not in parts else None
        row["ebitda_margin"] = margin(row["ebitda"], rev)
        row["pbt_margin"] = margin(v.get("pbt"), rev)
        row["net_margin"] = margin(net, rev)
    return row


def analyse(rows, quarters=None):
    """Add growth (YoY from the year-earlier row, QoQ) and the last four quarters."""
    by = {r["end"]: r for r in rows}
    rows = sorted(rows, key=lambda r: r["end"], reverse=True)
    for r in rows:
        prev_y = by.get(_prior_end(r["end"], 12))
        prev_q = by.get(_prior_end(r["end"], 3))
        for key, name in (("top_line", "top_line"), ("net_profit", "net_profit"), ("eps_diluted", "eps"),
                          ("nii", "nii")):
            r[name + "_yoy"] = pct(r.get(key), (prev_y or {}).get(key))
            r[name + "_qoq"] = pct(r.get(key), (prev_q or {}).get(key))
        r["yoy_vs"] = prev_y["end"] if prev_y else None
        r["yoy_source"] = prev_y["source"] if prev_y else None
        r["yoy_mixed"] = bool(prev_y and prev_y["source"] != r["source"])
        r["qoq_mixed"] = bool(prev_q and prev_q["source"] != r["source"])
        for m in ("ebitda_margin", "pbt_margin", "net_margin"):
            r[m + "_qoq"] = round(r[m] - prev_q[m], 1) if prev_q and r.get(m) is not None and \
                prev_q.get(m) is not None else None
    ttm = {}
    if len(rows) >= 4:
        last4 = rows[:4]
        consecutive = all(last4[i + 1]["end"] == _prior_end(last4[i]["end"], 3) for i in range(3))
        if consecutive:
            for key in ("top_line", "net_profit", "eps_diluted", "ebitda", "nii"):
                vals = [r.get(key) for r in last4]
                if None not in vals:
                    ttm[key] = round(sum(vals), 2) if key == "eps_diluted" else sum(vals)
            ttm["from"], ttm["to"] = last4[-1]["end"], last4[0]["end"]
            ttm["sources"] = sorted({r["source"] for r in last4})
    return {"rows": rows[:quarters] if quarters else rows, "ttm": ttm}


def report(fetcher, symbol, quarters=5, basis="consolidated", now=None, notes=None):
    """Everything for one symbol: {"symbol", "name", "basis", "bank", "rows", "ttm", "latest", "sources"}."""
    notes = [] if notes is None else notes
    now = now or nc.utc_now()
    filings, nse_error = [], None
    try:
        filings = parse_filings(_json(fetcher.get(FILINGS_URL % urllib.parse.quote(symbol), "integrated-filing-%s.json"
                                                  % symbol, nse=True), "NSE integrated filings"))
        if not filings:
            nse_error = "NSE has no integrated filings for %s" % symbol
    except ResultsError as exc:
        nse_error = "NSE filings list: %s" % exc
    picked, used = pick_filings(filings, basis, quarters) if filings else ([], basis)
    if picked and used != basis:
        notes.append("%s has no %s filings on NSE: standalone numbers shown" % (symbol, basis))
    rows, name, bank, missing = [], None, False, []
    for f in picked:
        fname = "xbrl-%s-%s-%s.xml" % (symbol, f["qe"], f["basis"])
        try:
            x = parse_xbrl(fetcher.xbrl(f["xbrl"], fname))
        except ResultsError as exc:
            missing.append((f, str(exc)))
            continue
        bank = bank or x["bank"]
        name = name or f.get("name") or x["meta"].get("NameOfTheCompany") or x["meta"].get("NameOfBank")
        rows.append(build_row(x["end"], x["start"], x["q"], SRC_NSE, x["bank"], f["basis"],
                              fiscal_label(x["end"], x["meta"]), f, x["meta"]))
    if nse_error or missing:
        want = [f["end"] for f, _ in missing]
        try:
            yq = yahoo_quarters(fetcher, symbol, now)
        except ResultsError as exc:
            yq = {}
            notes.append("Yahoo fallback for %s: %s" % (symbol, exc))
        if nse_error:
            notes.append("%s; numbers below come from Yahoo Finance (consolidated, unofficial, can have gaps)"
                         % nse_error)
            want = sorted(yq, reverse=True)[:quarters]
        for f, why in missing:
            if f["end"] not in yq:
                notes.append("%s %s: could not read the XBRL (%s) and Yahoo has no number for it"
                             % (symbol, f["qe"], why))
        for end in want:
            if end in yq:
                if not nse_error:
                    notes.append("%s quarter ending %s: XBRL unreadable, Yahoo's numbers used" % (symbol, end))
                rows.append(build_row(end, None, yq[end], SRC_YAHOO, bank, "consolidated", fiscal_label(end)))
    if not rows:
        raise ResultsError(nse_error or "no quarter could be read")
    result = analyse(rows, quarters)
    latest = picked[0] if picked else None
    return {"symbol": symbol, "name": name or (latest or {}).get("name"), "basis": used, "bank": bank,
            "rows": result["rows"], "ttm": result["ttm"], "latest": latest,
            "sources": sorted({r["source"] for r in result["rows"]})}


# ---------------------------------------------------------------- calendar and due dates

def parse_calendar(doc):
    rows = doc if isinstance(doc, list) else (doc or {}).get("data") or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            d = _dt.datetime.strptime(str(r.get("date") or "").strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        out.append({"symbol": nc.nse_symbol(r.get("symbol")), "purpose": str(r.get("purpose") or "").strip(),
                    "desc": re.sub(r"\s+", " ", str(r.get("bm_desc") or "")).strip(), "date": d.isoformat()})
    return out


def next_meeting(events, symbol, now):
    """The next board meeting on NSE's calendar for a symbol, with days to go."""
    today = nc.to_ist(now).date()
    mine = sorted((e for e in events if e["symbol"] == symbol and e["date"] >= today.isoformat()),
                  key=lambda e: e["date"])
    if not mine:
        return None
    e = dict(mine[0])
    e["days"] = (_dt.date.fromisoformat(e["date"]) - today).days
    e["results"] = bool(re.search(r"result", e["purpose"] + " " + e["desc"], re.I))
    e["this_week"] = e["days"] <= 7
    return e


def results_due(last_end):
    """SEBI LODR Regulation 33: quarterly results within 45 days of the quarter's end, the year's
    (March quarter) within 60. -> (next quarter end, due date) after last_end."""
    nxt = _dt.date.fromisoformat(_prior_end(last_end, -3))
    return nxt.isoformat(), (nxt + _dt.timedelta(days=60 if nxt.month == 3 else 45)).isoformat()


def overdue_note(result, now):
    if not result["rows"] or result["sources"] == [SRC_YAHOO]:
        return None
    q_end, due = results_due(result["rows"][0]["end"])
    today = nc.to_ist(now).date().isoformat()
    if today > due:
        return ("Results for the quarter ending %s were due by %s (SEBI LODR Reg 33), and NSE's integrated "
                "filing list does not have them yet. Check /nse-news for the filing before summarising."
                % (q_end, due))
    return None


# ---------------------------------------------------------------- output

def _cr(v):
    """Rupees -> "3,32,861" (crore, no symbol) for table cells."""
    if v is None:
        return "-"
    return nc.crore(v).replace("₹", "").replace(" cr", "")


def _p(v, mark=""):
    return "-" if v is None else ("%+.1f%%" % v if v else "0.0%") + mark


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def _np(v):
    return "-" if v is None else "%.2f%%" % v


def _eps(v):
    return "-" if v is None else "%.2f" % v


def _day(iso):
    d = _dt.date.fromisoformat(iso)
    return "%s %d %s %d" % (WEEKDAY[d.weekday()], d.day, d.strftime("%b"), d.year)


def _net_word(row):
    return "net profit to owners" if row["basis"] == "consolidated" and \
        row["values"].get("profit_owners") is not None else "net profit"


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    out = []
    bank = cur["bank"]
    net_word = _net_word(cur)
    top = "total income" if bank else ("revenue from operations" if cur["source"] == SRC_NSE
                                       else "revenue (Yahoo's total revenue)")
    line = "Quarter ending %s (%s): %s %s" % (cur["end"], cur["quarter"], top,
                                              nc.crore(cur["top_line"]) if cur["top_line"] is not None else "n/a")
    if cur["top_line_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["top_line_yoy"]
    if bank and cur.get("nii") is not None:
        line += ", net interest income %s" % nc.crore(cur["nii"])
        if cur["nii_yoy"] is not None:
            line += " (%+.1f%%)" % cur["nii_yoy"]
    line += ", %s %s" % (net_word, nc.crore(cur["net_profit"]) if cur["net_profit"] is not None else "n/a")
    if cur["net_profit_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_profit_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS %s" % nc.inr(cur["eps_diluted"], 2, keep_zeros=True)
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    if cur["yoy_mixed"]:
        out.append("This quarter comes from %s and the year-earlier one from %s: the growth figures compare "
                   "slightly different definitions." % (cur["source"], cur["yoy_source"]))
    for key, name in (("ebitda_margin", "EBITDA margin (derived)"), ("pbt_margin", "PBT margin"),
                      ("net_margin", "Net margin")):
        if cur.get(key) is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if cur.get(key + "_qoq") is not None:
            s += " (%+.1f points vs the quarter before)" % cur[key + "_qoq"]
        out.append(s + ".")
    v = cur["values"]
    if bank:
        bits = []
        if v.get("gross_npa_pct") is not None:
            bits.append("gross NPA %.2f%%" % v["gross_npa_pct"])
        if v.get("net_npa_pct") is not None:
            bits.append("net NPA %.2f%%" % v["net_npa_pct"])
        if v.get("provisions") is not None:
            bits.append("provisions %s" % nc.crore(v["provisions"]))
        if bits:
            out.append("Asset quality: " + ", ".join(bits) + ".")
        if v.get("gross_npa_pct") is None and cur["basis"] == "consolidated" and cur["source"] == SRC_NSE:
            out.append("NPA ratios are not in the consolidated filing: run with --standalone for them.")
    if v.get("exceptional"):
        out.append("Exceptional items of %s are inside PBT this quarter: check the notes before reading "
                   "the profit as growth." % nc.crore(v["exceptional"]))
    pbt = v.get("pbt")
    if not bank and v.get("other_income") and pbt and pbt > 0 and v["other_income"] / float(pbt) >= 0.2:
        out.append("Other income is %.0f%% of PBT: part of the profit is not from operations."
                   % (100.0 * v["other_income"] / pbt))
    if cur["top_line_qoq"] is not None:
        out.append("%s %+.1f%% vs the quarter before (many businesses are seasonal: compare with a year "
                   "earlier first)." % (top[0].upper() + top[1:], cur["top_line_qoq"]))
    return out


def render(result, now=None, meeting=None, notes=()):
    rows = result["rows"]
    bank = result["bank"]
    srcs = " and ".join("NSE integrated filings (XBRL)" if s == SRC_NSE else "Yahoo Finance (fallback)"
                        for s in result["sources"])
    who = "%s (%s)" % (result["name"], result["symbol"]) if result["name"] else result["symbol"]
    out = ["%s. %s, ₹ crore except EPS (₹). Source: %s." % (who, result["basis"].capitalize(), srcs), ""]
    y = lambda r: "  yahoo" if r["source"] == SRC_YAHOO else ""
    if bank:
        out.append("%-8s %-10s %10s %9s %10s %9s %10s %9s %10s %7s" % (
            "quarter", "end", "int earned", "NII", "tot income", "op profit", "provisions", "PBT",
            "net profit", "EPS dil"))
        for r in rows:
            v = r["values"]
            out.append("%-8s %-10s %10s %9s %10s %9s %10s %9s %10s %7s%s" % (
                r["quarter"], r["end"], _cr(v.get("interest_earned")), _cr(r.get("nii")),
                _cr(v.get("total_income")), _cr(v.get("operating_profit")), _cr(v.get("provisions")),
                _cr(v.get("pbt")), _cr(r["net_profit"]), _eps(r["eps_diluted"]), y(r)))
        out.append("")
        out.append("%-8s %8s %8s %10s %8s %8s %8s %6s %6s %6s" % (
            "quarter", "NII yoy", "NII qoq", "income yoy", "NP yoy", "NP qoq", "EPS yoy", "net%", "GNPA", "NNPA"))
        for r in rows:
            v = r["values"]
            m, q = "*" if r["yoy_mixed"] else "", "*" if r["qoq_mixed"] else ""
            out.append("%-8s %8s %8s %10s %8s %8s %8s %6s %6s %6s" % (
                r["quarter"], _p(r["nii_yoy"], m), _p(r["nii_qoq"], q), _p(r["top_line_yoy"], m),
                _p(r["net_profit_yoy"], m), _p(r["net_profit_qoq"], q), _p(r["eps_yoy"], m), _pm(r["net_margin"]),
                _np(v.get("gross_npa_pct")),
                _np(v.get("net_npa_pct"))))
    else:
        out.append("%-8s %-10s %10s %10s %10s %9s %9s %10s %7s" % (
            "quarter", "end", "revenue", "tot income", "expenses", "EBITDA", "PBT", "net profit", "EPS dil"))
        for r in rows:
            v = r["values"]
            out.append("%-8s %-10s %10s %10s %10s %9s %9s %10s %7s%s" % (
                r["quarter"], r["end"], _cr(r["top_line"]), _cr(v.get("total_income")), _cr(v.get("expenses")),
                _cr(r["ebitda"]), _cr(v.get("pbt")), _cr(r["net_profit"]), _eps(r["eps_diluted"]), y(r)))
        out.append("")
        out.append("%-8s %8s %8s %8s %8s %8s %8s %6s %6s" % (
            "quarter", "rev yoy", "rev qoq", "NP yoy", "NP qoq", "EPS yoy", "EBITDA%", "PBT%", "net%"))
        for r in rows:
            m, q = "*" if r["yoy_mixed"] else "", "*" if r["qoq_mixed"] else ""
            out.append("%-8s %8s %8s %8s %8s %8s %8s %6s %6s" % (
                r["quarter"], _p(r["top_line_yoy"], m), _p(r["top_line_qoq"], q), _p(r["net_profit_yoy"], m),
                _p(r["net_profit_qoq"], q), _p(r["eps_yoy"], m), _pm(r["ebitda_margin"]), _pm(r["pbt_margin"]),
                _pm(r["net_margin"])))
    if any(r["yoy_mixed"] or r["qoq_mixed"] for r in rows):
        out.append("* compared with a row from the other source (NSE XBRL vs Yahoo): definitions differ.")
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters (%s to %s): %s %s, %s %s, diluted EPS %s (sum of four quarters, "
                   "approximate)%s." % (
                       t["from"], t["to"], "total income" if bank else "revenue",
                       nc.crore(t["top_line"]) if t.get("top_line") is not None else "-",
                       _net_word(rows[0]), nc.crore(t["net_profit"]) if t.get("net_profit") is not None else "-",
                       nc.inr(t["eps_diluted"], 2, keep_zeros=True) if t.get("eps_diluted") is not None else "-",
                       "; sources: " + ", ".join(t["sources"]) if len(t.get("sources", [])) > 1 else ""))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    out.append("")
    lt = result.get("latest")
    if lt:
        out.append("Latest filing: quarter ending %s, %s, %s, broadcast %s IST  %s" % (
            lt["qe"], lt["basis"], (lt["audited"] or "audit status not given").lower(), lt["broadcast"],
            lt["ixbrl"] or lt["xbrl"]))
    if meeting is not None:
        if meeting is False:
            out.append("Next board meeting: none for %s on NSE's event calendar (it lists the next few weeks)."
                       % result["symbol"])
        else:
            soon = {0: "today", 1: "tomorrow"}.get(meeting["days"], "in %d days" % meeting["days"])
            what = "reports results" if meeting["results"] else "board meeting"
            out.append("Next board meeting: %s %s (%s), %s: %s.%s" % (
                _day(meeting["date"]), soon, meeting["purpose"] or "purpose not given", what,
                meeting["desc"][:140], " It reports within the next 7 days." if meeting["results"] and
                meeting["this_week"] else ""))
    if rows:
        q_end, due = results_due(rows[0]["end"])
        out.append("Results for the quarter ending %s are due by %s (SEBI LODR Regulation 33: 45 days after a "
                   "quarter, 60 after the March quarter)." % (q_end, due))
    note = overdue_note(result, now or nc.utc_now())
    for n in list(notes) + ([note] if note else []):
        out.append("NOTE: " + n)
    if any(r["source"] == SRC_YAHOO for r in rows):
        out.append("Rows marked yahoo come from Yahoo Finance, not the filing: its revenue is defined "
                   "slightly differently from NSE's revenue from operations.")
    return "\n".join(out) + "\n"


def metrics_row(result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    v = cur.get("values") or {}
    return {"symbol": result["symbol"], "name": result["name"], "basis": result["basis"],
            "bank": result["bank"], "period_end": cur.get("end"), "quarter": cur.get("quarter"),
            "currency": "INR", "revenue_ttm": t.get("top_line"), "net_profit_ttm": t.get("net_profit"),
            "eps_ttm": t.get("eps_diluted"), "revenue_growth_yoy": cur.get("top_line_yoy"),
            "net_profit_growth_yoy": cur.get("net_profit_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "ebitda_margin": cur.get("ebitda_margin"), "pbt_margin": cur.get("pbt_margin"),
            "net_margin": cur.get("net_margin"), "gross_npa_pct": v.get("gross_npa_pct"),
            "net_npa_pct": v.get("net_npa_pct"), "source": ", ".join(result["sources"])}


def load_calendar(fetcher, path=None):
    if path:
        with open(path, "rb") as fh:
            return parse_calendar(_json(fh.read(), "event calendar"))
    return parse_calendar(_json(fetcher.get(CAL_URL, "event-calendar.json", nse=True), "NSE event calendar"))


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. RELIANCE TCS")
    ap.add_argument("--quarters", type=int, default=5, help="quarters to read (one XBRL each; default 5)")
    ap.add_argument("--standalone", action="store_true", help="the company alone (default: consolidated)")
    ap.add_argument("--no-calendar", action="store_true", help="skip NSE's board meeting calendar")
    ap.add_argument("--calendar", metavar="FILE", help="a saved event-calendar.json")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='"YYYY-MM-DD HH:MM" IST, for reproducible runs')
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.symbols:
        ap.print_help()
        return 2
    try:
        now = nc.parse_now(args.now)
    except ValueError as exc:
        print("results: %s" % exc, file=sys.stderr)
        return 2
    symbols = [nc.nse_symbol(s) for s in args.symbols]
    bad = [s for s in symbols if not nc.is_nse_symbol(s)]
    if bad:
        print("results: not an NSE symbol: %s" % ", ".join(bad), file=sys.stderr)
        return 2
    fetcher = Fetcher(offline=args.offline)
    events, cal_note = None, None
    if not args.no_calendar:
        try:
            events = load_calendar(fetcher, args.calendar)
        except (ResultsError, OSError) as exc:
            if not (args.offline and "no file" in str(exc)):
                cal_note = "NSE event calendar: %s" % exc
    basis = "standalone" if args.standalone else "consolidated"
    out, rows, failed = [], [], 0
    for sym in symbols:
        notes = [cal_note] if cal_note else []
        try:
            result = report(fetcher, sym, args.quarters, basis, now, notes)
        except ResultsError as exc:
            print("results: %s: %s" % (sym, exc), file=sys.stderr)
            failed += 1
            continue
        meeting = None
        if events is not None:
            meeting = next_meeting(events, sym, now) or False
        note = overdue_note(result, now)
        out.append({"symbol": sym, "result": result, "changes": changes(result),
                    "next_meeting": meeting or None, "results_due": results_due(result["rows"][0]["end"]),
                    "notes": notes + ([note] if note else [])})
        rows.append(metrics_row(result))
        if not args.json:
            sys.stdout.write(render(result, now, meeting, notes))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    if args.metrics and rows:
        try:
            existing = {}
            doc = nc.read_json(args.metrics, {}) or {}
            for r in doc.get("companies", []):
                existing[r.get("symbol") or r.get("ticker")] = r
            for r in rows:
                existing[r["symbol"]] = r
            nc.write_json(args.metrics, {"as_of": nc.to_ist(now).date().isoformat(), "currency": "INR",
                                         "companies": sorted(existing.values(), key=lambda r: r["symbol"])})
        except (OSError, ValueError) as exc:
            print("results: could not write %s: %s" % (args.metrics, exc), file=sys.stderr)
            return 2
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if out and not failed else (1 if out else 2)


if __name__ == "__main__":
    sys.exit(main())
