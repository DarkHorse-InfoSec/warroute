# WarRoute - Active TODO

Current phase: **All five PLAN.md phases shipped.** Live verification + Phase 5 (push notifications) remain.

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
- [ ] First commit on `main` (awaiting Domenic's go-ahead)

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

## Remaining (post-v1)

- [ ] Live verification: real wardrive run, real ORS plan, real WDGoWars upload
- [ ] Phase 5 (PLAN.md §3.5): push notifications via ntfy.sh on run-complete
- [ ] Hetzner deploy: `infra/bootstrap.sh`, systemd unit for `warroute serve`, Caddy reverse proxy + basic auth
- [ ] Find WDGoWars territory-enumeration endpoint (probe `/api/territory`, `/api/cells`, `/api/gang/{id}` etc.)
