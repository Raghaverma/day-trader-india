---
name: nse-deals
description: >-
  Big-Money Tracker for NSE. Reads NSE's bulk and block deal files every
  evening (who bought or sold a large chunk of a stock, by name, quantity and
  price), keeps the history NSE's file does not, and alerts once per new deal.
  Filters by symbol, client (a follow list like "HDFC Mutual Fund"), side and
  minimum ₹ crore; flags your stocks; says when a file could not be read. Use
  when the user says "any bulk deals today", "who bought MEESHO", "block deals
  on NSE", "what are mutual funds buying", "did any FII buy", "track big
  money", "follow Societe Generale's deals", or "run the deals skill".
argument-hint: "[show|new|history] [--symbol SYM] [--client NAME] [--min-value 25cr]"
---

# nse-deals

When one client's trades in a stock add up to more than half a percent of
the company in a day, or when someone trades ₹25 crore or more in one go in
the block window, NSE publishes the name, the side, the quantity and the
price that evening. This skill reads those two files, remembers them (NSE
only keeps the latest day in the file), and tells you once about each deal
that matters to you.

It is fast. It is not early. The deals are published after the close, so by
the time anyone reads them the trade is done. There is no inside information
here: only public disclosures, read quickly. Say that every time.

Tool in this folder (standard library Python, no keys, no accounts):

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" --help
```

## The definitions (verified 14 Nov 2026)

- **Bulk deal:** all transactions in a stock on an exchange where the total
  quantity one client bought or sold that day is more than **0.5% of the
  company's listed equity shares**. It can build up through many trades in
  the normal market, so the price NSE shows is a weighted average. Brokers
  report it and the exchange publishes it the same day, after market hours.
  Source: SEBI circular SEBI/MRD/SE/Cir-7/2004 (14 Jan 2004), paragraph 1.1,
  as quoted in SEBI's block deal guidelines,
  <https://www.sebi.gov.in/legal/circulars/sep-2005/guidelines-for-execution-of-block-deals-on-the-stock-exchanges_8382.html>.
- **Block deal:** a single trade of **at least ₹25 crore** in a separate
  window: the **morning window 08:45-09:00 IST**, priced against the previous
  day's close, or the **afternoon window 14:05-14:20 IST**, priced against
  the VWAP of 13:45-14:00 (published between 14:00 and 14:05). Orders must
  be within **±3%** of that reference price, every trade must end in
  delivery (no squaring off or reversing), and the exchange publishes the
  client's name, quantity and price the same day after market hours. The
  same rules apply to the optional T+0 block window. Source: SEBI circular
  SEBI/HO/MRD/POD-III/CIR/P/2025/134, "Review of Block Deal Framework",
  8 Oct 2025, applicable from its 60th day (early December 2025),
  <https://www.sebi.gov.in/legal/circulars/oct-2025/review-of-block-deal-framework_97145.html>.
  Before it (the October 2017 framework): a ₹10 crore minimum and ±1%.
  Many websites still quote the old numbers.

A large block trade can show up in both files (the MEESHO sale on 13 Nov
2026 did: 7.64 crore shares, also over 0.5% of the company). The tool shows
it once, as the block deal, and says it was also a bulk deal.

## What it reads and writes

- Reads `https://nsearchives.nseindia.com/content/equities/bulk.csv` and
  `.../block.csv`: Date, Symbol, Security Name, Client Name, Buy/Sell,
  Quantity Traded, Trade Price / Wght. Avg. Price (bulk also has Remarks).
  Each file is the latest session only.
- Writes `trader/state/deals.json`: every deal seen in the last 90 days,
  deduplicated, and which ones have alerted, so each deal alerts once.
- Reads `trader/portfolio.json` (nse-broker) and `trader/watchlist.json`
  (nse-scanner) to flag deals in your stocks, and `trader/config.json` for
  `"follow_clients": ["hdfc mutual fund", "societe generale"]` (name
  substrings) and `"deals_min_value_inr"` (default ₹1 crore). The config is
  optional: one line on stderr says which file was used, or that the built-in
  defaults are (no follow list, ₹1 crore), so a config that was not found is
  never a silent difference. A portfolio or watchlist that is there but
  broken is a `note:` line, not an empty "your stocks".
- `trader/` is the one trader folder `nse_common` finds (`$NSE_TRADER_HOME`,
  else the nearest `trader/` with a `config.json` above the working folder,
  else `~/.day-trader-india`), so state and config are the same wherever Claude
  Code was started. The check that the files are the latest session's uses
  NSE's holiday list, refreshed when it is stale (best effort).

## The rules

- **Quote the file, not a tracker.** Every row comes from NSE's CSV with its
  date. Name the client exactly as NSE prints it.
- **Value is quantity × price**, shown in ₹ crore (or lakh). A bulk deal's
  price is an average across trades, not one print.
