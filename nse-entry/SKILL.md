---
name: nse-entry
description: >-
  Entry Timer for NSE. Watches a setup bar by bar and says the moment to get
  in. Reads Yahoo .NS bars, builds the opening range from 09:15 IST and calls
  the entry on the first completed bar that CLOSES above the range high (or a
  level from nse-scanner), once the whole range is in. Knows NSE holidays and
  the 09:30 to 14:30 window. Writes trader/entry.json for /nse-exit. Long only,
  never places an order. Use when the user says "tell me when to get in",
  "watch RELIANCE", "has it broken out", "did it break the opening range",
  "Nifty stock entry signal", "check my watchlist", or after /nse-scanner.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m] [--now \"YYYY-MM-DD HH:MM\"]"
---

# nse-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

Not financial advice. This skill never places an order. Nothing in this pack
is placed without the user's explicit **yes** in the chat for that one order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE              # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write      # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1340
python3 "${CLAUDE_SKILL_DIR}/entry.py" check M&M --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:30  # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                   # every hit in trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist --setup level     # each hit's own "level" (its prior 20-day closing high)
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-11-13 10:05"` (IST, or just `10:05` for today;
`...Z` for UTC) to replay a day, `--config FILE`. Symbols are bare NSE
symbols: `RELIANCE`, `M&M`, `BAJAJ-AUTO` (`RELIANCE.NS` and `NSE:RELIANCE`
are read too). `watch` with `--now` or `--offline` runs on a simulated clock
and replays the day bar by bar without waiting.

Every file lives in the trader folder, the same one for all the skills:
the `NSE_TRADER_HOME` environment variable if set, else the nearest `trader/` folder with a
`config.json` above where Claude Code was started, else `~/.day-trader-india`,
else `./trader`. Each run ends with a line saying which `config.json` it used,
or that none was found at that path and the built-in defaults are in use (this
skill does not guard money, so it runs without one).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`, and `hits[].level`, the prior 20-day closing high on a 20-day-breakout hit), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:30), IST; `special_sessions` (Muhurat trading); `extra_holidays`. Optional |
| `trader/state/nse-holidays.json` | holiday years beyond 2026, written by nse-alerts, or by this skill when the list is stale and it refreshes it (best effort, never an error; not on an `--offline` replay) |
| Yahoo chart API | today's bars, `RELIANCE.NS?range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time (IST HH:MM), bar, price, level, opening range, **`triggered_at`** (UTC, the trigger bar's close; `/nse-exit` refuses a trigger older than `trigger_max_age_minutes`, 30, unless you give it a price or `--allow-late`) and **`late_by_minutes`** |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes of the
  NSE session: the 09:15, 09:20 and 09:25 five-minute bars, or fifteen
  1-minute bars. The level to beat is the range high.
- **The range must be whole.** The orb only triggers once every bar of the
  range window is in: the 09:15, 09:20 and 09:25 bars for 15 minutes. Yahoo
  sometimes leaves a bar out. With 2 of 3 present it says WAITING, "opening
  range incomplete: 2 of 3 bars", and never triggers off a range it could not
  see whole (a bar missing from the window can hide the real range high). If
  the range is still incomplete at 14:30 the setup expires.
- **Level.** A price you give, like a 20-day high from `/nse-scanner`.
  `watchlist --setup level` takes each hit's own `level` from
  `trader/watchlist.json` (nse-scanner writes the prior 20-day closing high on
  every 20-day-breakout hit). A hit with no `level` (an oversold or flag hit)
  is reported as an error for that row, saying so: pass `--level PRICE` or
  watch it with `--setup orb`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time in IST (the 10:00 bar closes at
  10:05). The price is its close, in rupees.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:44:45, is never a bar, and a bar
  still forming is never used, not even to say "it is above".
- **The tick.** The tick printed and written is for ORDER prices
  (`nse_common.order_tick`: the coarsest NSE tick within 10% of the price,
  because NSE sets a stock's tick from an earlier close, not today's price).
- **Late triggers.** The JSON carries `late_by_minutes`: 0 on a fresh trigger
  (the trigger bar closed within one bar), otherwise how many minutes ago it
  closed. Text output says "That was N minutes ago": a late entry is a
  different trade, the price, the stop distance and the odds have moved.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. Yahoo stamps the 09:15
  bar of most NSE charts with volume 0, so the average leaves zero-volume
  bars out.
- **Time limits.** No entries before 09:30 or after 14:30 IST (config). The
  first bar that can trigger is the 09:30 bar, complete at 09:35. In a
  special session (Muhurat trading, from config) the window keeps its
  distance from that session's open and close.
- **What it says.** Waiting (and how far the last close is from the level,
  in ₹), triggered, expired, or no data. Before 09:00: not open yet. 09:00 to
  09:15: pre-open (the call auction makes no bars). Before 09:30: the range is
  still forming. Weekends and NSE holidays: the market is closed, with the
  next open. A year whose holiday list is not loaded is refreshed from NSE
  first (best effort); if that fails it is said, and weekdays count as trading
  days.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/nse-scanner`. Check `/nse-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If `late_by_minutes` is above 0 (the trigger is more than a bar old), say
   that too: a late entry is a different trade, and `/nse-exit` will refuse a
   trigger older than 30 minutes unless the user gives it a price on purpose.
5. Hand off to `/nse-exit` to set the stop and the target BEFORE anything
   is bought. Mention Zerodha's 15:25 MIS auto square-off; the plan's
   flat-by (15:10) is set there.

## Example: RELIANCE, Friday 13 Nov 2026 (real data)

5-minute bars. The opening range (09:15-09:30) is ₹1,324.30 to ₹1,333.40. At
10:04 the 10:00 bar is still open, so it waits:

```
WAITING  last close ₹1,331.60 (09:55 bar), ₹1.80 under the range high ₹1,333.40 (-0.13%).
The 10:00 bar is still forming (₹1,334.00 so far, closes 10:05); it does not count until it closes.
```

At 10:05 the bar has closed:

```
RELIANCE  Fri 2026-11-13 10:05 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 09:15-09:30: high ₹1,333.40, low ₹1,324.30 (3 of 3 bars).
TRIGGERED 10:05 at ₹1,334.00: the 10:00 bar (bar 9) closed above the range high ₹1,333.40 (+0.04%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /nse-exit (exit.py plan --from trader/entry.json). Tick at this price: ₹0.10.
```

With `--min-volume-x 1` the 10:00 bar is too thin (0.33x the range's
volume) and the trigger moves to 10:20 at ₹1,343.60 (the 10:15 bar, 1.27x).
On 1-minute bars the same range breaks earlier: the 09:45 bar closes at
₹1,333.70, a trigger at 09:46. Later that day RELIANCE traded up to
₹1,350.50 and closed at ₹1,339.70.

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Longs only: cash equity, intraday (MIS). If the user wants a short, say
  that this pack does not plan one.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.
- Times are IST. Money is in rupees with Indian grouping (₹1,23,456).

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/nse-exit`), never moves the level after the fact, and never counts a bar
that has not closed. It never runs `/nse-broker` from a background watch:
nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS from nse-risk is a rules check, not a recommendation.
- Paper trading by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk passed that
  exact order today, the user typed an explicit **yes** for that one order in the chat, and the user clicked
  Allow once on Claude's own card for the Kite order tool.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the MCP server's own config, never in the chat.
