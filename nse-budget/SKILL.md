---
name: nse-budget
description: >-
  Budget Manager for NSE intraday trades. Sizes each position to your rupee
  budget and max loss per trade, Zerodha charges counted in. Reads the
  plan from nse-exit, finds the whole shares inside four limits (max loss at
  the stop's worst fill, position cap, max order, cash), says which one
  decided, and writes a Kite MIS order: LIMIT buy, SL stop leg, target as an
  alert (no OCO for MIS). Nothing is placed. Use when the user says
  "how many shares", "size this trade", "how much should I buy", "I have ₹1
  lakh", "I only want to risk ₹500 a trade", "set my rules", or after
  /nse-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# nse-budget

The Budget Manager turns a plan ("buy RELIANCE at 1,334.00, stop 1,328.80")
into an exact order ("buy 37 shares, MIS"), inside rules the user sets once.
The stop decides the size: the further away the stop, the fewer shares. The
max loss is measured at the stop leg's WORST fill (its SL limit, a little
under the trigger), not at the trigger, and every round trip also pays
brokerage, STT, exchange fees, SEBI fees, stamp duty and GST, so the charges
count against the max loss too. It writes a proposal only. nse-risk checks it
and nse-broker asks the user yes or no.
Day trading loses most people money. Sizing to a max loss keeps each loss
small; it does not make a trade win. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --init-config --budget 100000 --max-loss 500 --max-daily-loss 1500   # once
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                  # write trader/orders.json (this order only)
python3 "${CLAUDE_SKILL_DIR}/size.py" --write --append         # add this symbol's order, keep the others
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1333.50 --write  # a LIMIT buy at 1,333.50, not the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --market --write         # a MARKET buy, only if the user asked for one
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 50000 --json    # another budget, JSON out
```

`--now "2026-11-13 10:05"` pins the clock (IST). A plan for another day is
refused: make a new one with nse-exit. Exit code 0 when an order is proposed,
1 when it refuses (the reason is printed), 2 when there is no `config.json`
(nothing is sized or written).

The trader folder (`config.json`, `plan.json`, `orders.json`, `state/`) is the
same wherever Claude Code was started: the `NSE_TRADER_HOME` environment
variable, else the nearest
`trader/` folder that has a `config.json` in the working folder or above, else
`~/.day-trader-india`. Error messages print the real path.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules. `--init-config` writes it; `config.example.json` in this folder lists every key |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (nse-exit writes it) |
| `trader/prices.json` | optional: a quote struck in the last `price_fresh_minutes` (5): for a MARKET buy, a fresh price above the plan's entry means a bigger risk; for a LIMIT buy, one above the limit gets a note (nse-alerts writes it) |
| `trader/portfolio.json` | optional: your real account. Its cash caps the size, and its value (cash + holdings) replaces the budget when smaller (nse-broker keeps it) |
| `trader/orders.json` | the proposed order(s) it writes with `--write`: `created_at` (UTC) on the file and on each order, `plan.symbol` and `plan.date` (IST) on each order; nse-risk and nse-broker read them to refuse an old order |

## Step 1: set the rules once

If there is no `config.json`, size.py stops (exit 2) and writes nothing: it
never guesses a budget or a loss limit. Ask the user, in plain words, for the
three money values (how many rupees this is for, the most one trade may lose,
the most a whole day may lose), then run `--init-config` with them:

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --init-config --budget 100000 --max-loss 500 --max-daily-loss 1500
```

It writes every default plus the numbers given (`"broker": "paper"` always),
prints what it wrote, and refuses to overwrite a config that exists: to change
a rule later, edit the file. It also takes `--cash-buffer-pct`,
`--max-position-pct`, `--max-order`, `--min-order`, `--max-trades` and
`--min-reward-risk`. It refuses numbers that make no sense (a max loss at or
over the budget, a daily limit under the per-trade limit). Every key, with its
default:

