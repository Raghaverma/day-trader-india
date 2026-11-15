---
name: nse-exit
description: >-
  Exit Planner for the Indian cash market (NSE). Sets your stop and your
  target before you enter, so you know when to get out of an intraday (MIS)
  trade. Reads the entry from nse-entry (or a price you give), puts the stop
  at the opening-range midpoint by default (or the range low, your price, a
  percent, or a multiple of the ATR), rounds it down to the NSE tick (₹0.10
  for a ₹1,334 stock), sets the target at 2R on the tick grid, shows the
  Zerodha charges a share, the reward:risk after charges and the break-even
  price, and sets flat-by 15:10 IST before Zerodha's 15:25 MIS square-off.
  Refuses a stop at or above the entry, a target at or below it, or a stop
  too tight to survive the spread, and says why. Writes trader/plan.json for
  the Time Machine, the Budget Manager and Price Alerts. Long side only. Use
  when the user says "where's my stop", "set my stop and target", "plan the
  exit", "stop loss for RELIANCE", "what's my R", "risk reward after
  charges", "break-even on Zerodha", "Kite stop loss", "Nifty stock exit
  plan", or right after /nse-entry triggers, before /nse-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X] [--qty N]"
---

# nse-exit

The Exit Planner decides how an NSE intraday trade ends before it starts. The
stop is where the setup has failed. The target is where you take what you
came for. Both are set BEFORE the entry, written down and handed on, so
nobody has to decide anything in the middle of a fast move. A stop is a
plan, not a guarantee: a gap or a fast market can fill past it. Day trading
loses most people money, and a neat plan does not change that.

Not financial advice. This skill never places an order. Nothing in this pack
is placed without the user's explicit **yes** in the chat for that one order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo .NS or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1334 --or-high 1333.40 --or-low 1324.30
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --qty 56 --spread 0.10   # charges on 56 shares, a ₹0.10 spread
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:10`, `--json`, `--now "2026-11-13 10:05"` (IST),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--offline FILE` (a saved Yahoo chart JSON for an ATR stop), `--config PATH`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from nse-entry: symbol, entry price, time (IST), bar, opening range |
| `trader/config.json` | `flat_by` (15:10), `min_reward_risk` (1.0: a note here, nse-risk enforces it), `charges`, `tick_sizes`, `special_sessions`, and the caps that size the charges estimate (`budget`, `max_position_pct`, `max_order_inr`, `max_loss_per_trade_inr`) |
| `trader/state/nse-holidays.json` | NSE holidays fetched by nse-alerts, to warn about a plan on a closed day |
| Yahoo chart API | only for an `atr:K` stop: the session's bars for SYMBOL.NS up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: the original exit planner's fields (entry, stop and its rule, target and its R, risk a share, reward:risk, flat by) plus `"exchange": "NSE"` and `"tick": "0.10"`. nse-timemachine, nse-budget and nse-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint, 09:15 to 09:30 IST (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

## India rules

- **Ticks.** NSE cash-market ticks since 15 Apr 2025: under ₹250 ₹0.01,
  ₹250 to 1,000 ₹0.05, ₹1,000 to 5,000 ₹0.10, ₹5,000 to 10,000 ₹0.50,
  ₹10,000 to 20,000 ₹1.00, above ₹5.00. The stop rounds DOWN to the tick for
  its own price (away from the entry). The entry and the target round half up
  to the tick. A price on a slab edge takes the coarser tick, which is on the
  finer grid too. `tick_sizes` in the config overrides a symbol, since NSE
  reviews ticks monthly.
- **Too tight.** The stop must sit at least 0.05% of the price and at least
  three spreads under the entry. A spread is one tick unless `--spread X`
  says what Kite's market depth shows. Closer than that is refused: in on the
  ask, out on the bid, and one tick of wiggle would stop you out.
- **Charges.** Zerodha's intraday equity rates (brokerage 0.03% or ₹20 an
  order, STT 0.025% on the sell, NSE transaction 0.00307%, SEBI, stamp duty
  0.003% on the buy, GST 18%) from `nse_common.intraday_charges`, on `--qty`
  shares or the size your config's caps allow. It prints the charges a share,
  the reward:risk after charges and the break-even sell price on the tick.
- **Flat by.** 15:10 IST by default (or `flat_by` in the config). Zerodha
  squares off MIS at 15:25 and charges ₹50 + GST an order for it; a flat-by
  after 15:20 gets a note. On a special session (Muhurat trading in
  `special_sessions`) it keeps the same distance before that session's close.
  An entry at or after the flat-by time is refused.
- **Kite has no OCO for MIS.** The stop goes in as an SL sell after the entry
  fills. The target is an alert, not an order: when it rings, cancel the
  stop, then sell. nse-budget builds the order legs; nse-alerts rings.
- **Holidays.** A plan for an NSE holiday or a weekend gets a note, and so
  does a year whose holiday list is not known yet (run nse-alerts
  `holidays --refresh`).
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A stop too tight to survive the spread. An entry.json that has
  not triggered (unless you give `--entry`). A short: longs only.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share in rupees and ticks, reward:risk
   before and after charges, the break-even and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/nse-timemachine` to replay the plan, then `/nse-budget` to
   size it to the max loss per trade. Offer `/nse-alerts add --from-plan` for
   the stop, the target and the flat-by time.

## Example: RELIANCE, Friday 13 Nov 2026

The 15-minute opening range ran from 1,324.30 to 1,333.40. The 10:00 bar
closed at 1,334.00, the first close above the high, so nse-entry triggered at
10:05 IST (bar 9). `exit.py plan --write`:

```
Exit plan: RELIANCE long on NSE, Fri 2026-11-13, opening-range breakout on 5-minute bars (tick ₹0.10)
  Entry         ₹1,334.00 at 10:05 IST (bar 9)
  Stop          ₹1,328.80  opening-range midpoint: (₹1,333.40 + ₹1,324.30) / 2 = ₹1,328.85, rounded down to the ₹0.10 tick
  Target        ₹1,344.40  2R (two times the risk)
  Risk a share  ₹5.20 (0.39% of the entry, 52 ticks of ₹0.10)
  Reward:risk   2.0 (1.39 after charges)
  Charges       about ₹1.32 a share at the target, ₹1.31 at the stop (₹73.87 and ₹73.62 a round trip on 56 shares, Zerodha intraday rates)
  Break-even    ₹1,335.40: the sell price that pays back the charges on 56 shares
  Flat by       15:10 IST, win or lose: no overnight hold (Zerodha squares off MIS at 15:25 for ₹50 + GST)
```

The 56 shares come from the default caps (75% of a ₹1,00,000 budget at
₹1,334). Charges cost about 0.25R a side here, which is why the reward:risk
falls from 2.0 to 1.39. A 1R target would be ₹1,339.20.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk, charges.
  It never prints a P&L or a return.
- Nothing here promises winning trades or money. A 2R target is not a
  prediction that the price gets there.
- Long side only, cash equity, intraday (MIS). No F&O, no shorting.

## What it never does

It never places an order, never sizes the real position (the quantity is an
estimate for the charges; nse-budget sizes), never widens a stop to make a
plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS from nse-risk is a rules check, not a recommendation.
- Paper trading by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk passed that
  exact order today, the user typed an explicit **yes** for that one order in the chat, and the user clicked
  Allow once on Claude's own card for the Kite order tool.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the MCP server's own config, never in the chat.
