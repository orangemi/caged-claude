#!/usr/bin/with-contenv bash
# Runs once at container init, before Chromium starts (the launcher re-runs
# 10-caged-settings later, so this must stay separate). The profile persists
# in state/<instance>/browser; a killed container leaves Chromium's singleton
# lock behind, which would otherwise block the next start.
find /config/.config/chromium -maxdepth 1 -name 'Singleton*' -exec rm -f {} + 2>/dev/null || true
