---
name: nse-alerts
description: >-
  Price Alerts and the NSE clock. Rings when a price, a stop or the 09:15
  opening bell hits on the National Stock Exchange. Turns the trade plan into
  three alerts in one go (the stop, the target and flat-by 15:10 IST), plus
  above / below / stop / target levels in rupees, percent moves from the
  previous close, any time of day, and the opening bell, each ringing once so
  you are not spammed. The target alert says what Zerodha Kite needs (no OCO
  for MIS: cancel the stop, then sell). Knows NSE hours (pre-open 09:00,
  normal market 09:15 to 15:30 IST), weekends and NSE trading holidays by
  name, refreshes the holiday list from NSE's holiday-master, fetches free
  delayed quotes from Yahoo (.NS) one symbol at a time, and writes
  trader/prices.json for sizing and the risk check. Use when the user says
  "alert me when RELIANCE hits", "watch my stop", "watch my trade", "remind
  me to square off by 15:10", "is the NSE open", "is the market open
  today", "when does Nifty open", "is it a market holiday", "NSE holidays",
  "get me a price in rupees", or right after /nse-exit writes a plan.
argument-hint: "[clock | holidays [--refresh] | quote SYMBOL | add --from-plan | check]"
---

# nse-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart, and keeps the NSE clock. An alert is information, never an order: when
a stop alert rings, nothing is sold by this skill. On Kite the stop is a
resting SL sell order and does the work; there is no OCO for MIS, so the
target is this alert. When the target rings, the user cancels the stop, then
sells (nse-broker builds the ticket).

Not financial advice. This skill never places, cancels or changes an order.
Nothing in this pack is placed without the user's explicit **yes** in the chat
for that one order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open, pre-open or closed; next open; holidays
python3 "${CLAUDE_SKILL_DIR}/alerts.py" holidays                       # this year's NSE trading holidays, with names
python3 "${CLAUDE_SKILL_DIR}/alerts.py" holidays --refresh             # NSE holiday-master -> trader/state/nse-holidays.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" holidays --year 2027
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat-by 15:10 from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell             # 09:15 IST each trading day
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1328.80
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1344.40
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol INFY --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE TCS --write     # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-11-13 15:10"` pins the clock (IST); `--offline DIR` reads saved
quote files (`yahoo-RELIANCE.json`) instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from nse-exit, for `add --from-plan` |
| `trader/config.json` | `flat_by` when the plan has none, `special_sessions` (Muhurat trading), `extra_holidays` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/state/nse-holidays.json` | written by `holidays --refresh`: `{"fetched_at", "source", "holidays": {date: name}, "years": [...]}`; every skill reads it through `nse_common.load_holidays` |
| `trader/prices.json` | the latest quotes in rupees, merged in, for nse-budget and nse-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price`; a `target` adds "cancel the stop, then sell" | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:10") on a trading day | once per trading day |
| `opening_bell` | the NSE normal market is open (09:15 IST) | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A `time` rule
with `"before_close": true` keeps the same distance before the close on a
special session, so a 15:10 flat-by rings 20 minutes before a Muhurat
session's close.

## The NSE clock

- **Hours.** Pre-open 09:00 to 09:15 IST (order entry 09:00 to 09:08), normal
  market 09:15 to 15:30 IST, Monday to Friday. IST is UTC+05:30 all year, no
  daylight saving, so the tool needs no time-zone files.
- **Holidays.** The 2026 capital-market list is built in, with names (24 Nov
  2026 is Prakash Gurpurb Sri Guru Nanak Dev). `clock` names the next ones.
  NSE publishes the next year's list around December: `holidays --refresh`
  fetches `https://www.nseindia.com/api/holiday-master?type=trading` (segment
  CM; on a 401 or 403 it visits nseindia.com once for cookies and asks again)
  and merges it into `trader/state/nse-holidays.json`. A year with no list is
  said plainly, and weekdays then count as trading days. `extra_holidays` in
  the config adds dates by hand; a year counts as known only once NSE's list
  is cached or the config names it in `holiday_years`.
- **Special sessions.** Muhurat trading and other one-off sessions come from
  `special_sessions` in the config.
- **Square-off.** Zerodha squares off MIS at 15:25 and charges ₹50 + GST an
  order for it, which is why the plan's flat-by is 15:10.

## Quotes

- Yahoo chart JSON for SYMBOL.NS (`range=1d&interval=5m`), an unofficial free
  endpoint, one request per symbol, half a second apart. On a 429 it waits and
  tries once more; on a 429 or a 5xx after that it tries `query1`. Symbols
  are bare NSE symbols (`RELIANCE`, `M&M`); `.NS` is added for Yahoo only.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time
  (UTC), and nse-risk only uses a fresh one.
- If Yahoo is down and the user has a paid Kite Connect plan, read the price
  with the Kite MCP `get_ltp` and write it into `trader/prices.json` as
  `{"as_of": "...Z", "prices": {"RELIANCE": 1339.7}}`.

## Steps

1. Right after nse-exit writes the plan, run `add --from-plan`. Confirm the
   three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the SL sell
   has probably triggered: check the order book and record the fill with
   nse-broker. A target alert means: cancel the stop, then sell. "Flat by"
   means close anything still open: nse-broker `flatten`, then nse-risk,
   then the user's **yes**. Never act for them.

## Example: RELIANCE, Friday 13 Nov 2026

Entry ₹1,334.00 at 10:05 IST, stop ₹1,328.80, target ₹1,344.40, flat by
15:10. `add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target`
and `reliance-flat-by` for that day. At 15:10 IST:

```
NSE OPEN (2026-11-13 15:10 IST, normal market).
ALERT Flat by: it is 15:10 IST (set for 15:10), the market closes at 15:30. If RELIANCE is still open, cancel
its stop, then sell: nse-broker's ticket.py flatten --symbol RELIANCE builds the sell ticket, then nse-risk and
your yes. Zerodha squares off MIS at 15:25 and charges ₹50 + GST an order for it.
```

On Saturday 14 Nov, `clock` says the NSE is closed (Saturday), the next open
is Mon 2026-11-16 09:15 IST, the next holidays are Tue 2026-11-24 (Prakash
Gurpurb Sri Guru Nanak Dev) and Fri 2026-12-25 (Christmas), and that the 2027
list is not known yet.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level in rupees, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is live,
never guesses a holiday list it does not have, and never predicts where a
price is going.

## Safety

- Education and tooling, not financial advice. A PASS from nse-risk is a rules check, not a recommendation.
- Paper trading by default (`"broker": "paper"`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk passed that
  exact order today, the user typed an explicit **yes** for that one order in the chat, and the user clicked
  Allow once on Claude's own card for the Kite order tool.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the MCP server's own config, never in the chat.
