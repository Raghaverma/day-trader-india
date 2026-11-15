#!/usr/bin/env python3
"""Big-money tracker for nse-deals. Standard library only.

Reads NSE's bulk and block deal files (who bought or sold a large chunk of a
stock that day, by name), keeps a history the exchange itself does not keep
in a file, and alerts once per new deal. Filters by symbol, client name, side
and size; guesses the client type from the name; flags deals in the stocks
you hold or watch.

    python3 deals.py show                                   # the latest day's deals, ₹1 cr and up
    python3 deals.py show --min-value 25cr --side BUY       # only big buys
    python3 deals.py new                                    # only what has not alerted before
    python3 deals.py new --client "mutual fund" --client "societe generale"
    python3 deals.py history --days 30 --type "foreign fund"
    python3 deals.py show --symbol MEESHO --min-value 0

Where the data comes from (published by NSE after the close, the latest day only):
  https://nsearchives.nseindia.com/content/equities/bulk.csv
  https://nsearchives.nseindia.com/content/equities/block.csv
  Columns: Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded,
  Trade Price / Wght. Avg. Price[, Remarks].

What these are, and are not:
  - A bulk deal: all of one client's trades in a stock on one day add up to
    more than 0.5% of the company's listed shares (SEBI/MRD/SE/Cir-7/2004).
    The price is the weighted average of those trades.
  - A block deal: a single trade of at least ₹25 crore in a separate window,
    08:45-09:00 (reference: the previous close) or 14:05-14:20 (reference:
    the 13:45-14:00 VWAP), within ±3% of the reference, delivery only
    (SEBI circular SEBI/HO/MRD/POD-III/CIR/P/2025/134, 8 Oct 2025).
  - Both are PUBLIC disclosures, published the same evening. Reading them is
    not inside information, and a deal says what someone did, not what they
    or anyone will do next.

Client types are a guess from the name (mutual fund, insurance, bank, foreign
fund, otherwise "other"); check the name yourself before saying who it is.

State: trader/state/deals.json keeps every deal seen (90 days, deduplicated)
and which ones have alerted, so each deal alerts once. Config (trader/config.json):
"follow_clients": ["societe generale", "hdfc mutual fund"] (name substrings) and
"deals_min_value_inr": 10000000. Offline: --offline DIR reads the newest
bulk*.csv and block*.csv in DIR, or --bulk FILE / --block FILE. The tests use it.
"""

import argparse
import csv
import datetime as _dt
import glob
import hashlib
import http.client
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_common as nc  # noqa: E402

BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"
KEEP_DAYS = 90
MIN_VALUE = 10000000  # ₹1 crore
CLIENT_TYPES = ("mutual fund", "insurance", "bank", "foreign fund", "other")

MUTUAL_FUND = re.compile(r"\bMUTUAL\s+FUND\b|\bMF\b", re.I)
INSURANCE = re.compile(r"\bINSURANCE\b|\bASSURANCE\b|\bLIFE\s+INSURANCE\s+CORPORATION\b", re.I)
# Names that read as foreign: legal forms and places abroad, sovereign and pension
# money, emerging-market fund wording and the big global houses.
FOREIGN = re.compile(
    r"\b(?:PTE|LLC|L\.?P\.?|PLC|INC|SICAV|ICAV|GMBH|S\.?A\.?|AG|SE|N\.?V\.?|B\.?V\.?|ULC)\s*\.?\s*$|"
    r"\b(?:PTE\.?|LLC|L\.P\.|PLC|SICAV|ICAV|GMBH)\b|\(DE\)|\bMAURITIUS\b|\bCAYMAN\b|\bSINGAPORE\b|"
    r"\bHONG\s+KONG\b|\bLUXEMBOURG\b|\bIRELAND\b|\bJERSEY\b|\bEUROPE\b|\bGOVERNMENT\s+OF\s+(?!INDIA)|"
    r"\bMONETARY\s+AUTHORITY\b|\bNORGES\b|\bABU\s+DHABI\b|\bQATAR\b|\bKUWAIT\b|\bSAUDI\b|\bPENSION\b|"
    r"\bRETIREMENT\b|\bSUPERANNUATION\b|\bENDOWMENT\b|\bSOVEREIGN\b|\bEMERGING\s+MARKETS?\b|\bEX\s+CHINA\b|"
    r"\bCOMMINGLED\b|\bMASTER\s+FUND\b|\bFIDELITY\b|\bFIAM\b|\bVANGUARD\b|\bISHARES\b|\bBLACKROCK\b|"
    r"\bGOLDMAN\s+SACHS\b|\bMORGAN\s+STANLEY\b|\bSOCIETE\s+GENERALE\b|\bBNP\s+PARIBAS\b|\bCITIGROUP\b|"
    r"\bJ\.?\s?P\.?\s?MORGAN\b|\bMERRILL\s+LYNCH\b|\bBOFA\b|\bNOMURA\b|\bUBS\b|\bCLSA\b|\bHSBC\b|"
    r"\bBARCLAYS\b|\bDEUTSCHE\b|\bWILLIAM\s+BLAIR\b|\bGHISALLO\b|\bCOPTHALL\b|\bINTEGRATED\s+CORE\b",
    re.I)
