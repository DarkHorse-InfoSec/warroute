# WarRoute - Hetzner deployment runbook

End-to-end procedure for deploying WarRoute to a fresh Debian 12 Hetzner VPS.
Targeted at the existing CPX11 at `5.161.250.8` (Hetzner CPX21 also fine if a beefier box is preferred).

**This runbook is for a human operator.** Claude Code can write the files in this dir but cannot (and should not) execute these steps on the production VPS without explicit per-step confirmation. See `DECISIONS.md` for the safety boundary.

---

## 0. Pre-flight checklist

Before touching the VPS, confirm:

- [ ] DNS A record `warroute.darkhorseinfosec.com` points at the VPS IP (`5.161.250.8`). Verify with `dig +short warroute.darkhorseinfosec.com` from your laptop.
- [ ] You have SSH access to the VPS as root or a sudoer (via the `hetzner-darksentinel-MSI` or similar key per global CLAUDE.md SSH section).
- [ ] You have the WiGLE, WDGoWars, ORS, and optional Mapbox tokens in hand. Do NOT paste tokens into chat with Claude.
- [ ] You're on a clean network (cert-chain check `wdgwars.pl` via `openssl s_client` per `feedback_verify_tls_chain_before_sending_tokens.md` if uncertain). The Caddy install pulls from a public repo, not your tokens, so the bootstrap itself is fine on a school-net machine; but the post-deploy verification step (`warroute precheck` / first real upload) must be from a clean network.
- [ ] Hetzner Cloud snapshot taken of the VPS before bootstrap (insurance policy; one-click revert if anything goes wrong).

---

## 1. Bootstrap the VPS

```bash
ssh root@5.161.250.8
# OR if you've already locked down root SSH:
ssh domenic@5.161.250.8 && sudo -i

# Pull the repo (or just bootstrap.sh standalone)
git clone https://github.com/DarkHorse-InfoSec/warroute.git /tmp/warroute
bash /tmp/warroute/infra/bootstrap.sh
```

`bootstrap.sh` is idempotent: re-running is safe. It will:

- `apt update && apt -y upgrade`
- Install Python 3, sqlite3, ufw, fail2ban, Caddy (from official repo), uv.
- Create the `warroute` user + group.
- Create `/var/spool/warroute/in/`, `/var/lib/warroute/`, `/var/lib/warroute/gpx/`, `/etc/warroute/`.
- Stage `infra/systemd/warroute.service` to `/etc/systemd/system/` (but NOT enable it yet).
- Configure (but NOT enable) ufw: 80/443 allow; SSH allow is intentionally left for the operator.

It does NOT:
- Open SSH in ufw (the operator MUST add their IP before enabling ufw, otherwise the next reboot locks them out).
- Clone the repo as the warroute user.
- Write `.env` with real secrets.
- Generate the Caddy password.

---

## 2. Open SSH in ufw and enable it

```bash
# From your laptop, find your current public IP (or the static IP your home/office uses):
curl -4 ifconfig.co

# On the VPS:
ufw allow from <YOUR_IP> to any port 22 comment 'ssh from operator'
ufw --force enable
ufw status verbose  # confirm: 22 from your IP, 80/443 from anywhere
```

If you have multiple operator locations (home + office + phone hotspot), add a rule per IP. Avoid `ufw allow 22/tcp` (any-source) on a production box.

---

## 3. Clone the warroute repo as the warroute user

```bash
sudo -u warroute git clone https://github.com/DarkHorse-InfoSec/warroute.git /home/warroute/warroute
sudo -u warroute -H bash -c 'cd /home/warroute/warroute && uv sync --all-extras'
```

If the repo is private and SSH/key-based, configure the warroute user's `~/.ssh/known_hosts` and deploy key first.

---

## 4. Configure secrets at `/etc/warroute/warroute.env`

```bash
# Start from the template:
cp /home/warroute/warroute/.env.example /etc/warroute/warroute.env
chown root:warroute /etc/warroute/warroute.env
chmod 640 /etc/warroute/warroute.env  # warroute group readable, no world access
$EDITOR /etc/warroute/warroute.env
```

Fill in (do NOT paste these into chat):
- `WIGLE_NAME=`, `WIGLE_TOKEN=`
- `WDGOWARS_NAME=`, `WDGOWARS_TOKEN=`
- `ORS_API_KEY=`
- `MAPBOX_API_KEY=` (optional fallback)
- `HOME_LAT=`, `HOME_LON=`, `HOME_RADIUS_KM=` (your defaults)
- `NTFY_TOPIC=` (optional, e.g. `warroute-domenic-<random>`)
- `WEB_BASE_URL=https://warroute.darkhorseinfosec.com` (for ntfy click links)

Production overrides for paths land in `warroute.service` itself (DATABASE_URL, SPOOL_DIR, GPX_OUT_DIR point at `/var/...`); no need to set them in the env file.

---

## 5. Generate the Caddy basic-auth password and install Caddyfile

