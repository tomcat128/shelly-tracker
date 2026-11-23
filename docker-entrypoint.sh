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
    if ! chown shelly:shelly /data 2>/dev/null; then
        echo "ERROR: /data is not writable (mounted read-only?)." >&2
        echo "       The database volume must be mounted read-write, e.g. './data:/data' (without ':ro')." >&2
        exit 1
    fi
    find /data -maxdepth 1 -name 'shelly.db*' -exec chown shelly:shelly {} +
    exec setpriv --reuid=shelly --regid=shelly --init-groups "$@"
fi
exec "$@"
