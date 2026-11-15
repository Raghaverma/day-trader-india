---
name: nse-budget
description: >-
  Budget Manager for NSE intraday trades. Sizes every position to your rupee
  budget and your max loss per trade, with Zerodha's charges counted in. Reads
  the trade plan from nse-exit (entry, stop, target), finds the whole shares
  that fit inside four limits (max loss per trade including estimated charges,
  position cap, max order, cash after your buffer), says which limit decided
  it, and writes ONE Kite-shaped order to trader/orders.json: a MIS LIMIT buy,
  an SL stop leg on the NSE tick, and the target as an alert (Kite has no OCO
  for MIS). Reports charges at the stop and the target and the break-even move.
  Nothing is placed. Use when the user says "how many shares", "size this
  trade", "how much should I buy", "I have ₹1 lakh", "I only want to risk ₹500
  a trade", "set my rules", "keep 10% in cash", "Zerodha charges", or after
  /nse-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# nse-budget

The Budget Manager turns a plan ("buy RELIANCE at 1,334.00, stop 1,328.80")
into an exact order ("buy 56 shares, MIS"), inside rules the user sets once.
The stop decides the size: the further away the stop, the fewer shares. On
the NSE every round trip also pays brokerage, STT, exchange fees, SEBI fees,
stamp duty and GST, so the charges count against the max loss too. It writes
a proposal only. nse-risk checks it and nse-broker asks the user yes or no.
Day trading loses most people money. Sizing to a max loss keeps each loss
small; it does not make a trade win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                  # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1333.50 --write  # a LIMIT buy at 1,333.50, not the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --market --write         # a MARKET buy, only if the user asked for one
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 50000 --json    # another budget, JSON out
```

`--now "2026-11-13 10:05"` pins the clock (IST). A plan for another day is
refused: make a new one with nse-exit. Exit code 0 when an order is proposed,
1 when it refuses (the reason is printed).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (nse-exit writes it) |
| `trader/prices.json` | optional: for a MARKET buy, a fresh price above the plan's entry means a bigger risk (nse-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (nse-broker keeps it) |
| `trader/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each money value in plain words.
Every key, with its default:

| key | default | what it means |
| --- | --- | --- |
| `budget` | 100000 | rupees for this, and only money they can afford to lose |
| `cash_buffer_pct` | 10 | always stays in cash, never invested |
| `max_position_pct` | 75 | the biggest position, as a % of the budget |
| `max_order_inr` / `min_order_inr` | 80000 / 500 | the biggest and smallest order in rupees |
| `max_loss_per_trade_inr` | 500 | the most one trade may lose if the stop fills at the stop, charges included |
| `max_daily_loss_inr` | 1500 | nse-risk: today's entries at their max loss must fit in this |
| `max_trades_per_day` / `max_orders_per_day` | 3 / 10 | nse-risk: entries a day / Kite orders a day |
| `min_reward_risk` | 1.0 | reward:risk after charges must be at least this |
| `require_stop` | true | every entry needs a stop leg |
| `include_charges` | true | count estimated charges in the max loss and in reward:risk |
| `no_entries_before` / `no_entries_after` | "09:30" / "14:30" | the entry window, IST |
| `flat_by` | "15:10" | out by then; Zerodha squares MIS off itself at 15:25 and charges for it |
| `broker` | "paper" | paper (no broker), manual (you type it into Kite) or kite (Kite MCP), see nse-broker |
| `product` | "MIS" | intraday; anything else is blocked by nse-risk |
| `stop_order_type` | "SL" | SL (a stop with a limit) or SL-M (market once triggered) |
| `stop_limit_buffer_pct` | 0.5 | the SL leg's limit sits this % under its trigger, rounded down to the tick |
| `allowed_series` | ["EQ"] | NSE series that may be traded; BE/BZ are trade-for-trade (no intraday) |
| `min_circuit_band_pct` | 10 | price bands under this (2% and 5% stocks) are blocked; "No Band" F&O stocks pass |
| `block_surveillance` | true | stocks under GSM/ASM surveillance are blocked |
| `blocked` | [] | symbols the user never wants to trade |
| `allow_margin` / `allow_short` / `allow_fno` | false | MIS leverage, short selling, F&O: all off |
| `charges` | {} | override Zerodha's rates, e.g. {"brokerage_cap": 0} for a zero-brokerage broker |
| `tick_sizes` | {} | per-symbol tick overrides, e.g. {"RELIANCE": 0.1}, when NSE resets a stock's tick |
| `special_sessions` | {} | e.g. Muhurat trading: {"2026-11-08": {"open": "18:15", "close": "19:15", "label": "Muhurat trading"}} |
| `extra_holidays` | {} | NSE holidays not in the built-in 2026 list, {"2027-01-26": "Republic Day"} |
| `holiday_years` | (not set) | years whose full NSE holiday list you have entered, e.g. [2027]; one extra holiday alone doesn't make a year known |

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares where qty x (entry minus stop) plus the estimated charges at the stop fit in `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` |

