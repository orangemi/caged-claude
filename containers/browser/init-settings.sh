#!/usr/bin/with-contenv bash
set -eu
test -f "/usr/share/zoneinfo/$TZ"
ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
printf '%s\n' "$TZ" > /etc/timezone
# The upstream image ships locale data; generate a requested locale if absent.
requested_locale=$(printf '%s' "$LC_ALL" | tr '[:upper:]' '[:lower:]' | tr -d '_-')
if ! locale -a | tr '[:upper:]' '[:lower:]' | tr -d '_-' | grep -Fxq "$requested_locale"; then
    locale-gen "$LC_ALL"
fi

# /config persists between containers, including the previous Openbox session.
# Rewrite its autostart and menu on every start so an old state directory cannot
# bring back the desktop, a terminal, or a close button.
app_home=$(getent passwd claude-user 2>/dev/null | cut -d: -f6)
[ -n "$app_home" ] || app_home=/config
for session_dir in "$app_home/.config/openbox" "$app_home/.config/labwc"; do
    mkdir -p "$session_dir"
    cat > "$session_dir/autostart" <<'EOF'
#!/bin/bash
exec /usr/local/bin/caged-browser-session
EOF
    chown claude-user:claude-user "$session_dir" "$session_dir/autostart" 2>/dev/null || true
    chmod 644 "$session_dir/autostart"
done
cat > "$app_home/.config/openbox/menu.xml" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
<menu id="root-menu" label="MENU"></menu>
</openbox_menu>
EOF
chown claude-user:claude-user "$app_home/.config/openbox/menu.xml" 2>/dev/null || true
chmod 644 "$app_home/.config/openbox/menu.xml"
