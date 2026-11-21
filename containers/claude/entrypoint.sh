#!/bin/bash
set -euo pipefail
test -f "/usr/share/zoneinfo/$TZ"
ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
printf '%s\n' "$TZ" > /etc/timezone
groupmod -o -g "$PGID" claude-user
usermod -o -u "$PUID" -g "$PGID" claude-user
chown "$PUID:$PGID" /config
mkdir -p "$CLAUDE_CONFIG_DIR"
# Only the explicitly dedicated state directory is owned here, never workspaces.
if [[ $(stat -c '%u:%g' "$CLAUDE_CONFIG_DIR") != "$PUID:$PGID" ]]; then
    chown -R --no-dereference "$PUID:$PGID" "$CLAUDE_CONFIG_DIR"
fi
chmod 700 "$CLAUDE_CONFIG_DIR"
printf 'ready\n' > /run/caged-claude-ready
exec gosu "$PUID:$PGID" "$@"
