#!/bin/sh
# Run as PUID:PGID (linuxserver-style) so files in /config are owned by the host user.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -o -g "$PGID" setlistarr
  fi
  if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g "$PGID" -d /config -M -s /usr/sbin/nologin setlistarr
  fi
  chown -R "$PUID:$PGID" /config
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
