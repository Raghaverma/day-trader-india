#!/usr/bin/env python3
"""The dataset: every breakout signal, every paper trade, every day and every call the research made, as plain CSV
tables to study and, later, to train on. Standard library only; paper only; not advice.

    python3 autopilot/dataset.py             # rebuild <trader folder>/dataset (or autopilot.json "dataset": {"dir"})
    python3 autopilot/dataset.py --check     # build it and print the row counts; write nothing

Nothing in it is new information: it is the trader folder's own records joined and labelled, so it is rebuilt from
them in full each time (run_day.py does it after the evening's journal when autopilot.json has
"dataset": {"enabled": true}). The raw records it reads are kept for good: the bar archive (archive/1m, archive/5m),
each day's folder (autopilot/<date>/: the signals, the decisions, the deep research) and the reports.

The tables (README.md in the folder lists every column):

  signals.csv       one row per long opening-range-breakout signal on every archived day: what was known when it
                    fired (the features), what the autopilot decided on the days it ran (live_*), and what the
                    signal went on to do when replayed with the live rules (label_*). The table to learn from.
  decisions.csv     one row per signal per paper account: what each account did with it and why.
  trades.csv        one row per paper order in every account: fills, exits, money, and the journal's reasons.
  account_days.csv  one row per account per day.
  days.csv          one row per archived day: the Nifty, India VIX, how the signals did; the autopilot's view of
                    the day where it ran.
  vetoes.csv        every stock the pre-market sat out (Claude's news check, the deep research's CIO), and what
                    it did that day.
  deep_calls.csv    the deep pre-market's call on every Nifty 50 stock (stance, conviction, rank) and what the stock
                    did that day.
  notes.jsonl       Claude's own words each day: the news check's summary, the deep morning note, the evening
                    analysis.

The features are what was known when a signal fired; the label_* columns are only known afterwards. Training on a
label as an input (or on anything from later that day) would look brilliant and mean nothing.
"""

import argparse
import csv
import datetime
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autopilot_common as ac  # noqa: E402
import journal  # noqa: E402
import market  # noqa: E402
import research  # noqa: E402
import study  # noqa: E402

nc = ac.nc
VERSION = 1
OPEN_MIN = 9 * 60 + 15
HISTORY = 20

ID, FEATURE, LIVE, LABEL = "id", "feature", "live", "label"
KIND_WORDS = {ID: "Which signal", FEATURE: "Known when the signal fired (features)",
              LIVE: "What the autopilot knew and did, on the days it ran (blank before 18 Nov 2026)",
              LABEL: "What happened after (labels: never use these as inputs)"}

