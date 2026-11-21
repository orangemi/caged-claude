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
