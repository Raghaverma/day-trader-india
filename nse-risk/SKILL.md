---
name: nse-risk
description: >-
  Risk Manager for NSE intraday orders. Checks every Kite-shaped order against
  your limits and blocks anything over before it reaches Zerodha or the paper
  broker: a stop, a fresh quote, today's order, loss at the worst stop fill
  plus charges within your ₹ max loss, reward:risk, daily loss, trades per day,
  IST entry window. Series EQ only (BE/BZ blocked), circuit bands, GSM/ASM, no
  F&O or shorts. Exits never blocked. Use when the user says "check my order",
  "is this safe", "risk check", "does this break my rules", "can I take this
  trade", "how much have I risked today", or before /nse-broker.
argument-hint: "[path to orders.json]"
---

# nse-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by nse-budget or by nse-broker's flatten passes through it. A BLOCK is
final for that order: it does not go to the user for a yes, and it does not go
to Kite or the paper broker. Most people who day trade lose money. These
limits keep each loss and each day's losses small. They do not make a trade
win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # the trader folder's files; writes state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-13 10:05"  # pin the check clock (IST), for a what-if
python3 "${CLAUDE_SKILL_DIR}/check.py" --sec-list sec_list.csv   # NSE's security list from a file (.csv or .csv.gz)
```

Exit code 0 when every order passes, 1 when any is blocked or on an error
(one plain line, never a traceback), 2 when there is no `config.json`: your
limits live there and the tool will not guess them, so it stops before it
reads or writes anything. Set them with nse-budget first.

Every file lives in the one trader folder: the `NSE_TRADER_HOME` folder if set, else the
nearest `trader/` (with a `config.json`) at or above where Claude Code was
started, else `~/.day-trader-india`. The error names the path it looked at.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (nse-budget, or nse-broker's flatten), with `created_at` and each order's `plan.date` |
| `trader/config.json` | the limits (nse-budget writes it and ships `config.example.json`); required |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | the quotes nse-alerts writes: every entry needs a fresh one for its symbol; also values holdings |
| `trader/state/broker.json` | what was placed today, and closed trades' real P&L |
| `trader/state/nse-holidays.json` | NSE's holiday list, refreshed from NSE when the year is unknown or the copy is old |
| `trader/cache/sec_list-YYYY-MM-DD.csv` | NSE's security list, downloaded once a day |
| `trader/state/risk.json` | the verdicts it writes, each under the order's fingerprint, with its `symbol` and `checked_at` |

NSE's security list comes from
`https://nsearchives.nseindia.com/content/equities/sec_list.csv` (Symbol,
Series, Security Name, Band, Remarks) and is cached for the day. If today's
download fails, it uses the newest cached copy and says from when. With no
copy at all, entries are blocked: series, band and surveillance can't be
checked. Exits still pass.

## Entries and exits

An **entry** opens or adds to a position: a BUY, or a SELL that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. The rules that still make sense for it (series, band, surveillance,
blocked list, size, duplicates, daily order limit) turn into warnings, never
a BLOCK. The entry-only rules (age, quote, loss, reward:risk, daily loss,
trades, entry window, holidays) are not applied to it at all: no quote and
no fresh order file are needed to get out.

## The checks

Orders are checked in file order, as if each passed order had filled.

