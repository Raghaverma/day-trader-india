# The Day Trader skill, India edition

11 Claude skills for day trading Indian stocks on the NSE: scan the Nifty for breakouts, read exchange filings, time every entry and exit, and replay every outcome before you risk a rupee.

A port of [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT) from US stocks to India's NSE cash market. It runs on the IST clock and NSE's holiday
list, puts every price on an NSE tick, and counts Indian intraday charges in every number.
Order tickets are written in Zerodha Kite's shape, and a built-in paper broker means you
need no broker account at all.

Free, MIT, no signup, no API key. The tools are plain Python that read free public data:
Yahoo Finance charts for `.NS` symbols, NSE's archives and announcements, and Google News.

Nothing in this pack places an order on its own. Paper trading is the default, and no
trade goes anywhere until you type **yes** for that exact order. How firmly that is
enforced depends on the broker mode: see "What is enforced where" under Fine print.

## Install

As a plugin, in Claude Code, from a clone of this repo:

```
/plugin marketplace add /path/to/day-trader-india
/plugin install day-trader-india@day-trader-india
```

Installed as a plugin, the skills are namespaced: `/day-trader-india:nse-scanner` and so on,
and the plugin's three hooks come with it (the order gate for kite mode, a recorder for your
last message, and a start-of-session warning about trades with no stop). Hooks need Claude
Code 2.1.295 or newer for the gate's fail-closed setting.

Copy the folders instead if you want plain `/nse-scanner`. Each folder carries its own copy
of `nse_common.py`, so it works on its own, but **hooks do not travel with copied folders**,
so kite mode refuses to approve an order without them:

```bash
cp -r day-trader-india/nse-* ~/.claude/skills/
```

The tools need Python 3.8 or newer. There's nothing to pip install.

## First run: set your rules

The tools that guard money (`nse-budget`, `nse-risk`, `nse-broker`) refuse to run without a
`config.json`, because falling back to built-in limits would quietly change what you risk.
Tell Claude your budget and max loss, or run it yourself:

```bash
python3 nse-budget/size.py --init-config --budget 100000 --max-loss 500 --max-daily-loss 1500
```

That writes the config (paper broker) to your trader folder and never overwrites an existing
one. The trader folder is found the same way from wherever you start Claude Code: `$NSE_TRADER_HOME`,
else the nearest `trader/` folder (with a `config.json`) in the working folder or above it,
else `~/.day-trader-india`.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/day-trader-india:nse-scanner` | Chart Scanner | Scans the Nifty 50, the Nifty 500, the F&O list or every EQ stock for setups: opening-range breakouts, 20-day breakouts, flags, oversold bounces. Drops trade-for-trade, 2%/5%-band and GSM stocks. |
| `/day-trader-india:nse-news` | News Monitor | NSE corporate announcements (the official filings), Google News headlines and upcoming board meetings for every stock you watch. Shows only what's new, tagged and in IST. |
| `/day-trader-india:nse-entry` | Entry Timer | Watches a setup bar by bar from 09:15 and calls the entry on the first close above the opening range. |
| `/day-trader-india:nse-exit` | Exit Planner | Sets the stop and target before you enter, on the NSE tick grid, with charges per share and the break-even. |
| `/day-trader-india:nse-timemachine` | Time Machine | Replays the rest of the day 10,000 times from the stock's own bars, plus every past session the setup fired, before and after charges. |
| `/day-trader-india:nse-risk` | Risk Manager | Checks every order against your limits and India's rules (series, circuit band, surveillance, entry window, charges) and stops anything over them. |
| `/day-trader-india:nse-budget` | Budget Manager | Sizes the position to your rupee budget and max loss, charges included, and writes one Kite-shaped order. |
| `/day-trader-india:nse-broker` | Order Executor | Paper broker by default. In manual mode it gives the exact order to type into Kite; with a self-hosted Kite MCP it places the order, never without your yes. |
| `/day-trader-india:nse-alerts` | Price Alerts | Rings on your stop, your target, the 09:15 bell and your flat-by time, on the NSE clock with its holidays. |
| `/day-trader-india:nse-analyst` | Earnings Analyst | Reads quarterly results from NSE's XBRL filings and sums up what changed, year on year, in ₹ crore. |
| `/day-trader-india:nse-deals` | Big-Money Tracker | Reads NSE's bulk and block deals, follows the funds you name, and flags deals in stocks you hold. |

## How to use it

Everything lands in your `trader/` folder (see "First run"), and each skill reads what the
last one wrote.

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:10                                           (while it is open)
news / analyst / deals                                                         (on a schedule)
```

An example with real data: RELIANCE, Friday 13 November 2026.

