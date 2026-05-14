#!/usr/bin/env bash
# bootstrap.sh - one-time provisioning script for a fresh Hetzner Debian 12 VPS.
#
# Idempotent: safe to re-run. Each step is gated so re-runs short-circuit on
# already-installed packages, existing users, etc.
#
# Usage (as root, on the VPS):
#   ssh root@<VPS_IP>
#   curl -fsSL https://raw.githubusercontent.com/DarkHorse-InfoSec/warroute/main/infra/bootstrap.sh | bash
#   # OR copy the file up and: bash bootstrap.sh
#
# What this does NOT do (operator follows up in infra/README.md):
#   - Open SSH port to your IP in ufw (operator must add)
#   - Clone the warroute repo as the warroute user
#   - Write .env with real secrets
#   - Generate Caddy basicauth password
#   - Configure DNS for warroute.darkhorseinfosec.com
#   - Configure Syncthing for phone-to-VPS CSV sync (separate runbook)

set -euo pipefail

WARROUTE_USER="warroute"
WARROUTE_HOME="/home/${WARROUTE_USER}"
SPOOL_DIR="/var/spool/warroute/in"
LIB_DIR="/var/lib/warroute"
GPX_DIR="${LIB_DIR}/gpx"
ETC_DIR="/etc/warroute"

# ----- 0. preflight ----------------------------------------------------------
if [[ "$(id -u)" -ne 0 ]]; then
    echo "bootstrap.sh must run as root (try: sudo bash bootstrap.sh)" >&2
    exit 1
fi

if ! grep -q "^ID=debian" /etc/os-release; then
    echo "WARNING: not Debian. This script targets Debian 12. Continuing anyway..." >&2
fi

echo "==> bootstrap.sh starting on $(hostname) at $(date -Is)"

# ----- 1. system update + base packages --------------------------------------
echo "==> apt update + upgrade"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade

echo "==> installing base packages"
apt-get install -y \
    python3 python3-venv python3-pip \
    git curl ca-certificates \
    sqlite3 \
    ufw fail2ban \
    inotify-tools rsync \
    debian-keyring debian-archive-keyring apt-transport-https

# ----- 2. Caddy (from official repo for current version) ---------------------
if ! command -v caddy >/dev/null 2>&1; then
    echo "==> installing Caddy from official repo"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
else
    echo "==> Caddy already installed: $(caddy version)"
fi

# ----- 3. uv (Python package manager used by warroute) -----------------------
if ! command -v uv >/dev/null 2>&1; then
    echo "==> installing uv (Astral)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to /root/.local/bin by default when running as root, but /root is
    # mode 700, so a symlink would fail for non-root users (e.g. the warroute user).
    # Copy the binary into /usr/local/bin so it is world-executable.
    if [[ -x /root/.local/bin/uv ]]; then
        cp /root/.local/bin/uv /usr/local/bin/uv
        chmod 755 /usr/local/bin/uv
    fi
    if [[ -x /root/.local/bin/uvx ]]; then
        cp /root/.local/bin/uvx /usr/local/bin/uvx
        chmod 755 /usr/local/bin/uvx
    fi
else
    echo "==> uv already installed: $(uv --version)"
fi

# ----- 4. warroute user + dirs -----------------------------------------------
if ! id "${WARROUTE_USER}" >/dev/null 2>&1; then
    echo "==> creating user ${WARROUTE_USER}"
    useradd -m -s /bin/bash "${WARROUTE_USER}"
else
    echo "==> user ${WARROUTE_USER} already exists"
fi

echo "==> creating /var dirs"
mkdir -p "${SPOOL_DIR}" "${LIB_DIR}" "${GPX_DIR}" "${ETC_DIR}"
chown -R "${WARROUTE_USER}:${WARROUTE_USER}" "${SPOOL_DIR}" "${LIB_DIR}" "${GPX_DIR}"
chmod 750 "${ETC_DIR}"
# .env will land in ${ETC_DIR}; only root reads it (mode 600 enforced by operator)

# ----- 5. firewall (ufw) -----------------------------------------------------
echo "==> configuring ufw"
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow 80/tcp comment 'http (caddy auto-tls challenge + redirect)'
ufw allow 443/tcp comment 'https (caddy)'
# SSH port intentionally NOT opened here -- operator MUST run:
#   ufw allow from <YOUR_IP> to any port 22
# before enabling ufw (otherwise we lock ourselves out)
echo "WARNING: ufw is configured but NOT enabled. Add your SSH IP and enable manually:"
echo "  ufw allow from <YOUR_IP> to any port 22 comment 'ssh from operator'"
echo "  ufw --force enable"

# ----- 6. fail2ban -----------------------------------------------------------
echo "==> enabling fail2ban (sshd jail default)"
systemctl enable --now fail2ban

# ----- 7. systemd unit (placed but NOT enabled until repo is cloned) ---------
SYSTEMD_UNIT="/etc/systemd/system/warroute.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/systemd/warroute.service" ]]; then
    echo "==> installing systemd unit"
    cp "${SCRIPT_DIR}/systemd/warroute.service" "${SYSTEMD_UNIT}"
    systemctl daemon-reload
    echo "    enable + start manually after repo clone + .env:"
    echo "      systemctl enable --now warroute"
else
    echo "==> systemd unit not co-located with bootstrap.sh; install manually from infra/systemd/warroute.service"
fi

# ----- 8. summary ------------------------------------------------------------
echo ""
echo "==> bootstrap.sh complete."
echo ""
echo "Next steps (manual; see infra/README.md):"
echo "  1. Allow SSH from your IP and enable ufw:"
echo "       ufw allow from <YOUR_IP> to any port 22"
echo "       ufw --force enable"
echo "  2. Clone the repo as the warroute user:"
echo "       sudo -u ${WARROUTE_USER} git clone https://github.com/DarkHorse-InfoSec/warroute.git ${WARROUTE_HOME}/warroute"
echo "  3. Install Python deps:"
echo "       sudo -u ${WARROUTE_USER} -H bash -c 'cd ${WARROUTE_HOME}/warroute && uv sync --all-extras'"
echo "  4. Write secrets to ${ETC_DIR}/warroute.env (mode 600, owner root):"
echo "       cp ${WARROUTE_HOME}/warroute/.env.example ${ETC_DIR}/warroute.env"
echo "       \$EDITOR ${ETC_DIR}/warroute.env  # fill in real tokens"
echo "       chmod 600 ${ETC_DIR}/warroute.env"
echo "  5. Generate Caddy basicauth password and install Caddyfile:"
echo "       caddy hash-password --plaintext '<YOUR_PASSWORD>'"
echo "       \$EDITOR ${WARROUTE_HOME}/warroute/infra/Caddyfile  # paste hash"
echo "       cp ${WARROUTE_HOME}/warroute/infra/Caddyfile /etc/caddy/Caddyfile"
echo "       systemctl reload caddy"
echo "  6. Enable warroute service:"
echo "       systemctl enable --now warroute"
echo "  7. Verify (from your laptop, not the VPS):"
echo "       curl -I https://warroute.darkhorseinfosec.com  # expect 401 (basic auth required)"
echo "       curl -I -u domenic:<YOUR_PASSWORD> https://warroute.darkhorseinfosec.com  # expect 200"
echo ""