```bash
# Generate hash interactively:
caddy hash-password
# (prompts for password twice; outputs a $2a$... bcrypt hash)

# Edit infra/Caddyfile in the repo (or directly in /etc/caddy/Caddyfile):
$EDITOR /home/warroute/warroute/infra/Caddyfile
# Replace REPLACE_WITH_BCRYPT_HASH_FROM_CADDY_HASH_PASSWORD with the hash output

# Install:
cp /home/warroute/warroute/infra/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile  # syntax check
systemctl reload caddy
```

Caddy will obtain a Let's Encrypt cert on first request (HTTP-01 challenge via port 80). Watch the first request closely: `journalctl -u caddy -f` shows the cert acquisition.

---

## 6. Enable the warroute systemd service

```bash
systemctl daemon-reload  # if the service file changed since bootstrap.sh ran
systemctl enable --now warroute
systemctl status warroute  # should show "active (running)"
journalctl -u warroute -f  # watch startup logs; should see "WarRoute serving on http://127.0.0.1:8000"
```

---

## 7. Verify from outside

```bash
# Expect 401 (basic auth required):
curl -I https://warroute.darkhorseinfosec.com

# Expect 200:
curl -I -u domenic:<YOUR_PASSWORD> https://warroute.darkhorseinfosec.com

# Cert sanity:
echo | openssl s_client -showcerts -servername warroute.darkhorseinfosec.com -connect warroute.darkhorseinfosec.com:443 2>&1 | grep -E "(issuer|subject|verify return)"
# Expect issuer to be a real Let's Encrypt cert. If it's anything Fortinet/Palo Alto/Zscaler/etc.,
# you're verifying through an inspection device -- repeat from a clean network.
```

Then open `https://warroute.darkhorseinfosec.com` in a browser, authenticate, confirm the dashboard renders, plan a small route, download a GPX.

---

## 7a. Add tester accounts (beta phase)

Multi-user basic_auth at the Caddy edge (PLAN.md §9 single-tenant constraint is preserved; the app stays auth-unaware). See `DECISIONS.md` 2026-05-14 for the architectural reasoning.

```bash
# Install the helpers once (already present after a full repo clone to /home/warroute/warroute):
install -m 755 /home/warroute/warroute/infra/add-tester.sh /usr/local/sbin/warroute-add-tester
install -m 755 /home/warroute/warroute/infra/remove-tester.sh /usr/local/sbin/warroute-remove-tester

# Add a tester:
warroute-add-tester alice
# -> generates password, bcrypts, appends to /etc/caddy/Caddyfile, reloads Caddy.
# -> prints the URL + username; password file path: /etc/warroute/tester-passwords/alice.txt (mode 600)

# Retrieve a tester's password to deliver out-of-band (Signal/Slack DM; NEVER in chat):
cat /etc/warroute/tester-passwords/alice.txt

# Revoke a tester:
warroute-remove-tester alice
# -> strips the line from Caddyfile, deletes the plaintext file, reloads Caddy.

# Rotate a tester's password:
warroute-remove-tester alice && warroute-add-tester alice
```

The operator account `domenic` is protected: `remove-tester.sh` refuses to delete it. To rotate `domenic`'s password, edit `/etc/caddy/Caddyfile` manually with a hash from `caddy hash-password`, then `systemctl reload caddy`.

---

## 8. Phone-to-VPS CSV sync (Syncthing, separate runbook)

PLAN.md section 6.1 specifies Syncthing as the phone-to-VPS CSV sync mechanism. Setting up Syncthing on both the Pixel phone and the Hetzner VPS is out of scope for this runbook. Once Syncthing is paired and the WiGLE-export folder syncs to `/var/spool/warroute/in/`, the `warroute watch` daemon (a separate systemd unit, TBD) will pick up new CSVs and trigger the dual-upload pipeline.

The current `warroute.service` runs the web UI (`warroute serve`). A future addition will be `warroute-watch.service` running `warroute watch` against `/var/spool/warroute/in/`. Not landed in this runbook because it depends on Syncthing being set up first.

---

## 9. Rollback / disaster recovery

If anything goes sideways:

```bash
# Stop the service:
systemctl stop warroute

# Restore from Hetzner snapshot (from the Hetzner Cloud Console): one click.

# OR manually back out:
systemctl disable --now warroute caddy
ufw disable
# Then redeploy from a clean snapshot.
```

The SQLite DB at `/var/lib/warroute/warroute.db` is the only stateful artifact worth preserving. Snapshot daily via cron once warroute is in active use:

```bash
# /etc/cron.daily/warroute-backup
sqlite3 /var/lib/warroute/warroute.db ".backup /var/backups/warroute-$(date +%Y%m%d).db"
find /var/backups/ -name 'warroute-*.db' -mtime +30 -delete
```

---

## 10. Operational notes

