#!/usr/bin/env bash
# Keep a usable browser on screen after the last tab/window closes or Chromium
# exits. Do not fall back to a desktop, terminal, or a noninteractive crash page.
set -u

trap 'exit 0' TERM INT

/usr/local/bin/caged-browser-configure || exit 1
while true; do
    echo '[caged] opening Chromium with a fresh tab'
    # Background mode must not leave a windowless browser process alive after
    # the last tab closes. The next launch always supplies an explicit new tab.
    /usr/bin/wrapped-chromium \
        --no-first-run \
        --disable-background-mode \
        --hide-crash-restore-bubble \
        ${CHROME_CLI:-} chrome://newtab
    status=$?
    echo "[caged] Chromium exited (${status}); reopening a new tab" >&2
    sleep 1
done