| check | blocks when |
| --- | --- |
| format | no entry leg; variety not `regular`; quantity not a whole number; order_type not MARKET/LIMIT/SL/SL-M; validity not DAY/IOC; a LIMIT with no price, a MARKET with one, an SL without trigger and price |
| exchange, product | not NSE; not MIS (CNC is delivery, NRML is F&O) |
| tick | an entry price, a trigger or an SL limit off the order tick: the coarsest NSE tick within `tick_band_pct` (10%) of the price, because NSE sets each stock's tick from an earlier close (997.95 is on the 0.05 grid and off the 0.10 one, so it is refused; use 998.00). An off-tick target only warns: it is an alert |
| asset | F&O contracts (NFO/BFO, `...FUT`, `...CE`/`...PE`) while `allow_fno` is false; anything that is not an NSE stock symbol |
| short | a SELL of more than you hold (`allow_short` false) |
| stale | (entries) `plan.date` (IST) is not today; or `orders.json` (its `created_at`, or the order's own) is older than `order_max_age_minutes` (120). It says how old |
| quote | (entries) no quote for the symbol in `prices.json` within `price_fresh_minutes` (5), or one with no time; or the fresh price is already at or below the stop trigger (at or above it for a short). A price more than 0.5R above the entry only warns |
| security | (entries) not in sec_list; series not in `allowed_series` (BE/BZ: trade-for-trade, delivery only, no intraday; SM/ST: SME); band under `min_circuit_band_pct` ("No Band" passes: F&O stocks have dynamic bands); `GSM`/`ASM` remarks while `block_surveillance` |
| blocked, size | (entries) on `blocked`; under `min_order_inr` or over `max_order_inr` |
| position, cash | (entries) over `max_position_pct` of the account base; under the cash buffer (`cash_buffer_pct` of the same base), or margin while `allow_margin` is false |
| stop | (entries) no stop leg while `require_stop`; a stop leg that is not a SELL of the same quantity and product, not SL/SL-M, a trigger not under the entry, an SL limit above its trigger |
| loss | (entries) qty x (entry minus the worst exit), plus the estimated charges when `include_charges`, over `max_loss_per_trade_inr`. The worst exit is the SL limit (`loss_basis` "limit", the default); `"trigger"` measures at the trigger instead |
| reward:risk | (entries) after charges, (qty x reward − charges at the target) / (qty x risk to the trigger + charges at the trigger), under `min_reward_risk`. The reward:risk if the stop fills at its limit is shown next to it |
| daily loss | (entries) today's entries at their max loss (or their real net loss from the log, if worse) plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after` (IST), or the NSE is closed: weekend, NSE holiday, outside 09:15-15:30 |
| holidays | (entries) NSE's holiday list for this year is still unknown after a refresh attempt |
| duplicate, daily limit | (entries) the same symbol and side twice in a batch or already placed today; `max_orders_per_day` orders placed |

How the numbers work:

- **The entry it measures from**: the LIMIT price. For a MARKET order, the
  HIGHER of the plan's entry and the fresh quote.
- **The quote** must be struck within `price_fresh_minutes` of the check (the
  time of the last trade in `trader/prices.json`). A LIMIT order is measured
  from its limit, so without the quote a limit at 1,334 would pass while the
  stock already trades under its stop. With none, the BLOCK says exactly what
  to run: nse-alerts `alerts.py quote RELIANCE --write`.
- **The worst exit.** The stop leg is an SL order: it fills no worse than its
  limit (the trigger less `stop_limit_buffer_pct`, rounded onto the tick), so
  that is where the max loss is measured. The same number is in the loss
  reason, in `max_loss` in the result and in the day's risk total. A stop leg
  someone edited to a lower limit is measured at its own limit.
- **Charges** are Zerodha's intraday rates from `nse_common.intraday_charges`
  (override in config `charges`).
- **The account base** for the position cap and the cash buffer is
  `nse_common.account_base`: the smaller of your budget and the account's real
  value (cash plus holdings). It is the same number nse-budget sizes with, so
  budget does not propose an order that risk then blocks, and a losing day
  shrinks the next order.
- **How old an order may be.** An order planned on an earlier day, or from an
  `orders.json` written more than `order_max_age_minutes` ago, is not
  traded: the price has moved. Plan it again.
- **The window** is IST, worked out in the tool, never from the computer's
  clock zone. If NSE's holiday list for the year isn't loaded, check.py asks NSE
  for it (best effort) and says so; if it is still unknown, new entries are
  blocked until you run nse-alerts `alerts.py holidays --refresh` or put the
  year's holidays in `extra_holidays` and the year in `holiday_years`. A
  special session in the config (Muhurat trading) opens a closed day.
- **How long a PASS lasts.** Each result in `state/risk.json` carries its
  `checked_at` (UTC) and `symbol`. nse-broker accepts a PASS only for
  `risk_pass_minutes` (5) after it, and only on the day it was written; after
  that, run the check again.
- There is no pattern-day-trader rule in India, so there is no `pdt_guard`.

## Steps

1. Refresh the quote for every entry (nse-alerts: `alerts.py quote RELIANCE
   --write`). A quote older than `price_fresh_minutes` blocks the order.
2. Run `check.py`.
3. Tell the user the result in plain words, order by order: the max loss at
   the worst exit with charges, reward:risk after charges, the NSE series and
   band, the quote it checked, and how much of today's limits it uses. For a
   BLOCK, give the reason and the fix (fewer shares, a closer stop from
   nse-exit, a fresh quote, plan it again if the order is old, wait for the
   window, or skip the trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. nse-broker matches
   each order to its verdict by `nse_common.order_fingerprint` (the entry and
   stop legs, the target and flat-by), so an edited order is not ready until
   it passes again, a verdict from yesterday does not count today, and a
   verdict older than `risk_pass_minutes` needs a new check.

## Example: RELIANCE, 13 Nov 2026, 10:05 IST

```
PASS  #1 BUY RELIANCE 37 shares, entry
          order written 1 min ago (Fri 2026-11-13 10:04 IST)
          RELIANCE last 1,334.00 at 10:04 IST (1 min old)
          NSE RELIANCE: series EQ, No Band (an F&O stock: NSE sets a dynamic band), no surveillance
          entry 1,334.00 (the limit price), stop 1,328.80 (SL limit 1,322.10), target 1,344.40: risk 5.20 a share
          max loss ₹492.48 at 1,322.10 with charges (37 x 11.90 = ₹440.30 + ₹52.18; limit ₹500), reward:risk 1.36 after charges (min 1), 0.67 if the stop fills at 1,322.10
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-11-13 10:05 IST. Cash after the passed orders: ₹50,642 (paper, no broker).
A PASS is good until 10:10 IST (risk_pass_minutes 5) and only today: after that nse-broker wants a new check.
```

Why 37 shares and not the 56 that fit the position cap: the stop triggers at
1,328.80 (5.20 a share) but it is an SL order with its limit at 1,322.10, so
at the worst fill each share loses 11.90. 56 shares would lose ₹666.40 plus
₹73.51 charges = ₹739.91 against the ₹500 cap, and the BLOCK says so; 37 lose
₹440.30 + ₹52.18 = ₹492.48, and 38 would lose ₹505.79. With `loss_basis`
"trigger" the 56 shares measure ₹364.82, the old way.

The same order is blocked at 09:20 by the window (no new entries before 09:30
IST). On Monday 16 Nov it is blocked as an order planned for Friday 13 Nov, 3
days earlier. With the last price at 1,320 it is blocked because the stock is
already under its 1,328.80 stop: Kite would reject a stop whose trigger is
above the last price, so the trade would have no stop. With no quote in
`prices.json` it is blocked with the command to run. A 5%-band stock like
A2ZINFRA, a BE-series stock like ABAN, or BLUECOAST under "GSM STAGE - 0" are
blocked; SBIN and RELIANCE ("No Band") pass. The 15:10 SELL that flattens the
position passes even after the 14:30 entry cutoff, with no quote and no fresh
order: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices, NSE's list or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills. An SL stop
  can fill at its limit, or not at all, in a gap: that is why the loss is measured at the limit.
- `--now` pins the check clock for a what-if. The result is stamped with that time (nse-broker counts
  `risk_pass_minutes` from it), so never pin it to anything but the real time to get an order past the
  window, the age check or the quote check.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Not financial advice. A PASS is a rules check, not a recommendation.
- Paper by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit **yes** for that one order in the chat,
  and (with the Kite MCP) the user clicked Allow once on Claude's own card because the order tool
  is set to "Needs approval". No yes, no order.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables, never in the chat.
