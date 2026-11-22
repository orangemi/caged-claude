#!/bin/bash
# Runs as root on every start. /etc, /usr, /var and /home/claude are the
# persisted state, so everything here must be idempotent.
set -euo pipefail
test -f "/usr/share/zoneinfo/$TZ"
ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
printf '%s\n' "$TZ" > /etc/timezone
groupmod -o -g "$PGID" claude-user
usermod -o -u "$PUID" -g "$PGID" claude-user
mkdir -p /home/claude "$CLAUDE_CONFIG_DIR"
# Only the dedicated home and Claude state are re-owned, never workspaces.
for directory in /home/claude "$CLAUDE_CONFIG_DIR"; do
    if [[ $(stat -c '%u:%g' "$directory") != "$PUID:$PGID" ]]; then
        chown -R --no-dereference "$PUID:$PGID" "$directory"
    fi
done
chmod 700 "$CLAUDE_CONFIG_DIR"
sudoers=/etc/sudoers.d/caged-claude
if [[ ${CAGED_SUDO:-1} == 1 ]]; then
    printf 'claude-user ALL=(ALL:ALL) NOPASSWD: ALL\n' > "$sudoers.tmp"
    chmod 440 "$sudoers.tmp"
    mv "$sudoers.tmp" "$sudoers"
else
    rm -f "$sudoers"
fi
printf 'ready\n' > /run/caged-claude-ready
exec gosu "$PUID:$PGID" "$@"
