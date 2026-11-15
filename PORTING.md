# Porting notes: day-trader-skill (US) → day-trader-india (NSE)

This repo ports [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT) from US stocks to the Indian cash market (NSE). The original is installed read-only at
`~/.claude/plugins/marketplaces/day-trader-skill`. Read it for structure, CLI shape, voice
and file formats. **Never modify it.**

Facts below were checked live on 14 Nov 2026 (a Saturday; the last session was Fri 13 Nov 2026).

## Ground rules for every skill

- Standard-library Python, 3.8-compatible syntax, run as `python3 script.py ...`.
- Every skill folder has an identical `nse_common.py`. Import it like this:
  ```python
  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
  import nse_common as nc
  ```
  **Do not edit `nse_common.py`.** Put skill-specific helpers in your own script. If you
  think `nse_common.py` has a bug or is missing something many skills need, say so in your
  final report.
- It gives you: the IST clock (`parse_now`, `to_ist`, `ist_from_epoch`, `market_status`,
  `trading_day`, `session_times`, `load_holidays`, `next_trading_day`), ticks (`tick_size`,
  `round_to_tick`, `on_tick`), money (`inr`, `crore`, `lakh`), symbols (`nse_symbol`,
  `is_nse_symbol`, `yahoo_symbol`, `yahoo_chart_url`, `yahoo_spark_url`, `nse_headers`),
  charges (`intraday_charges`), orders (`KITE_FIELDS`, `order_fingerprint`, `ORDER_TAG`),
  files (`read_json`, `write_json`, `load_config`, `DEFAULT_CONFIG`), `UA` and `utf8_stdout`.
  Read the file. It's short.
- Call `nc.utf8_stdout()` at the start of `main()` (output contains ₹).
- **Times are IST.** `--now "2026-11-13 10:05"` pins the clock in IST (`nc.parse_now`).
  Internally keep naive UTC like the original, or naive IST. Just be consistent.
- **Money is rupees** via `nc.inr` (Indian grouping). Never print `$` or say "dollar".
  Config keys end in `_inr`, not `_usd`.
- **Prices sit on NSE ticks** (`nc.tick_size(price, symbol, cfg)`). Stops round **down**, and
  limit buys and targets round to the tick as the original rounds to the cent.
- Long side only, cash equity (series EQ), intraday product MIS. No F&O, no shorting, no
  margin unless the config turns it on.
- Network: use `nc.UA` as User-Agent (never put a name, email or repo URL in requests).
  NSE `/api/` calls use `nc.nse_headers()`. Use timeouts and short polite sleeps between
  calls. A network failure is a plain one-line message and a non-zero exit, never a
  traceback. Cache bulky daily files (symbol lists, sec_list) under `trader/cache/` for the day.
- Keep the original's CLI shape, flags, exit codes and voice: plain words, short sentences,
  one honest verdict. Drop US-only features (SEC, PDT guard, Stooq, Alpaca, fractional
  shares, 13F weights).
- **Tests:** `tests/test_<name>.py`, `unittest`, fully offline, using real fixtures in
  `tests/fixtures/<skill-folder>/`. Use `from _load import load, FIX` and
  `mod = load("nse-entry", "entry")`. Port the original's tests where they still apply and
  add tests for every India rule. Run only your own files, for example
  `cd tests && python3 -m unittest test_entry test_scanner`. You may capture extra real
  fixtures, but keep them small and gzip anything over ~100 KB with `mtime=0`.
- **SKILL.md** for each folder: frontmatter `name: <folder>`, a `description: >-` over 200
  characters with India-specific trigger phrases (NSE, Nifty, ₹, Zerodha/Kite where
  relevant), and `argument-hint`. Then the body: what it does, the commands using
  `${CLAUDE_SKILL_DIR}`, what it reads and writes, the India rules, and the handoff to the
  next skill. Skills on the order path (scanner, entry, exit, timemachine, budget, deals,
  risk, broker, alerts) must say "Not financial advice" and carry the rule that nothing is
  placed without the user's explicit **yes**. Each skill body should be more than 60 lines.
- Stay in your own folders and test files. Don't touch README.md, `.claude-plugin/`,
  `tests/test_repo.py`, `tests/test_common.py`, `tests/_load.py` or other agents' folders.
  Don't make git commits.

## Skill map