SIGNAL_COLS = [
    ("date", ID, "the trading day (IST)"),
    ("weekday", ID, "Mon..Fri"),
    ("symbol", ID, "NSE symbol"),
    ("sector", ID, "NSE's industry for the stock"),
    ("setup", ID, "orb_long: the 15-minute opening-range breakout, long"),
    ("signal_at", FEATURE, "HH:MM when the breakout five-minute bar closed above the 09:15-09:30 high"),
    ("minutes_after_open", FEATURE, "minutes from 09:15 to signal_at"),
    ("signal_n", FEATURE, "this signal's place in the day's signals, in time order (1 = first)"),
    ("signals_same_bar", FEATURE, "how many stocks broke out on the same five-minute bar"),
    ("price", FEATURE, "the breakout bar's close (₹)"),
    ("or_high", FEATURE, "the opening range's high, 09:15-09:30 (₹)"),
    ("or_low", FEATURE, "the opening range's low (₹)"),
    ("or_range_pct", FEATURE, "the opening range as % of its low"),
    ("pct_over", FEATURE, "how far the breakout bar closed over the range high (%)"),
    ("entry", FEATURE, "the plan's LIMIT buy (₹)"),
    ("stop", FEATURE, "the plan's stop: the middle of the opening range (₹)"),
    ("target", FEATURE, "the plan's target: twice the risk (2R) (₹)"),
    ("risk_pct", FEATURE, "entry to stop as % of the entry"),
    ("qty", FEATURE, "shares the plan sizes for a ₹5,000 max loss on the ₹10 lakh account"),
    ("charges_r", FEATURE, "Zerodha's round trip at the stop as a share of the max loss (the cost filter's number)"),
    ("open_rvol", FEATURE, "opening volume 09:20-09:30 over its median of the 20 days before (the volume filter)"),
    ("bar_rvol", FEATURE, "the breakout bar's volume over the median volume of the same five minutes on the 20 days "
                          "before"),
    ("stock_gap_pct", FEATURE, "the stock's open vs its previous close (%)"),
    ("stock_chg_pct", FEATURE, "the entry vs the previous close (%)"),
    ("nifty_chg_pct", FEATURE, "the Nifty at the signal vs its previous close (%)"),
    ("rs", FEATURE, "stock_chg_pct minus nifty_chg_pct (percentage points): strength against the market"),
    ("range_needed", FEATURE, "the 2R target from the day's low so far, over the 14-day average daily range"),
    ("range_used", FEATURE, "the day's high minus low so far, over the 14-day average daily range"),
    ("at_20d_high", FEATURE, "1 if the previous close was at or above the 20 days' highest high before it"),
    ("nifty_gap_pct", FEATURE, "the Nifty's open vs its previous close (%)"),
    ("nifty_vs_20d_pct", FEATURE, "the Nifty's previous close vs its 20-day average close (%)"),
    ("vix_prev_close", FEATURE, "India VIX at the previous close"),
    ("live", LIVE, "1 if the autopilot was running that day and saw this signal"),
    ("live_first_seen", LIVE, "HH:MM the autopilot first saw it"),
    ("live_bar_volume_x", LIVE, "nse-entry's volume ratio for the breakout bar"),
    ("live_open_rvol", LIVE, "the opening volume the live volume filter used (its own 20-day median; open_rvol is "
                             "the archive's)"),
    ("live_daily_setups", LIVE, "daily-chart setups the morning scan found on the stock (bull flag, 20-day "
                                "breakout...)"),
    ("live_outcome", LIVE, "what the main account did: traded, shadow (trade cap), filtered (volume filter), "
                           "vetoed, cost_filter, size_refused..."),
    ("live_reason", LIVE, "the reason recorded with that outcome (a veto's words, a refusal)"),
    ("live_rank_tag", LIVE, "top half / bottom half of the deep research's rank (a tag only, never used to choose)"),
    ("nifty_trend", LIVE, "the pre-market's Nifty trend (up/down/flat)"),
    ("vix_band", LIVE, "the pre-market's India VIX band (low/normal/high)"),
    ("stock_file_flags", LIVE, "what the stock's pre-market file flagged (filings, results, pledges...)"),
    ("veto_source", LIVE, "claude (the news check) or deep (the deep research's CIO), if the stock was sat out"),
    ("veto_category", LIVE, "the veto's category"),
    ("veto_reason", LIVE, "the veto's words"),
    ("deep_rank", LIVE, "the deep research's 0-1 rank for the stock that morning"),
    ("deep_stance", LIVE, "the company analyst's stance (bullish/neutral/bearish)"),
    ("deep_conviction", LIVE, "the company analyst's ORB conviction (0-3)"),
    ("deep_confidence", LIVE, "the company analyst's confidence (0-1)"),
    ("deep_gap_bias", LIVE, "the company analyst's expected gap (up/down/flat)"),
    ("deep_favoured", LIVE, "the CIO's conviction if the stock was on the favoured list"),
    ("tm_p_target", LIVE, "Time Machine: share of 10,000 replays that hit the target first"),
    ("tm_p_stop", LIVE, "Time Machine: share that hit the stop first"),
    ("tm_p_neither", LIVE, "Time Machine: share that hit neither by 15:10"),
    ("tm_avg_r_net", LIVE, "Time Machine: average R after charges"),
    ("tm_category", LIVE, "Time Machine's verdict"),
    ("tm_hist_triggered", LIVE, "past days this setup fired on the stock"),
    ("tm_hist_target", LIVE, "of those, days it hit the target"),
    ("tm_hist_stop", LIVE, "of those, days it hit the stop"),
    ("live_traded", LIVE, "1 if the main account placed an order on it"),
    ("live_exit_reason", LIVE, "the main account's real trade: target, stop, flat_by or cancelled"),
    ("live_net_inr", LIVE, "the main account's real net (₹, after charges)"),
    ("live_r", LIVE, "the main account's real R"),
    ("label_bars", LABEL, "the bars the replay used: 1m (as the paper broker) or 5m (coarser, before 22 Oct 2026)"),
    ("label_filled", LABEL, "1 if the LIMIT buy filled within its 15 minutes"),
    ("label_fill_at", LABEL, "HH:MM of the fill"),
    ("label_fill", LABEL, "fill price (₹)"),
    ("label_result", LABEL, "target, stop, flat_by (sold at 15:10) or never filled"),
    ("label_exit_at", LABEL, "HH:MM of the exit"),
    ("label_exit", LABEL, "exit price (₹)"),
    ("label_minutes_held", LABEL, "fill to exit, minutes"),
    ("label_net_inr", LABEL, "net ₹ after Zerodha charges at the plan's size"),
    ("label_r", LABEL, "net over the plan's max loss (R), as every report counts it: a stop is about -0.5R, the "
                       "target about +1.1R"),
    ("label_win", LABEL, "1 if label_net_inr > 0"),
    ("label_mfe", LABEL, "best price while held, in multiples of the planned risk a share (the target is +2)"),
    ("label_mae", LABEL, "worst price while held, same units (the stop is -1)"),
    ("label_r_breakeven", LABEL, "R had the stop moved to breakeven (entry + charges) after +1R"),
    ("label_r_time", LABEL, "R with a time stop: out if not +0.5R 45 minutes after the fill"),
    ("label_r_trail", LABEL, "R with a trail and no target: after +1R the stop follows the 5-bar low"),
    ("label_stock_day_pct", LABEL, "the stock's close vs its previous close (%)"),
    ("label_close_vs_entry_pct", LABEL, "the stock's close vs the entry (%)"),
]

