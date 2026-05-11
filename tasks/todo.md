# WarRoute - Active TODO

Current phase: **v1 code-complete + live-verified on home network.** All five PLAN.md phases shipped, plus post-v1 code (precheck, infra artifacts). WDGoWars territory-enumeration question is now answered (definitive: not exposed by the API). Remaining live-execution work: ORS key, in-car drive verification, Hetzner deploy.

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
- [ ] EXECUTE: actual deployment to 5.161.250.8 (deferred; Domenic's go-ahead required + must be on clean network per Fortinet finding)

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
- [ ] **Precheck robustness fix.** When `httpx.RequestError` has empty `str()` (e.g. transport-layer issues with no message), `clients/wigle.py` raises `WigleError("WiGLE request failed: ")` with no detail and the precheck hint suggests TLS-chain check, which is misleading. Use `repr(exc)` or `type(exc).__name__: {exc!s}` so the type at minimum is in the message. Same review for `clients/wdgowars.py` and `clients/ors.py`.

### `.env.example` commit

- [ ] **Commit `.env.example` ntfy block** (currently uncommitted; Domenic pasted the 7 lines manually). Minor formatting: one stray blank line before the block + no trailing newline. Either Domenic tidies and commits, or widen the `Read(./.env.example)` permission and let me edit it.