| original | India folder | script | notes |
| --- | --- | --- | --- |
| trader-scanner | nse-scanner | scan.py | NSE universes, ORB 09:15-09:30 |
| trader-news | nse-news | news.py | NSE announcements + Google News + board-meeting calendar |
| trader-entry | nse-entry | entry.py | NSE session, ORB from 09:15 |
| trader-exit | nse-exit | exit.py | tick rounding, charges-aware R |
| trader-timemachine | nse-timemachine | timemachine.py | 75 five-minute bars a session |
| trader-risk | nse-risk | check.py | Kite order legs, series/band/GSM rules, charges |
| trader-budget | nse-budget | size.py, config.example.json | Kite-shaped orders.json, charges in sizing; drop weights mode |
| trader-broker | nse-broker | ticket.py, kite-mcp.example.json | paper (default) / manual / kite modes |
| trader-alerts | nse-alerts | alerts.py, alerts.example.json | NSE clock, holiday refresh, Yahoo .NS quotes |
| trader-analyst | nse-analyst | results.py | NSE integrated-filing XBRL results |
| trader-pelosi | nse-deals | deals.py | **new**: NSE bulk & block deals, follow named clients |

## India market facts (checked 14 Nov 2026)

- **Session:** pre-open 09:00-09:15 IST (order entry 09:00-09:08), normal market
  09:15-15:30 IST. IST is UTC+05:30 all year. Yahoo 5-minute bars for a full session:
  75 bars, stamped 09:15 … 15:25.
- **Holidays:** `nc.NSE_HOLIDAYS` holds the 2026 CM list. NSE publishes the next year's list
  around December. When a date's year isn't known (`nc.load_holidays` returns known
  years), say so plainly and treat weekdays as trading days. Special sessions such as
  Muhurat trading come from config `special_sessions`.
- **Opening range:** the first 15 minutes = the 09:15, 09:20 and 09:25 five-minute bars. The
  first bar that can trigger is the 09:30 bar (complete at 09:35). Defaults:
  `no_entries_before` 09:30, `no_entries_after` 14:30, `flat_by` 15:10.
- **Broker square-off:** Zerodha auto-squares MIS equity at 15:25 (Z-Connect, 26 Dec 2025)
  and charges ₹50 + 18% GST per order. Hence flat_by 15:10. Other brokers differ, so it's
  configurable.
- **Ticks (cash market, since 15 Apr 2025, reviewed monthly):** < ₹250 → 0.01;
  ₹250-1,000 → 0.05; ₹1,000-5,000 → 0.10; ₹5,000-10,000 → 0.50; ₹10,000-20,000 → 1.00;
  > ₹20,000 → 5.00. See `nc.tick_size`.
- **Charges (Zerodha equity intraday):** brokerage 0.03% or ₹20 per executed order
  (lower); STT 0.025% sell side; NSE transaction 0.00307%; SEBI ₹10/crore; IPFT
  ₹0.01/crore; stamp 0.003% buy side; GST 18% on brokerage + transaction + SEBI + IPFT.
  See `nc.intraday_charges`. Example: 56 RELIANCE bought at 1,334.00 and sold at 1,344.40
  costs ₹73.87 in charges.
- **Series:** EQ = normal (intraday allowed). BE/BZ = trade-for-trade: every trade must
  settle by delivery, so **no intraday**. SM/ST = SME. Only `allowed_series` (default EQ)
  is tradable.
- **Circuit bands** (`sec_list.csv`, column Band): 2, 5, 10, 20 (%), or "No Band" (F&O
  stocks with dynamic bands, which are fine). Stocks in 2% and 5% bands are thin and can
  lock at the limit. Config `min_circuit_band_pct` (default 10) excludes them, and "No Band"
  passes. Remarks like "GSM STAGE - 0" mean graded surveillance. Config
  `block_surveillance` (default true) excludes them.
- **No pattern-day-trader rule** in India. Drop `pdt_guard`.
- **Kite (Zerodha) API:** no bracket orders and **no OCO for MIS**. A resting target LIMIT
  plus a resting stop can both fill and leave you short. So the order is an entry leg plus a
  stop leg placed after the entry fills, and the target is an alert. When it rings, cancel
  the stop, then sell. The hosted Kite MCP (`https://mcp.kite.trade/mcp`) excludes order
  placement except GTT. Placing orders needs a self-hosted
  [zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server) with your own Kite
  Connect key (the Personal plan is free for orders, but has no market data). Since 1 April
  2026, API orders must come from a **static IP whitelisted** with Zerodha. Tools:
  `place_order`, `modify_order`, `cancel_order`, `get_orders`, `get_positions`,
  `get_ltp`/`get_quotes` (paid plan), `place_gtt_order`, ….