DECISION_COLS = [
    ("date", ID, "the trading day"), ("symbol", ID, "NSE symbol"),
    ("account", ID, "the rules the account traded by: cap3, cap5... (the same rules line up across days)"),
    ("account_label", ID, "the account in words"), ("main", ID, "1 for the main account"),
    ("cap", ID, "trades a day allowed"), ("volume_filter", ID, "the opening-volume filter's minimum, if any"),
    ("outcome", LIVE, "traded, shadow (the trade cap), filtered, vetoed, cost_filter, size_refused..."),
    ("qty", LIVE, "shares, if it was sized"), ("bound_by", LIVE, "what set the size: max loss, cash, position cap"),
    ("label_r", LABEL, "the signal's replayed R (signals.csv), to score the decision"),
]

TRADE_COLS = [
    ("date", ID, ""), ("account", ID, "the rules name (cap3, cap5...)"), ("account_label", ID, ""),
    ("main", ID, "1 for the main account"), ("symbol", ID, ""), ("slot", ID, "the trade slot it took that day"),
    ("signal_at", FEATURE, ""), ("ordered_at", FEATURE, ""), ("qty", FEATURE, ""), ("bound_by", FEATURE, ""),
    ("position_value", FEATURE, "₹"), ("stop", FEATURE, ""), ("target", FEATURE, ""),
    ("breakout_pct_over_range", FEATURE, ""), ("volume_x", FEATURE, "the breakout bar's volume ratio"),
    ("tm_p_target", FEATURE, ""), ("tm_verdict", FEATURE, ""),
    ("result", LABEL, "WIN, LOSS, FLAT, NO FILL"), ("fill_at", LABEL, ""), ("fill_price", LABEL, ""),
    ("exit_at", LABEL, ""), ("exit_price", LABEL, ""), ("exit_reason", LABEL, ""), ("minutes_held", LABEL, ""),
    ("gross", LABEL, "₹"), ("charges", LABEL, "₹"), ("net", LABEL, "₹ after charges"), ("R", LABEL, ""),
    ("best_R", LABEL, "best while held, in planned risk a share"), ("worst_R", LABEL, ""),
    ("nifty_pct_during", LABEL, "the Nifty's move while it was held (%)"),
    ("why_category", LABEL, "the journal's reason in a few words"), ("why", LABEL, "the journal's reason in full"),
    ("decisions", FEATURE, "the autopilot's steps, as the day PDF tells them, joined by |"),
]

ACCOUNT_DAY_COLS = [
    ("date", ID, ""), ("account", ID, ""), ("account_label", ID, ""), ("main", ID, ""), ("cap", ID, ""),
    ("volume_filter", ID, ""), ("orders", LABEL, ""), ("filled", LABEL, ""), ("no_fills", LABEL, ""),
    ("wins", LABEL, ""), ("losses", LABEL, ""), ("charges", LABEL, "₹"), ("net", LABEL, "₹"),
    ("total_R", LABEL, ""), ("return_pct", LABEL, "net over the account's budget (%)"),
    ("capped", LABEL, "signals the trade cap kept it out of"),
    ("capped_net_if_taken", LABEL, "what they would have made, replayed (₹)"),
    ("kept_out_net_if_taken", LABEL, "what the volume filter's rejects would have made, replayed (₹)"),
]

DAY_COLS = [
    ("date", ID, ""), ("weekday", ID, ""), ("live", ID, "1 if the autopilot ran that day"),
    ("nifty_prev_close", FEATURE, ""), ("nifty_open", FEATURE, ""), ("nifty_gap_pct", FEATURE, ""),
    ("nifty_vs_20d_pct", FEATURE, ""), ("vix_prev_close", FEATURE, ""), ("nifty_trend", LIVE, ""),
    ("vix_band", LIVE, ""), ("deep_regime", LIVE, "the CIO's regime call"),
    ("deep_expected_open", LIVE, "the CIO's expected open"), ("vetoes", LIVE, "stocks sat out"),
    ("main_account", LIVE, "the rules the main account traded by"), ("main_net", LIVE, "₹"),
    ("nifty_close", LABEL, ""), ("nifty_day_pct", LABEL, "close vs previous close (%)"),
    ("vix_close", LABEL, ""), ("signals", LABEL, "breakout signals that day (replayed)"),
    ("signals_filled", LABEL, ""), ("signals_won", LABEL, ""),
    ("signals_avg_r", LABEL, "average label_r of the filled signals"),
    ("first5_r", LABEL, "the first five filled signals' R added up (5 a day, no filter)"),
]

VETO_COLS = [
    ("date", ID, ""), ("symbol", ID, ""), ("source", ID, "claude (the news check) or deep (the CIO)"),
    ("category", FEATURE, ""), ("reason", FEATURE, ""), ("had_signal", LABEL, "1 if it broke out that day"),
    ("signal_result", LABEL, "what the breakout did, replayed"), ("signal_r", LABEL, ""),
    ("stock_day_pct", LABEL, "close vs previous close (%)"), ("stock_open_to_close_pct", LABEL, ""),
]

