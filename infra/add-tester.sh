#!/usr/bin/env bash
# add-tester.sh <username>
#
# Adds a basic_auth user to /etc/caddy/Caddyfile, generates a strong random
# password, bcrypts it via `caddy hash-password`, validates the new config,
# reloads Caddy. Plaintext lands at /etc/warroute/tester-passwords/<user>.txt
# (mode 600, root-readable only) so the operator can fetch it later.
#
# Run as root on the deployed box. Idempotent: re-running for an existing
# user is rejected (use remove-tester.sh + add-tester.sh to rotate).
#
# See DECISIONS.md 2026-05-14 entry for the rationale.

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "add-tester.sh must run as root" >&2
    exit 1
fi

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <username>" >&2
    exit 1
fi

USER="$1"

# Username must be 2-32 chars, lowercase alphanumeric plus _ and -.
# Prevents shell-meta injection into the awk script and the secrets-dir path.
if [[ ! "$USER" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]]; then
    echo "username must match ^[a-z0-9][a-z0-9_-]{1,31}\$" >&2
    exit 1
fi

CADDYFILE=/etc/caddy/Caddyfile
SECRETS_DIR=/etc/warroute/tester-passwords

if [[ ! -f "$CADDYFILE" ]]; then
    echo "$CADDYFILE not found; is Caddy deployed?" >&2
    exit 1
fi

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# Reject duplicates. Matches "    <user> $2..." inside any basic_auth block.
if awk -v user="$USER" '
    /^[[:space:]]*basic_auth[[:space:]]*\{/ { in_auth = 1; next }
    in_auth && /^[[:space:]]*\}/ { in_auth = 0; next }
    in_auth && $1 == user { found = 1; exit }
    END { exit !found }
' "$CADDYFILE"; then
    echo "user '$USER' already present in $CADDYFILE; aborting" >&2
    exit 2
fi

PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")
HASH=$(caddy hash-password --plaintext "$PW")

NEW_FILE="$(mktemp)"
trap 'rm -f "$NEW_FILE"' EXIT

awk -v user="$USER" -v hash="$HASH" '
    BEGIN { in_auth = 0; inserted = 0 }
    /^[[:space:]]*basic_auth[[:space:]]*\{/ && !inserted { in_auth = 1; print; next }
    in_auth && /^[[:space:]]*\}/ && !inserted {
        print "        " user " " hash
        inserted = 1
        in_auth = 0
    }
    { print }
    END { exit !inserted }
' "$CADDYFILE" > "$NEW_FILE"

if ! caddy validate --config "$NEW_FILE" --adapter caddyfile >/dev/null 2>&1; then
    echo "Caddyfile validation failed after insertion; aborting" >&2
    exit 3
fi

# Commit: install Caddyfile, save plaintext, reload Caddy.
install -m 644 "$NEW_FILE" "$CADDYFILE"

umask 077
printf "%s\n" "$PW" > "$SECRETS_DIR/$USER.txt"

systemctl reload caddy

echo "added tester '$USER'"
echo "  url:      https://warroute.darkhorseinfosec.com"
echo "  username: $USER"
echo "  password: $SECRETS_DIR/$USER.txt   (cat as root to retrieve)"
