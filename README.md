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
trade goes anywhere until you type **yes** for that exact order.

## Install

As a plugin, in Claude Code, from a clone of this repo:

```
/plugin marketplace add /path/to/day-trader-india
/plugin install day-trader-india@day-trader-india
```

Installed as a plugin, the skills are namespaced: `/day-trader-india:nse-scanner` and so on.
Copy the folders instead if you want plain `/nse-scanner`. Each folder carries its own copy
of `nse_common.py`, so it works on its own:

```bash
cp -r day-trader-india/nse-* ~/.claude/skills/
```

The tools need Python 3.8 or newer. There's nothing to pip install.

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

Everything lands in a `trader/` folder in the directory you run Claude Code from, and each
skill reads what the last one wrote. Start Claude Code in a folder you keep for this.

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
   triggered: the first five-minute close above the range, at 1,334.00.
4. **`/day-trader-india:nse-exit`** set the plan before the entry. The stop goes at the
   range's midpoint: 1,328.85, rounded down to RELIANCE's ₹0.10 tick, so 1,328.80. That
   puts ₹5.20 a share at risk. The target is two times the risk, 1,344.40, and the trade is
   flat by 15:10 either way. Charges come to about ₹1.32 a share for the round trip, so
   break-even is 1,335.40.
5. **`/day-trader-india:nse-timemachine`** replayed the rest of the day 10,000 times (seed
   19) from RELIANCE's own five-minute bars over the previous 58 sessions:

   | outcome | share of replays |
   | --- | --- |
   | target first | 22.6% |
   | stop first | 69.1% |
   | neither by 15:10 | 8.3% |

   It also re-ran the same rule on those 58 real sessions. The rule fired 31 times: 7 hit
   the target, 15 the stop and 9 neither, for an average of −0.03R before charges and
   −0.28R after. Its verdict: leans toward the stop. That is the point of looking before you
   risk a rupee.
6. **`/day-trader-india:nse-budget`** with a ₹1,00,000 account and a ₹500 max loss per trade:
   - The max-loss limit allows 79 shares once charges are counted, but the 75% position cap
     binds at **56 shares** (₹74,704).
   - At the stop you'd lose ₹291.20, plus ₹73.62 in charges, so ₹364.82.
   - It writes one order: BUY 56 RELIANCE, LIMIT 1,334.00, product MIS. Once that fills,
     the stop goes in: SELL 56, SL trigger 1,328.80, limit 1,322.10.
   - The target is an alert, not an order. Kite has no OCO for intraday, so a resting stop
     and a resting target could both fill.
7. **`/day-trader-india:nse-risk`** checks the order against your limits: max loss with
   charges, reward:risk after charges, the daily loss limit, trades per day, the entry
   window, series EQ, the circuit band, surveillance, position size and cash. It answers
   PASS or BLOCK, with the reason.
8. **`/day-trader-india:nse-broker`** shows the ticket in plain words and records your yes.
   On the paper broker at 10:05, RELIANCE filled at 1,334.00 and hit the target on the 10:13
   bar: ₹582.40 gross, ₹73.87 charges, **₹508.53 net**. One minute later it would have gone
   differently. At 10:06 the price was 1,336.00, and it never came back down to 1,334.00 that
   day, so the same LIMIT order would have sat unfilled. One good day proves nothing. The
   Time Machine's 31 past sessions are the better guide.
9. **`/day-trader-india:nse-alerts`** rings on the stop, on the target ("cancel the stop,
   then sell"), and at 15:10 to flatten whatever is still open. That's before Zerodha's
   15:25 auto square-off and its ₹50 + GST fee.

## Brokers

- **paper** (default): a local paper broker driven by Yahoo's 1-minute bars. It fills,
  stops out, takes profit and flattens, and records P&L net of charges. No account, no money.
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
python3 nse-budget/size.py --write                                # -> trader/orders.json
python3 nse-risk/check.py                                        # PASS / BLOCK per order
python3 nse-broker/ticket.py show                                # the tickets, READY or not
python3 nse-broker/ticket.py paper-fill --index 1                # paper broker
python3 nse-alerts/alerts.py add --from-plan                     # stop, target, flat by 15:10
python3 nse-alerts/alerts.py clock                               # is the NSE open? next open, holidays
python3 nse-alerts/alerts.py holidays --refresh                  # NSE's holiday list for a new year
python3 nse-analyst/results.py RELIANCE                          # quarters, YoY, TTM, ₹ crore
python3 nse-deals/deals.py show                                  # today's bulk and block deals
```

Pass `--now "2026-11-13 10:05"` to any tool to pin the clock (IST).

```bash
cd tests && python3 -m unittest discover -v
```

The tests run offline, on fixtures cut from real data from Friday 13 November 2026:
RELIANCE's five-minute bars for 60 sessions, the Nifty 50's bars that day, NSE's symbol
lists, circuit bands and holiday list, NSE announcements and Google News for RELIANCE and
TCS, NSE XBRL results for RELIANCE and HDFC Bank, and that day's bulk and block deals.
They reproduce every number in the example above.

## Fine print

**Not financial advice.** This is software that reads public data and does arithmetic. It
doesn't know your situation and it can't see the future. SEBI's own
[study of intraday trading in the equity cash segment](https://www.business-standard.com/markets/news/7-in-10-intraday-traders-in-equity-cash-suffered-losses-in-fy23-sebi-study-124072400975_1.html)
(July 2024) found that 71% of individual intraday traders lost money in FY2022-23, and
that the losers paid trading costs equal to another 57% of their trading losses. That's
why every number here counts charges. You can lose money. Decide for yourself, and talk to
a SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `nse-broker` can lead to an order, and
only after `nse-risk` passes that exact order (its legs, its target and its flat-by time)
and you type yes for it in the chat. In kite mode, set the order tools to ask every time,
never "always allow", so Claude's own Allow-once card is a second check. No shorting, no
F&O and no MIS leverage unless you turn them on.

**Charges are estimates.** Defaults are Zerodha's published equity-intraday rates: brokerage
0.03% or ₹20 per order (lower), STT 0.025% on the sell side, NSE transaction 0.00307%, SEBI
₹10/crore, stamp duty 0.003% on the buy side, and GST 18%. Your broker's contract note is
the truth. Override the rates under `charges` in `trader/config.json`.

**Ticks and bands change.** NSE resets each stock's tick size monthly from the previous
month's close, and moves stocks between circuit bands and surveillance stages. The tools
read NSE's current lists, but near a slab edge, check your broker.

**Holidays.** NSE's 2026 list is built in. NSE publishes the next year's list around
December; run `alerts.py holidays --refresh` once it's out. Until then, the tools say plainly
that the year's list is unknown. Muhurat trading and other special sessions go under
`special_sessions` in the config.

**The Time Machine is a simulation, not a prediction.** It's built from the stock's own
last 60 sessions of five-minute bars. A results day, a block deal or a circuit can do things
those sessions never did. Treat its odds as a reality check, never a promise.

**A stop is a plan, not a guarantee.** An SL order can fill well past its trigger in a fast
market, or not at all if the price jumps through its limit. The max loss shown is the loss
at the trigger.

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
(every folder)    nse_common.py, the shared NSE clock, ticks, charges and rupees
PORTING.md        how the port was done, and the India facts it rests on
tests/            unittest suite + offline fixtures
```

MIT. Original US pack by [Jake Schincariol](https://opusjake.ai); India port by Raghav Verma.