- **Client types are a guess from the name:** "mutual fund", "insurance",
  "bank", "foreign fund" (legal forms and places abroad, sovereign and
  pension money, the global houses), otherwise "other". Many "other" names
  are proprietary trading firms. Check the name before saying who it is.
- **Round trips are not positions.** A client who bought and sold the same
  stock the same day (most high-frequency firms in the bulk file) is hidden
  by default; `--round-trips` shows them.
- **A file that failed is not "no deals".** An empty body, a web page, the
  wrong columns or rows none of which parse make that file fail: the header
  says `INCOMPLETE: block.csv could not be read, so this covers bulk deals
  only.`, `(block.csv NOT READ, 136 bulk)` replaces "0 block", and a `note:`
  gives the reason. Rows that do not parse among good ones are counted in a
  note. Say it to the user: "no block deals" is only true when block.csv was
  read. A day with only NSE's "NO RECORDS" row, or only the header, is a real
  empty day.
- **A deal is history, not a signal.** It says what someone did yesterday,
  not what they or anyone will do next. No return claims.

## Step 1: what happened today

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" show
python3 "${CLAUDE_SKILL_DIR}/deals.py" show --min-value 25cr --side BUY
python3 "${CLAUDE_SKILL_DIR}/deals.py" show --symbol MEESHO --min-value 0
python3 "${CLAUDE_SKILL_DIR}/deals.py" show --type "mutual fund" --type insurance
python3 "${CLAUDE_SKILL_DIR}/deals.py" show --mine          # only your stocks
```

`show` reads both files, adds them to the history and prints the deals that
pass every filter, biggest first. `--kind bulk|block`, `--client NAME`
(repeat it; it adds to `follow_clients`), `--any-client` ignores the follow
list, `--json` for everything machine-readable.

## Step 2: alert once

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" new
python3 "${CLAUDE_SKILL_DIR}/deals.py" new --client "mutual fund" --min-value 10cr
```

Shows only deals that have not alerted before, then records them. NSE posts
the files in the evening, so one run a day after 18:00 IST on trading days is
plenty: `/loop` or a scheduled task. A note says when the files are older than
the last session. The header says "since 2026-11-14 10:00 IST", the last
check in IST; when a file could not be read the check does not move that time
forward.

Exit codes: 0 deals shown, 1 nothing matched (every file was read), 2 usage,
or no file could be read at all, 3 nothing shown and one file could not be
read (the output says which: treat it as "not checked", not "nothing new").

## Step 3: the history

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py" history --days 30 --client "societe generale"
```

Everything kept in `trader/state/deals.json`, with the same filters. NSE's
own archive (`historical/bulk-deals`) answered 503 when checked, so the
history only holds the days this skill has seen.

## Example: Friday 13 Nov 2026 (real data)

`deals.py show` (default filters: ₹1 cr and up, round trips hidden) shows
37 of 162 rows. It leads with a block deal: SVF II MEERKAT (DE) LLC
[foreign fund] sold 7,64,00,000 MEESHO at ₹246.75, ₹1,885 cr, "also a bulk
deal". On the other side: Societe Generale bought 1,47,78,000 (₹365 cr),
Fidelity funds, Morgan Stanley Asia Singapore, the Government of Singapore
and Norges Bank [foreign fund], Motilal Oswal, Aditya Birla Sun Life, HDFC
and Edelweiss Mutual Fund [mutual fund], and Bajaj Allianz Life Insurance
[insurance]. In the bulk file: three sellers of IIFLCAPS (₹66 to ₹84 cr each)
and ASK Investment Managers selling RALLIS (₹24.24 cr). Then: "These are
public disclosures from Friday's close. They say who traded MEESHO on
Friday, not what anyone will do on Monday."

If the user wants to act on it, it goes the long way: a deal is a reason to
look at a chart, not a setup. `/nse-scanner` and `/nse-entry` decide if there
is a trade today; then cash equity only, sized by `/nse-budget`, checked by
`/nse-risk`, placed by `/nse-broker` only after the user's explicit yes.

## What it never does

- Never says or implies a client has inside information, or that copying
  big money beats the market.
- Never presents a guessed client type as fact.
- Never places an order.
- Never hammers NSE: two small files a run.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything.
- Paper trading by default (`"broker": "paper"` in `trader/config.json`).
- Nothing places an order except `/nse-broker`, and only after `/nse-risk`
  passes AND the user types an explicit yes for that order in the chat.
  With the Kite MCP, the order tool stays on "Needs approval" so Claude's own
  "Allow once / Deny" card appears too. Never "Always allow" an order tool.
- Long side, cash equity, MIS only, unless the config allows more and the
  user confirms.
- Bulk and block deals are PUBLIC disclosures that NSE publishes after the
  close. This skill reads them fast; it has no inside information, and it
  says so.