India rules it applies:

- **Ticks.** Every price sits on the NSE tick for its slab (0.01 under ₹250,
  0.05 to ₹1,000, 0.10 to ₹5,000, 0.50 to ₹10,000, 1.00 to ₹20,000, 5.00 above).
  The stop rounds DOWN; the limit and the target round to the nearest tick.
- **Charges.** Zerodha's intraday equity rates (0.03% or ₹20 brokerage per
  order, 0.025% STT on the sell, 0.00307% NSE fee, SEBI ₹10/crore, stamp 0.003%
  on the buy, 18% GST). It reports the charges at the stop and at the target,
  reward:risk after charges, and the break-even price.
- **The order.** An entry leg (LIMIT at the plan's entry by default, MARKET only
  with `--market`) and an SL stop leg (trigger at the stop, limit
  `stop_limit_buffer_pct` under it, rounded down) that is placed once the entry
  fills. Kite has no bracket orders and no OCO for MIS: a resting target and a
  resting stop can both fill and leave you short. So the target is an alert:
  when it rings, cancel the stop, then sell.
- An SL order can fail to fill in a gap below its limit; say so once. With
  `"stop_order_type": "SL-M"` the leg has no price, and market orders sent by
  API may need market protection at the broker.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk after charges is under `min_reward_risk`, the
stop is not below the entry, the target is not above it, the plan is for
another day or another exchange, the symbol is blocked, or the plan is a short.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees, the
max loss at the stop with charges, the charges at the target, and the
break-even. Then hand over to `/nse-risk`.

## Example: RELIANCE, Friday 13 Nov 2026

Opening-range breakout: entry 1,334.00 at 10:05, stop 1,328.80 (the
opening-range midpoint 1,328.85 rounded down to the 0.10 tick), target
1,344.40 (2R). Risk 5.20 a share. ₹1,00,000 budget, ₹500 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 79 x 5.20 = ₹410.80 + ₹84.47 charges = ₹495.27 | 79 |
| position cap | 75% of ₹1,00,000 = ₹75,000 / 1,334.00 | **56 (decides it)** |
| max order | ₹80,000 / 1,334.00 | 59 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 1,334.00 | 67 |

56 shares = ₹74,704. At the stop: 56 x 5.20 = ₹291.20 + ₹73.62 charges =
₹364.82. At the target: ₹582.40 − ₹73.87 charges = ₹508.53, reward:risk 1.39
after charges (2.00 before). Break-even 1,335.40. The stop leg: SELL 56 SL,
trigger 1,328.80, limit 1,322.10 (1,328.80 x 0.995 = 1,322.156, rounded down).

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Charges are estimates at Zerodha's published rates; the contract note is the real number.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## Safety

- Not financial advice. Education and tooling; a sized order is arithmetic, not a recommendation.
- Paper by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit **yes** for that one order in the chat,
  and (with the Kite MCP) the user clicked Allow once on Claude's own card because the order tool
  is set to "Needs approval". No yes, no order.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables, never in the chat.
