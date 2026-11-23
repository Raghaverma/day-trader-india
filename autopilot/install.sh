#!/bin/bash
# Install the day-trader-india PAPER autopilot on this Mac (meant for an always-on Mac mini).
#
#   autopilot/install.sh [--home DIR]       # default trader folder: ~/Trading/nse-autopilot
#
# What it does, in order (nothing here touches a broker or real money):
#   1. checks where the repo lives (launchd jobs cannot read ~/Desktop, ~/Documents or ~/Downloads)
#   2. finds python3 (3.8+) and claude
#   3. creates the trader folder with a PAPER config (README defaults) and switches "autopilot" on in it
#   4. writes autopilot.json (asks for the Gmail sender and recipients) and stores the Gmail app password
#      in the login keychain (macOS asks for it; it is never echoed or written to a file)
#   5. installs ~/Library/LaunchAgents/com.nse-autopilot.plist (the day, every 60 s) and
#      com.nse-autopilot-progress.plist (the half-hourly progress email, every 60 s) and loads them
#   6. runs the selftest INSIDE launchd (Claude login, Gmail login + one test email) and prints the result
#
# Undo with autopilot/uninstall.sh. Paper only: the autopilot refuses any config that is not "broker": "paper".
set -euo pipefail

LABEL="com.nse-autopilot"
PROGRESS_LABEL="com.nse-autopilot-progress"
DEEP_LABEL="com.nse-autopilot-deep"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TRADER_HOME="$HOME/Trading/nse-autopilot"
while [ $# -gt 0 ]; do
  case "$1" in
    --home) TRADER_HOME="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
SUPPORT="$HOME/Library/Application Support/nse-autopilot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
say() { printf '\n== %s\n' "$*"; }

say "1/6 Where the repo lives: $REPO"
case "$REPO" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "launchd jobs cannot read ~/Desktop, ~/Documents or ~/Downloads (macOS privacy protection)."
    echo "Clone the repo somewhere else, e.g.:  git clone <repo> ~/Trading/day-trader-india"
    exit 1 ;;
esac
[ "$(uname -s)" = "Darwin" ] || { echo "this installer is for macOS (launchd)"; exit 1; }

say "2/6 python3 and claude"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found on PATH"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "need Python 3.8+ ($PY)"; exit 1; }
echo "python3: $PY ($("$PY" -V 2>&1))"
CLAUDE="$(command -v claude || true)"
if [ -n "$CLAUDE" ]; then
  echo "claude:  $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -1))"
else
  echo "claude not found on PATH: the pre-market veto and the EOD analysis will be off."
  echo "Install Claude Code and log in once with \`claude\`, then run this installer again to turn them on."
fi
LAUNCH_PATH="$(dirname "$PY"):${CLAUDE:+$(dirname "$CLAUDE"):}/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

say "3/6 Trader folder and paper config: $TRADER_HOME"
mkdir -p "$TRADER_HOME" "$SUPPORT"
export NSE_TRADER_HOME="$TRADER_HOME"
if [ ! -f "$TRADER_HOME/config.json" ]; then
  "$PY" "$REPO/nse-budget/size.py" --init-config --budget 100000 --max-loss 500 --max-daily-loss 1500
fi
"$PY" - "$TRADER_HOME/config.json" <<'EOF'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)
if str(cfg.get("broker", "paper")).lower() != "paper":
    sys.exit("config.json says broker %r: the autopilot is paper only. Leaving it alone; use another --home." % cfg["broker"])
if cfg.get("autopilot") is not True:
    cfg["autopilot"] = True
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    print("switched on: \"autopilot\": true (paper only) in %s" % path)
else:
    print("already on: \"autopilot\": true (paper only)")
EOF

say "4/6 autopilot.json and Gmail"
SETTINGS="$TRADER_HOME/autopilot.json"
if [ ! -f "$SETTINGS" ]; then
  cp "$REPO/autopilot/autopilot.example.json" "$SETTINGS"
  read -r -p "Gmail address to send the daily report FROM (blank: no email): " FROM || FROM=""
  TO=""
  if [ -n "$FROM" ]; then
    read -r -p "Send it TO (comma separated, blank: the same address): " TO || TO=""
    [ -n "$TO" ] || TO="$FROM"
  fi
  "$PY" - "$SETTINGS" "$FROM" "$TO" "${CLAUDE:-}" <<'EOF'
import json, sys
path, sender, to, claude = sys.argv[1:5]
with open(path, encoding="utf-8") as fh:
    s = json.load(fh)
if sender:
    s["mail"].update({"enabled": True, "from": sender, "to": [t.strip() for t in to.split(",") if t.strip()]})
s["claude"]["enabled"] = bool(claude)
if claude:
    s["claude"]["bin"] = claude
with open(path, "w", encoding="utf-8") as fh:
    json.dump(s, fh, indent=2)
    fh.write("\n")
