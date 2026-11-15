---
name: nse-broker
description: >-
  Order Executor for NSE intraday trades, paper by default, never without
  your yes. Shows each order as a plain ticket (MIS entry, SL stop leg after
  the fill, target as an alert because Zerodha Kite has no OCO for MIS, max
  loss in ₹ with charges) and records your yes word for word. Three modes: a
  built-in paper broker driven by Yahoo 1-minute bars (paper-fill,
  paper-check, P&L net of charges); manual, which prints exactly what to type
  into Kite; and kite, the exact JSON for place_order on a self-hosted Kite
  MCP server, where Claude's Allow once card asks again. Builds the flatten
  ticket (cancel the stop, then SELL MIS) by your flat-by time, records
  fills, and covers setting up zerodha/kite-mcp-server with a Kite Connect
  key, the static IP rule and "Needs approval". Use when the user says
  "place the order", "buy it", "execute", "send it to Zerodha", "paper
  trade", "close my position", "flatten", "get me out", "connect Kite", or
  after /nse-risk passes.
argument-hint: "[order number]"
---

# nse-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order goes anywhere only after these gates,
every time, for every order:

1. **nse-risk passed this exact order today** (matched by `nse_common.order_fingerprint`).
2. **The user typed an explicit yes for this one order** in the chat.
3. With the Kite MCP: **the user clicked Allow once** on Claude's own permission
   card, which appears because the order tools are set to "Needs approval".

No yes, no order. Not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                      # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-fill --index 1      # paper: fill at the latest Yahoo price
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-check               # paper: stop, target, flat_by on 1-minute bars
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 56 --filled-price 1334
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --leg stop --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 56 --price 1344.40 --reason target
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE   # SELL MIS ticket; --limit P for a LIMIT
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to Zerodha. It renders tickets, checks that a reply is
a real yes for that order, runs the paper broker, builds the flatten ticket,
and keeps the log. `--now "2026-11-13 10:05"` pins the clock (IST); `--bars
FILE` feeds paper mode a saved Yahoo 1-minute chart instead of fetching one.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (nse-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | nse-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, filled and closed (with net P&L), and each entry's max loss |
| `trader/state/paper.json` | paper trades: fills, the paper stop, exits, P&L net of charges |
| `trader/portfolio.json` | cash and shares, updated by fills (paper and real) |
| `trader/plan.json` | the flat-by time for `flatten` |

## The order on Kite: no OCO for MIS

Kite has no bracket orders and no OCO (one-cancels-other) for MIS. If a target
LIMIT sell and a stop sell both rest at the broker, both can fill and leave
you short. So every trade is:

1. the **entry leg**: BUY, MIS, LIMIT (MARKET only if the user asked);
2. the **stop leg**, placed once the entry fills: SELL, MIS, SL with a trigger
   at the stop and a limit a little under it (an SL can fail to fill in a gap);
3. the **target as an alert** (nse-alerts): when it rings, cancel the stop, then sell.

`flatten` is the same exit: cancel the stop first, then the SELL MIS.

## Mode 1: paper (the default, `"broker": "paper"`)

No broker, no money. After the yes:

- `paper-fill --index N` fills the entry at the latest Yahoo 1-minute close if
  a LIMIT buy is marketable (price at or under the limit, filled at that
  price). Otherwise it says so and leaves it resting. The paper stop goes in
  with the fill. A SELL (a flatten ticket) cancels the paper stop and sells.
- `paper-check` walks the 1-minute bars since the fill: the stop is hit when a
  bar's low reaches the trigger (filled at the trigger, or at the bar's open
  if it gapped below); the target when a bar's high reaches it; at `flat_by`
  it closes at that bar's close. A bar that touches both counts as the stop.
  A resting LIMIT fills when a bar trades down to it, or is cancelled at
  `flat_by`. Yahoo leaves many late 1-minute bars empty (most of 15:16-15:29):
  empty bars are skipped, and if the flat_by bar is empty the trade closes at
  the last real bar at or before flat_by, and says so. P&L is net of
  `nse_common.intraday_charges`, and it updates
  `portfolio.json` and `broker.json` so nse-risk sees the day's real losses.

Run `paper-check` whenever the user asks how the trade is doing, and after
`flat_by`.

## Mode 2: manual (`"broker": "manual"`)

The ticket prints exactly what to type into Kite (web or app): Buy, Product
Intraday (MIS), Order type Limit, Qty, Price, Validity Day; then, once the buy
shows COMPLETE, the Sell with Order type SL, Trigger price and Price. The user
places it themselves and tells Claude what happened; record it with `result`
(with `--filled-qty`/`--filled-price`), `result --leg stop`, and `fill` for the
exit.

## Mode 3: kite (`"broker": "kite"`, a self-hosted Kite MCP server)

Zerodha's **hosted** Kite MCP (`https://mcp.kite.trade/mcp`) is read-only for
trading: it leaves out order placement (only GTT orders are offered). It
cannot place a regular MIS order. To place orders, the user self-hosts
[zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server) with
their own Kite Connect app:

