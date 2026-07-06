#!/usr/bin/env bash
# remove-tester.sh <username>
#
# Removes a basic_auth user from /etc/caddy/Caddyfile, deletes their
# plaintext password file, validates the new config, reloads Caddy.
# Refuses to remove "admin" (the operator account) as a safety guard.

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "remove-tester.sh must run as root" >&2
    exit 1
fi

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <username>" >&2
    exit 1
fi

USER="$1"

if [[ "$USER" == "admin" ]]; then
    echo "refusing to remove the operator account 'admin'" >&2
    exit 1
fi

if [[ ! "$USER" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
    echo "username must match ^[a-z0-9][a-z0-9_-]{1,31}\$" >&2
    exit 1
fi

CADDYFILE=/etc/caddy/Caddyfile
SECRETS_DIR=/etc/warroute/tester-passwords

NEW_FILE="$(mktemp)"
trap 'rm -f "$NEW_FILE"' EXIT

# Drop the matching line inside any basic_auth block. Match on $1 == user
# so we never strip a comment or another identifier that happens to contain the name.
awk -v user="$USER" '
    /^[[:space:]]*basic_auth[[:space:]]*\{/ { in_auth = 1; print; next }
    in_auth && /^[[:space:]]*\}/ { in_auth = 0; print; next }
    in_auth && $1 == user { removed = 1; next }
    { print }
    END { exit !removed }
' "$CADDYFILE" > "$NEW_FILE" || {
    echo "user '$USER' not found in $CADDYFILE" >&2
    exit 2
}

if ! caddy validate --config "$NEW_FILE" --adapter caddyfile >/dev/null 2>&1; then
    echo "Caddyfile validation failed after removal; aborting" >&2
    exit 3
fi

# Mode 640 root:caddy: the bcrypt tester hashes are not world-readable.
install -m 640 -o root -g caddy "$NEW_FILE" "$CADDYFILE"
rm -f "$SECRETS_DIR/$USER.txt"
systemctl reload caddy

echo "removed tester '$USER'"
