---
name: nse-scanner
description: >-
  Chart Scanner for NSE. Scans hundreds of charts for setups: opening-range
  breakouts from 09:15 IST, bull flags, 20-day breakouts, oversold bounces.
  Builds the universe from NSE's archives (Nifty 50, Nifty 500, F&O), drops
  what is not fit for intraday (sec_list.csv), applies rupee liquidity floors,
  writes trader/watchlist.json for nse-news and nse-entry. Use when
  the user says "scan the market", "scan Nifty 50", "find me setups on NSE",
  "what is breaking out", "ORB scan", "oversold stocks India", "pre-market
  scan", "build my watchlist", or at the start of the day before /nse-news.
argument-hint: "[universe [nifty50|nifty500|fno|all] | daily | orb] [--universe NAME] [--at HH:MM]"
---

# nse-scanner

The Chart Scanner is the first step of an NSE day trade. It reads free charts
and lists the ones that match a rule. That is all it does. A scan is a list of
charts that match a rule, not a list of stocks to buy, and a setup on a chart
is not a prediction. Most people who day trade lose money; the scanner does
not change that. It only saves you from flipping through 500 charts by hand.

Not financial advice. This skill never places an order. Nothing in this pack
is placed without the user's explicit **yes** in the chat for that one order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                        # Nifty 500, screened -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe fno                    # or nifty50, all
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                           # before 09:15 IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --universe nifty50        # or --symbols RELIANCE,M&M,BAJAJ-AUTO
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --universe nifty50          # after 09:35 IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:30                  # cut the clock at 10:30 IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--universe nifty50|nifty500|fno|all|FILE` (default nifty500),
`--symbols`, `--watchlist FILE`, `--limit N`, `--min-price` (₹20) and
`--min-value-cr` (daily ₹10 crore, orb 0), `--or-minutes 15`,
`--max-confirm 200` (orb stage 2), `--until HH:MM` (orb, default config
`no_entries_after`, 14:30), `--now "2026-11-13 10:05"` (IST) to pin the
clock, `--offline PATH` to read saved Yahoo files, `--lists DIR` or
`--sec-list FILE` to read saved NSE files, `--refresh` to download the NSE
files again, `--no-write`, `--show N`, `--json`.

