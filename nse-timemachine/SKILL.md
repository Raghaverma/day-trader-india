---
name: nse-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  trade before you risk a rupee: 10,000 simulated sessions plus every past
  time the setup fired, before and after Zerodha charges. Takes the plan from
  nse-exit (entry, stop, target, flat-by 15:10 IST), rebuilds the rest of the
  day 10,000 times from the stock's own 5-minute bars over the last 60 NSE
  sessions (Yahoo .NS), and re-runs the same opening-range rule on every past
  session. Prints the odds of target first, stop first, or neither by the
  flat-by time, where the price sits then, the average R before and after
  charges, the history, and one honest verdict, then hands off to
  /nse-budget. Use when the user says "run the time machine", "replay this
  trade", "what are the odds on RELIANCE", "simulate this trade", "monte
  carlo", "backtest this setup on NSE", "is this a coin flip", "does it beat
  the charges", or after /nse-exit writes a plan, before /nse-budget.
argument-hint: "[path to plan.json, or NSE symbol]"
---

# nse-timemachine

The Time Machine sits between the plan and the money. nse-exit has set the
entry, the stop, the target and the flat-by time. Before anything is sized or
sent, this skill asks one question: if today plays out like the stock's own
last 60 NSE sessions, how often does this plan hit the target first, the stop
first, or neither by 15:10? And does it still pay once Zerodha's charges come
out? It answers with 10,000 replays and with the real record of the same rule
on every past session. It is a simulation built from the stock's own recent
bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

Not financial advice. This skill never places an order. Nothing in this pack
is placed without the user's explicit **yes** in the chat for that one order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:00      # end each replay before the plan's flat-by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --qty 56 --json    # charges on 56 shares
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-11-13 \
    --entry 1334 --stop 1328.80 --target 1344.40 --entry-time 10:05
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-11-13 10:05"` (IST) to pin the clock, `--config PATH`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from nse-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by |
| `trader/config.json` | `flat_by`, `charges`, `tick_sizes`, and the caps that size the charges estimate |
| Yahoo chart API | one request: 60 days of 5-minute bars for SYMBOL.NS (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the after-charges sums, the fan-chart bands, 160 sample paths and a 24-bin histogram |

## How it works

1. **The pool.** The NSE normal market runs 09:15 to 15:30 IST: 75
   five-minute bars, stamped 09:15 to 15:25. It keeps the sessions before
   the plan's date that have every bar from 09:15 to the flat-by time.
   Yahoo leaves the 15:15 to 15:25 bars of most NSE sessions empty (the
   closing session). That is Yahoo's gap, not a halt: those bars never drop
   a session, and with `--until` after 15:10 an empty one is skipped (a slot
   no session has stays flat). A gap earlier in the day, special sessions and
   today are left out. Every bar becomes four ratios to the close before it:
   open, high, low and close over the previous close. The 09:15 bar often
   shows volume 0 on Yahoo; only prices are used.
2. **One replay.** Start at the entry price. For each bar left until the
   flat-by time, pick a past bar from the same time of day (plus or minus 2
   bars, kept inside 09:20 to the flat-by bar) at random, and scale it to
   the current price. Bar by bar, it checks the stop first, then the target.
   A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at the flat-by time (15:10 IST by
   default: you are out by then, win or lose). Green, which means target
   first or above the entry at the flat-by time. R is +2 at a 2R target, -1
   at the stop, and otherwise the distance from the entry at the flat-by
   time divided by the risk. A gap through the stop still counts as -1R
   here. A real fill can be worse.
4. **Charges.** Indian round-trip costs are material on a tight intraday
   stop. Every replay and every history trade also gets its R after
   Zerodha's intraday charges (`nse_common.intraday_charges`) on `--qty`
   shares, or the plan's quantity, or the size your config's caps allow.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, prices on the NSE tick) runs again on every one of those
   past sessions, flat at the same time. That is what really happened, not a
   simulation.
6. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R. Less than 5 points from break-even: "Close to
   a coin flip" at 1R, "Close to break-even" at any other R. 5 to 15 points:
   "Leans toward the target" or "Leans toward the stop". 15 or more:
   "Strongly toward" one of them.
7. **Below the verdict**: a line when more than 40 % of the replays hit
   neither, the average R when its sign goes against the lean, and always a
   line with the same sums after charges (the R at the target and at the
   stop net of charges, the share the target needs then, and the average R).

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default, in exactly the original US time machine's order. Same plan, same
data, same seed: the same numbers, on any machine. Say the seed with every
result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 whole sessions, when the entry bar is
not in the data (wrong date, an NSE holiday, not closed yet, a future day), or
when the plan does not make sense. A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/nse-exit`. If there is
   none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. It ends each replay at the plan's
   flat-by time, so "neither" means still open when you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the flat-by time, the average R, the average R after
   charges, the history line, then the verdict line and every line under it
   exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. If
   the charges turn a small edge into a loss, say that too. If the history
   and the replays disagree, say so.
5. Say once that this is built from the last 60 sessions and is not a
   prediction. Results, news, circuit limits and index events can do things
   those sessions never did. Check `/nse-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/nse-budget` to size it.

## Example: RELIANCE, Friday 13 Nov 2026

Entry ₹1,334.00 at 10:05 IST (bar 9), stop ₹1,328.80 (the range midpoint,
rounded down to the ₹0.10 tick), target ₹1,344.40 (2R, risk ₹5.20 a share),
flat by 15:10. Seed 19, 58 whole sessions from 18 Aug to 12 Nov 2026 (15 Oct
had a gap and was left out):

```
10,000 replays of the rest of the session, 10:05 to 15:10 IST (61 five-minute bars), seed 19.
  target first   22.6 %
  stop first     69.1 %
  neither        8.3 %    still open at 15:10
  green          28.8 %   target first, or above the entry at 15:10
  Average R: -0.199
  After charges on 56 shares: average R -0.452
History: fired 31 times in 58 sessions: 7 target, 15 stop, 9 neither. Average R -0.030, -0.280 after charges.
Leans toward the stop: at 2R the target has to come first in 33 % of the replays that hit one side; it did in 25 %.
After charges: +1.75R at the target, -1.25R at the stop, so the target has to come first in 42 %.
```

That day the real price ran to ₹1,350.50 and the target filled in the 10:10
bar. One good day is not the odds: the replays and the history both lean
toward the stop, and the charges make it worse. That is what the user should
hear before `/nse-budget` sizes anything.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R plan by target first vs stop
  first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never uses a bar that has not
closed yet, and never mixes today's bars into the pool.

## Safety

- Education and tooling, not financial advice. A PASS from nse-risk is a rules check, not a recommendation.
- Paper trading by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk passed that
  exact order today, the user typed an explicit **yes** for that one order in the chat, and the user clicked
  Allow once on Claude's own card for the Kite order tool.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the MCP server's own config, never in the chat.