DEEP_COLS = [
    ("date", ID, ""), ("symbol", ID, ""), ("rank", FEATURE, "the CIO's 0-1 rank"),
    ("favoured", FEATURE, "the CIO's conviction if favoured"), ("stance", FEATURE, ""),
    ("conviction", FEATURE, "the analyst's ORB conviction (0-3)"), ("confidence", FEATURE, ""),
    ("gap_bias", FEATURE, ""), ("sit_out", FEATURE, "1 if the analyst asked to sit it out"),
    ("category", FEATURE, ""), ("one_line", FEATURE, "the analyst's call in one line"),
    ("stock_gap_pct", LABEL, "open vs previous close (%)"), ("stock_day_pct", LABEL, ""),
    ("stock_open_to_close_pct", LABEL, ""), ("had_signal", LABEL, ""), ("signal_result", LABEL, ""),
    ("signal_r", LABEL, ""),
]

TABLES = [("signals", SIGNAL_COLS), ("decisions", DECISION_COLS), ("trades", TRADE_COLS),
          ("account_days", ACCOUNT_DAY_COLS), ("days", DAY_COLS), ("vetoes", VETO_COLS), ("deep_calls", DEEP_COLS)]


# ---------------------------------------------------------------- small helpers

def mins(hhmm):
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def pct(a, b):
    return round(100.0 * (a / b - 1), 2) if a is not None and b else None


def rnd(x, n=2):
    return None if x is None else round(float(x), n)


def weekday(day):
    return datetime.date.fromisoformat(day).strftime("%a")


def cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return v


def index_bars(home, day, sym):
    doc = market.read_day(market.archive_path(home, "5m", day, sym)) or {}
    return research.good(doc.get("bars"))


class Context(object):
    """The Nifty and India VIX by day, from the archive."""

    def __init__(self, home, days):
        self.days = list(days)
        self.nifty = {d: index_bars(home, d, market.NIFTY) for d in self.days}
        self.vix = {d: index_bars(home, d, market.VIX) for d in self.days}

    def prev(self, day):
        i = self.days.index(day)
        return self.days[i - 1] if i > 0 else None

    def close(self, table, day):
        b = table.get(day) if day else None
        return b[-1][4] if b else None

    def of(self, day):
        n, p = self.nifty.get(day) or [], self.prev(day)
        pc = self.close(self.nifty, p)
        i = self.days.index(day)
        closes = [self.close(self.nifty, d) for d in self.days[max(0, i - HISTORY):i]]
        closes = [c for c in closes if c]
        return {"nifty_prev_close": rnd(pc), "nifty_open": rnd(n[0][1]) if n else None,
                "nifty_gap_pct": pct(n[0][1], pc) if n else None,
                "nifty_vs_20d_pct": pct(pc, statistics.mean(closes)) if pc and len(closes) >= HISTORY else None,
                "vix_prev_close": rnd(self.close(self.vix, p)), "nifty_close": rnd(n[-1][4]) if n else None,
                "nifty_day_pct": pct(n[-1][4], pc) if n else None, "vix_close": rnd(self.close(self.vix, day))}


def bar_rvol(arch, day, sym, at):
    """The breakout bar's volume over the median of the same five minutes on the 20 archived days before."""
    start = "%02d:%02d" % divmod(mins(at) - 5, 60)

    def vol(d):
        for b in arch.five(d).get(sym) or []:
            if research.hm(b[0]) == start:
                return b[5]
        return None
    now = vol(day)
    prior = [v for v in (vol(d) for d in [d for d in arch.days5 if d < day][-HISTORY:]) if v]
    if not now or len(prior) < research.MIN_HISTORY:
        return None
    return round(now / statistics.median(prior), 2)


def excursion(r, plan, step):
    """(best, worst) while held, in multiples of the planned risk a share."""
    if r.get("fill") is None or not r.get("fill_at"):
        return None, None
    fill, stop = float(r["fill"]), float(plan["stop"])
    risk = fill - stop
    if risk <= 0:
        return None, None
    end = r.get("exit_at") or "15:10"
    held = []
    for when, _o, h, l, _c in r.get("bars") or []:
        t = when.strftime("%H:%M")
        if t < r["fill_at"] or (not r.get("fill_immediate") and t == r["fill_at"] and step > 1):
            continue
        if t > end:
            break
        held.append((h, l))
    if not held:
        return None, None
    return round((max(h for h, _ in held) - fill) / risk, 2), round((min(l for _, l in held) - fill) / risk, 2)


# ---------------------------------------------------------------- the live records of a day