| key | default | what it means |
| --- | --- | --- |
| `budget` | 100000 | rupees for this, and only money they can afford to lose. The position cap and the cash buffer use the smaller of this and the real account in `portfolio.json` |
| `cash_buffer_pct` | 10 | always stays in cash, never invested |
| `max_position_pct` | 75 | the biggest position, as a % of the budget |
| `max_order_inr` / `min_order_inr` | 80000 / 500 | the biggest and smallest order in rupees |
| `max_loss_per_trade_inr` | 500 | the most one trade may lose if the stop fills at its worst price (the SL limit), charges included |
| `max_daily_loss_inr` | 1500 | nse-risk: today's entries at their max loss must fit in this |
| `max_trades_per_day` / `max_orders_per_day` | 3 / 10 | nse-risk: entries a day / Kite orders a day |
| `min_reward_risk` | 1.0 | reward:risk after charges, to the stop trigger, must be at least this |
| `require_stop` | true | every entry needs a stop leg |
| `include_charges` | true | count estimated charges in the max loss and in reward:risk |
| `no_entries_before` / `no_entries_after` | "09:30" / "14:30" | the entry window, IST |
| `flat_by` | "15:10" | out by then; Zerodha squares MIS off itself at 15:25 and charges for it |
| `broker` | "paper" | paper (no broker), manual (you type it into Kite) or kite (Kite MCP), see nse-broker |
| `product` | "MIS" | intraday; anything else is blocked by nse-risk |
| `stop_order_type` | "SL" | SL (a stop with a limit) or SL-M (market once triggered) |
| `stop_limit_buffer_pct` | 0.5 | the SL leg's limit sits this % under its trigger, rounded down to the tick; the max loss is measured at that limit |
| `loss_basis` | "limit" | where the max loss is measured: `"limit"` (the SL limit, the worst fill) or `"trigger"` (the old way: a stop that never slips) |
| `tick_band_pct` | 10 | prices use the coarsest tick within this % of the price, because NSE sets a stock's tick from an earlier close |
| `risk_pass_minutes` | 5 | nse-risk: a PASS is good for this long |
| `price_fresh_minutes` | 5 | a quote older than this is not used |
| `order_max_age_minutes` | 120 | nse-risk: an orders.json older than this is not traded; size it again |
| `trigger_max_age_minutes` | 30 | nse-exit: an entry trigger older than this is not planned from |
| `holiday_refresh_days` | 30 | nse-alerts / nse-risk: refresh NSE's holiday list when the cache is older |
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
| max loss | the most shares where qty x (entry minus the stop leg's worst fill) plus the estimated round-trip charges fit in `max_loss_per_trade_inr` |
| position cap | account x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (account minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` |

The account is the budget, or the real account (cash + holdings in
`portfolio.json`) when that is smaller, so a losing day shrinks the next order.
nse-exit and nse-timemachine estimate the share count with the same function
(`nse_common.size_qty`) and nse-risk checks the order against the same rules,
so the four skills agree on the number.

India rules it applies:

- **Ticks.** Every price sits on the NSE tick for the stock (0.01 under ₹250,
  0.05 to ₹1,000, 0.10 to ₹5,000, 0.50 to ₹10,000, 1.00 to ₹20,000, 5.00
  above), taking the coarsest tick within `tick_band_pct` (10%) of the price:
  NSE sets a stock's tick from an earlier close, so a stop at 997.97 goes to
  997.90, valid on the 0.05 and on the 0.10 grid (997.95 would be rejected on
  0.10). The stop rounds DOWN; the limit and the target round to the nearest
  tick. `tick_sizes` in the config overrides a symbol.
- **Worst fill.** The stop leg is an SL order: when the trigger is hit it
  sells at its limit or better. The limit sits `stop_limit_buffer_pct` under
  the trigger, so the loss the user can actually take is measured there, not
  at the trigger. Every max-loss line says which price it is measured at.
  Reward:risk after charges is the plan's R to the trigger (the number nse-exit
  prints and nse-risk gates on); the same ratio at the worst fill is printed
  next to it and does not gate.
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
- **One order per symbol.** `--write` replaces orders.json with this order.
  `--write --append` adds it and keeps the orders for other symbols (the same
  symbol is replaced; an order for another day is dropped, with a note; an
  older order keeps its own `created_at`, so adding one never makes another
  look fresh).

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk after charges is under `min_reward_risk`, the
stop is not below the entry, the target is not above it, the plan is for
another day or another exchange, the symbol is blocked, or the plan is a short.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees, the
max loss at the worst fill with charges (and the price it is measured at), the
charges at the target, and the break-even. If the worst fill is much worse than
the trigger (here 2.3 times the planned risk, because the SL limit's 0.5%
buffer is bigger than the stop's own distance), say so once: that is why the
size is smaller than the position cap alone would give. Then hand over to
`/nse-risk`.

## Example: RELIANCE, Friday 13 Nov 2026

Opening-range breakout: entry 1,334.00 at 10:05, stop 1,328.80 (the
opening-range midpoint 1,328.85 rounded down to the 0.10 tick), target
1,344.40 (2R). Risk 5.20 a share to the trigger. The stop leg's limit is
1,328.80 x 0.995 = 1,322.156, rounded down to 1,322.10, so the worst fill
loses 11.90 a share, 2.3 times the planned risk. ₹1,00,000 budget, ₹500 max
loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 37 x 11.90 = ₹440.30 + ₹52.18 charges = ₹492.48 (38 would be ₹505.79) | **37 (decides it)** |
| position cap | 75% of ₹1,00,000 = ₹75,000 / 1,334.00 | 56 |
| max order | ₹80,000 / 1,334.00 | 59 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 1,334.00 | 67 |

37 shares = ₹49,358. If the stop is hit and fills at the SL limit 1,322.10:
37 x 11.90 = ₹440.30 + ₹52.18 charges = ₹492.48 (a fill at the trigger
itself would lose 37 x 5.20 = ₹192.40 + ₹52.33 = ₹244.73). At the target: 37 x
10.40 = ₹384.80 − ₹52.71 charges = ₹332.09, reward:risk 1.36 after charges to
the stop trigger (2.00 before charges; 0.67 if the stop fills at its limit).
Break-even 1,335.50, a move of 1.50 a share. The stop leg: SELL 37 SL,
trigger 1,328.80, limit 1,322.10.

Why not 56, the position cap? Because 56 x 11.90 = ₹666.40 + ₹73.51 charges
= ₹739.91 at the worst fill, against a ₹500 cap. Measuring at the trigger
would have said ₹364.82 and let it through.

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
