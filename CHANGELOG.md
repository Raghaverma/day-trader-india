# Changelog

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
