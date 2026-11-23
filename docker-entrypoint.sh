#!/bin/sh
# Run as unprivileged user "shelly" (UID/GID adjustable via PUID/PGID, e.g. to
# match the host user that backs up ./data), make /data writable, then drop root.
set -e
if [ "$(id -u)" = "0" ]; then
    if [ -n "$PGID" ] && [ "$PGID" != "$(id -g shelly)" ]; then
        groupmod -o -g "$PGID" shelly
    fi
    if [ -n "$PUID" ] && [ "$PUID" != "$(id -u shelly)" ]; then
        usermod -o -u "$PUID" shelly
    fi
    mkdir -p /data
    chown shelly:shelly /data
    find /data -maxdepth 1 -name 'shelly.db*' -exec chown shelly:shelly {} +
    exec setpriv --reuid=shelly --regid=shelly --init-groups "$@"
fi
exec "$@"
