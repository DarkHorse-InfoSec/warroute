# WarRoute - Active TODO

Current phase: **v1 deployed to Hetzner prod (2026-05-14).** All five PLAN.md phases shipped + post-v1 code (precheck, infra artifacts) + Hetzner deploy executed. `https://warroute.darkhorseinfosec.com` is live, LE-cert-signed, basic-auth-gated. Next: open up to testers, then in-car drive verification, then public release.

### Session 2026-05-14 (PM, home MSI) outcome

PR #5 (geocoding + planner UX, 10 commits) resolved locally as fast-forwards. All branches now at `951435b` (the deployed prod tip):

- `main` fast-forwarded from `d9879c3` -> `951435b` (+22 commits: phases 1/3/4/5, precheck, deploy artifacts, geocoding stack, planner UX, deploy fixes, tester basic_auth).
- `feature/phase-4-web-ui` fast-forwarded from `4362bf5` -> `951435b` (+10 commits: geocoding + planner UX + deploy fixes + tester scripts).
- `feature/geocoding` already at `951435b` (no-op).
- Verified green pre-merge: 188 tests pass, ruff clean, mypy clean (34 source files).

**Remote dead.** `origin` (`https://github.com/DarkHorse-InfoSec/warroute.git`) returns 404 from both `HackingPain` and `DarkHorse-InfoSec` accounts. The repo was reachable for the 2026-05-12 PR #5 push but has since vanished (deleted? renamed? transferred?). Local repo is intact; push is blocked until a new remote is set up. Decisions needed:
- New repo owner: `HackingPain` (personal, matches `D:\Projects\Open-Source\` path) vs `darkhorse-infosec` org.
- Visibility: public (open-source) vs private.
- Name: `warroute` (matches deployed hostname + code) vs `wardriving-map` (matches local dir + PROJECT_INDEX).

Notes / caveats from the consolidation:
- Local `.venv` got rebuilt against Python 3.14 (the cached 3.13 interpreter was missing) — `uv sync --all-extras` reinstalled cleanly.
- 20 deprecation warnings on `datetime.utcnow()` from Python 3.14 in `gpx.py`, `orchestrator.py`, `planner.py`. Not blocking, but worth a cleanup pass to `datetime.now(datetime.UTC)`.
- Stale feature branches now redundant: `feature/phase-1-uploader`, `feature/phase-2-coverage`, `feature/phase-3-route-planner` all sit on commits that are reachable from main. Safe to delete locally + remotely (once we have a remote again).

### Session 2026-05-14 (AM, home MSI) outcome

Hetzner deploy executed end-to-end against `5.161.250.8`. App live at `https://warroute.darkhorseinfosec.com` behind LE cert + Caddy basic_auth (user `domenic`, password at `/root/warroute-admin-password.txt` on the box, mode 600). Migration `_v1.sql` applied. Hetzner Cloud Firewall confirmed passing 80/443. Two real infra footguns surfaced during deploy and fixed in repo (commit `ff599cc`):

- `infra/bootstrap.sh` symlinked uv into `/usr/local/bin` from `/root/.local/bin`, but `/root` is mode 700 so non-root users couldn't traverse the symlink. Switched to `cp`.
- `infra/systemd/warroute.service` had `ProtectHome=read-only` blocking `~/.cache/uv` lock acquisition. Added `HOME=/home/warroute/warroute` + `UV_CACHE_DIR=/home/warroute/warroute/.uv-cache` overrides.

Plus a DNS gotcha worth permanent recording: `darkhorseinfosec.com` is delegated to **Cloudflare**, not Squarespace, despite Squarespace having a DNS panel for it. Adding records in Squarespace is a no-op. See `memory/reference_dns_authoritative_cloudflare.md`.

### Deploy follow-ups (open)

- [ ] Delete the stale `warroute → 5.161.250.8` A record in the Squarespace DNS panel (no-op, but confusing future-you).
- [ ] Add SSH alias to `~/.ssh/config`:
      ```
      Host warroute
        HostName 5.161.250.8
        User root
        IdentityFile D:/Projects/.ssh/hetzner-warroute
        IdentitiesOnly yes
      ```
- [ ] Rotate the Cloudflare API token at `D:/Projects/.ssh/cloudflare_api_token.txt` (expired 2026-05-11). Use scope: Zone:DNS:Edit on `darkhorseinfosec.com`.
- [ ] Add a daily SQLite backup cron per `infra/README.md §9` (`sqlite3 /var/lib/warroute/warroute.db ".backup ..."` with 30-day retention). Not urgent until the DB has real data.

### Tester access (before public release)

- [x] **Decide auth model for tester access.** Picked multi-user basic_auth at the Caddy edge — preserves PLAN.md §9 single-tenant constraint (app stays auth-unaware). See `DECISIONS.md` 2026-05-14 entry. Alternatives (Cloudflare Access, Tailscale, signed URL tokens) documented there with rejection reasoning.
- [x] **Implement.** `infra/add-tester.sh` + `infra/remove-tester.sh` shipped (validates input, bcrypts via `caddy hash-password`, validates Caddyfile before installing, reloads Caddy on success). Installed to `/usr/local/sbin/warroute-add-tester` and `warroute-remove-tester` on the live box. Passwords land at `/etc/warroute/tester-passwords/<user>.txt` mode 600.
- [ ] Deliver credentials to first round of testers (out-of-band, Signal/Slack DM — never in chat or commit). Run `warroute-add-tester <name>` per tester; `cat /etc/warroute/tester-passwords/<name>.txt` to retrieve.
- [ ] Define exit criteria for promoting to public release (e.g., N testers completed M plans, zero critical issues, performance under concurrent load).
- [ ] Decide on public-release auth posture (drop basic_auth entirely vs Cloudflare Access). Defer until tester-phase signal is in.

### Session 2026-05-11 (AM, school PC) outcome

Three commits on `feature/phase-4-web-ui`:
- `077ec8a` feat(notifications): phase 5 ntfy.sh push on run-complete
- `a69f91e` feat(infra): hetzner deploy artifacts (bootstrap + systemd + Caddyfile + runbook)
- `1eac54c` feat(precheck): pre-drive sanity harness (warroute precheck)

Also recorded a security finding: school-PC NCSUVT network is TLS-intercepted by a FortiGate device (`CN=FG6H0FTB22903890`). See `DECISIONS.md` for the no-bypass-verify reasoning. Token leak status: zero (Python aborted the handshake before transmission).

Test count: 123 -> 156 (+33: 14 ntfy + 19 precheck).

### Session 2026-05-11 (PM, home MSI) outcome

Clean network (cert-chain pre-verified Let's Encrypt for all three API hosts). Unblocked the parked items that didn't require a car:

- **Precheck live-verified.** First run hit a transient WiGLE `RequestError` (cold venv after `uv` rebuilt `.venv` from missing Python ref); second run clean. WiGLE: 151 networks in home bbox. WDGoWars: user=darkhorse, wifi=34870, recent_today=0, headroom=20000. ORS: still missing key. SPOOL_DIR + GPX_OUT_DIR: now created (`spool/in`, `gpx-out`, gitignored).
- **WDGoWars 1.3.0 API surface mapped definitively.** Per-cell ownership IS NOT exposed by the API to token auth. See top DECISIONS.md entry. Newly discovered endpoints: `/api/territories` (187 gang polygons), `/api/badges`, `/api/leaderboard`, `/api/stats` (server v1.3.0). The current `cells.wdgowars_owner` NULL behavior is correct by design, not a missing-implementation bug.
- **Minor robustness gap surfaced:** when `httpx.RequestError` has empty `str()`, `clients/wigle.py:125` raises `WigleError("WiGLE request failed: ")` with no detail, and the precheck hint defaults to TLS-chain advice that's misleading on a clean network. Tracked below.

## Phase 0 - Bootstrap

- [x] `.gitignore` (protects `.env`)
- [x] `git init` on `main`
- [x] `pyproject.toml` with `uv` (FastAPI, httpx, pydantic-settings, ruff, mypy, pytest, respx)
- [x] `.env.example`
- [x] `README.md`
- [x] `CLAUDE.md` (project-level)
- [x] `.claude/settings.local.json` (tailored permissions, deny rules for `.env`)
- [x] `warroute/` package skeleton + `config.py` + `db.py` + `cli.py`
- [x] `migrations/_v1.sql` (sessions, observations, cells, planned_routes)
- [x] `tests/` harness + `conftest.py` + smoke tests
- [x] `tasks/todo.md` + `tasks/lessons.md`
- [x] `uv sync --all-extras` succeeds (53 packages installed)
- [x] `uv run ruff check .` clean
- [x] `uv run mypy warroute` clean (8 source files)
- [x] `uv run pytest` green (8/8 passed)
- [x] First commit on `main` (shipped; project is on `feature/phase-4-web-ui` with Phases 1-4 merged)

## Phase 0 acceptance

`git clone`, `uv sync --all-extras`, `uv run pytest`, `uv run ruff check .`, `uv run mypy warroute` all succeed on a fresh checkout.

---

## Phase 2 - Coverage analyzer (DONE)

- [x] `clients/wigle.py` - WiGLE search API client, throttled to 1 req/sec
- [x] `clients/wdgowars.py` - WDGoWars client, /api/me + probe + upload skeleton
- [x] `coverage/grid.py` - aligned 2x3 km cell grid generator
- [x] `coverage/cells.py` - DAL: upsert, density, ownership, stale filter
- [x] `coverage/sync.py` - orchestration: paint grid + WDGoWars + WiGLE
- [x] `coverage/report.py` - text summary
- [x] CLI: `warroute coverage refresh|report|probe-wdgowars`
- [x] DECISIONS.md created (WDGoWars endpoints undocumented; phase-skip reasoning; HTTPS push)
- [x] 50/50 tests passing, ruff + mypy clean

### Phase 2 acceptance status

- Logic and structure complete; full end-to-end against real APIs pending:
  - WiGLE rate limit budget for ~1300 cells at 50 km radius = ~22 minutes for first refresh. Acceptable.
  - WDGoWars endpoint shapes for owned-cell list need confirmation via `coverage probe-wdgowars` (run when convenient).

---

## Phase 1 - Dual-uploader (DONE - back-built after Phase 2/3 due to ordering miss)

- [x] `uploader/parser.py` - WigleWifi-1.6 CSV parser, dedup-by-BSSID-keep-strongest, sha256
- [x] `uploader/wigle_upload.py` - POST to WiGLE.net `/api/v2/file/upload`, 429 backoff
- [x] `uploader/wdgowars_upload.py` - quota pre-flight via `/api/me`, then upload
- [x] `uploader/orchestrator.py` - parallel dual-upload + sessions/observations write + sha256 idempotency
- [x] `uploader/watcher.py` - watchdog daemon on `SPOOL_DIR`, fires on close-write
- [x] CLI: `warroute upload <file>` and `warroute watch`
- [x] Fixture CSV + 75/75 tests passing, ruff + mypy clean
- [x] Bugfix: WDGoWars `/api/me` parser was treating `0` as falsy in `or` chains; switched to `_first_present()` (key-presence checks)
- [x] Bugfix: dropped `sqlite3.PARSE_DECLTYPES` (it choked on ISO-T timestamps)

---

## Phase 3 - Route planner (DONE, awaiting ORS key for live verification)

- [x] `clients/ors.py` - async OpenRouteService client (directions + optimization)
- [x] `router/scorer.py` - native scoring via WDGoWars capture_value x WiGLE density
- [x] `router/planner.py` - greedy pick + optimization + back-off if over budget
- [x] `router/gpx.py` - GPX 1.1 writer + Google Maps multi-stop URL
- [x] `planned_routes` table persistence integrated into planner
- [x] CLI: `warroute plan --duration 90m --mode loop|oneway --out drive.gpx`
- [x] 83/83 tests passing, ruff + mypy clean

### Phase 3 acceptance status

- All logic and structure complete with mocked ORS responses.
- Live verification blocked on ORS_API_KEY (Domenic adding tomorrow).
- Empirical verification (3x new APs/min vs commute baseline) requires a real drive.

---

## Phase 4 - Mobile-friendly UI (DONE)

- [x] FastAPI app factory with lifespan handler (no on_event deprecation)
- [x] Base layout: HTMX + Leaflet via CDN, mobile dark theme, sticky topbar nav
- [x] `/` dashboard - WDGoWars player card, recent-runs table, cell counts
- [x] `/plan` form (GET) + planner result (POST) with Leaflet map, GMaps deep-link, GPX download
- [x] `/coverage` - Leaflet map of all cells colored by ownership, GeoJSON feed
- [x] `/runs/{id}` - session breakdown + predicted vs actual when a plan is associated
- [x] `/settings` - read-only display, secret values masked to last4 only
- [x] CLI: `warroute serve [--host --port --reload]`
- [x] 123/123 tests passing, ruff + mypy clean
- [x] Smoke-tested live: all 5 routes + GeoJSON endpoint return 200

---

## Phase 5 - Push notifications (DONE, run-complete only; plan + quota toggles for future)

- [x] `warroute/clients/ntfy.py` - async ntfy.sh client, best-effort (never raises on failure)
- [x] `config.py` settings: `NTFY_TOPIC`, `NTFY_BASE_URL` (default https://ntfy.sh), `NTFY_AUTH_TOKEN`, `NTFY_NOTIFY_RUN` (default true), `NTFY_NOTIFY_PLAN` (default false, future), `NTFY_NOTIFY_QUOTA` (default false, future), `WEB_BASE_URL` (for click links)
- [x] `uploader/orchestrator.py` hook: `_send_run_notification()` fires post-ingest if topic set + toggle on; failure swallowed
- [x] Tests: 9 new in `test_ntfy_client.py` + 5 new in `test_orchestrator.py`. 137/137 passing total. Ruff + mypy clean.
- [x] DECISIONS.md entry documenting the togglable design (run-complete v1, plan + quota wired-but-no-op for future)
- [ ] `.env.example` additions (blocked: Read(./.env.*) deny rule. Domenic to paste 7 lines manually -- see commit message / chat for the snippet)
- [ ] (Future) Wire plan-complete notification path -- emit from `router/planner.py` after GPX write
- [ ] (Future) Wire quota-warning watcher -- separate systemd timer job, fires once/day when WiGLE or ORS quota <10%
- [ ] (Future) Decide whether to point `NTFY_BASE_URL` at self-hosted `ntfy.darkhorseinfosec.com` vs public ntfy.sh for run-summary privacy

## Hetzner deploy artifacts (DONE: files in repo, NOT executed on VPS)

- [x] `infra/bootstrap.sh` - idempotent Debian 12 provisioner: apt update, install python3/sqlite3/ufw/fail2ban/Caddy/uv, create warroute user + group + dirs, stage systemd unit, configure (but not enable) ufw
- [x] `infra/systemd/warroute.service` - systemd unit with hardening (NoNewPrivileges, ProtectSystem=strict, ReadWritePaths scoped to /var/lib/warroute + /var/spool/warroute + /home/warroute/warroute, MemoryDenyWriteExecute, RestrictAddressFamilies, etc.). EnvironmentFile=/etc/warroute/warroute.env
- [x] `infra/Caddyfile` - TLS auto via Let's Encrypt + basic_auth + reverse_proxy 127.0.0.1:8000 + HSTS/CSP/X-Frame-Options headers + JSON access log
- [x] `infra/README.md` - 10-section runbook: pre-flight (DNS, SSH, snapshot), bootstrap, ufw setup, repo clone, secrets at /etc/warroute/warroute.env, Caddy password generation, systemd enable, external verification, Syncthing pointer, rollback, ops notes
- [x] EXECUTE: actual deployment to 5.161.250.8 (2026-05-14, home MSI clean network). App live at `https://warroute.darkhorseinfosec.com` with LE cert + Caddy basic_auth. Two infra fixes landed (commit `ff599cc`). DNS at Cloudflare, not Squarespace.

## Pre-drive sanity harness (DONE, code shipped; live execution parked)

- [x] `warroute/precheck.py` - four health checks: WIGLE auth (search_bbox at home), WDGoWars (/api/me + quota headroom), ORS (2-point directions call), filesystem (SPOOL_DIR + GPX_OUT_DIR writability). External checks run concurrently via asyncio.gather.
- [x] `CheckResult` dataclass with status (ok|warn|fail), detail, and actionable hint. `verdict()` helper for overall.
- [x] CLI: `warroute precheck` with colored per-check output and exit code (0=PASS, 1=WARN, 2=FAIL). Chainable into shell scripts and phone shortcuts.
- [x] Tests: 19 new in `test_precheck.py` with respx mocks covering each check's happy/warn/fail paths plus the run_all() verdict logic. 156/156 total passing. Ruff + mypy clean.
- [ ] LIVE EXECUTION: parked until clean network (must NOT run from school PC on NCSUVT Fortinet network -- tokens transit the inspection device).

### Parked for clean-network session (see DECISIONS.md 2026-05-11 TLS interception entry)

- [x] **Find WDGoWars territory-enumeration endpoint** (probed 2026-05-11 PM from home MSI). Answer: WDGoWars 1.3.0 does not expose per-cell ownership to API-token auth. See top DECISIONS.md entry + memory `reference_wdgowars_api.md`. `cells.wdgowars_owner` stays NULL by design.
- [ ] Live verification: real wardrive run, real ORS plan, real WDGoWars upload (requires being in a car AND on a clean network)

### Unblocked enhancements from the 2026-05-11 PM API-surface probe (post-v1, queued)

- [ ] **Gang-territory overlay on `/coverage`.** Pull `/api/territories` and render the 187 gang `hull` polygons as colored layers on the Leaflet map. Highlight `gang_id==16` ("Biscuits", our gang) distinctly. New file `warroute/clients/wdgowars.py` method `gang_territories()` + a `/coverage/gangs.geojson` route + Leaflet layer.
- [ ] **Richer `/api/me` parsing.** Extend `WdgowarsClient.me()` and the `PlayerState` dataclass to surface the 16 currently-unmapped fields: `country, joined, is_superuser, trusted, gang, gang_id, gang_role, mesh, cracked, aircraft, recent_7d, reinforce (per-zoom-counts), reinforce_total, credits {balance/bounties/lifetime}, badges`. Web dashboard's player card can then show gang affiliation, 7-day activity, credits balance, badge count.
- [ ] **Server-version awareness.** Cheap GET to `/api/stats.version` at precheck time; warn if WDGoWars version changes (might expose new endpoints worth re-probing).
- [x] **Precheck robustness fix.** (Done 2026-05-11 PM.) Two changes:
  - `check_wigle()` now uses `/api/v2/profile/user` (sub-second, auth-only) instead of `search_bbox` (which hit the slow network index and timed out at 30-60s on the free tier).
  - All three clients (`wigle.py`, `wdgowars.py`, `ors.py`) now include `type(exc).__name__` in the wrapped error message, so empty-`str()` exceptions like `ReadTimeout('')` still surface their type in precheck detail. Includes a regression test (`test_profile_request_error_includes_exception_type`).
  - Test count: 156 -> 160 (+4 new WigleClient.profile() coverage).

### `.env.example` commit

- [ ] **Commit `.env.example` ntfy block** (currently uncommitted; Domenic pasted the 7 lines manually). Minor formatting: one stray blank line before the block + no trailing newline. Either Domenic tidies and commits, or widen the `Read(./.env.example)` permission and let me edit it.