1. **`/day-trader-india:nse-scanner`** at 10:05 IST: 31 of the Nifty 50 had closed a
   five-minute bar above their 15-minute opening range (34 by the end of the day). RELIANCE
   is one of them: its range ran from 1,324.30 to 1,333.40, and the 10:00 bar closed at
   1,334.00.
2. **`/day-trader-india:nse-news`**: RELIANCE's latest NSE filings and headlines, in IST,
   tagged, plus any board meeting coming up. A results date this week is a reason to stand
   aside.
3. **`/day-trader-india:nse-entry`** watched RELIANCE bar by bar. At 10:05 it said
   triggered: the first five-minute close above the range, at 1,334.00. It only triggers once
   the whole opening range is in (a missing bar means "waiting", never a guessed high), and it
   stamps when the trigger happened.
4. **`/day-trader-india:nse-exit`** set the plan before the entry. The stop goes at the
   range's midpoint: 1,328.85, rounded down to the ₹0.10 order tick, so 1,328.80. That
   puts ₹5.20 a share at risk. The target is two times the risk, 1,344.40, and the trade is
   flat by 15:10 either way. Charges come to about ₹1.42 a share for the round trip, so
   break-even is 1,335.50. A trigger more than 30 minutes old is refused (the price has
   moved): give `--entry PRICE` or `--allow-late` if you mean it.
5. **`/day-trader-india:nse-timemachine`** replayed the rest of the day 10,000 times (seed
   19) from RELIANCE's own five-minute bars over the previous 58 sessions:

   | outcome | share of replays |
   | --- | --- |
   | target first | 22.6% |
   | stop first | 69.1% |
   | neither by 15:10 | 8.3% |

   It also re-ran the same rule on those 58 real sessions. The rule fired 31 times: 7 hit
   the target, 15 the stop and 9 neither, for an average of −0.03R before charges and
   −0.29R after. Its verdict: leans toward the stop. That is the point of looking before you
   risk a rupee.
6. **`/day-trader-india:nse-budget`** with a ₹1,00,000 account and a ₹500 max loss per trade.
   The max loss is measured where the stop can actually fill. The stop leg is an SL order
   with its limit 0.5% under the trigger (1,322.10), so the worst fill loses ₹11.90 a share,
   not ₹5.20:
   - The max-loss limit allows **37 shares** (₹49,358): 37 × 11.90 = ₹440.30, plus ₹52.18 in
     charges, so ₹492.48 at the worst fill. The other limits would allow 56 (the 75% position
     cap), 59 (max order) and 67 (cash after the buffer). At 56 shares the worst fill loses
     ₹739.91, over your ₹500.
   - It writes one order: BUY 37 RELIANCE, LIMIT 1,334.00, product MIS. Once that fills,
     the stop goes in for the shares that filled: SELL 37, SL trigger 1,328.80, limit 1,322.10.
   - At the target you'd make ₹384.80 less ₹52.71 charges, ₹332.09: reward:risk 1.36 after
     charges to the stop's trigger (0.67 if the stop fills at its limit, shown beside it).
   - The target is an alert, not an order. Kite has no OCO for intraday, so a resting stop
     and a resting target could both fill.
7. **`/day-trader-india:nse-alerts`** `quote RELIANCE --write` fetches a fresh price, then
   **`/day-trader-india:nse-risk`** checks the order against your limits: a quote under five
   minutes old (blocked without one, or if the price is already through the stop), today's
   plan (an order from another day is blocked), max loss at the worst fill with charges,
   reward:risk after charges, the daily loss limit, trades per day, the entry window, series
   EQ, the circuit band, surveillance, position size and cash. It answers PASS or BLOCK, with
   the reason, and a PASS is good for five minutes.