def live_day(home, day, cfg, settings):
    """What the autopilot recorded on a day it ran, or None."""
    rep = ac.read_json(os.path.join(ac.reports_dir(home), "%s.json" % day))
    if not rep:
        return None
    st = ac.load_day(home, day)
    dd = ac.day_dir(home, day)
    vetoes = {}
    for v in (ac.read_json(os.path.join(dd, "vetoes.json")) or {}).get("vetoes") or []:
        if isinstance(v, dict) and v.get("symbol"):
            vetoes[v["symbol"]] = v
    deep_dir = os.path.join(dd, "deep")
    ranks = (ac.read_json(os.path.join(deep_dir, "ranks.json")) or {}).get("ranks") or {}
    cio = ((ac.read_json(os.path.join(deep_dir, "desks", "cio.json")) or {}).get("json")) or {}
    favoured = {f.get("symbol"): f.get("conviction") for f in cio.get("favoured") or [] if isinstance(f, dict)}
    desks = {}
    for path in glob.glob(os.path.join(deep_dir, "desks", "company-*.json")):
        j = (ac.read_json(path) or {}).get("json")
        if isinstance(j, dict) and j.get("symbol"):
            desks[j["symbol"]] = j
    deep_vetoes = {}
    for v in (ac.read_json(os.path.join(deep_dir, "vetoes.json")) or {}).get("vetoes") or []:
        if isinstance(v, dict) and v.get("symbol"):
            deep_vetoes[v["symbol"]] = v
    flags = {f.get("symbol"): f.get("flags") or [] for f in (rep.get("stocks") or {}).get("flagged") or []
             if isinstance(f, dict)}
    trades = {t.get("symbol"): t for t in rep.get("trades") or [] if t.get("symbol")}
    try:
        D = journal.build_day(home, day, cfg, settings)
    except Exception as e:                              # the journal's own rows are extra here
        D = {"error": "%s: %s" % (type(e).__name__, e)}
    return {"report": rep, "state": st, "vetoes": vetoes, "deep_vetoes": deep_vetoes, "ranks": ranks, "cio": cio,
            "favoured": favoured, "desks": desks, "flags": flags, "trades": trades, "journal": D,
            "accounts": journal.day_accounts(home, day, rep, cfg)}


def live_columns(L, sym):
    c = (L["state"].get("candidates") or {}).get(sym) or {}
    rep, reg = L["report"], L["report"].get("regime") or {}
    v = L["vetoes"].get(sym) or L["deep_vetoes"].get(sym) or {}
    desk = L["desks"].get(sym) or {}
    tm, hist = c.get("tm") or {}, (c.get("tm") or {}).get("history") or {}
    t = L["trades"].get(sym) or {}
    out = {"live": bool(c), "nifty_trend": reg.get("nifty_trend"), "vix_band": reg.get("vix_band"),
           "stock_file_flags": L["flags"].get(sym) if (rep.get("stocks") or {}).get("count") else None,
           "veto_source": (v.get("source") or ("deep" if sym in L["deep_vetoes"] else "claude")) if v else None,
           "veto_category": v.get("category"), "veto_reason": v.get("reason"),
           "deep_rank": L["ranks"].get(sym), "deep_stance": desk.get("stance"),
           "deep_conviction": desk.get("orb_conviction"), "deep_confidence": desk.get("confidence"),
           "deep_gap_bias": desk.get("gap_bias"), "deep_favoured": L["favoured"].get(sym)}
    if c:
        out.update(live_first_seen=c.get("first_seen"), live_bar_volume_x=(c.get("trigger") or {}).get("volume_x"),
                   live_open_rvol=c.get("open_rvol"),
                   live_daily_setups=c.get("daily_setups") or None, live_outcome=c.get("outcome"),
                   live_reason=c.get("reason"), live_rank_tag=(c.get("tags") or {}).get("rank"),
                   tm_p_target=tm.get("p_target"), tm_p_stop=tm.get("p_stop"), tm_p_neither=tm.get("p_neither"),
                   tm_avg_r_net=tm.get("avg_R_net"), tm_category=tm.get("category"),
                   tm_hist_triggered=hist.get("triggered"), tm_hist_target=hist.get("target"),
                   tm_hist_stop=hist.get("stop"), live_traded=bool(t),
                   live_exit_reason=t.get("exit_reason") or (t.get("status") if t else None),
                   live_net_inr=t.get("net"), live_r=t.get("r"))
    return out


# ---------------------------------------------------------------- the tables

def stock_day(arch, day, sym):
    """(gap %, day %, open-to-close %) from the 5-minute archive."""
    today = (arch.daily().get(sym) or {}).get(day)
    prior = arch.prior(sym, day)
    if not today:
        return None, None, None
    pc = prior[-1][1]["c"] if prior else None
    return pct(today["o"], pc), pct(today["c"], pc), pct(today["c"], today["o"])