print("wrote %s" % path)
EOF
else
  echo "keeping $SETTINGS"
fi
read -r SERVICE SENDER ENABLED <<<"$("$PY" -c '
import json, sys
m = json.load(open(sys.argv[1])).get("mail") or {}
print(m.get("keychain_service") or "nse-autopilot-gmail", m.get("from") or "-", "yes" if m.get("enabled") else "no")
' "$SETTINGS")"
if [ "$ENABLED" = "yes" ]; then
  if security find-generic-password -s "$SERVICE" -a "$SENDER" >/dev/null 2>&1; then
    echo "the Gmail app password for $SENDER is already in the keychain ($SERVICE)"
  else
    echo "Make an app password at https://myaccount.google.com/apppasswords (needs 2-Step Verification),"
    echo "then paste it at the prompt below. macOS stores it in your login keychain; it is not echoed."
    security add-generic-password -U -s "$SERVICE" -a "$SENDER" -w
  fi
  "$PY" "$REPO/autopilot/mail.py" check || echo "(fix the Gmail settings, then run this installer again)"
fi
if [ -n "$CLAUDE" ]; then
  "$PY" "$REPO/autopilot/claude_run.py" selftest || echo "(log in with \`claude\` in Terminal, then run this installer again)"
fi

say "5/6 launchd jobs $LABEL and $PROGRESS_LABEL (every 60 s)"
write_plist() {   # label, run.sh command, log prefix
  "$PY" - "$HOME/Library/LaunchAgents/$1.plist" "$1" "$REPO" "$TRADER_HOME" "$PY" "$LAUNCH_PATH" \
    "$HOME/Library/Logs/nse-autopilot" "$2" "$3" <<'EOF'
import os, plistlib, sys
plist, label, repo, home, py, path, logs, cmd, prefix = sys.argv[1:10]
os.makedirs(os.path.dirname(plist), exist_ok=True)
os.makedirs(logs, exist_ok=True)
doc = {
    "Label": label,
    "ProgramArguments": ["/bin/sh", os.path.join(repo, "autopilot", "run.sh"), cmd],
    "EnvironmentVariables": {"NSE_TRADER_HOME": home, "NSE_AUTOPILOT_PYTHON": py, "PATH": path},
    "StartInterval": 60,
    "RunAtLoad": True,
    "ProcessType": "Standard",
    "WorkingDirectory": home,
    "StandardOutPath": os.path.join(logs, prefix + ".out.log"),
    "StandardErrorPath": os.path.join(logs, prefix + ".err.log"),
}
with open(plist, "wb") as fh:
    plistlib.dump(doc, fh)
print("wrote %s" % plist)
EOF
  launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$1.plist"
  launchctl enable "gui/$(id -u)/$1"
  echo "loaded $1: $(launchctl print "gui/$(id -u)/$1" 2>/dev/null | grep -m1 'state =' | xargs)"
}
write_plist "$LABEL" tick launchd
# the half-hourly progress email (progress.py): its own job, so it still reports when a tick is stuck
write_plist "$PROGRESS_LABEL" progress progress
# the deep pre-market research (deepscan.py): its own job, an hour or more from 03:30, never holding the tick
write_plist "$DEEP_LABEL" deep deep

say "6/6 Selftest inside launchd (this also sends one test email if mail is on)"
rm -f "$SUPPORT/selftest.result"
touch "$SUPPORT/selftest.request"
launchctl kickstart "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
for _ in $(seq 1 90); do
  [ -f "$SUPPORT/selftest.result" ] && break
  sleep 2
done
if [ -f "$SUPPORT/selftest.result" ]; then
  cat "$SUPPORT/selftest.result"
else
  echo "no selftest result after 3 minutes: see ~/Library/Logs/nse-autopilot/"
fi

cat <<EOF

Installed. On every NSE trading day the autopilot now runs on its own (paper only):
  08:40 IST  scan, news, Claude's pre-market veto
  09:35-14:30 opening-range breakouts, sized, risk-checked and paper-filled
  15:35 IST  report in $TRADER_HOME/reports/ and by email
  08:42, then every 30 minutes to 15:30: a progress email (what it is doing, trades, money made and lost)

Keep this Mac awake and logged in (System Settings):
  - Energy: "Prevent automatic sleeping when the display is off" ON, "Start up automatically after a power failure" ON
  - Users & Groups: automatic login for this user (launchd jobs and the keychain need the session)
  - Date & Time: time zone Asia/Kolkata (optional; the autopilot works in IST either way)

Check on it:
  NSE_TRADER_HOME="$TRADER_HOME" $PY $REPO/autopilot/run_day.py status
  tail -f ~/Library/Logs/nse-autopilot/\$(date +%Y-%m-%d).log
Remove it: $REPO/autopilot/uninstall.sh   (your trader folder and reports stay)
EOF