8. **`/day-trader-india:nse-broker`** shows the ticket in plain words (with the order's age)
   and records your yes, which has to be the words you actually typed. It re-checks the
   clock and the PASS when you approve. On the paper broker at 10:05, RELIANCE filled at
   1,334.00. The 10:13 bar reached the 1,344.40 target, and because on Kite the target is only
   an alert followed by a market sell, the paper broker sells at the next bar's open,
   1,344.30: ₹381.10 gross, ₹52.70 charges, **₹328.40 net**. One minute later it would have
   gone differently. At 10:06 the price was 1,336.00, and it never came back down to 1,334.00
   that day, so the same LIMIT order would have sat unfilled. One good day proves nothing. The
   Time Machine's 31 past sessions are the better guide.
9. **`/day-trader-india:nse-alerts`** rings on the stop, on the target ("cancel the stop,
   then sell"), and at 15:10 to flatten whatever is still open. A target or stop touched
   between two polls still rings, from the five-minute bar's high or low. That's before
   Zerodha's 15:25 auto square-off and its ₹50 + GST fee.

## Brokers

- **paper** (default): a local paper broker driven by Yahoo's 1-minute bars. It fills,
  stops out, takes profit and flattens, and records P&L net of charges. No account, no money.
  It is deliberately not generous: the target fills at the next bar's open (on Kite the target
  is an alert, then a market sell), and a bar that gaps below the stop's limit leaves the stop
  unfilled, loudly, until price comes back or the flat-by time.
- **manual**: the exact order to type into Kite (or any broker): BUY, Intraday (MIS), LIMIT,
  qty, price; then the SL order once it fills.
- **kite**: Claude calls the Kite MCP server's `place_order` after your yes and Claude's own
  Allow-once card. Zerodha's hosted server (`mcp.kite.trade`) does not place regular orders,
  so this needs a self-hosted [zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server)
  with your own Kite Connect key. Since 1 April 2026, API orders must also come from a
  static IP you've whitelisted with Zerodha. See `nse-broker/SKILL.md`.

## The tools

All standard-library Python. The data tools read public endpoints and are polite about it.

```bash
python3 nse-scanner/scan.py universe nifty500                    # NSE's list, minus T2T / 2-5% band / GSM
python3 nse-scanner/scan.py daily                                # pre-market setups
python3 nse-scanner/scan.py orb --at 10:05                       # opening-range breakouts so far
python3 nse-news/news.py --tickers RELIANCE TCS                  # filings, headlines, board meetings
python3 nse-entry/entry.py check RELIANCE --setup orb            # waiting / triggered / expired
python3 nse-entry/entry.py watch RELIANCE --every 30              # bar by bar until it triggers
python3 nse-exit/exit.py plan --from trader/entry.json --write    # -> trader/plan.json
python3 nse-timemachine/timemachine.py run --write                # 10,000 replays + the history
python3 nse-budget/size.py --write                                # -> trader/orders.json (--append: keep other symbols' orders)
python3 nse-alerts/alerts.py quote RELIANCE --write               # a fresh price: nse-risk blocks an entry without one
python3 nse-risk/check.py                                        # PASS / BLOCK per order
python3 nse-broker/ticket.py show                                # the tickets, READY or not
python3 nse-broker/ticket.py paper-fill --index 1                # paper broker
python3 nse-broker/ticket.py status                              # trades with no stop, approvals nobody placed
python3 nse-broker/ticket.py move-stop --symbol RELIANCE --to breakeven   # ticket to raise the stop
python3 nse-broker/ticket.py reconcile --positions FILE          # portfolio.json against Kite's get_positions
python3 nse-broker/ticket.py journal --days 20                   # trades, R-multiples, slippage, win rate
python3 nse-alerts/alerts.py add --from-plan                     # stop, target, flat by 15:10
python3 nse-alerts/alerts.py clock                               # is the NSE open? next open, holidays
python3 nse-alerts/alerts.py holidays --refresh                  # NSE's holiday list for a new year
python3 nse-analyst/results.py RELIANCE                          # quarters, YoY, TTM, ₹ crore
python3 nse-deals/deals.py show                                  # today's bulk and block deals
```

Pass `--now "2026-11-13 10:05"` to the data and planning tools to pin the clock (IST).
`nse-broker` refuses `--now` and `--today` unless `NSE_TEST_MODE=1` is set, so a real session
cannot pin the clock to get an old PASS through.

```bash
cd tests && python3 -m unittest discover -v
```

The tests run offline, on fixtures cut from real data from Friday 13 November 2026:
RELIANCE's five-minute bars for 60 sessions, the Nifty 50's bars that day, NSE's symbol
lists, circuit bands and holiday list, NSE announcements and Google News for RELIANCE and
TCS, NSE XBRL results for RELIANCE and HDFC Bank, and that day's bulk and block deals.
They reproduce the numbers in the example above.

## Fine print

**Not financial advice.** This is software that reads public data and does arithmetic. It
doesn't know your situation and it can't see the future. SEBI's own
[study of intraday trading in the equity cash segment](https://www.business-standard.com/markets/news/7-in-10-intraday-traders-in-equity-cash-suffered-losses-in-fy23-sebi-study-124072400975_1.html)
(July 2024) found that 71% of individual intraday traders lost money in FY2022-23, and
that the losers paid trading costs equal to another 57% of their trading losses. That's
why every number here counts charges. You can lose money. Decide for yourself, and talk to
a SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `nse-broker` can lead to an order, and
only after `nse-risk` passes that exact order (its legs, its target and its flat-by time,
within the last five minutes, with a fresh quote) and you type yes for it in the chat. No
shorting, no F&O and no MIS leverage unless you turn them on.

**What is enforced where.** Be clear about this before you go live.

- **paper**: enforced in code. A yes has to start with "yes" and may name only this order's
  symbol or share count ("yes for TCS" and a bare "y" are refused), it has to be the words you
  typed, and one yes gives one paper fill.
- **kite**: enforced by the plugin's PreToolUse hook, which only exists when you install this as
  a plugin. The hook denies any Kite `place_order`, `modify_order` or `cancel_order` call that does
  not match, field for field, a leg you approved moments ago, and each approved leg works once.
  `approve` also refuses if your last message was not recorded (a sign the hooks are not
  installed). The hook only ever denies: set the order tools to "Needs approval", never "always
  allow", so Claude's own Allow-once card is a second gate. GTT orders are always denied.
- **manual**: you place the order with your own hands in Kite. Nothing is enforced.

What the gate cannot do: it covers MCP order tools only, so a Bash or curl call to the Kite API
is not gated. It cannot tell a `/loop` or scheduled run from you at the keyboard, and the "your
last message" record can hold text that was not typed by you (a `/loop` iteration, a subagent's
report). Never run the broker from a loop. An approved leg is spent when the gate allows the call,
so if you click Deny on the Allow-once card you need a new yes.

**Charges are estimates.** Defaults are Zerodha's published equity-intraday rates: brokerage
0.03% or ₹20 per order (lower), STT 0.025% on the sell side, NSE transaction 0.00307%, SEBI
₹10/crore, stamp duty 0.003% on the buy side, and GST 18%. Your broker's contract note is
the truth. Override the rates under `charges` in `trader/config.json`.

**Ticks and bands change.** NSE resets each stock's tick size monthly from an earlier close,
not from today's price, and moves stocks between circuit bands and surveillance stages. Near a
slab edge (₹250, ₹1,000, ₹5,000 and so on) the tools put every order price on the coarser of
the ticks found within 10% of the price, which is valid under either, and `tick_sizes` in the
config pins a stock's real tick. The tools read NSE's current lists for series, bands and
surveillance.

**Holidays.** NSE's 2026 list is built in. When a year's list is missing (NSE publishes the next
year's around December) or the cached list is more than 30 days old, the tools fetch it
themselves, best effort. `alerts.py holidays --refresh` does it by hand. If the year is still
unknown, `nse-risk` blocks new entries rather than guessing, and says how to fix it. Muhurat
trading and other special sessions go under `special_sessions` in the config.

**The Time Machine is a simulation, not a prediction.** It's built from the stock's own
last 60 sessions of five-minute bars. A results day, a block deal or a circuit can do things
those sessions never did. Treat its odds as a reality check, never a promise.

**A stop is a plan, not a guarantee.** An SL order can fill well past its trigger in a fast
market, or not at all if the price jumps through its limit. The max loss per trade is
measured at the SL limit, the worst fill the order allows, charges included
(`"loss_basis": "trigger"` measures at the trigger instead). Past the limit nothing caps the
loss, so keep `stop_limit_buffer_pct` small for a tight stop: a buffer wider than the stop's own
distance, as in the RELIANCE example, is what shrinks the size.

**The data is free and unofficial.** Yahoo's chart endpoint and NSE's website APIs aren't
official feeds: they can lag, change shape or rate-limit you. Yahoo also leaves the last
few minutes of NSE sessions empty. Check anything that matters against your broker before
you act on it.

**Taxes.** Intraday equity profits are speculative business income in India. Nothing here
is tax advice.

**Bulk and block deals are public disclosures,** published by NSE after the session. They
aren't inside information.

## Files

```
nse-scanner/      SKILL.md, scan.py
nse-news/         SKILL.md, news.py
nse-entry/        SKILL.md, entry.py
nse-exit/         SKILL.md, exit.py
nse-timemachine/  SKILL.md, timemachine.py
nse-risk/         SKILL.md, check.py
nse-budget/       SKILL.md, size.py, config.example.json
nse-broker/       SKILL.md, ticket.py, kite-mcp.example.json
nse-alerts/       SKILL.md, alerts.py, alerts.example.json
nse-analyst/      SKILL.md, results.py
nse-deals/        SKILL.md, deals.py
hooks/            hooks.json, gate.py, record_prompt.py, session_status.py (the plugin's hooks)
(every folder)    nse_common.py, the shared NSE clock, ticks, charges, loss and sizing rules
CHANGELOG.md      what changed in each version
PORTING.md        how the port was done, and the India facts it rests on
tests/            unittest suite + offline fixtures
```

MIT. Original US pack by [Jake Schincariol](https://opusjake.ai); India port by Raghav Verma.