def signal_rows(arch, cfg, ctx, lives, days, max_charges_r=None):
    rows = []
    for day in days:
        kind = "1m" if day in arch.days1 else "5m"
        sigs = sorted(research.orb_signals(arch, cfg, [day]), key=lambda s: (s["at"], s["symbol"]))
        per_bar = {}
        for s in sigs:
            per_bar[s["at"]] = per_bar.get(s["at"], 0) + 1
        day_ctx = ctx.of(day)
        L = lives.get(day)
        seen = set()
        for n, s in enumerate(sigs, 1):
            sym, p, h = s["symbol"], s["plan"], s.get("hit") or {}
            r = research.replay(arch, s, cfg, kind)
            if r is None or r.get("result") in ("no plan", "no data"):
                continue
            seen.add(sym)
            f = research.features(arch, day, sym, s["at"], p["entry"], p["target"])
            gap, day_pct, _oc = stock_day(arch, day, sym)
            row = {"date": day, "weekday": weekday(day), "symbol": sym, "sector": f.get("sector"),
                   "setup": "orb_long", "signal_at": s["at"], "minutes_after_open": mins(s["at"]) - OPEN_MIN,
                   "signal_n": n, "signals_same_bar": per_bar[s["at"]], "price": h.get("price"),
                   "or_high": h.get("or_high"), "or_low": h.get("or_low"),
                   "or_range_pct": pct(h.get("or_high"), h.get("or_low")), "pct_over": h.get("pct_over"),
                   "entry": p["entry"], "stop": p["stop"], "target": p["target"],
                   "risk_pct": round(100 * (p["entry"] - p["stop"]) / p["entry"], 3), "qty": p["quantity"],
                   "charges_r": p.get("charges_r"), "open_rvol": f.get("rvol"),
                   "bar_rvol": bar_rvol(arch, day, sym, s["at"]), "stock_gap_pct": gap,
                   "stock_chg_pct": f.get("stock_chg"), "nifty_chg_pct": f.get("nifty_chg"), "rs": f.get("rs"),
                   "range_needed": f.get("range_needed"), "range_used": f.get("used"),
                   "at_20d_high": f.get("at_20d_high"), "nifty_gap_pct": day_ctx["nifty_gap_pct"],
                   "nifty_vs_20d_pct": day_ctx["nifty_vs_20d_pct"], "vix_prev_close": day_ctx["vix_prev_close"]}
            if L:
                row.update(live_columns(L, sym))
            filled = r.get("fill") is not None and r.get("fill_at") is not None
            row.update(label_bars=kind, label_filled=filled, label_fill_at=r.get("fill_at"), label_fill=r.get("fill"),
                       label_result=r.get("result"), label_exit_at=r.get("exit_at"), label_exit=r.get("exit"),
                       label_net_inr=r.get("net"), label_r=r.get("r"),
                       label_win=(r["net"] > 0) if r.get("net") is not None else None,
                       label_stock_day_pct=day_pct,
                       label_close_vs_entry_pct=pct((arch.daily().get(sym) or {}).get(day, {}).get("c"), p["entry"]))
            if r.get("fill_at") and r.get("exit_at"):
                row["label_minutes_held"] = mins(r["exit_at"]) - mins(r["fill_at"])
            if filled:
                row["label_mfe"], row["label_mae"] = excursion(r, p, study.STEP[kind])
                for rule in ("breakeven", "time", "trail"):
                    e = research.walk_exit(r["bars"], day, r["fill_at"], r.get("fill_immediate"), r["fill"], p, rule,
                                           cfg, sym, study.STEP[kind])
                    if e is not None:
                        row["label_r_" + rule] = research.r_of(p, r["fill"], e[2], cfg)[1]
            rows.append(row)
        if L:                                      # a live signal the replay did not find: keep it, from the report
            shadows = {x.get("symbol"): x for x in (L["report"].get("shadows") or {}).get("rows") or []
                       if x.get("side") != "short"}
            for sym, c in sorted((L["state"].get("candidates") or {}).items()):
                if sym in seen or c.get("side") == "short":
                    continue
                trig, p = c.get("trigger") or {}, c.get("plan") or {}
                row = {"date": day, "weekday": weekday(day), "symbol": sym, "setup": "orb_long",
                       "sector": arch.sectors.get(sym), "signal_at": trig.get("at") or c.get("scan_at"),
                       "price": trig.get("price") or c.get("scan_price"), "or_high": c.get("or_high"),
                       "or_low": c.get("or_low"), "or_range_pct": pct(c.get("or_high"), c.get("or_low")),
                       "pct_over": c.get("pct_over"), "entry": p.get("entry"), "stop": p.get("stop"),
                       "target": p.get("target"), "qty": p.get("quantity"), "charges_r": p.get("charges_r"),
                       "open_rvol": c.get("open_rvol"), "label_bars": "report"}
                if row["signal_at"]:
                    row["minutes_after_open"] = mins(row["signal_at"]) - OPEN_MIN
                row.update(live_columns(L, sym))
                sh = shadows.get(sym) or {}
                t = L["trades"].get(sym) or {}
                res, net, rr = (t.get("exit_reason"), t.get("net"), t.get("r")) if t else \
                    (sh.get("result"), sh.get("net"), sh.get("r"))
                row.update(label_result=res, label_net_inr=net, label_r=rr,
                           label_win=(net > 0) if net is not None else None)
                rows.append(row)
    return rows


def decision_rows(lives, by_key):
    rows = []
    for day, L in sorted(lives.items()):
        accs = L["accounts"]
        for sym, c in sorted((L["state"].get("candidates") or {}).items()):
            if c.get("side") == "short":
                continue
            for a in accs:
                if a["main"]:
                    o, order = c.get("outcome"), c.get("order") or {}
                    d = {"outcome": o, "qty": order.get("qty"), "bound_by": order.get("bound_by")}
                else:
                    d = (c.get("accounts") or {}).get(a["name"]) or {}
                if not d.get("outcome"):
                    continue
                rows.append({"date": day, "symbol": sym, "account": a["name"], "account_label": a["short"],
                             "main": a["main"], "cap": a["cap"],
                             "volume_filter": (a.get("filter") or {}).get("min_open_rvol"),
                             "outcome": d.get("outcome"), "qty": d.get("qty"), "bound_by": d.get("bound_by"),
                             "label_r": (by_key.get((day, sym)) or {}).get("label_r")})
    return rows


