# Changelog

## 1.19.0

### The dataset, and why each trade was made in the progress email

- **`autopilot/dataset.py`**: the trader folder's records as CSV tables in `<trader folder>/dataset/` (or
  `"dataset": {"dir"}`), rebuilt in full after the evening's journal with `"dataset": {"enabled": true}`, never
  failing the day; `run_day.py dataset [--check]` by hand. `signals.csv`: every long ORB signal on every archived
  day (64 days, 1,750 signals at first build), its features at the signal, the live autopilot's decision and
  context on the days it ran (outcome, veto, deep rank and the company desk's call, Time Machine odds, the real
  trade), and its replay with the live rules as labels (result, R, best and worst while held, R under the breakeven,
  time and trail exits). Also `decisions.csv` (each account's decision on each signal), `trades.csv` (every paper
  order with the journal's reasons and steps), `account_days.csv`, `days.csv`, `vetoes.csv`, `deep_calls.csv` (the
  deep research's call on every stock and what it did) and `notes.jsonl` (the news check's summary, the morning
  note, the evening analysis). The folder's README lists every column as feature, live or label, generated from
  the same list that writes the files. `research.orb_signals` keeps the scanner's hit on each signal.
- **The progress email says why.** Under each trade: why it was bought (the breakout bar, % over the range, the
  bar's and the opening volume, not vetoed), a note when the breakout was weak, the plan (stop at the range's middle
  and the risk a share, the 2R target, 15:10, the size and what set it, the trade slot), the Time Machine's odds
  and, once closed or cancelled, the journal's own reason, worded for a session still running; an open trade shows
  now, best and worst in R.
- **Fixes.** An order the paper broker cancelled after `entry_ttl_minutes` gives its trade slot back in the day
  page's steps ("5th of 5", not "6th of 5" after a cancelled order; `journal.slot_numbers`). An unfilled order's
  "What happened" is no longer empty: the order and when it was cancelled.

## 1.17.0

### The deep pre-market: a research desk before the open, live on the paper accounts

- **`autopilot/deepscan.py`**, its own launchd job (`com.nse-autopilot-deep`, `run_day.py deep`), never holding the
  tick's lock. Phase 1 from 03:30 IST gathers the world overnight (US, Europe and Asia indices, US futures, VIX,
  Treasury yields, the dollar, rupee, yen and yuan, Brent, WTI, gas, gold, silver, copper, bitcoin, EM and India
  ETFs, the Indian ADRs, US peers of each sector), the whole Nifty 500 (breadth, sectors, leaders, laggards,
  52-week highs and lows, volume surges), NSE's FII/DII cash flows and F&O participant open interest, the bulk and
  block deals, India and world macro headlines, and for every Nifty 50 stock its NSE stock file, a year of daily
  bars, the last session's 5-minute bars, Yahoo's fundamentals (4 years, 5 quarters, valuation), 30 days of
  headlines and every past ORB signal on it. Then the desks: macro, one analyst per Nifty 50 company, a devil's
  advocate on every sit-out and every high-conviction call, a head per sector. Phase 2 at 07:45 refreshes the world
  and the headlines and the CIO (Opus) writes the morning note, the sit-outs (at most 8) and a 0-1 rank per stock.
- **Live** (`"deep": {"apply": true}`): run_day.py's pre-market adds the CIO's sit-outs to Claude's veto list with
  `"source": "deep"`; if the deep pre-market finishes after that, it adds them to the day's `vetoes.json` itself,
  so every later signal sees them. Vetoed signals say which check vetoed them. The ranks go to
  `<trader folder>/deep-ranks.json` (the `rank_file`), which only tags signals "top half" / "bottom half".
- **The morning note** as Markdown, a PDF in the P&L journal folder (`<date> pre-market research.pdf`) and an email.
- **The report** gets a "Deep pre-market research" section: each deep sit-out with what the stock did (replayed),
  the favoured stocks' signals, the calls, tokens and estimated cost. Claude's end-of-day analysis sees it.
- **Limits:** `"max_tokens"` (6,000,000) stops new calls once used; the first usage-limit error stops the day's
  remaining calls; every desk's answer is saved as it lands, so a crash or a reboot carries on. Claude gets no
  tools, as before. Off unless `"deep": {"enabled": true}`.