Exit codes: **0** the scan found something. **1** it ran and found nothing
("No setups"), or could not start (a bad flag, no list; the reason is one plain
line). **3** the scan FAILED to read prices: more than half of the symbols (or,
in the orb scan's stage 2, of the candidate charts) could not be fetched. That
is not "no setups". It prints `scan failed: could not read prices (N of M
symbols failed)` with the first few reasons, **leaves the existing
`trader/watchlist.json` exactly as it was and writes nothing**. Tell the user
Yahoo could not be read and to try again in a few minutes; never describe an
exit 3 as an empty market. (Before this, a Yahoo outage overwrote the watchlist
with `hits: []` and exited 1, the same as a genuine empty scan.)

Every file lives in the trader folder, the same one for all the skills: the
`NSE_TRADER_HOME` environment variable if set, else the nearest `trader/` folder
with a `config.json` above where Claude Code was started, else
`~/.day-trader-india`, else `./trader`. Each run ends with a line saying which
`config.json` it used, or that none was found at that path and the built-in
defaults are in use (this skill does not guard money, so it runs without one).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/cache/` | NSE's list files and sec_list.csv for the day, named by date (`sec_list-2026-11-14.csv`, `EQUITY_L-2026-11-14.csv`); nse-news shares the same `EQUITY_L-<date>.csv` |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line; `--universe trader/universe.txt` reads it back |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `errors` (symbols that could not be read) with up to five reasons in `error_samples`, `dropped`, `hits[]`. nse-news, nse-entry and nse-deals read `hits[].symbol` (bare NSE symbols: `RELIANCE`, `M&M`). A hit also carries `name` when the company name is known (nse-news searches the news by it) and, on a `breakout_20d` hit, `level` (below). A hit whose liquidity could not be checked carries `"value_checked": false` |
| `trader/config.json` | `allowed_series` (EQ), `min_circuit_band_pct` (10), `block_surveillance` (true), `no_entries_after` (14:30), `special_sessions`, `extra_holidays` |
| `trader/state/nse-holidays.json` | holiday years beyond 2026, written by nse-alerts |

## The universe (NSE archives)

From `nsearchives.nseindia.com/content/`: `indices/ind_nifty50list.csv`,
`indices/ind_nifty500list.csv`, `fo/fo_mktlots.csv` (the stock rows only:
the first rows are indices such as NIFTY and BANKNIFTY, skipped) and
`equities/EQUITY_L.csv` (series EQ). Then every list goes through
`equities/sec_list.csv`, NSE's daily list of tradable securities:

1. Out: not in sec_list at all (suspended, or a placeholder like DUMMYHEG).
2. Out: no row in an allowed series (`allowed_series`, EQ). BE and BZ are
   trade-for-trade: every trade settles by delivery, so no intraday. SM, ST
   and SZ are SME, RR a REIT, IV an InvIT.
3. Out: a 2% or 5% circuit band (under `min_circuit_band_pct`, 10). Such
   stocks are thin and can lock at the limit. "No Band" (F&O stocks with
   dynamic bands) passes.
4. Out: graded surveillance, a Remarks like "GSM STAGE - 0"
   (`block_surveillance`, true).

It always says how many it dropped and why. On the 13 Nov 2026 files the Nifty
500 has 501 rows and 494 survive: 3 REITs (series RR), HFCL and STLTECH
(series BE), VISL (5% band) and DUMMYHEG (not in sec_list). The F&O list has
218 stocks, all kept. Every EQ stock: 2,391, of which 121 sit in a 5% band
and 26 are under GSM, so 2,244 are kept.

## The daily scan (before 09:15 IST)

Yahoo's spark endpoint returns daily closes for 20 symbols a request, so the
Nifty 500 takes 25 requests. Liquidity, in rupees: the last close at least
`--min-price` (₹20), and the traded value at least `--min-value-cr` crore
(₹10 crore). The traded value is measured on COMPLETE sessions only. Spark
has no volume history, so the traded value is the last session's price times
volume from the spark meta (with a v8 chart that has daily volumes, the average
of the last 20 complete sessions). Before 09:15, after 15:30 and on a day the
NSE is closed that meta volume is the last complete session's, so run the
daily scan then: today's daily bar is left out until 15:30 IST. While the
session is running the meta volume is only today's partial count, and a liquid
stock would fail the floor early in the day. So during the session the floor is
checked on each hit's own daily chart (one extra request per hit, complete
sessions only) and the output says so; a hit whose chart cannot be read is kept
with `"value_checked": false`, never dropped for a partial volume.

`name` and `level`. Every hit carries `name`, the company name, when it is
known: from `EQUITY_L.csv` when the scanner has it (the `--lists` folder, or
today's `cache/EQUITY_L-<date>.csv`; it is never downloaded just for this),
else the universe list's own name, else sec_list's. A `breakout_20d` hit also
carries `level`, the prior 20-day closing high (the close the last close just
beat, rounded to the paisa, a number). nse-entry reads it:
`entry.py watchlist --setup level` watches each hit for a first close above its
own `level`. Hits of the other setups have no `level`; nse-entry says so for
that row and suggests `--level PRICE` or `--setup orb`.

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (rupees traded).

## The opening-range breakout scan (during the session)

NSE's normal market runs 09:15-15:30 IST; the pre-open call auction runs
09:00-09:15 and makes no 5-minute bars. The opening range is the first
`--or-minutes` (15): the 09:15, 09:20 and 09:25 bars. A hit is the first
completed 5-minute bar to CLOSE above the range high, at that bar's close
time. The first bar that can trigger is the 09:30 bar, complete at 09:35.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on if a completed bar after the range closed at or
   above the highest close of the range (a necessary condition), or if spark
   dropped some of its bars. Spark does that on busy NSE charts (ITC lost 9 of
   75 bars on 13 Nov 2026), and a dropped bar is not a halt.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each, at most `--max-confirm` (200), the most traded first. It
   prints PARTIAL with the count it skipped.

A chart is usable only when every 5-minute bar from 09:15 to the cutoff is
there; a missing bar means a halt or no trades. The exception is the last
bars of the session: Yahoo leaves the 15:20 bar (and often 15:15) empty on
every Nifty 50 chart, so a gap from 15:15 on is let through. Null closes and
off-grid ticks are dropped, and a bar counts once it is complete. On a
weekend or an NSE holiday, in the pre-open, before 09:35, after the close, or
after `no_entries_after` (14:30, an expired setup) it prints one plain line
and writes nothing. Muhurat trading and other special sessions come from
config `special_sessions`.

## Example: Friday 13 Nov 2026 (real data)

The Nifty 50 at 15:30 IST, replayed from the saved 5-minute charts:

```
python3 scan.py orb --offline wall5m-nifty50-5d-2026-11-13.json.gz --lists tests/fixtures/nse-scanner \
    --universe nifty50 --at 15:30 --until 15:30
Universe: Nifty 50, 50 symbols. sec_list.csv: none of 50 dropped (series EQ, bands 10%+ or No Band, no GSM).
Fetched 50 of 50 charts; 50 usable; 42 candidates, 42 confirmed with real highs and lows.
34 of 50 charts closed above their opening-range high by 15:30 IST:
  09:35  BAJFINANCE    ₹1,107.10  range 1,091.75 to 1,105.75  +0.12% over
  ...
  10:05  RELIANCE      ₹1,334.00  range 1,324.30 to 1,333.40  +0.04% over
  ...
  10:50  AXISBANK      ₹1,334.70  range 1,326.00 to 1,333.80  +0.07% over
```

All 34 came before 14:30, so the scan at the default cutoff finds the same
34; at 10:05 it finds 31. RELIANCE's range is the 09:15 to 09:25 bars, high
1,333.40 and low 1,324.30; the 10:00 bar closed at ₹1,334.00, the first close
above the high, at 10:05. The user picks RELIANCE and Claude hands it to
`/nse-news RELIANCE`, then `/nse-entry RELIANCE`.

The daily scan for Monday 16 Nov (closes through Friday) tags 16 of the
Nifty 50: BAJFINANCE, BSE and MAXHEALTH a 20-day breakout and a bull flag,
13 more (RELIANCE, LT, MARUTI, ...) a bull flag, after the rally into Diwali.

## Data and limits

- Yahoo's chart and spark endpoints (`RELIANCE.NS`, `M%26M.NS`) are free,
  unofficial and can lag. Bars are 5-minute bars, bar by bar. A 429 or 5xx
  goes to the other host, with one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 50 of 80 charts (30 failed or
  had no data)", and `errors` in the watchlist says how many. Never round a
  partial scan up to "the whole market". More than half unread is not partial,
  it is a failed scan (exit 3, nothing written).
- NSE's 2026 holidays are built in (nse_common). For another year the scan
  says the list is not loaded and treats weekdays as trading days; nse-alerts
  `holidays --refresh` fetches it.

## Steps

1. Once a day or so: `scan.py universe` (or let `daily` and `orb` fetch the
   list themselves; they share the cache).
2. Before 09:15 IST: `scan.py daily`. Tell the user the counts (listed,
   dropped by sec_list and why, fetched, liquid, scanned, hits) and the top
   few by setup.
3. After 09:35 IST (10:00 to 10:30 is common): `scan.py orb`. Tell the user
   how many usable charts broke their range and when, and that a later hit is
   closer to the price now. Do not scan the Nifty 500 intraday again and again;
   scan the daily watchlist or the Nifty 50.
4. The user picks one chart. Hand it on: `/nse-news SYMBOL` (what is moving
   it), then `/nse-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Rules

- Say what was scanned: which list, how many symbols, how many sec_list
  dropped and why, how many charts came back, how many were usable, and the
  time of the data in IST.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L or a return figure. Money is in rupees.
- A hit is stale once the price has moved on. Re-run before acting on it.
- Long side, cash equity (series EQ), intraday MIS only. No F&O, no shorts. `orb --side short` (or `both`)
  also lists opening-range breakdowns, for research and the paper autopilot's shadow-only short side; the
  order path stays long-only, so never offer a breakdown as a trade.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls the data live or exchange-grade, and never promises that a setup
works.

## Safety

- Education and tooling, not financial advice. A PASS from nse-risk is a rules check, not a recommendation.
- Paper trading by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk passed that
  exact order today, the user typed an explicit **yes** for that one order in the chat, and the user clicked
  Allow once on Claude's own card for the Kite order tool.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the MCP server's own config, never in the chat.