## Data sources (checked live)

All of these worked with `nc.UA` and no cookies:

- **Yahoo chart v8:** `nc.yahoo_chart_url(sym, "60d", "5m")` gives 4,500 bars (60 sessions);
  1-minute data goes back 7 days. `meta.exchangeName` "NSI", `gmtoffset` 19800,
  currency INR. The first bar of a session can have volume 0.
- **Yahoo spark v7:** `nc.yahoo_spark_url(syms, "3mo", "1d")`, at most 20 symbols, closes only.
- **Yahoo RSS headlines are EMPTY for .NS. Don't use them.**
- **Yahoo fundamentals-timeseries:**
  `https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/RELIANCE.NS?type=quarterlyTotalRevenue,...&period1=..&period2=..`
  is consolidated and in INR, but has gaps (RELIANCE skips 2025-09-30). Use it as a fallback only.
- **NSE archives** (`https://nsearchives.nseindia.com/content/`):
  `equities/EQUITY_L.csv` (every listed equity: SYMBOL, NAME OF COMPANY, " SERIES", …,
  headers have leading spaces); `indices/ind_nifty50list.csv`, `indices/ind_nifty500list.csv`
  (Company Name, Industry, Symbol, Series, ISIN Code); `fo/fo_mktlots.csv` (F&O underlyings,
  padded columns, the first rows are indices); `equities/sec_list.csv` (Symbol, Series,
  Security Name, Band, Remarks); `equities/bulk.csv` and `equities/block.csv` (the latest
  day's deals: Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded,
  Trade Price / Wght. Avg. Price[, Remarks]).
- **NSE API** (`https://www.nseindia.com/api/...` with `nc.nse_headers()`; if it ever answers
  401/403, GET `https://www.nseindia.com/` with a cookie jar first, then retry):
  - `holiday-master?type=trading` → `{"CM": [{"tradingDate": "15-Jan-2026", "description": …}], …}`
    (`nc.parse_holiday_master`)
  - `marketStatus`
  - `corporate-announcements?index=equities&symbol=RELIANCE` → list of
    `{an_dt: "13-Nov-2026 18:25:01", desc, attchmntText, attchmntFile, sm_name, symbol, …}`.
    It's the full history (3 MB), so filter by date.
  - `event-calendar?index=equities` → upcoming board meetings
    `[{symbol, company, purpose, bm_desc, date: "16-Nov-2026"}]`
  - `integrated-filing-results?index=equities&symbol=RELIANCE&type=Integrated%20Filing-%20Financials`
    → `{"data": [{qe_Date: "30-SEP-2026", consolidated: "Consolidated"|"Standalone", audited,
    broadcast_Date, xbrl, ixbrl, …}]}` covering about 7 quarters from Mar 2025.
  - The XBRL (`xbrl` URL, about 100 KB consolidated) has context `OneD` = the quarter and
    `FourD` = year to date. Tags (namespace `in-capmkt`): RevenueFromOperations, Income,
    Expenses, FinanceCosts, DepreciationDepletionAndAmortisationExpense, ProfitBeforeTax,
    ProfitLossForPeriod, ProfitOrLossAttributableToOwnersOfParent,
    DilutedEarningsLossPerShareFromContinuingOperations. Bank filings
    (`INTEGRATED_FILING_BANKING_…`) use different tags (InterestEarned, Income, …).
  - **Stale, don't use:** `results-comparision` (stops Dec 2024) and
    `corporates-financial-results` (stops Jan 2025). `corporates-pit` returns empty.
    `historical/bulk-deals` answered 503.
- **Google News RSS:**
  `https://news.google.com/rss/search?q=%22Reliance+Industries%22+when:2d&hl=en-IN&gl=IN&ceid=IN:en`
  works. The feed is for personal, non-commercial use, which this is.
- **BSE** announcements (`api.bseindia.com`, Referer `https://www.bseindia.com/`) work but need
  BSE scrip codes. Optional.

