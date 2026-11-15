---
name: nse-risk
description: >-
  Risk Manager for NSE intraday orders. Checks every Kite-shaped order against
  your limits and stops anything over them before it can reach Zerodha or the
  paper broker. A stop leg is required; the loss at the stop plus estimated
  charges must fit your max loss per trade in ₹; reward:risk after charges
  must meet your minimum; the day's risk must fit your daily loss limit; no
  more than your trades per day; no new entries outside your IST entry window
  or when the NSE is closed (weekends, NSE holidays). It also checks NSE's
  security list: series EQ only (BE/BZ trade-for-trade blocked), 2% and 5%
  circuit bands blocked ("No Band" F&O stocks pass), GSM/ASM surveillance
  blocked; plus MIS product, NSE exchange, prices on the tick, no F&O, no
  shorts, cash buffer, margin, duplicates. Getting out is never blocked by a
  day limit. Use when the user says "check my order", "is this safe", "risk
  check", "does this break my rules", "can I take this trade", "how much have
  I risked today", or before /nse-broker, always.
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
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-13 10:05"  # pin the clock (IST)
python3 "${CLAUDE_SKILL_DIR}/check.py" --sec-list sec_list.csv   # NSE's security list from a file (.csv or .csv.gz)
```

Exit code 0 when every order passes, 1 when any is blocked or on an error
(one plain line, never a traceback).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (nse-budget, or nse-broker's flatten) |
| `trader/config.json` | the limits (nse-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry of a MARKET order; also values holdings |
| `trader/state/broker.json` | what was placed today, and closed trades' real P&L |
| `trader/state/nse-holidays.json` | NSE holidays beyond 2026 (nse-alerts `holidays --refresh`) |
| `trader/cache/sec_list-YYYY-MM-DD.csv` | NSE's security list, downloaded once a day |
| `trader/state/risk.json` | the verdicts it writes, each under the order's fingerprint |

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
only. Every other rule (day limits, entry window, loss, reward:risk, series,
band, surveillance, blocked list, size, duplicates) turns into a warning for
an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled.

| check | blocks when |
| --- | --- |
| format | no entry leg; variety not `regular`; quantity not a whole number; order_type not MARKET/LIMIT/SL/SL-M; validity not DAY/IOC; a LIMIT with no price, a MARKET with one, an SL without trigger and price |
| exchange, product | not NSE; not MIS (CNC is delivery, NRML is F&O) |
| tick | an entry price, a trigger or an SL limit off the NSE tick (an off-tick target only warns: it is an alert) |
| asset | F&O contracts (NFO/BFO, `...FUT`, `...CE`/`...PE`) while `allow_fno` is false; anything that is not an NSE stock symbol |
| short | a SELL of more than you hold (`allow_short` false) |
| security | (entries) not in sec_list; series not in `allowed_series` (BE/BZ: trade-for-trade, delivery only, no intraday; SM/ST: SME); band under `min_circuit_band_pct` ("No Band" passes: F&O stocks have dynamic bands); `GSM`/`ASM` remarks while `block_surveillance` |
| blocked, size | (entries) on `blocked`; under `min_order_inr` or over `max_order_inr` |
| position, cash | (entries) over `max_position_pct` of the account; under the cash buffer, or margin while `allow_margin` is false |
| stop | (entries) no stop leg while `require_stop`; a stop leg that is not a SELL of the same quantity and product, not SL/SL-M, a trigger not under the entry, an SL limit above its trigger |
| loss | (entries) qty x (entry minus trigger), plus the estimated charges when `include_charges`, over `max_loss_per_trade_inr` |
| reward:risk | (entries) after charges, (qty x reward − charges at the target) / (qty x risk + charges at the stop), under `min_reward_risk` |
| daily loss | (entries) today's entries at their max loss (or their real net loss from the log, if worse) plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after` (IST), or the NSE is closed: weekend, NSE holiday, outside 09:15-15:30 |
| duplicate, daily limit | (entries) the same symbol and side twice in a batch or already placed today; `max_orders_per_day` orders placed |

How the numbers work:

- **The entry it measures from**: the LIMIT price. For a MARKET order, the
  HIGHER of the plan's entry and a fresh price (under 15 minutes old) from
  `trader/prices.json`. An older price is not used, and it says so.
- **Charges** are Zerodha's intraday rates from `nse_common.intraday_charges`
  (override in config `charges`). RELIANCE, 56 shares, 1,334.00 to the
  1,328.80 stop: ₹291.20 + ₹73.62 charges = ₹364.82.
- **The window** is IST, worked out in the tool, never from the computer's
  clock zone. A year whose NSE holiday list isn't known yet is a warning, not
  a block: refresh it with nse-alerts (`alerts.py holidays --refresh`). A
  special session in the config (Muhurat trading) opens a closed day.
- There is no pattern-day-trader rule in India, so there is no `pdt_guard`.

## Steps

1. If a MARKET order's price may have moved since the plan, refresh it first
   (nse-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with charges, reward:risk after charges, the NSE series and band,
   and how much of today's limits it uses. For a BLOCK, give the reason and
   the fix (fewer shares, a closer stop from nse-exit, wait for the window, or
   skip the trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. nse-broker matches
   each order to its verdict by `nse_common.order_fingerprint` (the entry and
   stop legs), so an edited order is not ready until it passes again, and a
   verdict from yesterday does not count today.

## Example: RELIANCE, 13 Nov 2026, 10:05 IST

```
PASS  #1 BUY RELIANCE 56 shares, entry
          NSE RELIANCE: series EQ, No Band (an F&O stock: NSE sets a dynamic band), no surveillance
          entry 1,334.00 (the limit price), stop 1,328.80 (SL limit 1,322.10), target 1,344.40: risk 5.20 a share
          max loss ₹364.82 with charges (₹291.20 + ₹73.62; limit ₹500), reward:risk 1.39 after charges (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-11-13 10:05 IST. Cash after the passed orders: ₹25,296 (paper, no broker).
```

At 09:20 the same order is blocked by the window (no new entries before
09:30 IST). With a ₹360 max loss it is blocked by the charges: ₹291.20 at the
stop plus ₹73.62 charges = ₹364.82. A 5%-band stock like A2ZINFRA, a BE-series
stock like ABAN, or BLUECOAST under "GSM STAGE - 0" are blocked; SBIN and
RELIANCE ("No Band") pass. The 15:10 SELL that flattens the position passes
even after the 14:30 entry cutoff: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices, NSE's list or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and an
  SL stop can fill worse than its trigger, or not at all, in a gap.

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
