---
name: nse-broker
description: >-
  Order Executor for NSE intraday trades, paper by default, never without
  your yes. Shows a plain ticket (MIS entry, SL stop leg after the fill,
  target as an alert because Kite has no OCO for MIS, max loss in ₹ at the
  worst fill) and records your yes word for word. Modes: a paper broker on
  Yahoo 1-minute bars, manual (what to type into Kite), or a self-hosted
  Kite MCP behind an order gate. Also flatten, move the stop, journal. Use
  when the user says "place the order", "buy it", "send it to Zerodha",
  "paper trade", "flatten", "get me out", "move my stop", "connect Kite".
argument-hint: "[order number]"
---

# nse-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. Not financial advice. An order goes anywhere
only after these gates, every time, for every order:

1. **nse-risk passed this exact order** (matched by `nse_common.order_fingerprint`),
   **within the last few minutes** (`risk_pass_minutes`, 5): a PASS from 10:05 is
   not a PASS at 10:15.
2. **The order is today's and fresh**: its plan date is today (IST) and `orders.json`
   is under `order_max_age_minutes` (120) old. For an entry the NSE must be open,
   inside your entry window (`no_entries_before` / `no_entries_after`) and before
   `flat_by`. Show, approve and paper-fill all look at the clock again. Exits
   (flatten) are never blocked by the clock.
3. **The user typed an explicit yes for this one order** in the chat.
4. In kite mode, **the order gate** lets the Kite order tool call through only if it
   matches the ticket the user said yes to, once, within minutes.

No yes, no order.

## What is enforced where (the honest version)

| mode | what stops a wrong order | where it lives |
| --- | --- | --- |
| paper | nothing is sent anywhere; fills are simulated | `ticket.py` (code) |
| manual | you type the order into Kite yourself, so your own hands are the gate | you |
| kite | a PreToolUse hook denies any `place_order` / `modify_order` / `cancel_order` call that does not match an approved, unused, unexpired ticket leg; `approve --reply` must equal your last message; Claude's Allow once card is the second gate | `hooks/`, which **only exists when this pack is installed as a plugin** |