def trade_rows(lives):
    rows, accts = [], []
    for day, L in sorted(lives.items()):
        D = L.get("journal") or {}
        for B in D.get("accounts") or []:
            filt = (B.get("filter") or {}).get("min_open_rvol")
            for r in B["rows"]:
                row = {k: r.get(k) for k, _, _ in TRADE_COLS}
                row.update(date=day, account=B["name"], account_label=B["short"], main=B["main"],
                           decisions=" | ".join(r.get("decisions") or []).replace("**", ""))
                rows.append(row)
            accts.append({"date": day, "account": B["name"], "account_label": B["short"], "main": B["main"],
                          "cap": B["cap"], "volume_filter": filt, "orders": B["orders"], "filled": B["filled"],
                          "no_fills": B["no_fills"], "wins": B["wins"], "losses": B["losses"],
                          "charges": B["charges"], "net": B["net"], "total_R": B["total_R"],
                          "return_pct": B.get("return_pct"), "capped": B.get("not_taken_cap"),
                          "capped_net_if_taken": B.get("not_taken_cap_net_if_taken"),
                          "kept_out_net_if_taken": B.get("kept_out_net_if_taken")})
    return rows, accts


def day_rows(ctx, signals, lives, days):
    by_day = {}
    for s in signals:
        by_day.setdefault(s["date"], []).append(s)
    rows = []
    for day in days:
        sig = by_day.get(day) or []
        filled = [s for s in sig if s.get("label_filled") and s.get("label_r") is not None]
        L = lives.get(day)
        row = dict(ctx.of(day), date=day, weekday=weekday(day), live=bool(L), signals=len(sig),
                   signals_filled=len(filled), signals_won=sum(1 for s in filled if (s.get("label_r") or 0) > 0),
                   signals_avg_r=round(statistics.mean(s["label_r"] for s in filled), 3) if filled else None,
                   first5_r=round(sum(s["label_r"] for s in filled[:5]), 2) if filled else None)
        if L:
            rep = L["report"]
            reg, deep = rep.get("regime") or {}, rep.get("deep") or {}
            main = next((a for a in L["accounts"] if a["main"]), {})
            jm = next((B for B in (L.get("journal") or {}).get("accounts") or [] if B.get("main")), {})
            row.update(nifty_trend=reg.get("nifty_trend"), vix_band=reg.get("vix_band"),
                       deep_regime=deep.get("regime") if deep.get("ran") else None,
                       deep_expected_open=deep.get("expected_open") if deep.get("ran") else None,
                       vetoes=len(set(L["vetoes"]) | set(L["deep_vetoes"])), main_account=main.get("name"),
                       main_net=jm.get("net"))
        rows.append(row)
    return rows


def veto_rows(arch, lives, by_key):
    rows = []
    for day, L in sorted(lives.items()):
        both = [(s, v, "claude") for s, v in L["vetoes"].items()] + \
               [(s, v, "deep") for s, v in L["deep_vetoes"].items() if s not in L["vetoes"]]
        for sym, v, src in sorted(both):
            sig = by_key.get((day, sym)) or {}
            _gap, day_pct, oc = stock_day(arch, day, sym)
            rows.append({"date": day, "symbol": sym, "source": v.get("source") or src,
                         "category": v.get("category"), "reason": v.get("reason"), "had_signal": bool(sig),
                         "signal_result": sig.get("label_result"), "signal_r": sig.get("label_r"),
                         "stock_day_pct": day_pct, "stock_open_to_close_pct": oc})
    return rows


def deep_rows(arch, lives, by_key):
    rows = []
    for day, L in sorted(lives.items()):
        syms = sorted(set(L["desks"]) | set(L["ranks"]))
        for sym in syms:
            d = L["desks"].get(sym) or {}
            sig = by_key.get((day, sym)) or {}
            gap, day_pct, oc = stock_day(arch, day, sym)
            rows.append({"date": day, "symbol": sym, "rank": L["ranks"].get(sym),
                         "favoured": L["favoured"].get(sym), "stance": d.get("stance"),
                         "conviction": d.get("orb_conviction"), "confidence": d.get("confidence"),
                         "gap_bias": d.get("gap_bias"), "sit_out": d.get("sit_out"), "category": d.get("category"),
                         "one_line": d.get("one_line"), "stock_gap_pct": gap, "stock_day_pct": day_pct,
                         "stock_open_to_close_pct": oc, "had_signal": bool(sig),
                         "signal_result": sig.get("label_result"), "signal_r": sig.get("label_r")})
    return rows


def note_rows(home, lives):
    out = []
    for day, L in sorted(lives.items()):
        dd = ac.day_dir(home, day)
        v = ac.read_json(os.path.join(dd, "vetoes.json")) or {}
        if v.get("summary"):
            out.append({"date": day, "kind": "news_check", "text": v["summary"]})
        for kind, path in (("deep_morning_note", os.path.join(dd, "deep", "note.md")),
                           ("evening_analysis", os.path.join(ac.reports_dir(home), "%s.analysis.md" % day))):
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().strip()
            except OSError:
                continue
            if text:
                out.append({"date": day, "kind": kind, "text": text})
    return out


# ---------------------------------------------------------------- building and writing