## 1.16.0

### The journal as PDFs, with the strategy and why each trade was made

- **PDFs at the top of the journal folder.** With `"journal": {"pdf": true}`, each day's page and the summary are
  printed to `<date>.pdf` and `Summary.pdf` next to the CSVs (the Markdown stays in `days/`). `autopilot/pdf.py`
  turns the journal's Markdown into one styled HTML page (gains in green, losses in red) and prints it to A4 with
  macOS's own WebKit through `autopilot/html2pdf.swift`, compiled once into `<trader folder>/tools/html2pdf` (Xcode
  command-line tools). A page is printed again only when it changed; the first failure stops the rest and is
  reported in the journal's line, never failing the day. Nothing to install from outside macOS.
- **The strategy, every day.** Each day's page starts with the setup and its rules that day: the opening-range
  breakout, the signal, the plan (stop at the range's middle, 2R target, flat by 15:10, the LIMIT's expiry), the
  checks in order (Claude's veto, the cost filter, an account's own filter, the trade cap, the daily loss limit),
  the sizing limits, nse-risk and the Time Machine; and each account's cap, loss limit and filter.
- **Before the open.** The market's mood and Claude's pre-market news check: what it vetoed and its own reasoning.
- **Why each trade was made.** Every trade gets the autopilot's own steps, from what it recorded at the time: the
  stock file's flags and the veto, the signal (the bar, the range, how far over, the volume, a daily-chart setup),
  nse-exit's plan with its charges and break-even, the checks it passed (cost, opening volume, the slot), the Time
  Machine's odds and past sessions, nse-budget's size and the limit that set it, nse-risk's verdict and the order.
  Then "What happened" and "Why it made or lost money", as before.
- **The report rules** now also record the entry expiry, the cash buffer, the position and order caps and the cost
  filter, so later days' strategy text is exact.