## Fixtures already captured (real data, Fri 13 Nov 2026)

`tests/fixtures/`:
- `nse-scanner/`: ind_nifty50list.csv, ind_nifty500list.csv, EQUITY_L.csv.gz,
  sec_list.csv.gz, fo_mktlots.csv, daily-spark-nifty50-2026-11-13.json (3mo daily spark,
  all 50), wall5m-nifty50-5d-2026-11-13.json.gz (`{SYMBOL: v8 chart result}`, 5 sessions
  of 5m to 13 Nov).
- `nse-entry/`: RELIANCE-2026-11-13-5m.json, RELIANCE-2026-11-13-1m.json (v8 chart
  docs trimmed to 13 Nov).
- `nse-timemachine/`: RELIANCE-60d5m.json.gz (v8 chart doc, 4,500 bars).
- `nse-alerts/`: holiday-master.json, yahoo-{RELIANCE,TCS,INFY,HDFCBANK}.json (1d 5m).
- `nse-news/`: nse-announcements-{RELIANCE,TCS}.json (since 15 Oct 2026),
  event-calendar.json, gnews-{RELIANCE,TCS}.xml, chart-RELIANCE.json (5d 1d).
- `nse-analyst/`: integrated-filing-{RELIANCE,HDFCBANK}.json,
  xbrl-RELIANCE-30-SEP-2026-consolidated.xml.gz, xbrl-HDFCBANK-30-SEP-2026-consolidated.xml.gz,
  yahoo-timeseries-{RELIANCE,TCS}.json, plus stale results-comparision and
  financial-results files (only to show they're stale).
- `nse-deals/`: bulk-2026-11-13.csv, block-2026-11-13.csv.

## The worked example: RELIANCE, Friday 13 Nov 2026 (real data)

On 13 Nov, 34 of the Nifty 50 closed a 5-minute bar above their 15-minute opening range.
RELIANCE: the opening range (09:15-09:30) ran from 1,324.30 to 1,333.40. The 10:00 bar closed
at 1,334.00, the first close above the high, so it triggered at 10:05. Later that day the
stock traded up to 1,350.50 and closed at 1,339.70. The tick at this price is ₹0.10, so the
opening-range-midpoint stop (1,328.85) rounds down to 1,328.80. The tests should reproduce
these numbers from the fixtures.

## Shared files under `trader/` (contracts between skills)

Keep the original's shapes unless this section says otherwise. When your skill reads a file
another skill writes, read the original writer's code for the shape. Symbols are bare NSE
symbols (`RELIANCE`, never `RELIANCE.NS`). Times are IST strings "HH:MM", dates ISO.

| file | written by | read by |
| --- | --- | --- |
| `config.json` | the user (nse-budget `config.example.json`) | all; keys = `nc.DEFAULT_CONFIG` |
| `watchlist.json` | nse-scanner | nse-news, nse-entry, nse-deals |
| `entry.json` | nse-entry | nse-exit |
| `plan.json` | nse-exit | nse-timemachine, nse-budget, nse-alerts |
| `orders.json` | nse-budget (`flatten` in nse-broker replaces it) | nse-risk, nse-broker |
| `state/risk.json` | nse-risk | nse-broker |
| `state/broker.json` | nse-broker | nse-risk (today's trades, realised losses) |
| `portfolio.json` | nse-broker (`fill`, paper fills) | nse-budget, nse-risk, nse-deals |
| `prices.json` | nse-alerts | nse-budget, nse-risk |
| `state/nse-holidays.json` | nse-alerts `holidays --refresh` | all (via `nc.load_holidays`) |

`plan.json` keeps the original fields and adds `"exchange": "NSE"` and `"tick": "0.10"` (string).
`flat_by` defaults to "15:10".

### `orders.json` (new shape, written by nse-budget)

```json
{
  "created_by": "nse-budget",
  "created_at": "2026-11-13T04:35:00Z",
  "status": "proposed",
  "broker": "paper",
  "note": "Proposals only. Nothing is placed until nse-risk passes each order and you say yes to it in the chat.",
  "source": "trader/plan.json",
  "orders": [
    {
      "symbol": "RELIANCE",
      "entry": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                "transaction_type": "BUY", "quantity": 56, "product": "MIS",
                "order_type": "LIMIT", "price": 1334.0, "validity": "DAY", "tag": "dtindia"},
      "stop":  {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                "transaction_type": "SELL", "quantity": 56, "product": "MIS",
                "order_type": "SL", "trigger_price": 1328.8, "price": 1322.2,
                "validity": "DAY", "tag": "dtindia"},
      "target": {"price": 1344.4,
                 "how": "an alert, not a resting order: Kite has no OCO for MIS. When it rings, cancel the stop, then sell."},
      "flat_by": "15:10",
      "why": "Opening-range breakout on RELIANCE: 56 shares, bound by ...",
      "plan": {"entry": 1334.0, "stop": 1328.8, "target": 1344.4, "risk_per_share": 5.2,
               "max_loss": 291.2, "charges_at_stop": 61.9, "max_loss_with_charges": 353.1,
               "charges_at_target": 73.87, "setup": "orb", "entry_time": "10:05",
               "flat_by": "15:10", "tick": "0.10"}
    }
  ]
}
```

(The numbers are illustrative. Compute the real ones.) Rules:
- Entry `order_type` is "LIMIT" at the plan's entry, or "MARKET" only if the user asked
  for a market order (budget's `--market`). Prices are numbers on the tick grid.
- The stop leg is `order_type` "SL" with `trigger_price` = plan stop and `price` = the
  trigger minus `stop_limit_buffer_pct` (default 0.5%), rounded **down** to the tick. If
  config `stop_order_type` is "SL-M", there's no `price` and the report warns that market
  orders by API may need market protection. SL can fail to fill in a gap. Say so once in
  the report, not as a scare.
- `nc.order_fingerprint(order)` hashes the entry and stop legs. nse-risk records verdicts
  under it, and nse-broker only shows an order whose fingerprint has a PASS from today.
- `max_loss` is quantity × (entry − stop trigger). `max_loss_with_charges` adds
  `nc.intraday_charges(qty, entry, stop)["total"]`. When `include_charges` is true, sizing
  and the risk check use the version with charges.

### nse-broker modes (config `broker`)

- `paper` (default, no broker at all): after the user's yes, `ticket.py paper-fill --index N`
  fills the entry at the latest Yahoo price if a LIMIT buy is marketable (price ≤ limit,
  filled at that price). Otherwise it says it isn't marketable and leaves it open.
  `ticket.py paper-check` walks the 1-minute bars since the fill (a fixture or `--bars`
  file for tests): the stop is hit when a bar's low ≤ trigger (fill at the trigger, or at
  the bar's open if it gapped below), the target when a bar's high ≥ target, and flat_by
  closes at that bar's close. It records P&L net of `nc.intraday_charges`, updates
  portfolio.json and broker.json, and keeps everything in `trader/state/paper.json`.
- `manual`: prints exactly what to type into Kite (BUY, product MIS (Intraday), LIMIT,
  qty, price; then once filled the SELL SL order with trigger and price). `result` and
  `fill` record what happened.
- `kite`: Claude calls the self-hosted Kite MCP `place_order` with the entry leg JSON after
  the yes and the Allow-once card, then places the stop leg after the fill. The target is
  an alert: cancel the stop, then sell. Setup notes cover the static IP and "Needs approval".
- `flatten` builds the SELL MIS order that closes a position by flat_by, after cancelling
  the stop.

### nse-risk rules (on top of the original's, adapted)

Stop required. Loss at the stop (+ charges) ≤ `max_loss_per_trade_inr`. Reward:risk after
charges ≥ `min_reward_risk`. The day's risk fits `max_daily_loss_inr`, plus trades per day,
orders per day and duplicates. Entry window (IST) and market open per `nc.market_status`;
an unknown holiday year is a warning, not a block. Symbol is valid NSE; series is in
`allowed_series`; band ≥ `min_circuit_band_pct` or "No Band"; GSM/ASM blocked when
`block_surveillance`. Exchange NSE, product MIS, no F&O symbols, no SELL entries unless
`allow_short`. Order value ≤ cash after the buffer unless `allow_margin`. Prices on the
tick. The stop leg is a SELL, same quantity, trigger < entry, limit ≤ trigger. Blocked
tickers come from `blocked`. Getting out is never blocked by a day limit. `sec_list.csv`
comes from NSE archives, cached daily, with `--sec-list FILE` for tests.
