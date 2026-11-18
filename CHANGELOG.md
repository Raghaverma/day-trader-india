# Changelog

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