1. Create a Kite Connect app at developers.kite.trade. The Personal plan is free
   and places orders (it has no market data, which this pack gets from Yahoo
   and NSE). Note the API key and secret.
2. **Static IP.** Since 1 April 2026, orders sent by API must come from a
   static IP that is whitelisted for the app in the Kite Connect developer
   console. Run the server on a machine with that IP (a static IP from the ISP,
   or a small VPS). Orders from any other IP are rejected.
3. Build the server (`go build -o kite-mcp-server`, Go 1.21+) and add it to the
   MCP config. `kite-mcp.example.json` in this folder runs it in stdio mode.
   The keys come from environment variables (`KITE_API_KEY`,
   `KITE_API_SECRET`): Claude Code expands `${KITE_API_KEY}` from the shell.
   In Claude Desktop, type them into the config file yourself. **Keys never go
   in the chat.** Never ask for them, never read the config back into the
   conversation; if the user pastes one, tell them to delete the message and
   regenerate the secret. `EXCLUDED_TOOLS` hides the tools this pack doesn't
   need (modify_order, GTT).
4. Log in each day with the server's `login` tool: it gives a Kite login link
   the user opens in their browser. Kite sessions end every morning.
5. **Set the order tools to ask.** In Claude Code, `.claude/settings.json`:
   `"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__cancel_order",
   "mcp__kite__modify_order"]}`, and never put an order tool in `allow`. In
   Claude Desktop: Customize > Connectors > kite, set `place_order`,
   `cancel_order` and `modify_order` to **Needs approval**, never Always allow.
   Read-only tools (`get_orders`, `get_positions`, `get_margins`, `get_profile`)
   can be Always allow.
6. Check it: `get_profile` (read only) shows the right account.

Then, after the yes, Claude calls `place_order` with exactly the entry leg JSON
the ticket printed; Claude's card shows it with Allow once / Deny. Once the
entry fills (`get_orders`), Claude calls `place_order` with the stop leg JSON
(another card). Market orders sent by API may need market protection; if
Kite rejects one, use a LIMIT.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. NOT READY or BLOCKED
   goes back to `/nse-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per
   question. Never offer "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   "Not recorded" means it was not an explicit yes for this order ("sure",
   "ok", "haan", "yes to all", a yes naming another stock or another quantity).
   Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes": paper → `paper-fill`; manual → the user types
   it into Kite; kite → `place_order` with exactly the printed JSON.
5. Record what happened (manual, kite): `result --index N --status placed
   --broker-id <Kite order id> --filled-qty Q --filled-price P`, or `--status
   denied`, or `--status failed --note ...`. A failed order is not retried
   without a new check and a new yes. Then the stop leg, then `result --leg stop`.

## Steps: out by flat_by (15:10 IST)

Zerodha squares MIS off at 15:25 and charges ₹50 + GST for it, so be out by
`flat_by`:

1. How many shares are held: `get_positions` (kite) or `trader/portfolio.json`.
2. `ticket.py flatten --symbol RELIANCE` writes the SELL MIS ticket (with the
   stop's Kite order id to cancel first, if it was recorded).
3. `/nse-risk`. A sell that only closes what the user holds is never blocked by
   the day limits or the entry window.
4. `ticket.py show`, then the user's yes for that one order.
5. Cancel the stop first (`cancel_order`, Needs approval), then the SELL. In
   paper mode `paper-fill` does both. Then `result` or `fill`.

## Example: RELIANCE, 13 Nov 2026 (paper)

```
ORDER 1 of 1, PAPER (no broker, no money)
  BUY 56 RELIANCE, NSE, MIS (intraday), LIMIT 1,334.00, DAY
  then the stop: SELL 56 RELIANCE, MIS (intraday), SL trigger 1,328.80, limit 1,322.10, DAY, placed once the entry fills
  target 1,344.40: an alert, not an order (Kite has no OCO for MIS): when it rings, cancel the stop, then sell
  max loss about ₹364.82 with charges if the stop fills at 1,328.80 (₹291.20 + ₹73.62 charges; ...)
  READY: nse-risk PASS, waiting for your yes
```

The user replies "yes". `paper-fill` at 10:05 buys 56 at 1,334.00 (the 10:04
close). `paper-check` after the close: target hit at 1,344.40 on the 10:13
bar; gross ₹582.40, charges ₹73.87, net ₹508.53.

## What it never does

- Never places an order without the gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets or passwords.
- Never switches `"broker"` from paper to manual or kite. The user does that themselves.
- Never leaves a target LIMIT and a stop resting together on Kite.
- Never promises a fill price: a stop can fill worse, or not at all, in a gap.

## Safety

- Not financial advice. Education and tooling; a PASS is a rules check, not a recommendation.
- Paper by default. Going live is the user's own decision, in their own config.
- Nothing is placed without nse-risk's PASS for that exact order today and the user's explicit
  **yes** for that one order; with the Kite MCP, Allow once on Claude's card as well.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the user's own MCP config, never in the chat.