- **Caddy cert renewal:** automatic, no action needed. Watch `journalctl -u caddy --since '30 days ago' | grep -i renew` to confirm.
- **fail2ban:** SSH brute-force bans are logged at `/var/log/fail2ban.log`. Whitelist your IP in `/etc/fail2ban/jail.local` if needed.
- **Disk:** SQLite + GPX outputs + Caddy logs. Monitor with `df -h /var`; rotate logs aggressively if disk becomes a constraint.
- **Updates:** `apt update && apt -y upgrade` weekly. `uv sync --no-dev` in the repo after `git pull` to refresh Python deps.
- **Secret rotation:** if any WIGLE/WDGOWARS/ORS token leaks, rotate at the provider, then update `/etc/warroute/warroute.env`, then `systemctl restart warroute`.

---

## 11. Deploying app updates (post-2026-05-14)

The live install at `/home/warroute/warroute` is a git checkout cloned via an
SSH **deploy key** added to the GitHub repo (read-only; key fingerprint
registered in the repo settings under "Deploy keys" as `warroute-prod (5.161.250.8)`).
This replaces the tarball+rsync deploy used through the v1 cutover.

### One-time setup (already done on prod, here for replay on a new box)

```bash
# As warroute user, generate an ed25519 keypair scoped to this host:
sudo -u warroute ssh-keygen -t ed25519 -f /home/warroute/.ssh/id_ed25519_github -N '' \
  -C "warroute-deploy@$(hostname -I | awk '{print $1}')"

# Pin the key for github.com (warroute user's ~/.ssh/config):
sudo -u warroute tee /home/warroute/.ssh/config <<EOF
Host github.com
  HostName github.com
  User git
  IdentityFile /home/warroute/.ssh/id_ed25519_github
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
sudo chown warroute:warroute /home/warroute/.ssh/config
sudo chmod 600 /home/warroute/.ssh/config

# On your laptop, paste the public key into the repo:
gh repo deploy-key add /path/to/id_ed25519_github.pub \
  --repo DarkHorse-InfoSec/warroute \
  --title "warroute-prod (<IP>)"
# (Leave "Allow write access" UNCHECKED - this key is read-only.)

# Sanity check from the box:
sudo -u warroute ssh -T git@github.com
# -> "Hi DarkHorse-InfoSec/warroute! You've successfully authenticated..."
```

### Routine deploy (every PR merge)

```bash
ssh warroute  # via host alias in your ~/.ssh/config
cd /home/warroute/warroute
sudo -u warroute git pull --ff-only
sudo -u warroute -H bash -lc 'cd /home/warroute/warroute && /usr/local/bin/uv sync --no-dev'
systemctl restart warroute
systemctl is-active warroute    # -> active
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8000/plan  # -> 200
```

Migrations run automatically when the app boots (the planner's first DB touch
invokes `run_migrations`); no manual SQL needed for additive schema changes.

### When `git pull --ff-only` refuses (local changes / divergence)

The deploy is meant to be one-way (laptop -> prod). If `git pull --ff-only`
refuses, something edited prod files out-of-band:

1. `git status` to see what changed.
2. Decide: commit upstream + redeploy, OR `git stash` the local change and pull.
3. Never `git reset --hard` on prod without backing up first; the safe path is
   to clone fresh into `warroute.new`, atomic-swap, and keep the old dir for
   forensics. See the 2026-05-14 git-deploy switchover for the playbook.

### Enabling the ntfy departure alarm (Phase 6b.2)

The notify-due timer scans `scheduled_departures` every minute and pushes an
ntfy alert when a planned departure is within `NTFY_DEPARTURE_LEAD_MIN` (default 5).
Only fires when `NTFY_TOPIC` is set in `/etc/warroute/warroute.env`.

```bash
ssh warroute
cp /home/warroute/warroute/infra/systemd/warroute-notify-due.service /etc/systemd/system/
cp /home/warroute/warroute/infra/systemd/warroute-notify-due.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now warroute-notify-due.timer
systemctl list-timers warroute-notify-due.timer    # confirm Next: ~1 min out
journalctl -u warroute-notify-due.service -n 20    # see scan runs ("Notified N departure(s).")
```

To raise/lower the lead time, edit `NTFY_DEPARTURE_LEAD_MIN` in
`/etc/warroute/warroute.env` and the next scan picks it up; no daemon-reload needed.

### Rollback

```bash
ssh warroute
cd /home/warroute/warroute
sudo -u warroute git log --oneline -5    # find last-known-good SHA
sudo -u warroute git checkout <SHA>
systemctl restart warroute
```

For a destructive rollback (schema migration backed something out), restore the
DB from the most recent `.backup` per section 9.

---

## What this runbook does NOT cover (yet)

- Automated CI/CD deploys (currently manual `git pull && systemctl restart warroute`).
- Monitoring / alerting beyond fail2ban + manual journalctl.
- Multi-region failover (single-tenant; if the box dies, restore from snapshot).
- ntfy.sh self-hosted instance (currently using public ntfy.sh; revisit if notification body privacy becomes a concern -- see `DECISIONS.md` 2026-05-11 ntfy entry).
