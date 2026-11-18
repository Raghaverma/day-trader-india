#!/bin/sh
# What launchd runs every minute (com.nse-autopilot): one paper-autopilot tick. Paper only.
#
#   run.sh tick        the default; quiet unless something happened or failed
#   run.sh selftest    python, the config, headless Claude and Gmail, in launchd's own context
#
# A request file makes the next tick run the selftest instead (install.sh uses it, so the check runs
# exactly where the daily job runs: same user session, same keychain, same PATH).
# Logs: ~/Library/Logs/nse-autopilot/<date>.log, kept 30 days.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${NSE_AUTOPILOT_PYTHON:-python3}"
LOGDIR="$HOME/Library/Logs/nse-autopilot"
SUPPORT="$HOME/Library/Application Support/nse-autopilot"
mkdir -p "$LOGDIR" "$SUPPORT"
LOG="$LOGDIR/$(date +%Y-%m-%d).log"
find "$LOGDIR" -name '*.log' -mtime +30 -delete 2>/dev/null

cmd="${1:-tick}"
[ $# -gt 0 ] && shift
if [ "$cmd" = "tick" ] && [ -f "$SUPPORT/selftest.request" ]; then
  rm -f "$SUPPORT/selftest.request"
  out="$("$PY" "$REPO/autopilot/run_day.py" selftest --send-mail 2>&1)"
  code=$?
  printf '%s\n%s\nexit %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$out" "$code" > "$SUPPORT/selftest.result"
  printf '%s selftest (exit %s)\n%s\n' "$(date '+%H:%M:%S')" "$code" "$out" >> "$LOG"
  exit 0
fi

out="$("$PY" "$REPO/autopilot/run_day.py" "$cmd" "$@" 2>&1)"
code=$?
if [ -n "$out" ] || [ "$code" -ne 0 ]; then
  printf '%s %s (exit %s)\n%s\n' "$(date '+%H:%M:%S')" "$cmd" "$code" "$out" >> "$LOG"
fi
exit 0