- **Fix:** a signal nse-budget could not size is recorded with its own sentence ("one share of HDFCBANK costs
  ₹830.15, more than your cash limit...") instead of the tail of its JSON.
- 8 new tests.

## 1.15.0

### The main account can carry the opening-volume filter, and the journal covers every account

- **A filter follows its number of trades a day.** `accounts.filters` is keyed by cap (`"cap5"`), and when the main
  account's own `max_trades_per_day` is that cap, the filter is the main account's. `run_day` applies it after the
  signal has gone to the other paper accounts, so they still see every signal; a signal it keeps out is finished as
  `filtered` and replayed with the rest. It is read even with the extra accounts switched off.
- **Report.** The rules line names the filter; the main account's filter table adds what each kept-out signal made
  in its replay ("If taken") and the total; the edge table gets its own row for filtered signals; the sizes and
  "whole principal" replays leave filtered signals out (they follow today's rules) while the trade-cap replay keeps
  them (it has no filter). "Three rules each stop the day at about three entries" now reads "Three rules limit the
  day's entries", since the cap is no longer 3. The progress email names the new outcome.
- **The P&L journal covers every paper account.** Each day's page has the accounts side by side, then each
  account's trades with the story and the reason, what its trade cap kept out and what its filter kept out, each
  replayed. A trade another account made the same way (same order, fill and exit) is told once and referred to
  after that. The summary puts the accounts side by side, all days and day by day (bold: the main account that
  day); a row follows one set of rules wherever it ran, so "5 a day + volume filter" continues from its own account
  folder to the main account. The ledger and the daily CSV get `account` and `main` columns; the daily CSV has one
  row per account per day with that account's running total.
- **An unfilled order cancelled after `entry_ttl_minutes`** is now written up as freeing its slot (true since
  1.12.0), not as blocking a later signal all day; its "never came back" low is taken over the order's own minutes.
- 7 new tests.

## 1.14.0

### The opening-volume filter, tried on the 5-a-day paper account

- **A filter per account.** `"accounts": {"filters": {"cap5": {"min_open_rvol": 1.5}}}` in autopilot.json gives one
  paper account a rule the others do not have. The 5-a-day account now takes a signal only when its opening volume
  (09:20-09:30) is at least 1.5x its median over the last 20 archived days, the one lead in `research.py`; a signal
  whose opening volume is not known is left out, as the research does. A filtered signal never reaches the
  account's sizing, quote or risk check. The main account (3 a day) and the 8- and 10-a-day accounts are unchanged.
- **One number per signal.** The opening volume is worked out once when the signal is first seen and kept on it
  (`open_rvol`), so the tag, every account and the report use the same figure.
- **Report.** The account's label says it is filtered; a table lists each signal that reached it with its opening
  volume and whether the filter let it through or kept it out. The trade-cap replay's 5-a-day row is the same
  account without the filter. The progress email's account lines widen to fit the longer label.
- **What the archive expects** (research rows at 5 filled a day, cash and the daily loss rule not modelled): 1-minute,
  20 days: +0.19R a trade, +0.80R a day, interval above zero, against +0.10R and +0.51R without it; 5-minute, 61
  days: +0.04R a trade against -0.02R, interval spans zero.

## 1.13.0

### Research on the archive: which signals, which exits, which setups

- **`autopilot/research.py`.** Every long ORB signal on the archived days, with what was known when it fired:
  opening volume (09:20-09:30, since Yahoo's 09:15 bar has none) over its 20-day median, the share of the 14-day
  average range the 2R target needs from the day's low, the move against the Nifty since the previous close, the
  sector (NSE's Nifty 50 list) and a previous close at a 20-day high. Each is replayed with the live execution
  rules (the order a minute after the breakout bar, a trade-through fill, a 15-minute expiry).
- **Which signals.** At most 3 filled a day (an unfilled order frees its slot): the first 3 in time order (now),
  opening volume of 1.5x or more, the target within the stock's typical day, stronger than the Nifty, all three, a
  rank within each scan batch, and the rank with one stock per sector; and the R by thirds of each feature.
- **Which exits.** The same fills under the plan (stop, 2R alert, flat by 15:10), a breakeven stop (entry plus the
  round trip's charges) after +1R, a time stop (out if not +0.5R in 45 minutes) and a trail (no target; after +1R
  the stop follows the 5-bar low).
- **Which setups.** A VWAP pullback after a strong open (up 0.8% by 09:45 on 1.5x opening volume; a LIMIT at the
  VWAP until 12:00, stop under the day's low) and an afternoon breakout (a 12:30-13:30 range within half the
  14-day range, then the first close above it by 14:15). A news gap-and-go needs past news the archive lacks.
- **Measured live.** Every signal gets an `open_volume` tag, so the scorecard's edge-by-tag table follows the
  opening-volume filter on new days (`market.opening_rvol`, one Yahoo request a signal). Nothing traded changes.
- Stray `trader/` folders the plugin's hooks create inside the repo are ignored. 10 new tests.

## 1.12.0

### Replays that place the order when the live account would

- **The report's replays** place a signal's LIMIT at the minute the autopilot first saw it (its `first_seen`), as
  the live account would have, instead of at the breakout bar's close: the old replay filled nearly every breakout
  at its close, including the runaway winners a live order arrives too late for (19 Nov: M&M).
- **`study.py --latency N`** (default 1: a 09:35 breakout is seen at 09:36, as live) and **`--entry-bps N`** (the
  LIMIT N bps above the entry, paid to be filled on a breakout that runs; stop, target, quantity and R are the
  plan's). `--latency 0` is the old replay.
- **`study.py --compare`** replays the long signals under six execution assumptions (the old replay; a minute later
  on a touch; on a trade-through; with the 15-minute expiry now live; a LIMIT 5 and 10 bps above) and prints the
  share filled, R per filled trade, R per signal counting the unfilled as 0, and the per-day verdict.
- 4 new tests.

## 1.11.0

### Execution closer to a real broker

- **Entries expire** (`"entry_ttl_minutes"` in config.json, off when unset). The paper broker cancels a LIMIT buy
  that has not filled that many minutes after the order, with the reason in the log, so a breakout that ran away
  (19 Nov: M&M) never fills on a pullback long after (19 Nov: INDIGO, a late fill that stopped out).
- **Fills on a trade-through** (`"paper_fill": "through"`, default `"touch"`). A resting LIMIT buy fills only when
  a bar trades at least one tick below it: a touch at the limit can leave a real order unfilled behind the queue.
  A LIMIT that is marketable when it is placed still fills at once.
- **A cancelled entry frees its slot.** nse-risk, the autopilot and each paper account no longer count an entry the
  paper broker cancelled unfilled against the day's trade cap or daily loss (19 Nov: M&M held a slot all day).
- **Waiting entries hold their cash.** nse-budget and nse-risk subtract today's paper LIMIT buys still waiting to
  fill from the cash, so signals in the same minute are sized one after another on what is left (19 Nov: three
  orders came to ₹11.08 lakh against ₹9 lakh of usable cash).
- **The simulators agree.** `sizes.simulate` (account sizes, what-ifs, trade caps) holds a waiting order's slot,
  loss and cash until it expires, and the report's replays use the paper broker's walk with the same config.
- **Claude's vetoes by category.** The pre-market prompt asks for RESULTS, CORPORATE_ACTION, NEGATIVE_NEWS,
  REGULATORY_OR_BAN or OTHER with each veto; the report shows it, and the scorecard keeps each category's replayed
  net and average R over all days, so a category that vetoes winners shows up.
- **News searches for confusable names.** BSE, ITC, TRENT, TITAN, BEL, HAL, LT and M&M get a spelled-out Google News
  search (`news.QUERY_OVERRIDES`), so the exchange BSE or a footballer named Trent stop showing up as company news.
- 11 new tests.

## 1.10.0

### Paper accounts at 5, 8 and 10 trades a day

- **`autopilot/accounts.py`.** With `"accounts": {"enabled": true, "caps": [5, 8, 10]}` each cap gets a paper account
  of its own in `<trader folder>/accounts/cap<N>/`, a trader folder like the main one: the main config.json with
  that many trades a day and a daily loss limit of that many times the max loss a trade (rewritten from the main
  config each pre-market), its own `portfolio.json` cash starting at the budget, its own broker, paper and risk
  state, and a link to the main cache. A cap equal to the main account's is the main account.
- **They trade for real, on paper.** Every long signal the main account plans, after Claude's veto and the cost
  filter, goes to each account: nse-budget on its cash, a fresh quote, nse-risk on its rules, autopilot-approve
  and the paper broker. Each account's open trades get the same paper-check every minute and at the close. An
  account whose config is not paper with `"autopilot": true` is left out; one account failing never touches the
  others or the main one.
- **Side by side.** The 15:35 report has a section with every account's orders, fills, wins, losses, net, charges,
  return and cash, and every trade each took; the scorecard keeps every account over all days; the half-hourly
  progress email and `run_day.py status` list each account's trades used, open and closed, and net.
- 6 new tests.

## 1.9.0

### 3, 5, 8 and 10 trades a day

- **`whatif.trade_caps`.** The same signals at several numbers of trades a day, at one budget, each with a daily
  loss limit of that many times the max loss a trade (at ₹10 lakh: ₹15,000, ₹25,000, ₹40,000 and ₹50,000), so the
  last trade is never kept out by the loss rule's reservation. Everything else is the paper account's: the max
  loss a trade, the cost filter, Claude's veto and the cash with no margin.
- **In the report** (`"trade_caps": {"enabled": true, "caps": [3, 5, 8, 10], "budget": null}`): a section with each
  cap's trades, net, return, average R, the most money in stock at once and what was kept out, every trade each
  took, and an all-days table in the scorecard. Measurement only: the paper account keeps config.json's cap.
- **`study.py --caps [3,5,8,10] [--budget RUPEES]`** replays the bar archive at each cap and writes every trade to
  `reports/caps-study.csv` and `.md` (`caps-study-5m.*` with `--bars 5m`).
- 6 new tests.

## 1.8.0

### The deep scan, why only three trades, and the whole principal

- **`nse-news/dossier.py`: everything public on one stock.** Price and trend from a year of Yahoo daily bars, the
  series and circuit band, NSE board meetings and corporate actions, the latest quarters from `nse-analyst`, 30
  days of NSE filings, a week of headlines that name the stock, the shareholding pattern, pledges, insider (SEBI
  PIT) trades over 90 days, bulk and block deals and the F&O ban list. It starts with plain-words flags (a board
  meeting or ex-date close, the F&O ban, fresh results, a big move or volume, a 52-week extreme, insider or
  promoter selling, a pledge, a bulk deal, important filings). A failed source is named, never shown as "none".
  `--out DIR` writes `<SYM>.md`, `<SYM>.json` and `index.json`, each as soon as it is built.
- **The deep scan before the open** (`"dossier"` in autopilot.json). After the news, the pre-market builds a file
  for every stock in the universe (about 6 minutes for the Nifty 50). It stops starting new stocks at
  `budget_s` or 09:10, whichever is sooner, and is skipped with under 2 minutes left, so a tick never runs into
  the session. Claude's veto gets each stock's flags (`stock_flags`), and the prompt says which flags are
  reasons. The report has a "Stock files" section, the Health list has the scan, and the traded and vetoed
  stocks' files are attached to the email as `<date>.stocks.md`.
- **`autopilot/whatif.py`: why only N trades.** The report names the three limits that each stop the day at
  about three entries (the trade cap, the daily loss limit's reservation of each entry's whole max loss, and the
  cash), with the day's numbers, then lifts them one at a time on the day's own signals: trades, net, return and
  the most money in stock at once.
- **The whole principal.** The same signals with every trade taking all the free money, with the money alone
  and with 5x intraday margin (`"whatif": {"margin": 5}`): the net, the worst trade, the largest position, its
  loss at the stop and what a 10% or 20% fall in it would cost. The scorecard keeps every way over all days.
- **What it trades.** Every report says it in one line, from the config: shares in NSE's cash market, intraday,
  long only, no futures or options, no margin.
- `sizes.simulate` takes `margin` and `overrides` (unchanged when left out). 26 new tests.

## 1.7.0

### The P&L journal: every trade, what it made or lost, and why

- **`autopilot/journal.py`.** After the day's report, the email and the bar archive, the end of day rebuilds a
  journal folder (`"journal": {"enabled": true, "dir": "~/Desktop/NSE Trading P&L"}` in autopilot.json):
  - `Summary.md`: running totals, the reasons trades won or lost, the signals not taken, and each day
  - `P&L ledger.csv`: one row per paper order across all days
  - `Daily P&L.csv`: one row per day, with the running total
  - `days/<date>.md`: each trade's story and the day's analysis
  - `My notes.md`: never touched again
- **The reason is computed, not guessed.** For each order it reads the 1-minute bars and puts it in one
  category: target hit, gave back a winner (+1R or more before the stop), late fill on a pullback (the LIMIT
  filled 15+ minutes after the order, after the breakout had run without it), failed breakout (never +0.5R),
  squared off at 15:10, or never filled because the price ran away (with what it would have made). It also
  records:
  - the Nifty's move during the trade
  - what the stock did after the stop
  - how weak the breakout was
  - the Time Machine's odds
- **Day-level reasons.** When the trade cap was used up, and on which signals. What the signals the cap kept
  out would have made. How LIMIT orders at the breakout price treated the runners and the failures.
- **Rebuilt each time.** The journal is rebuilt from the trader folder's own records, so it can be deleted
  and rebuilt with `run_day.py journal`. If macOS refuses the folder, it goes to `<trader folder>/journal`
  and says so. A journal failure never fails the end of day.
- **Tests.** 11 new.

## 1.6.0

### A progress email every half hour

- **`autopilot/progress.py`.** A second LaunchAgent, `com.nse-autopilot-progress`, checks every minute and
  emails the same recipients:
  - at 08:42: it's a trading day and the autopilot will run (or it's off, and why), and the first tick ran
  - at :00 and :30 from 09:00 to 15:30: what it's doing now, every paper trade, money made and lost (after
    charges, realised and open at Yahoo's last price), the trade cap and loss limit used, each signal and why
    it was or wasn't traded, the pre-market veto, and any failed step
  - on a closed day: one email saying why it isn't running
  - at 16:00: only if the 15:35 report's email didn't go out, the day's numbers anyway
- **A stuck tick shows.** The job only reads the day's files and never takes the tick's lock. A tick that
  hasn't run for 5 minutes, or a launchd job that isn't loaded or a keep-awake that isn't running, is flagged
  with `!!` in the subject.
- **Each slot goes out once.** Slots are recorded in `autopilot/<date>/progress.json`. A failed send is retried
  on the next two ticks, and a slot more than 20 minutes late (the Mac slept) is skipped.
- **Settings.** `"progress_mail": {"enabled", "every_min", "to", "grace_min", "wrapup_at"}` in autopilot.json,
  all optional. `run_day.py progress --show` prints the email without sending it; `--send-now` sends one.
- `install.sh` installs both jobs and `uninstall.sh` removes both.
- **Tests.** 13 new.

## 1.5.0

### Slippage, days as the sample, and a 5-minute replay

Nothing here changes what is traded. It changes how the results are measured.

- **Slippage.** Every paper fill is exact: a LIMIT fills on a touch and a stop at its trigger. Each signal now
  carries `bp_r`, the R that one basis point of slippage on each side costs it. `report.slippage` gives:
  - the edge at 0, 1, 2, 3 and 5 bps a side, per signal and per day
  - the trades and days it would take to tell
  - the break-even slippage

  It is in the daily report, the scorecard, Claude's EOD data, `study.py` and each budget in `sizes.simulate`.
  Live paper trades use the journal's planned max loss (now carried in the report's trades).
- **Per-day statistics lead.** Signals on one day move together, so each edge group in the report and the
  scorecard adds `per_day` (each day's average R, bootstrapped over days). The verdict column shown is the
  per-day one. The tag splits (the market conditions) get the same.
- **`study.py --bars 5m`.** Yahoo keeps 60 days of 5-minute bars but only 30 of 1-minute bars. The study can
  now replay on 5-minute bars, which covers every archived day. The bar length is a new `step` argument to
  `ticket.walk`, `ticket.done_bars` and `report.shadow`. It defaults to 1, so the paper broker is unchanged. On
  5-minute bars, the target's market sell waits for the next 5-minute open. `--sizes` writes
  `sizes-study-5m.*` and keeps the 1-minute files.
- **`study.py --calibrate`.** On days with both bar sizes, it compares the 5-minute replay with the 1-minute
  one, signal by signal. On the first 19 archived days (1,102 signals), 99% of results agree, and the
  5-minute replay is 0.003R a signal harsher (95% −0.009 to +0.004).
- **Tests.** 12 new.

## 1.4.0

### The same signals at other account sizes

- **`autopilot/sizes.py`.** `sizes.simulate` re-sizes each day's long signals for other budgets, and each budget
  gets its own rules:
  - rupee limits scaled with the budget (every percentage the same)
  - `nse_common.size_qty` with the cash that open positions leave free
  - the cost filter on that size's own charges (brokerage's ₹20 cap makes big orders pay a smaller share of
    their risk)
  - the trade cap, and nse-risk's daily loss rule
  Fills and exits are the paper broker's. Market impact is not modelled.
- **Report and scorecard** (`"sizes": [...]` in autopilot.json; the example lists ₹1 lakh, ₹10 lakh, ₹50 lakh,
  ₹1 crore and ₹5 crore). Each day's report has a table per budget and every trade at every budget. The scorecard
  keeps each budget's running net and its per-day 95% interval.
- **`study.py --sizes`.** Runs the archive through the same rules at each budget and writes every trade to
  `reports/sizes-study.csv` and `.md`, with each order's share of its entry bar's volume.
- **Tests.** 10 new. At its own budget, the re-sizing reproduces the live paper trade to the share and the paisa.

## 1.3.0

### The autopilot measures what would make it better

Only the cost filter changes what is traded. Everything else records and replays. Each is off unless
`autopilot.json` turns it on, and `autopilot.example.json` turns them all on.

- **Cost filter** (`cost_filter.max_charges_r`). A long signal whose round-trip charges at the stop exceed this
  share of its risk is skipped (outcome `cost_filter`) and replayed. Measured on 5 fixture days, the median ORB
  signal pays 0.16R in charges. 0.1R would skip about 90% of signals; 0.2R skips the worst third.
- **Short side, shadow only** (`shorts: "shadow"`).
  - `nse-scanner orb --side short|both` finds breakdowns. Long stays the default, and its output is unchanged.
  - `autopilot/shorts.py` plans each breakdown as the mirror of nse-exit's ORB plan, sized by
    `nse_common.size_qty(long=False)`.
  - `report.shadow` replays a short on mirrored bars, so `ticket.walk`'s rules apply unchanged.
  - Nothing short is approved, sized by nse-budget, checked by nse-risk or paper-filled.
- **Tags** (`tags`, `rank_file`). Each signal records:
  - the Nifty vs its 50-day average
  - India VIX's one-year band
  - the Nifty's move so far that day
  - a rank from a file, when there is one
  The scorecard splits results by each tag.
- **Edge statistics.** `report.edge_stats` gives the average R, a 95% bootstrap interval, t, and the trades
  needed. It reports by group (traded, cost-filtered, other skipped signals, shorts) and by tag, in the report,
  the scorecard and Claude's EOD prompt.
- **Bar archive** (`archive.enabled`). The day's 1- and 5-minute bars go to `archive/<interval>/<date>/`, and
  a day is only ever replaced by a fuller copy. `run_day.py archive --backfill` saves what Yahoo still has.
- **`study.py`.** Replays every archived ORB signal, long and short, and gives the verdict per signal and per
  day. On an archive cut from the 13 November fixtures, it reproduces the README's RELIANCE trade.
- **Tests.** 27 new offline tests, covering:
  - the scanner's short side
  - mirrored replays: target, stop, a gap through the SL limit, an unfilled sell limit
  - the cost filter on the README trade
  - shorts never reaching the order path
  - the tags, with no network in tests
  - the edge verdicts and the rank file
  - the archive
  - the study

## 1.2.0

### Autopilot: paper trading with nobody at the keyboard (new `autopilot/`)

- `run_day.py tick`, run by a launchd LaunchAgent every minute on an always-on Mac. One idempotent tick at a
  time, with all state on disk (`$NSE_TRADER_HOME/autopilot/<date>/`). It covers:
  - the pre-market (Nifty 50, daily scan, news, Claude's veto)
  - the opening-range scans every 5 minutes, with each signal chained through nse-entry, nse-exit,
    nse-timemachine, nse-budget, nse-alerts, nse-risk and the paper broker
  - `paper-check` every minute
  - the end of day at 15:35, which runs late if the Mac was off. A crash, a sleep or a reboot only means the
    next tick carries on.
- **Paper only, twice.** The runner refuses a config that is not `"broker": "paper"` with `"autopilot": true`.
  The new `ticket.py autopilot-approve` checks both again. It needs the same READY ticket as `approve` (a fresh
  nse-risk PASS for that exact order, today's order, the entry window). It records `approved_by: "autopilot"`
  with no reply, and refuses in manual and kite mode with no override. `journal` and `log` say who approved
  each trade.
- **`report.py`.** The day's trades, every signal from scan to exit, and the Time Machine's forecasts against
  outcomes (Brier score, calibration buckets, by verdict). Signals not taken (vetoed, capped, blocked) are
  replayed with the paper broker's own rules (`ticket.walk`). Also execution, health (failed steps, missed
  minutes) and the Nifty 50's move. It writes JSON, Markdown, HTML and a running `scorecard.json`. Every number
  is computed in Python.
- **`claude_run.py`.** Headless `claude -p` on the logged-in account, with `--tools ""`, no MCP servers and no
  settings: Claude only answers, and the runner saves the answer. Two runs a day: the pre-market veto (JSON)
  and the EOD analysis (Markdown). If either fails, the day goes on.
- **`mail.py`.** Gmail over SMTP with an app password read from the macOS login keychain.
- **`install.sh` and `uninstall.sh`.** `install.sh` writes the paper config and settings, stores the
  Keychain entry, installs the LaunchAgent and runs a selftest inside launchd.
- **Tests.** About 30 new offline tests. A replay of 13 November 2026 through the real scripts reproduces the
  README's RELIANCE trade: 37 shares, the target at 1,344.30, +₹328.40 net.

## 1.1.0

A review of 1.0.0 found places where a trade could slip past the safety rules. This release
fixes them. `nse_common.py` goes to 1.1.0 and carries the shared rules, so every skill sizes,
rounds and measures loss the same way.

### Orders and approval (nse-broker, nse-risk, hooks)

- A risk PASS now expires: it is good for `risk_pass_minutes` (5) and only on the day it was
  written. `show`, `approve` and `paper-fill` re-check the clock, the entry window and flat-by.
  A PASS stamped in the future is not a PASS.
- Yesterday's orders no longer pass today: nse-risk blocks a plan from another day or an
  `orders.json` older than `order_max_age_minutes` (120), and the ticket prints the order's age.
- Every entry needs a fresh quote (under `price_fresh_minutes`, 5). An entry is blocked when there
  is none or the price is already at or through the stop. LIMIT entries are checked, not only MARKET.
- The stop leg, the flatten quantity and portfolio.json follow the quantity that actually filled
  after a partial fill.
- **Kite mode gate.** A plugin PreToolUse hook denies any Kite `place_order`, `modify_order` or
  `cancel_order` call that does not match an approved, unused, unexpired leg, and each leg works once.
  GTT is always denied. `approve --reply` must equal your last recorded message, and kite mode refuses
  to approve when that record is missing (the hooks are not installed). The hook only denies, so
  "Needs approval" still asks. `--now` and `--today` on nse-broker need `NSE_TEST_MODE=1`.
- The yes parser is an allowlist: "yes for TCS", "yes buy INFY" and a bare "y" no longer count.
- Outside paper mode, flatten takes its quantity from the broker (`--positions FILE` or `--qty`),
  and `reconcile` compares portfolio.json with Kite's positions.
- Flatten replaces only its own symbol's order.
- The paper broker is no longer generous: the target fills at the next bar's open, and a gap through the
  SL limit leaves the stop unfilled until price returns or the flat-by time. `paper-check` re-reads the
  last bar and never acts on an unfinished one.
- New: `ticket.py status` (entries with no stop, approvals nobody placed; also run at session start),
  `move-stop` (raise a stop, to break-even or a price), partial `flatten --qty`, and `journal`
  (per-trade R-multiples, slippage, day and all-days totals).

### Money math (nse_common, nse-budget, nse-exit, nse-timemachine, nse-risk)

- The max loss per trade is measured at the SL limit (the worst fill), not the trigger. The RELIANCE
  example now sizes to 37 shares, not 56. `"loss_basis": "trigger"` restores the old measure.
  Reward:risk still gates at the stop's trigger; the worst-fill ratio is printed next to it.
- Order prices use the coarsest NSE tick within `tick_band_pct` (10) of the price, so a price near
  a slab edge is valid whichever tick NSE has set. `tick_sizes` still pins a stock.
- One sizing rule: nse-exit, nse-timemachine, nse-budget and nse-risk share `nc.size_limits`, and the
  position cap and cash buffer use the same base, the smaller of your budget and the real account.
- nse-budget `--append` keeps other symbols' orders; `--init-config` creates the config.
- nse-exit refuses an entry trigger older than `trigger_max_age_minutes` (30) unless `--entry` or
  `--allow-late`; plan.json now carries `quantity` and the worst-fill risk.

### State, config and holidays

- One trader folder from anywhere (`$NSE_TRADER_HOME`, the nearest `trader/` with a config, or
  `~/.day-trader-india`). nse-risk, nse-budget and nse-broker refuse to run without `config.json`
  instead of falling back to defaults, so daily limits cannot silently reset.
- NSE's holiday list refreshes itself when the year is unknown or the cache is over 30 days old;
  nse-risk blocks new entries if the year is still unknown.

### Data and signals

- nse-alerts rings a target or stop that was touched between polls, from the five-minute bar's high or low.
- nse-entry waits for a complete opening range and stamps `triggered_at`; the scanner writes `level` and `name`,
  and a scan that could not read most of its symbols fails (exit 3) instead of writing an empty watchlist.
  The scanner's liquidity floor no longer uses a half-finished session's volume.
- nse-news, nse-analyst and nse-deals no longer turn a failed fetch into "nothing new": failures are named
  and exit non-zero, block pages are never cached as data, and news looks back to its last full check.

### Docs and tests

- SKILL.md descriptions are under 600 characters. The README says what is enforced in each broker mode.
- About 800 offline tests (408 before), including regression tests that fail on the 1.0.0 code.

### Not changed

- Charges rates: re-checked against zerodha.com/charges (NSE 0.00307%, IPFT ₹0.01/crore); the code was right.
- No `/loop` detection for the gate (Claude Code gives hooks no such signal), no gating of Bash/curl calls to
  the Kite API, longs only, and no timemachine verdict gate: these are limits, not fixed.
