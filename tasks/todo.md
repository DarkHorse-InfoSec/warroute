# WarRoute - Active TODO

Current phase: **Phase 2 (coverage analyzer)** - per Domenic's 2026-05-10 direction to skip the dual-uploader for now.

> Note: PLAN.md sequencing was Phase 1 (uploader) -> Phase 2 (coverage). Phase 1 will be revisited once Domenic decides whether to go fully automated upload or stay manual.

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

## Phase 1 - Dual-uploader (deferred)

Skipped per Domenic 2026-05-10 (DECISIONS.md). Manual upload until route planner ships.

- [ ] `uploader/parser.py` - WigleWifi-1.6 CSV parser, dedup within file
- [ ] `uploader/wigle.py` - POST to WiGLE.net upload endpoint, 429 backoff
- [ ] `uploader/wdgowars.py` - already has `upload_csv` skeleton in clients/wdgowars.py; promote + harden
- [ ] `uploader/watcher.py` - watchdog daemon on `SPOOL_DIR`
- [ ] `cli.py`: add `warroute upload <file>` and `warroute watch`

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

## Phase 4 - Mobile-friendly UI (next)

Pages: `/`, `/plan`, `/coverage`, `/runs/<id>`, `/settings`. FastAPI + Jinja2 + HTMX + Leaflet. HTTP basic auth at Caddy layer. See `PLAN.md` §3.4.
