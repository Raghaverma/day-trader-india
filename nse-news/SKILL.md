---
name: nse-news
description: >-
  News Monitor for Indian stocks on NSE. Tracks every stock you watch or hold
  and flags what moves it: NSE corporate announcements (the official exchange
  filings: results, board meetings, order wins, acquisitions, credit ratings,
  pledges, resignations), Google News headlines searched by company name, the
  board meetings and results dates coming up on NSE's event calendar, and how
  far each stock is from its previous close in ₹. Shows only what is new since
  the last check, newest first, in IST, tags each item (results, order win,
  deal/merger, SEBI/RBI regulatory, promoter pledge, block deal, credit
  rating, circuit/halt, macro...) and never repeats one. Use when the user
  says "any news on RELIANCE", "why is TCS moving", "what's the news on my
  watchlist", "morning briefing before the Nifty opens", "pre-open news",
  "check the NSE filings", "did anything break", "when does Infosys report",
  or after /nse-scanner finds setups.
argument-hint: "[NSE symbols]"
---

# nse-news

A chart can look perfect and still be about to gap on a results date, a
pledge invocation or a SEBI order. Before a setup becomes a trade, this skill
reads the two feeds that carry those first, for every stock on the
watchlist, and tells you only what is new. It also prints how far each stock
is from its previous close and any board meeting NSE has on its calendar for
it, so a filing, a headline, a move and a results date sit side by side.

Tool in this folder (standard library Python, no keys, no accounts):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                              # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 24
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-google --json           # NSE filings only
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers INFY --all --hours 168
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (nse-scanner); bare NSE symbols like `RELIANCE` |
| `trader/plan.json` | the trade you are planning (nse-exit) |
| `trader/portfolio.json` | what you hold (nse-broker records fills there) |
| NSE corporate announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE&from_date=..&to_date=..`, only the window asked for |
| Google News RSS | `https://news.google.com/rss/search?q="Reliance Industries" when:3d&hl=en-IN&gl=IN&ceid=IN:en` (personal, non-commercial use) |
| NSE event calendar | `https://www.nseindia.com/api/event-calendar?index=equities`: upcoming board meetings and their purpose |
| Yahoo Finance chart | 5 daily bars of `RELIANCE.NS`: the last price against the previous session's close (unofficial, can lag) |
| `trader/cache/EQUITY_L-<date>.csv` | NSE's list of every listed equity, for company names, cached for the day |
| `trader/state/news.json` | what has been shown (kept 30 days) and each symbol's company name |

Google News is searched by the company's name, which comes from NSE itself
(`sm_name` in the announcements, else `EQUITY_L.csv`). Headlines whose title
does not name the company (by symbol, name, a distinctive first word such as
"Reliance", or initials such as "RIL"), and price-quote or "share price
prediction" pages, are dropped; `--json` counts both.

## The rules

- **Filings beat headlines.** An NSE announcement is what the company told the
  exchange under SEBI's listing rules. If a headline and a filing disagree,
  the filing wins. Open the PDF before saying what it says.
- **Headlines are leads, not facts.** Open the story before telling the user
  something happened. Quote the source and link it. Never summarise a
  paywalled story you could not read: say so.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags: results, board meeting, dividend/bonus/split/buyback, order win,
  deal/merger, analyst/brokerage, regulatory (SEBI, RBI, CCI, NCLT and other
  regulators or courts), promoter/pledge/stake, block deal, credit rating,
  management change, circuit/halt, macro (RBI policy, Budget, FII flows),
  fund raising and investor meet. Every filing cites SEBI's LODR
  regulations, so that boilerplate is ignored when tagging.
- **`!` marks filings that can move a stock:** financial results, the outcome
  of a board meeting, a credit rating, a resignation or appointment, an
  acquisition, merger or restructuring, an order win, a pledge, a suspension,
  a dividend, bonus, split or buyback.
- **Results dates come first.** The "Coming up" block shows each of your
  symbols on NSE's board meeting calendar: "SANDHAR reports on Mon 16 Nov
  2026 (in 2 days)". A results announcement usually lands after 15:30 IST,
  so an intraday position is not exposed to it but anything held overnight is.
- **MOVING** marks a stock 3 % or more from its previous close
  (`--move-pct`). A move with no filing and no headline is worth saying out
  loud too. On a weekend or holiday the line shows the last session.
- **No predictions.** Report what was published and how far the price is from
  the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new, 2 means no symbols or every source failed. `--all` shows
   items already seen, `--json` gives everything machine readable,
   `--dry-run` leaves the state alone, `--hours` sets the window (default 72,
   so a Monday pre-open check still sees Friday evening's filings).
2. Read the items that matter: every `!` filing, anything tagged results,
   circuit/halt, regulatory, promoter/pledge/stake or credit rating, and every
   MOVING symbol first. Open the PDF link of each filing you mention.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, any date coming up, and one line on why it matters to the setup (for
   example "results are on Monday: an overnight gap can jump past any stop").
4. If something changes the picture, say so before `/nse-entry` and
   `/nse-exit`: a results date inside the trade, a pledge invocation, a SEBI
   order, a move to a 5 % band or surveillance. Never act on a headline alone.

During the session: `/loop 15m /nse-news`, or a scheduled task each morning
before 09:00 IST (pre-open starts at 09:00, the market opens at 09:15).

## Example: Saturday 14 Nov 2026

`news.py --tickers RELIANCE TCS` at 21:30 IST (real data). The price line:
`RELIANCE ₹1,339.70 +0.00% vs previous close ₹1,339.70 (Fri 13 Nov 2026 15:28
IST)`. The newest NSE filing is Reliance's 18:25 IST notice that its
executives meet investors at the Morgan Stanley Asia-Pacific Summit on 18 and
19 Nov (tag: investor meet). Headlines lead with the ₹43,000 crore Andhra
Pradesh coal-gasification plan (Reuters, Business Standard, CNBC TV18) and
"Reliance Industries gets 25-year approval to lay overhead transmission line
in Gujarat" (regulatory). Earlier in the month, `--hours 744` shows the 30 Oct
"Outcome of Board Meeting" (`!`, results, board meeting, fund raising: Q2
results and an enabling resolution for up to ₹15,000 crore of NCDs; NSE
posted it twice and it shows once) and two credit rating filings (`!`). TCS
has no filing in the last three days. The brief: "No results date inside a day trade
for either stock; Reliance's moves this week are about the coal-to-gas plan,
not a filing." When a symbol has no news, say so rather than padding.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline or a filing.
- Never hammers a server: one NSE request per symbol for the window only,
  one calendar request a run, one Google News and one chart request per
  symbol, a pause between calls, and NSE's cookie dance only when it answers
  401/403.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/nse-broker`, after `/nse-risk`
passes and the user types an explicit yes for that order.
