---
name: nse-news
description: >-
  News Monitor for Indian stocks on NSE. Tracks the stocks you watch or hold:
  NSE corporate announcements (results, board meetings, order wins, deals,
  ratings, pledges), Google News headlines by company name, results dates from
  NSE's calendar, and each stock's move from its previous close in ₹. Shows
  only what is new, newest first, in IST, tagged, and says plainly when a
  source failed. dossier.py builds a full stock file. Use when the user says
  "any news on RELIANCE", "why is TCS moving", "morning briefing", "check the
  NSE filings", "when does Infosys report", "tell me everything about INFY",
  "deep scan", or after /nse-scanner.
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

## Everything on one stock: dossier.py

When the user wants all of it ("tell me everything about INFY", "deep scan
RELIANCE", "is there anything I should know before I buy TCS"), build the
stock file instead of the news check:

```bash
python3 "${CLAUDE_SKILL_DIR}/dossier.py" INFY                       # the whole file, as text
python3 "${CLAUDE_SKILL_DIR}/dossier.py" INFY TCS --out trader/stocks  # INFY.md, INFY.json, index.json
python3 "${CLAUDE_SKILL_DIR}/dossier.py" INFY --no-results          # skip the quarterly numbers
```

It reads, for each stock: a year of Yahoo daily prices (moves over a day, a
week, a month, three months and a year; the 52-week range; the 20, 50 and
200-day averages; the average daily range; volume against its 20-day
average), the series and circuit band from NSE's `sec_list.csv`, NSE's board
meetings and corporate actions (dividends, splits, bonus issues, ex-dates),
the latest quarters from `nse-analyst` (`results.py`), 30 days of NSE
filings, a week of Google News headlines that name the stock, NSE's
shareholding pattern (promoter and public, four quarters), pledge data,
insider (SEBI PIT) trades over 90 days, bulk and block deals (NSE's latest
files plus `nse-deals`' history) and NSE's F&O ban list. It starts with
**flags**: a board meeting or ex-date within 14 days, the F&O ban, results
filed in the last two days, a 3% move or twice the usual volume, within 2% of
the 52-week high or low, insiders buying or selling over ₹1 crore on the
market, promoters pledging 5% or more, promoter holding moving a point in a
quarter, a recent bulk or block deal, important filings in the last 3 days.
A source that failed is named in the flags ("Could not read: ...") and in
its section, never shown as "none". NSE's live quote page refuses scripts,
so prices come from Yahoo. Read the flags out first, then the sections the
user asked about; the paper autopilot builds these files for its whole
universe before the open (`"dossier"` in autopilot.json).

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
| `trader/cache/EQUITY_L-<date>.csv` | NSE's list of every listed equity, for company names, cached for the day. `nse-scanner` reads the same file, so it is only written whole and only when it is the real CSV (the SYMBOL, NAME OF COMPANY and SERIES columns, 1,000+ companies): a web page or a cut-off download is never cached, and a bad file left for today is deleted. When NSE fails, the newest earlier good file gives the names. |
| `trader/state/news.json` | what has been shown (kept 30 days), each symbol's company name, and when each symbol's NSE filings and headlines were last read in full |

`trader/` is the one trader folder `nse_common` finds (`$NSE_TRADER_HOME`,
else the nearest `trader/` with a `config.json` above the working folder, else
`~/.day-trader-india`), so the check reads the same files wherever Claude Code
was started. A watchlist, plan or portfolio file that is there but broken is a
`note:` line, never a silent skip of the stocks in it.

Google News is searched by the company's name. It comes from the watchlist hit
when `nse-scanner` wrote one, else from NSE itself (`sm_name` in the
announcements, else the `EQUITY_L` cache above); with no name anywhere the
search uses the symbol and a `note:` says so. Headlines whose title
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
- **A source that failed is not "nothing new".** Every run is tracked per
  source (NSE filings, Google News, the NSE calendar, Yahoo prices) and per
  symbol. If one failed, a line right under the moves says which:
  `checked: NSE filings ok; Google News failed; NSE calendar ok.` Say that to
  the user: "no news from Google" means Google was not read. A failed source's
  items are never marked seen, so they show on the first run that reaches it.
  If NSE filings and Google News both failed, the output starts
  `News check FAILED: could not reach NSE or Google News: nothing was checked.`
  and the exit code is 3: tell the user nothing was checked, never "no news".
  A block page (HTML where JSON or RSS should be), an error object or an empty
  calendar counts as a failure, not as an empty feed.
- **No predictions.** Report what was published and how far the price is from
  the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit codes: 0
   something new, 1 nothing new in what could be checked (read the `checked:`
   line), 2 usage (no symbols, a bad symbol or `--now`), 3 both news feeds
   failed so nothing was checked. `--all` shows items already seen, `--json`
   gives everything machine readable (`status`, `checked_line`, `failure` and
   per-source `sources` are in it), `--dry-run` leaves the state alone.
   The window is 72 hours, so a Monday pre-open check still sees Friday
   evening's filings, and further back when a symbol's feed was last read in
   full longer ago than that (a long weekend, a few days of failures: the run
   says `looking back N hours`). `--hours N` is used exactly as given and is
   capped at 720 unless `--all`.
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
