"""More paper accounts: the same signals, traded for real on paper at other numbers of trades a day.

The main trader folder is the main paper account (its config.json's cap, 3 by default). With
"accounts": {"enabled": true, "caps": [5, 8, 10]} in autopilot.json, each cap gets a paper account of its own in
<trader folder>/accounts/cap<N>/, a trader folder like the main one:

  * config.json: the main config.json, with "max_trades_per_day": N and a daily loss limit of N x the max loss a
    trade (so the Nth trade is never kept out by the loss rule's reservation). Rewritten from the main config
    each pre-market, so a change to the main rules reaches every account. Paper and "autopilot": true are copied
    from the main config, never set here: the guards in run_day and ticket.py check each account's own config.
  * portfolio.json: the account's own cash, starting at the budget and carrying its P&L from day to day.
  * cache/: a link to the main folder's cache (NSE's sec_list, the equity list), so nothing is fetched twice.
  * state/: its own broker.json, paper.json and risk.json: its own orders, fills, stops and P&L.

Every long signal the main account plans (after Claude's veto and the cost filter) goes to each account in turn:
nse-budget sizes it on the account's cash, a fresh quote, nse-risk checks it on the account's rules (its cap, its
daily loss, its cash), autopilot-approve and the paper broker. Each account's open trades get the same paper-check
every minute and at the close. The report puts the accounts side by side.
"""

import json
import os

import autopilot_common as ac

nc = ac.nc
DIR = "accounts"


def settings_of(settings):
    a = settings.get("accounts") or {}
    return a if a.get("enabled") else None


def caps_of(settings):
    a = settings_of(settings)
    if not a:
        return []
    out = []
    for c in a.get("caps") or []:
        try:
            n = int(c)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def name_of(cap):
    return "cap%d" % int(cap)


def home_of(home, cap):
    return os.path.join(home, DIR, name_of(cap))


def config_for(cfg, cap):
    """The main config with N trades a day and a daily loss limit of N x the max loss a trade."""
    out = dict(cfg)
    per = float(cfg.get("max_loss_per_trade_inr") or 0)
    out["max_trades_per_day"] = int(cap)
    if per:
        out["max_daily_loss_inr"] = round(per * int(cap), 2)
    return out


def ensure(home, cfg, cap):
    """Create or refresh one account's folder; returns its path. The main config.json on disk is the source (the
    loaded cfg can carry defaults the file does not)."""
    path = home_of(home, cap)
    os.makedirs(os.path.join(path, "state"), exist_ok=True)
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = dict(cfg)
    ac.write_json(os.path.join(path, "config.json"), config_for(raw, cap))
    pf = os.path.join(path, "portfolio.json")
    if not os.path.isfile(pf):
        ac.write_json(pf, {"cash": float(raw.get("budget") or cfg.get("budget") or 0), "positions": []})
    cache = os.path.join(path, "cache")
    if not os.path.lexists(cache):
        os.makedirs(os.path.join(home, "cache"), exist_ok=True)
        try:
            os.symlink(os.path.join(home, "cache"), cache)
        except OSError:
            os.makedirs(cache, exist_ok=True)
    return path


def load_cfg(path):
    try:
        with open(os.path.join(path, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def live_trades(path):
    paper = ac.read_json(os.path.join(path, "state", "paper.json")) or {}
    return [t for t in paper.get("trades") or [] if t.get("status") in ("open", "filled")]


def cap_reason(path, cfg, day):
    """Why this account takes no more trades today (its cap or its loss limit), else None. nse-risk decides; this
    only saves the requests for a trade that cannot be taken."""
    state = ac.read_json(os.path.join(path, "state", "broker.json")) or {}
    rows = [r for r in state.get("orders") or [] if isinstance(r, dict) and r.get("date") == day]
    gone = {r.get("fingerprint") for r in rows if r.get("status") == "cancelled" and r.get("fingerprint")}
    taken = sum(1 for r in rows if r.get("status") == "approved" and r.get("kind") == "entry"
                and r.get("fingerprint") not in gone)    # an entry cancelled unfilled frees its slot
    cap = int((cfg or {}).get("max_trades_per_day") or 0)
    if cap and taken >= cap:
        return "the day's cap of %d trades is used" % cap
    realised = sum(float(r.get("pnl_net") or 0) for r in rows if r.get("status") == "closed")
    limit = float((cfg or {}).get("max_daily_loss_inr") or 0)
    if limit and realised <= -limit:
        return "the day's loss limit of %s is reached" % nc.inr(limit)
    return None


def day_summary(path, day, budget=None):
    """One account's day from its paper.json: orders, fills, wins, losses, net, charges, and every trade."""
    paper = ac.read_json(os.path.join(path, "state", "paper.json")) or {}
    trades = [t for t in paper.get("trades") or [] if t.get("date") == day]
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl_net") is not None]
    nets = [float(t["pnl_net"]) for t in closed]
    pf = ac.read_json(os.path.join(path, "portfolio.json")) or {}
    cfg = load_cfg(path) or {}
    budget = float(budget or cfg.get("budget") or 0)
    out = {"orders": len(trades), "filled": sum(1 for t in trades if t.get("fill_price") is not None),
           "never_filled": sum(1 for t in trades if t.get("status") == "cancelled"),
           "still_open": sum(1 for t in trades if t.get("status") in ("open", "filled")),
           "closed": len(closed), "wins": sum(1 for x in nets if x > 0), "losses": sum(1 for x in nets if x < 0),
           "net": round(sum(nets), 2), "charges": round(sum(float(t.get("charges") or 0) for t in closed), 2),
           "cash": pf.get("cash"), "budget": budget,
           "cap": int(cfg.get("max_trades_per_day") or 0), "daily_loss": cfg.get("max_daily_loss_inr"),
           "trades": [{"symbol": t.get("symbol"), "qty": t.get("qty"), "ordered_at": t.get("ordered_at"),
                       "fill": t.get("fill_price"), "fill_time": t.get("fill_time"), "exit": t.get("exit_price"),
                       "exit_time": t.get("exit_time"), "exit_reason": t.get("exit_reason"),
                       "status": t.get("status"), "net": t.get("pnl_net"), "charges": t.get("charges"),
                       "max_loss": t.get("max_loss")} for t in trades]}
    out["return_pct"] = round(100 * out["net"] / budget, 3) if budget else None
    return out


def all_summaries(home, day, settings, main_cfg):
    """[{name, label, cap, main, ...day_summary}] for the main account and every extra one, by cap."""
    rows = [dict(day_summary(home, day, main_cfg.get("budget")), name=name_of(main_cfg.get("max_trades_per_day") or 0),
                 label="%d trades a day (main account)" % int(main_cfg.get("max_trades_per_day") or 0),
                 cap=int(main_cfg.get("max_trades_per_day") or 0), main=True,
                 daily_loss=main_cfg.get("max_daily_loss_inr"))]
    for cap in caps_of(settings):
        path = home_of(home, cap)
        if not os.path.isdir(path):
            continue
        s = day_summary(path, day)
        rows.append(dict(s, name=name_of(cap), label="%d trades a day" % cap, cap=cap, main=False))
    rows.sort(key=lambda r: (r["cap"], not r["main"]))
    return rows
