#!/bin/bash
# Remove the paper autopilot's launchd jobs (the day and the progress email). The trader folder (config, paper trades, reports) and the
# keychain entry stay; the script says how to remove those by hand if you want to.
set -u
for LABEL in com.nse-autopilot com.nse-autopilot-progress; do
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "stopped $LABEL" || echo "$LABEL was not loaded"
  if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "removed $PLIST"
  fi
done
rm -f "$HOME/Library/Application Support/nse-autopilot/selftest.request"
cat <<'EOF'
Kept (delete by hand if you want):
  ~/Trading/nse-autopilot (or the --home you installed with): config, paper trades, reports
  ~/Library/Logs/nse-autopilot
  the Gmail app password:  security delete-generic-password -s nse-autopilot-gmail
EOF
