---
name: nse-analyst
description: >-
  The Results Analyst for Indian companies on NSE. Reads a company's quarterly
  results straight from the XBRL it files with NSE (Integrated Filing -
  Financials, consolidated or standalone) and sums up what changed in ₹ crore:
  revenue from operations, total income, expenses, EBITDA, PBT, net profit to
  owners, diluted EPS and margins, each quarter against the same quarter a year
  earlier and the quarter before, with the last four quarters. Banks get
  interest earned, NII, provisions and gross/net NPA %. Says when the next
  board meeting is on NSE's calendar. Use when the user says "how were
  Reliance's results", "analyse TCS's Q2", "what changed this quarter", "is
  HDFC Bank's NII growing", "check the NPA", "compare margins", "sum up the
  con call", "does it report this week", "when are Infosys results", or before
  trading a Nifty stock around its results.
argument-hint: "[NSE symbols] [--standalone]"
---

# nse-analyst

Results move Indian stocks more than anything else on the calendar, and the
press release puts the best number first. This skill reads the numbers the
company actually filed with NSE in XBRL, lines them up quarter by quarter,
and says plainly what changed. For a day trader it answers two questions
before a trade: what did the last results say, and is the next board meeting
inside this trade?

Tool in this folder (standard library Python, no keys, no accounts):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE
python3 "${CLAUDE_SKILL_DIR}/results.py" HDFCBANK --standalone      # NPA ratios live in the standalone filing
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE TCS INFY --metrics trader/metrics.json
```

No SEC-style user agent or account is needed. It is polite to NSE: one list
request per symbol, one XBRL per quarter (cached for good under
`trader/cache/nse-xbrl/`, since a filing never changes), a pause between
calls, and one calendar request a run.

## What it reads and writes

- Reads NSE's list of the company's integrated filings
  (`/api/integrated-filing-results?index=equities&symbol=RELIANCE&type=Integrated%20Filing-%20Financials`):
  every quarter since March 2025, consolidated and standalone, each with its
  XBRL and the human-readable iXBRL page.
- Reads each quarter's XBRL (about 50 to 100 KB). Context `OneD` is the
  quarter, `FourD` the year to date. Companies: `RevenueFromOperations`,
  `Income`, `Expenses`, `FinanceCosts`, `DepreciationDepletionAndAmortisationExpense`,
  `OtherIncome`, `ProfitBeforeExceptionalItemsAndTax`, `ProfitBeforeTax`,
  `ProfitOrLossAttributableToOwnersOfParent`, diluted EPS. Banks
  (`INTEGRATED_FILING_BANKING`): `InterestEarned`, `InterestExpended`,
  `Income`, `OperatingProfitBeforeProvisionAndContingencies`,
  `ProvisionsOtherThanTaxAndContingencies`, `ProfitLossFromOrdinaryActivitiesBeforeTax`,
  `ProfitLossForThePeriod`, `PercentageOfGrossNpa`, `PercentageOfNpa`.
- Reads NSE's event calendar (`/api/event-calendar?index=equities`) for the
  next board meeting and its purpose.
- Falls back to Yahoo Finance's fundamentals-timeseries (consolidated, in INR,
  unofficial, with gaps: RELIANCE skips Sep 2025) only when NSE fails, for the
  whole company or a single quarter. Those rows are marked `yahoo`, and any
  growth figure that compares an NSE row with a Yahoo row carries a `*`.
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net profit and EPS, the latest margins, growth and NPA), in rupees.

## The rules

- **Filed numbers first.** The table comes from the XBRL the company filed
  with NSE. Use the press release, the investor presentation or the con-call
  transcript (all in `/nse-news` filings) for context, and say which numbers
  came from where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier, read from that quarter's own filing. Quarter-on-quarter is shown,
  but many businesses are seasonal (festive-season quarters, monsoon-hit
  ones). Consolidated by default; `--standalone` for the company alone.
- **Say what is derived.** EBITDA is PBT before exceptional items + finance
  costs + depreciation - other income, derived, not reported. NII for a bank
  is interest earned - interest expended. TTM EPS is the sum of four
  quarters, approximate.
- **Flag one-offs.** Exceptional items inside PBT, or other income above a
  fifth of PBT, are called out: part of the profit is not from operations.
- **Banks have no EBITDA.** NPA ratios are reported in the standalone filing;
  a consolidated bank filing carries zeros there, shown blank, not zero.
- **Know the deadlines.** SEBI LODR Regulation 33: quarterly results within
  45 days of the quarter's end, the March quarter (audited, with the year)
  within 60. The tool prints the next due date, and a NOTE when a quarter is
  past due and NSE's list does not have it yet.

## Steps

1. Run it for the companies in question (or the symbols in
   `trader/watchlist.json`). `--quarters 7` reads every quarter NSE's list
   has, so the older rows get year-on-year numbers too. `--json` gives
   everything machine-readable.
2. Open the latest filing (the iXBRL link) or the results PDF from
   `/nse-news` when a number needs context, and read it.
3. Write the summary: one line on the quarter (revenue, net profit to owners,
   EPS, growth), one on margins (or NII and NPA for a bank), one on anything
   unusual (an exceptional item, a Yahoo row, a missing quarter), and the
   links. Plain words, real numbers in ₹ crore, no adjectives the numbers do
   not support.
4. If the next board meeting falls inside the planned trade (an overnight
   hold, or results before the open), say it before `/nse-exit` sets the
   stop: a stop does not protect against a gap, the stock can open past it.

## Example: RELIANCE, Sep 2026 quarter

`results.py RELIANCE` (run 14 Nov 2026, real data): quarter ending
2026-09-30 (Q2 FY27), consolidated revenue from operations ₹3,32,861 cr,
+28.6% against a year earlier and +6.7% on the quarter before; net profit
to owners ₹25,548 cr (+40.6%); diluted EPS ₹18.88 (+40.7%). EBITDA (derived)
₹56,616 cr, a 17.0% margin, +1.8 points on the quarter before; PBT margin
11.6%; net margin 7.7%. Last four quarters: ₹12,12,828 cr revenue, ₹82,110 cr
net profit to owners, ₹60.68 EPS. Results were broadcast on 30 Oct 2026;
the December quarter's are due by 14 Feb 2027 and NSE's calendar has no
board meeting for it yet, so "a day trade this month is not exposed to a
results gap".

`results.py HDFCBANK` (consolidated): interest earned ₹95,132 cr, NII
₹44,464 cr (+10.5%), net profit to owners ₹20,700 cr (+5.6%), EPS ₹13.41;
the filing puts -₹837 cr under exceptional items, which the tool flags.
`--standalone` shows gross NPA 1.11% and net NPA 0.37%.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filing does not have, never mixes Yahoo's
  numbers in without marking them, and never estimates a missing quarter.
- Never hammers NSE: cached filings, a pause between calls, and the cookie
  dance only when NSE answers 401/403.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/nse-broker`, after `/nse-risk` passes and
the user types an explicit yes for that order.