Hooks do not ship with copied skill folders. If you copied `nse-broker` into
`~/.claude/skills`, there is no gate: `approve` in kite mode refuses ("the approval
gate is not installed") because it cannot see your last message. Install the pack as
a plugin (`/plugin install`, see the README), or stay in paper or manual mode. The
hook cannot tell a `/loop` or a scheduled run from you at the keyboard; it relies on
the yes and on your "Needs approval" setting. Never run this skill from a loop.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                      # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "<the user's message, word for word>"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-fill --index 1      # paper: fill at the latest Yahoo price
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-check               # paper: stop, target, flat_by on 1-minute bars
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 56 --filled-price 1334
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --leg stop --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 56 --price 1344.40 --reason target
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --positions positions.json   # SELL MIS ticket
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --positions positions.json --qty 20   # a partial exit
python3 "${CLAUDE_SKILL_DIR}/ticket.py" move-stop --symbol RELIANCE --to breakeven   # then again with --reply
python3 "${CLAUDE_SKILL_DIR}/ticket.py" reconcile --positions positions.json --apply
python3 "${CLAUDE_SKILL_DIR}/ticket.py" status                    # what needs attention right now
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
python3 "${CLAUDE_SKILL_DIR}/ticket.py" journal --days 7
```

`ticket.py` never talks to Zerodha. It renders tickets, checks that a reply is a
real yes for that order, runs the paper broker, builds the flatten and move-stop
tickets, keeps the log and answers the gate. `--bars FILE` feeds paper mode a saved
Yahoo 1-minute chart instead of fetching one. `--now` and `--today` exist for the
tests only: they are refused unless `NSE_TEST_MODE=1`, so a real ticket always runs
on the real clock. Without a `config.json` it stops and says so; it will not guess
your limits.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (nse-budget writes it; `flatten` replaces only that symbol's order) |
| `trader/state/risk.json` | nse-risk's verdicts; only a PASS from today, under 5 minutes old, is READY |
| `trader/state/broker.json` | every yes, no, placed, filled and closed (with net P&L), each entry's max loss, and the approved order-tool legs the gate matches |
| `trader/state/last_prompt.json` | your last message, saved by the UserPromptSubmit hook, so `approve --reply` can be checked against it |
| `trader/state/paper.json` | paper trades: fills, the paper stop, exits, P&L net of charges |
| `trader/portfolio.json` | cash and shares, updated by fills (paper and real) |
| `trader/plan.json` | the flat-by time for `flatten` |

## The order on Kite: no OCO for MIS

Kite has no bracket orders and no OCO (one-cancels-other) for MIS. If a target
LIMIT sell and a stop sell both rest at the broker, both can fill and leave
you short. So every trade is:

1. the **entry leg**: BUY, MIS, LIMIT (MARKET only if the user asked);
2. the **stop leg**, placed once the entry fills, **for the shares that filled**:
   SELL, MIS, SL with a trigger at the stop and a limit a little under it (an SL
   can fail to fill in a gap);
3. the **target as an alert** (nse-alerts): when it rings, cancel the stop, then sell.

The ticket's max loss is measured where the stop can fill worst, at the SL **limit**
(the trigger when the config says `"loss_basis": "trigger"`), plus the round trip's
charges. `flatten` is the same exit: cancel the stop first, then the SELL MIS.

## Mode 1: paper (the default, `"broker": "paper"`)

No broker, no money. The paper broker is deliberately no kinder than Kite:

- `paper-fill --index N` fills the entry at the latest Yahoo 1-minute close if a
  LIMIT buy is marketable (price at or under the limit). Otherwise it says so and
  leaves it resting. It re-checks the clock and your yes's age first, and refuses
  after `flat_by` or outside the window. `--filled-qty N` rehearses a partial fill:
  the paper stop and the exit follow the shares that filled.
- `paper-check` walks the complete 1-minute bars since the fill:
  - **Target**: on Kite it is only an alert, so when a bar's high reaches it you
    cancel the stop and sell at market. The paper exit fills at the **next bar's
    open** (above the target if it opens there, below if the price pulled back).
    With no next bar on Yahoo yet it fills at the target minus one tick and says so.
  - **Stop**: triggers when a bar's low reaches the trigger and fills at the
    trigger. A stop is a limit order, so if a bar **opens below the SL limit** (a
    gap) it does **not** fill: the position stays open, `paper-check` says "STOP
    TRIGGERED BUT NOT FILLED" loudly, the stop fills at the limit only when a later
    bar's high reaches it, and otherwise the trade is closed at `flat_by` at that
    bar's close. A bar that opens between the limit and the trigger fills at its open.
  - A bar that touches both the stop and the target counts as the stop.
  - A resting LIMIT fills when a bar trades down to it (and is not also read as a
    stop bar), or is cancelled at `flat_by`.
  - `flat_by`: it closes at that bar's close. Yahoo leaves many late 1-minute bars
    empty (most of 15:16-15:29): empty bars are skipped, and if the flat_by bar is
    empty the trade closes at the last real bar at or before it, and says so.
  - It never acts on a bar that is not complete yet, and it re-reads the last bar
    next time (`checked_to` is the start of the last bar read), so a bar Yahoo
    revises or finishes late is not missed.
  P&L is net of `nse_common.intraday_charges`, and updates `portfolio.json` and
  `broker.json` so nse-risk sees the day's real losses.

Run `paper-check` whenever the user asks how the trade is doing, and after `flat_by`.

## Mode 2: manual (`"broker": "manual"`)

The ticket prints exactly what to type into Kite (web or app): Buy, Product
Intraday (MIS), Order type Limit, Qty, Price, Validity Day; then, once the buy
shows COMPLETE, the Sell with Order type SL, Trigger price and Price. The user
places it themselves and tells Claude what happened; record it with `result`
(with `--filled-qty`/`--filled-price`), `result --leg stop`, and `fill` for the
exit. The user's own hands are the only gate here.

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
   need (GTT orders). `modify_order` stays on: `move-stop` and a partial exit use it.
4. Log in each day with the server's `login` tool: it gives a Kite login link
   the user opens in their browser. Kite sessions end every morning.
5. **Install this pack as a plugin** so the order gate exists (see What is
   enforced where). Then set the order tools to ask, as the second gate. In
   Claude Code, `.claude/settings.json`: `"permissions": {"ask":
   ["mcp__kite__place_order", "mcp__kite__cancel_order",
   "mcp__kite__modify_order"]}`, and never put an order tool in `allow`. In
   Claude Desktop: Customize > Connectors > kite, set `place_order`,
   `cancel_order` and `modify_order` to **Needs approval**, never Always allow.
   Read-only tools (`get_orders`, `get_positions`, `get_margins`, `get_profile`)
   can be Always allow. The gate hook only ever denies; it never approves for you.
6. Check it: `get_profile` (read only) shows the right account.

How the gate works. `approve` records, in `broker.json`, the exact order-tool calls
the yes unlocks ("legs"): the entry leg now, expiring in `approval_minutes` (5);
the stop leg when the entry fill is recorded, built from the **filled** quantity
(30 minutes); for a flatten, the cancel (or the stop-shrinking modify) first and the
sell after it; for move-stop, one modify. The hook allows a call only if it equals
an approved leg on the Kite fields (variety, exchange, tradingsymbol,
transaction_type, quantity, product, order_type, price, trigger_price, validity;
order_id for modify and cancel), and marks that leg used, so the same call twice is
denied. GTT orders are always denied. A call that differs says which field differs.
Send exactly the JSON the ticket printed. It fails closed: a corrupt state file, no
config or any error is a deny.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. NOT READY (an old PASS, an
   old order, outside the entry window) or BLOCKED goes back to `/nse-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order per
   question. Never offer "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   A yes is the word **yes**, optionally with "please", "go ahead", "place it", the
   stock's own symbol or its share count ("yes 56 RELIANCE"). Anything else is "Not
   recorded": "y", "ok", "sure", "haan", "yes to all", a yes naming another stock
   ("yes TCS", "yes buy INFY") or another number. Ask again. Never turn a reply into
   a yes yourself: in kite mode the reply must equal the user's last message, or
   `approve` refuses.
4. Only after "Recorded your yes": paper → `paper-fill`; manual → the user types
   it into Kite; kite → `place_order` with exactly the printed JSON.
5. Record what happened (manual, kite): `result --index N --status placed
   --broker-id <Kite order id> --filled-qty Q --filled-price P`, or `--status
   denied`, or `--status failed --note ...`. `Q` is the shares that **filled**, from
   `get_orders`; if it is less than ordered, the ticket says PARTIAL FILL and prints
   the stop leg for `Q`, never the planned quantity, and offers to cancel the rest.
   A failed order is not retried without a new check and a new yes.
6. Place the stop leg (the JSON `result` printed), then `result --leg stop --status
   placed --broker-id <id>`. If the entry fills later, record it with `fill`; that
   prints the stop leg too. Run `ticket.py status`: it lists a filled entry with no
   stop recorded, a stop that covers fewer shares than held, an approval nobody
   placed, and a stale PASS. The SessionStart hook runs it for you.

## Steps: out by flat_by (15:10 IST)

Zerodha squares MIS off at 15:25 and charges ₹50 + GST for it, so be out by
`flat_by`. Never size a sell from `portfolio.json` alone: if the stop already
filled at Zerodha and nobody recorded it, a SELL of the recorded 56 leaves you short.

1. **Call `get_positions` first** (kite), save the JSON the tool returns to a file,
   and run `ticket.py flatten --symbol RELIANCE --positions positions.json`. In
   manual mode read the quantity in Kite > Positions and pass `--qty N`. Without the
   broker's number, `flatten` outside paper mode refuses. If it disagrees with
   `portfolio.json` it says so and uses the broker's; `ticket.py reconcile
   --positions positions.json --apply` fixes `portfolio.json`. Add `--stop-id ID`
   (from `get_orders`) if the stop's Kite order id was not recorded, or `--no-stop`
   if none is resting. Paper mode takes the quantity from the paper position.
2. It writes the SELL MIS ticket into `orders.json`, replacing only that symbol's
   order (other pending orders stay).
3. `/nse-risk`. A sell that only closes what the user holds is never blocked by
   the day limits or the entry window.
4. `ticket.py show`, then the user's yes for that one order.
5. Cancel the stop first (`cancel_order`, Needs approval), then the SELL; the gate
   refuses the sell until the cancel has gone through. In paper mode `paper-fill`
   does both. Then `result` or `fill`.

**A partial exit.** `flatten --symbol RELIANCE --positions positions.json --qty 20`
sells 20 of the 56. The stop must shrink first or it will try to sell shares you no
longer hold, so the ticket starts with a `modify_order` that cuts the stop to the 36
that stay (the gate enforces the order), then the SELL 20. In paper mode the sold
shares become a closed trade and the paper stop stays on the rest.

## Steps: move the stop

`ticket.py move-stop --symbol RELIANCE --to breakeven` (or `--to 1336.00`) prints a
ticket for Kite's `modify_order`: the new trigger and limit on the NSE tick, and
for break-even the lowest tick price that pays back the round trip's charges
(`nse_common.intraday_charges`). Long only, only **up**, and never at or above the
last price (a fresh quote from `prices.json`, `--price`, or the paper bars). It
needs the same yes: run it again with `--reply "<the user's reply>"`. In kite mode
that records the approval and the gate lets exactly that `modify_order` through
once; in paper mode it moves the paper stop. Use it also to resize a stop after a
late fill.

## Example: RELIANCE, 13 Nov 2026 (paper)

```
ORDER 1 of 1, PAPER (no broker, no money)
  BUY 56 RELIANCE, NSE, MIS (intraday), LIMIT 1,334.00, DAY
  then the stop: SELL 56 RELIANCE, MIS (intraday), SL trigger 1,328.80, limit 1,322.10, DAY, placed once the entry fills
  target 1,344.40: an alert, not an order (Kite has no OCO for MIS): when it rings, cancel the stop, then sell
  max loss about ₹739.91 with charges if the stop fills at its SL limit 1,322.10 (trigger 1,328.80) (...)
  order: plan date 2026-11-13, written under a minute ago (10:05 IST)
  READY: nse-risk PASS, waiting for your yes
```

(That order is bigger than a ₹500 cap allows once the worst fill is counted; in
practice nse-budget sizes it down and nse-risk blocks it. The numbers just show the
shape.) The user replies "yes". `paper-fill` at 10:05 buys 56 at 1,334.00 (the 10:04
close). The 10:13 bar reaches the 1,344.40 target, so `paper-check` sells at the 10:14
open, 1,344.30: gross ₹576.80, charges ₹73.87, net ₹502.93. `ticket.py journal`
then shows the trade with its R-multiple (net over the planned worst-case loss),
entry slippage against the plan, the exit against the target and the minutes held,
with day totals and a win rate over the days shown.

## What it never does

- Never places an order without the gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes. The one
  exception is the paper autopilot (`autopilot/` in the repo): `ticket.py autopilot-approve` works only
  with `"broker": "paper"` and `"autopilot": true`, records `approved_by: "autopilot"` (never a
  yes), and refuses in manual and kite mode, by code. Never set either for the user.
- Never asks for, reads, stores or repeats API keys, secrets or passwords.
- Never switches `"broker"` from paper to manual or kite. The user does that themselves.
- Never leaves a target LIMIT and a stop resting together on Kite.
- Never sizes a stop or a flatten from a quantity the broker has not confirmed.
- Never promises a fill price: a stop can fill worse, or not at all, in a gap.

## Safety

- Not financial advice. Education and tooling; a PASS is a rules check, not a recommendation.
- Paper by default. Going live is the user's own decision, in their own config.
- Nothing is placed without nse-risk's PASS for that exact order (fresh), the order
  being today's, and the user's explicit **yes** for that one order; in kite mode the
  order gate (a plugin hook) and Allow once on Claude's card as well.
- No F&O, margin or short selling unless the config allows it and the user confirms.
- Kite API keys live in environment variables or the user's own MCP config, never in the chat.