def build(home, settings, cfg):
    """{table: rows} plus "notes" and "meta"."""
    arch = research.Archive(home)
    days = list(arch.days5)
    ctx = Context(home, days)
    lives = {}
    for day in sorted(set(days) | set(journal.report_days(home))):
        L = live_day(home, day, cfg, settings)
        if L:
            lives[day] = L
    signals = signal_rows(arch, cfg, ctx, lives, days)
    by_key = {(s["date"], s["symbol"]): s for s in signals}
    trades, accts = trade_rows(lives)
    out = {"signals": signals, "decisions": decision_rows(lives, by_key), "trades": trades, "account_days": accts,
           "days": day_rows(ctx, signals, lives, days), "vetoes": veto_rows(arch, lives, by_key),
           "deep_calls": deep_rows(arch, lives, by_key), "notes": note_rows(home, lives)}
    out["meta"] = {"version": VERSION, "built_at": nc.utc_iso(nc.utc_now()), "days": len(days),
                   "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
                   "live_days": sorted(lives), "days_1m": len(arch.days1),
                   "journal_errors": {d: L["journal"]["error"] for d, L in lives.items()
                                      if (L.get("journal") or {}).get("error")},
                   "rows": {k: len(v) for k, v in out.items() if isinstance(v, list)}}
    return out


def folder_of(settings, home):
    d = (settings.get("dataset") or {}).get("dir")
    return os.path.expanduser(d) if d else os.path.join(home, "dataset")


def readme(meta):
    L = ["# NSE autopilot dataset", "",
         "Built %s from the trader folder's own records: %d trading days (%s to %s), %d with 1-minute bars; the "
         "autopilot ran on %s. Paper trading only. Rebuilt in full after each evening's report, so never edit these "
         "files by hand: add notes elsewhere." % (
             meta["built_at"], meta["days"], meta["first_day"], meta["last_day"], meta["days_1m"],
             ", ".join(meta["live_days"]) or "no days yet"), "",
         "Rows: " + ", ".join("%s %d" % (k, v) for k, v in meta["rows"].items()) + ".", "",
         "## How to read it", "",
         "- **R** is net rupees after Zerodha's charges over the plan's max loss (the loss if the stop filled at its "
         "worst price). A normal stop-out is about -0.5R and the 2R target about +1.1R, as in every report.",
         "- **Replayed, not traded:** every `label_*` column replays the signal with the live rules (a LIMIT a "
         "minute after the breakout bar, filled only on a trade through it, cancelled after 15 minutes; stop at the "
         "range's middle, 2R target, out at 15:10) on Yahoo's bars at the ₹10 lakh account's size. Days before 22 "
         "Oct 2026 only have 5-minute bars (`label_bars` = 5m): coarser, read them apart. The `live_*` columns are "
         "what really happened, on the days the autopilot ran.",
         "- **Features vs labels:** the feature columns were known when the signal fired. The label columns are only "
         "known afterwards. A model must never be given a label (or anything from later that day) as an input.",
         "- **One market:** every row so far comes from a few months of one market regime. Treat anything found here "
         "as a lead to test on more days.", ""]
    for name, cols in TABLES:
        L += ["## %s.csv" % name, ""]
        for kind in (ID, FEATURE, LIVE, LABEL):
            mine = [(c, d) for c, k, d in cols if k == kind]
            if not mine:
                continue
            L += ["**%s**" % KIND_WORDS[kind], "", "| Column | Meaning |", "|---|---|"]
            L += ["| `%s` | %s |" % (c, d or "") for c, d in mine]
            L.append("")
    L += ["## notes.jsonl", "", "One JSON object a line: `date`, `kind` (news_check, deep_morning_note, "
          "evening_analysis) and `text`, Claude's own words that day.", ""]
    return "\n".join(L)


def write(folder, data):
    os.makedirs(folder, exist_ok=True)
    for name, cols in TABLES:
        path = os.path.join(folder, name + ".csv")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([c for c, _, _ in cols])
            for r in data[name]:
                w.writerow([cell(r.get(c)) for c, _, _ in cols])
        os.replace(tmp, path)
    path = os.path.join(folder, "notes.jsonl")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        for n in data["notes"]:
            fh.write(json.dumps(n, ensure_ascii=False) + "\n")
    os.replace(path + ".tmp", path)
    ac.write_json(os.path.join(folder, "meta.json"), data["meta"])
    with open(os.path.join(folder, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(readme(data["meta"]))


def write_all(home, settings, cfg):
    """(folder, meta) after a rebuild."""
    data = build(home, settings, cfg)
    folder = folder_of(settings, home)
    write(folder, data)
    return folder, data["meta"]


def main(argv=None):
    nc.utf8_stdout()
    ap = argparse.ArgumentParser(description="Rebuild the autopilot's dataset (CSV tables) from its records.")
    ap.add_argument("--check", action="store_true", help="build it and print the row counts; write nothing")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    home = ac.trader_home()
    settings, cfg = ac.load_settings(home), ac.load_cfg(home)
    if a.check:
        data = build(home, settings, cfg)
        print(json.dumps(data["meta"], indent=1))
        return 0
    folder, meta = write_all(home, settings, cfg)
    print("dataset: %s (%s)" % (folder, ", ".join("%s %d" % kv for kv in meta["rows"].items())))
    if meta["journal_errors"]:
        print("journal rows missing for: %s" % meta["journal_errors"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
