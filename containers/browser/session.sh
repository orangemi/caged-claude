#!/usr/bin/env bash
# Caged Claude browser session.
#
# The browser window is the only program the remote user may interact with.
# If it exits for any reason (close button, keyboard shortcut, crash), keep the
# session alive with a static crash screen instead of falling back to the
# Openbox desktop, a terminal, or a fresh browser. Recovery is host-side:
# ./cage down && ./cage up.
set -u

trap 'exit 0' TERM INT

CRASH_PROFILE=/tmp/caged-crash-profile

echo '[caged] starting kiosk Chromium'
/usr/bin/wrapped-chromium --kiosk ${CHROME_CLI:-}
status=$?
echo "[caged] Chromium exited (${status}); showing the crash screen" >&2

mkdir -p "$CRASH_PROFILE"
while true; do
    /usr/bin/chromium \
        --no-sandbox \
        --password-store=basic \
        --test-type \
        --kiosk \
        --no-first-run \
        --no-default-browser-check \
        --hide-crash-restore-bubble \
        --user-data-dir="$CRASH_PROFILE" \
        file:///usr/share/caged/crash.html \
        >/dev/null 2>&1
    sleep 1
done