BANK = re.compile(r"\bBANK\b", re.I)


class DealsError(Exception):
    pass


# ---------------------------------------------------------------- reading

def classify(client):
    """A guess at who the client is, from the name alone."""
    name = " ".join(str(client or "").split())
    if MUTUAL_FUND.search(name):
        return "mutual fund"
    if INSURANCE.search(name):
        return "insurance"
    if FOREIGN.search(name):
        return "foreign fund"
    if BANK.search(name):
        return "bank"
    return "other"


def _iso(text):
    s = str(text or "").strip().title()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _num(text):
    s = str(text or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _col(head, *words):
    for i, h in enumerate(head):
        low = h.lower()
        if all(w in low for w in words):
            return i
    return None


def parse_deals(text, kind):
    """bulk.csv / block.csv text -> deals (one dict per row), in file order."""
    text = text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    cols = {"date": _col(head, "date"), "symbol": _col(head, "symbol"), "security": _col(head, "security"),
            "client": _col(head, "client"), "side": _col(head, "buy"), "qty": _col(head, "quantity"),
            "price": _col(head, "price"), "remarks": _col(head, "remark")}
    if None in (cols["date"], cols["symbol"], cols["client"], cols["side"], cols["qty"], cols["price"]):
        raise DealsError("%s.csv does not have the expected columns (got: %s)" % (kind, ", ".join(head)))
    out, count = [], {}
    for r in rows[1:]:
        if len(r) < len(head) - 1 or not "".join(r).strip():
            continue
        get = lambda k: r[cols[k]].strip() if cols[k] is not None and cols[k] < len(r) else ""
        date, qty, price = _iso(get("date")), _num(get("qty")), _num(get("price"))
        side = get("side").upper()
        if not date or qty is None or price is None or side not in ("BUY", "SELL"):
            continue
        sym = nc.nse_symbol(get("symbol"))
        client = " ".join(get("client").split())
        key = (kind, date, sym, client.upper(), side, int(qty), round(price, 4))
        count[key] = count.get(key, 0) + 1  # two identical rows in one file are two trades
        did = hashlib.sha1(("%s|%d" % ("|".join(str(k) for k in key), count[key])).encode("utf-8")).hexdigest()[:16]
        remarks = get("remarks")
        out.append({"id": did, "kind": kind, "date": date, "symbol": sym, "security": get("security"),
                    "client": client, "side": side, "qty": int(qty), "price": price,
                    "value": round(qty * price, 2), "client_type": classify(client),
                    "remarks": "" if remarks in ("-", "") else remarks})
    return out


def annotate(deals):
    """Mark bulk rows that are also block deals, and clients on both sides the same day."""
    blocks = {(d["date"], d["symbol"], d["client"].upper(), d["side"], d["qty"], round(d["price"], 2))
              for d in deals if d["kind"] == "block"}
    sides = {}
    for d in deals:
        sides.setdefault((d["date"], d["symbol"], d["client"].upper()), set()).add(d["side"])
    bulks = {(d["date"], d["symbol"], d["client"].upper(), d["side"], d["qty"], round(d["price"], 2))
             for d in deals if d["kind"] == "bulk"}
    for d in deals:
        key = (d["date"], d["symbol"], d["client"].upper(), d["side"], d["qty"], round(d["price"], 2))
        d["also_block"] = d["kind"] == "bulk" and key in blocks
        d["also_bulk"] = d["kind"] == "block" and key in bulks
        d["round_trip"] = sides[(d["date"], d["symbol"], d["client"].upper())] == {"BUY", "SELL"}
    return deals


class Source(object):
    """Gets bulk.csv and block.csv: from NSE, or from files for tests."""

    def __init__(self, offline=None, bulk=None, block=None):
        self.offline, self.files = offline, {"bulk": bulk, "block": block}

    def _newest(self, kind):
        found = sorted(glob.glob(os.path.join(self.offline, "%s*.csv" % kind)))
        if not found:
            raise DealsError("offline: no %s*.csv in %s" % (kind, self.offline))
        return found[-1]

    def text(self, kind):
        path = self.files.get(kind) or (self._newest(kind) if self.offline else None)
        if path:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        url = BULK_URL if kind == "bulk" else BLOCK_URL
        req = urllib.request.Request(url, headers={"User-Agent": nc.UA, "Accept": "text/csv,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise DealsError("HTTP %s for %s" % (exc.code, url))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise DealsError("could not reach %s: %s" % (url, getattr(exc, "reason", exc)))


def load_latest(source, kinds=("bulk", "block"), errors=None):
    errors = [] if errors is None else errors
    deals = []
    for kind in kinds:
        try:
            deals += parse_deals(source.text(kind), kind)
        except (DealsError, OSError) as exc:
            errors.append("%s deals: %s" % (kind, exc))
    return annotate(deals)


# ---------------------------------------------------------------- state

def merge(state, deals, now):
    """Add deals to the history (deduplicated); forget deals older than KEEP_DAYS. -> new ids."""
    hist = state.setdefault("deals", {})
    stamp = nc.utc_iso(now)
    added = []
    for d in deals:
        if d["id"] not in hist:
            hist[d["id"]] = dict(d, first_seen=stamp)
            added.append(d["id"])
        else:
            hist[d["id"]].update({k: d[k] for k in ("also_block", "also_bulk", "round_trip", "client_type")})
    cutoff = (nc.to_ist(now).date() - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["deals"] = {k: v for k, v in hist.items() if v["date"] >= cutoff}
    state["alerted"] = {k: v for k, v in (state.get("alerted") or {}).items() if k in state["deals"]}
    state["days"] = sorted({v["date"] for v in state["deals"].values()})
    return added


# ---------------------------------------------------------------- filters

def parse_value(text):
    """ "25cr", "₹25 crore", "50L", "50 lakh", "10000000" -> rupees."""
    s = str(text or "").strip().lower().replace("₹", "").replace(",", "").replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)(cr|crore|crores|l|lakh|lakhs|lac)?$", s)
    if not m:
        raise ValueError("--min-value wants rupees, like 25cr, 50lakh or 10000000, got %r" % text)
    v = float(m.group(1))
    unit = m.group(2) or ""
    if unit.startswith("c"):
        v *= 1e7
    elif unit:
        v *= 1e5
    return v


def _norm(s):
    return " ".join(str(s or "").upper().split())


def select(deals, symbols=None, clients=None, side=None, min_value=0, kinds=None, types=None, mine=None,
           days=None, today=None, round_trips=False):
    """Deals passing every filter, newest day first, biggest first. A bulk row that repeats a
    block deal is left out when block deals are shown (the block row says so), and so are
    same-day round trips (a client buying and selling the same stock) unless round_trips."""
    out = []
    syms = {nc.nse_symbol(s) for s in symbols or []}
    subs = [_norm(c) for c in clients or [] if _norm(c)]
    for d in deals:
        if syms and d["symbol"] not in syms:
            continue
        if subs and not any(s in _norm(d["client"]) for s in subs):
            continue
        if side and d["side"] != side.upper():
            continue
        if min_value and d["value"] < min_value:
            continue
        if kinds and d["kind"] not in kinds:
            continue
        if types and d["client_type"] not in types:
            continue
        if mine is not None and d["symbol"] not in mine:
            continue
        if days and today and d["date"] < (today - _dt.timedelta(days=days - 1)).isoformat():
            continue
        if d.get("also_block") and (not kinds or "block" in kinds):
            continue
        if d.get("round_trip") and not round_trips:
            continue
        out.append(d)
    out.sort(key=lambda d: (d["date"], d["value"]), reverse=True)
    return out


def your_symbols(portfolio, watchlist):
    """{SYMBOL: "portfolio" | "watchlist"} from trader/portfolio.json and trader/watchlist.json."""
    out = {}
    for path, key, label in ((watchlist, "hits", "watchlist"), (portfolio, "positions", "portfolio")):
        try:
            doc = nc.read_json(path) if path else None
        except (OSError, ValueError):
            doc = None
        for row in (doc or {}).get(key, []) if isinstance(doc, dict) else []:
            if isinstance(row, dict):
                sym = row.get("symbol") or row.get("tradingsymbol") or row.get("ticker")
                if sym:
                    out[nc.nse_symbol(sym)] = label
    return out


def prev_session(today, holidays=None):
    d = today - _dt.timedelta(days=1)
    for _ in range(15):
        if nc.trading_day(d, holidays)[0]:
            return d
        d -= _dt.timedelta(days=1)
    return d


# ---------------------------------------------------------------- output

def _qty(n):
    return nc.inr(n, 0)[1:]


def money(v):
    return nc.crore(v) if abs(v) >= 1e7 else nc.lakh(v)


def _day(iso):
    d = _dt.date.fromisoformat(iso)
    return "%d %s %d" % (d.day, d.strftime("%b"), d.year)


def describe(d, yours=None, follow=()):
    line = "%-5s %-11s %-12s %-4s %12s @ %s = %s  %s [%s]" % (
        d["kind"].upper(), d["date"], d["symbol"], d["side"], _qty(d["qty"]),
        nc.inr(d["price"], 2, keep_zeros=True), money(d["value"]), d["client"], d["client_type"])
    flags = []
    where = (yours or {}).get(d["symbol"])
    if where:
        flags.append("%s is in your %s" % (d["symbol"], where))
    if any(_norm(f) in _norm(d["client"]) for f in follow if _norm(f)):
        flags.append("a client you follow")
    if d.get("also_block"):
        flags.append("also a block deal")
    if d.get("also_bulk"):
        flags.append("also a bulk deal: over 0.5% of the company's shares")
    if d.get("round_trip"):
        flags.append("bought and sold the same day: likely intraday or arbitrage, not a position")
    if d.get("remarks"):
        flags.append(d["remarks"])
    return line + ("\n      <- " + "; ".join(flags) if flags else "")


FOOTER = ("Public disclosures NSE publishes after the close. Bulk: one client's trades in a stock that day "
          "above 0.5% of its listed shares (weighted average price). Block: a single trade of at least "
          "₹25 crore in the 08:45-09:00 or 14:05-14:20 window. Not inside information. Not financial advice.")


def render(deals, header, yours=None, follow=(), empty="No deals match."):
    out = [header, ""]
    if not deals:
        out.append(empty)
    for d in deals:
        out.append(describe(d, yours, follow))
    out.append("")
    out.append(FOOTER)
    return "\n".join(out) + "\n"


def summary(deals):
    """Totals per side and client type, in rupees."""
    out = {}
    for d in deals:
        k = "%s %s" % (d["client_type"], d["side"].lower())
        out[k] = out.get(k, 0) + d["value"]
    return {k: round(v, 2) for k, v in sorted(out.items())}


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p, fetch=True):
        p.add_argument("--symbol", nargs="*", help="only these NSE symbols")
        p.add_argument("--client", action="append", help="client name contains this (repeat for more); "
                                                         "adds to follow_clients in the config")
        p.add_argument("--any-client", action="store_true", help="ignore follow_clients from the config")
        p.add_argument("--side", choices=["BUY", "SELL", "buy", "sell"])
        p.add_argument("--min-value", help="minimum deal value in rupees: 25cr, 50lakh, 10000000 "
                                           "(default: deals_min_value_inr or ₹1 cr)")
        p.add_argument("--kind", choices=["bulk", "block", "all"], default="all")
        p.add_argument("--type", action="append", choices=CLIENT_TYPES, help="client type (a guess from the name)")
        p.add_argument("--mine", action="store_true", help="only stocks in your portfolio or watchlist")
        p.add_argument("--round-trips", action="store_true",
                       help="include clients who bought and sold the same stock the same day (hidden by default)")
        p.add_argument("--config", default=os.path.join("trader", "config.json"))
        p.add_argument("--portfolio", default=os.path.join("trader", "portfolio.json"))
        p.add_argument("--watchlist", default=os.path.join("trader", "watchlist.json"))
        p.add_argument("--state", default=os.path.join("trader", "state", "deals.json"))
        p.add_argument("--now", help='"YYYY-MM-DD HH:MM" IST, for reproducible runs')
        p.add_argument("--json", action="store_true")
        if fetch:
            p.add_argument("--offline", metavar="DIR", help="newest bulk*.csv and block*.csv in DIR")
            p.add_argument("--bulk", metavar="FILE")
            p.add_argument("--block", metavar="FILE")
            p.add_argument("--dry-run", action="store_true", help="do not update the state file")

    p = sub.add_parser("show", help="the latest day's deals (adds them to the history)")
    common(p)
    p = sub.add_parser("new", help="only deals that have not alerted before")
    common(p)
    p = sub.add_parser("history", help="deals kept in trader/state/deals.json")
    common(p, fetch=False)
    p.add_argument("--days", type=int, default=30, help="last N calendar days (default 30)")
    return ap


def main(argv=None):
    nc.utf8_stdout()
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except (DealsError, ValueError, OSError) as exc:
        print("deals: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    cfg = nc.load_config(args.config)
    now = nc.parse_now(args.now)
    today = nc.to_ist(now).date()
    follow = [] if args.any_client else [str(c) for c in cfg.get("follow_clients") or []]
    clients = follow + (args.client or [])
    min_value = parse_value(args.min_value) if args.min_value is not None else \
        float(cfg.get("deals_min_value_inr", MIN_VALUE) or 0)
    yours = your_symbols(args.portfolio, args.watchlist)
    kinds = None if args.kind == "all" else {args.kind}
    filt = dict(symbols=args.symbol, clients=clients, side=args.side, min_value=min_value, kinds=kinds,
                types=set(args.type) if args.type else None, mine=set(yours) if args.mine else None,
                round_trips=args.round_trips)
    try:
        state = nc.read_json(args.state, {}) or {}
    except ValueError:
        raise DealsError("%s is not valid JSON" % args.state)
    errors = []
    what = []
    if clients:
        what.append("clients: " + ", ".join(clients))
    if min_value:
        what.append("%s or more" % money(min_value))
    if args.symbol:
        what.append("symbols: " + ", ".join(nc.nse_symbol(s) for s in args.symbol))
    if args.side:
        what.append(args.side.upper() + " only")
    if args.type:
        what.append("client type: " + ", ".join(args.type))
    if args.mine:
        what.append("your stocks only")
    if not args.round_trips:
        what.append("same-day round trips hidden (--round-trips shows them)")
    filters = (" Filters: " + "; ".join(what) + ".") if what else ""

    if args.cmd == "history":
        deals = annotate(list((state.get("deals") or {}).values())) if state.get("deals") else []
        picked = select(deals, days=args.days, today=today, **filt)
        header = "NSE bulk and block deals kept in %s, last %d days: %d of %d shown.%s" % (
            args.state, args.days, len(picked), len(deals), filters)
        if args.json:
            print(json.dumps({"deals": picked, "summary": summary(picked)}, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(render(picked, header, yours, follow, "No deals in the history match. Run "
                                    "`deals.py show` after 18:00 IST on a trading day to collect them."))
        return 0 if picked else 1

    source = Source(args.offline, args.bulk, args.block)
    latest = load_latest(source, ("bulk", "block") if args.kind == "all" else (args.kind,), errors)
    if not latest and errors:
        for e in errors:
            print("deals: %s" % e, file=sys.stderr)
        return 2
    merge(state, latest, now)
    dates = sorted({d["date"] for d in latest})
    day = dates[-1] if dates else None
    if day:
        hol, _ = nc.load_holidays(cfg)
        expected = prev_session(today, hol) if nc.to_ist(now).time() < _dt.time(18, 0) or \
            not nc.trading_day(today, hol)[0] else today
        if day < expected.isoformat():
            errors.append("NSE's files are for %s; the last session was %s. They may not be updated yet."
                          % (day, expected.isoformat()))
    picked = select(latest, **filt)
    counts = "%d block, %d bulk" % (sum(1 for d in latest if d["kind"] == "block"),
                                    sum(1 for d in latest if d["kind"] == "bulk"))
    if args.cmd == "show":
        shown, fresh = picked, None
        header = "NSE bulk and block deals for %s (%s): %d shown.%s" % (
            _day(day) if day else "?", counts, len(shown), filters)
    else:
        alerted = state.setdefault("alerted", {})
        fresh = [d for d in picked if d["id"] not in alerted]
        shown = fresh
        header = "New NSE bulk and block deals since %s: %d (%s on %s).%s" % (
            state.get("last_check") or "the first check", len(fresh), counts, _day(day) if day else "?", filters)
    if args.json:
        print(json.dumps({"date": day, "deals": shown, "summary": summary(shown), "notes": errors,
                          "filters": {"clients": clients, "min_value_inr": min_value, "symbols": args.symbol,
                                      "side": args.side, "kind": args.kind, "types": args.type,
                                      "mine": args.mine}}, indent=2, ensure_ascii=False))
    else:
        text = render(shown, header, yours, follow,
                      "Nothing new." if args.cmd == "new" else "No deals match.")
        for e in errors:
            text += "note: %s\n" % e
        sys.stdout.write(text)
    if not args.dry_run:
        if args.cmd == "new":
            stamp = nc.utc_iso(now)
            for d in fresh:
                state["alerted"][d["id"]] = stamp
            state["last_check"] = stamp
        nc.write_json(args.state, state)
    return 0 if shown else 1


if __name__ == "__main__":
    sys.exit(main())
