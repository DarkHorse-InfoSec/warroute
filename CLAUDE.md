# CLAUDE.md - WarRoute

## Project Overview

**Project:** WarRoute
**Description:** Wardriving route planner + dual-uploader (WiGLE.net + WDGoWars). Single-user, mobile-friendly web UI, safe-to-use (plan-before-drive only, no live in-drive UI).
**Stack:** Python 3.11, FastAPI, SQLite, HTMX + Leaflet. Routing via OpenRouteService API (no self-hosted OSRM).
**Owner:** Domenic Laurenzi
**Source of truth for design decisions:** [`PLAN.md`](./PLAN.md)

---

## Workflow

Follow the Boris Cherny method documented in the global `~/.claude/CLAUDE.md`. Project-specific notes:

- **Phase gates matter.** Don't slide from one phase into the next without acceptance criteria passing. The phases in `PLAN.md` are independent shippable increments by design.
- **`PLAN.md` is canon.** If a real architectural fork emerges (something the plan didn't anticipate, or where the plan turned out wrong), write the question + your proposed answer to `DECISIONS.md` and ask Domenic before changing direction.
- **`tasks/todo.md`** tracks current phase work. **`tasks/lessons.md`** captures corrections.

---

## Directory Structure

```
warroute/                          # Python package
  __init__.py
  config.py                        # pydantic-settings, loads .env
  db.py                            # SQLite helpers (+ migration runner)
  cli.py                           # typer CLI entrypoint
  clients/                         # shared HTTP clients (used by uploader, coverage, router)
    wigle.py                       # WiGLE.net search; throttled to 1 req/sec
    wdgowars.py                    # WDGoWars; X-API-Key auth (NOT Bearer)
    ors.py                         # OpenRouteService (worldwide routing)
  uploader/                        # Phase 1: dual-uploader
    parser.py                      # WigleWifi-1.6 CSV parser, sha256 dedup
    wigle_upload.py                # POST /api/v2/file/upload
    wdgowars_upload.py             # quota pre-flight + POST /api/upload-csv
    orchestrator.py                # parse + parallel uploads + DB writes
    watcher.py                     # watchdog daemon on SPOOL_DIR
  coverage/                        # Phase 2: cell ownership / density
    grid.py                        # 2x3 km aligned grid, haversine clipping
    cells.py                       # SQLite DAL
    sync.py                        # paint grid + WDGoWars + WiGLE refresh
    report.py                      # text summary
  router/                          # Phase 3: route planner
    scorer.py                      # native scoring (WDGoWars value x WiGLE density)
    planner.py                     # greedy pick + ORS optimize + back-off
    gpx.py                         # GPX 1.1 + Google Maps multi-stop URL
  web/                             # Phase 4: FastAPI + HTMX + Leaflet
    app.py                         # app factory with lifespan handler
    templating.py                  # Jinja2 helper
    routes/                        # one file per page (dashboard, plan, coverage, runs, settings)
    templates/                     # base.html + per-page templates
    static/                        # app.css (mobile-first dark theme)
migrations/                        # SQL migration files (_v1.sql, _v2.sql, ...)
infra/                             # bootstrap.sh, systemd units, Caddyfile (TBD)
tests/
  fixtures/                        # sample_wiglewifi.csv etc.
  unit/                            # all current tests; mocked external calls via respx
  integration/                     # gated by RUN_INTEGRATION=1
tasks/
  todo.md                          # active work / phase status
  lessons.md                       # patterns learned from corrections
PLAN.md                            # canonical design spec
DECISIONS.md                       # architectural forks + their resolutions (newest at top)
README.md
CLAUDE.md                          # this file
.env                               # SECRETS - never commit (gitignored)
.env.example
pyproject.toml                     # deps via uv
```

## Key Files & Paths

| File/Dir | Purpose |
|----------|---------|
| `PLAN.md` | Canonical design + build phases. Read first if you forget anything. |
| `DECISIONS.md` | Architectural forks + resolutions (newest at top). Includes the WDGoWars `X-API-Key` discovery, the OSRM-to-OpenRouteService swap, and the phase-ordering reversal. |
| `.env` | Secrets (WIGLE, WDGOWARS, ORS keys + Hetzner IP). **Gitignored.** |
| `migrations/_v1.sql` | Initial SQLite schema (sessions, observations, cells, planned_routes). |
| `tasks/todo.md` | Active work + phase status. |
| `tasks/lessons.md` | Patterns learned from corrections. Update after every redirect. |

## Environment & Commands

```bash
uv sync --all-extras                                  # install deps
uv run pytest                                         # tests (currently 100+; respx mocks externals)
uv run ruff check .                                   # lint
uv run ruff format .                                  # format
uv run mypy warroute                                  # type check (strict)
uv run warroute --help                                # CLI
uv run warroute serve                                 # web UI on http://127.0.0.1:8000
uv run warroute serve --host 0.0.0.0 --reload         # dev: bind LAN, auto-reload
RUN_INTEGRATION=1 uv run pytest tests/integration    # real-API tests (rare)
```

### Windows footgun

Git Bash on Windows mangles Unix-style path arguments (e.g. `/api/me` -> `C:/Program Files/Git/api/me`). Prefix with `MSYS_NO_PATHCONV=1` for any CLI call that passes a Unix-looking path:

```bash
MSYS_NO_PATHCONV=1 uv run warroute coverage probe-wdgowars /api/me
```

## Environment Variables (.env, never commit)

See `.env.example` for the full list. Required from day one:
- `WIGLE_NAME`, `WIGLE_TOKEN` (https://wigle.net/account)
- `WDGOWARS_NAME`, `WDGOWARS_TOKEN` (https://wdgwars.pl/profile)
- `ORS_API_KEY` (https://openrouteservice.org/dev/#/signup)
- `HETZNER_IP_ADDR` (deploy target)

---

## Coding Standards

- **Python 3.11+**, type hints everywhere, mypy strict mode.
- **Formatting:** ruff format (double quotes, line length 100).
- **Linting:** ruff check (E/F/W/I/B/UP/SIM/RUF rule sets).
- **Async-first:** the uploader hits two external APIs in parallel; use `asyncio.gather`. The web layer is FastAPI async.
- **Errors:** raise specific exceptions, never bare `except`. Log at the boundary that handles the exception, not at every level.
- **Logging:** `logging` stdlib with module-level `logger = logging.getLogger(__name__)`. JSON output in prod, human-readable in dev.
- **No em dashes.** Use comma, semicolon, or colon. Applies to prose, comments, docstrings, error messages, everywhere.
- **No `Co-Authored-By:` trailers** on commits.

### Commit conventions

```
feat:     New feature
fix:      Bug fix
chore:    Maintenance / tooling
docs:     Documentation only
refactor: Code restructure, no behavior change
test:     Adding or updating tests
```

Branch naming: `feature/`, `fix/`, `refactor/`, `hotfix/`. Never push to `main` directly.

### Testing Policy

- Unit tests for every new function in `warroute/`. Use fixtures in `tests/fixtures/`.
- HTTP-touching code: mock with `respx`, not real network.
- Integration tests gated by `RUN_INTEGRATION=1`. They hit real APIs with a tiny throwaway CSV. Run sparingly.
- A change isn't "done" until `pytest && ruff check && mypy warroute` all pass.

---

## Architecture Notes

- **Worldwide routing via OpenRouteService API.** PLAN.md §6.2 captures why we dropped self-hosted OSRM. Don't reintroduce OSRM without re-opening that decision in `DECISIONS.md`.
- **Native scoring (WDGoWars + WiGLE), no custom formula.** PLAN.md §6.4. The scorer combines numbers from those two services rather than rolling its own weighting.
- **Phone -> Hetzner via Syncthing.** PLAN.md §6.1. The `watcher` daemon watches `SPOOL_DIR` for new CSVs and triggers the dual-upload pipeline.
- **Single-tenant.** No login, no JWT, no session management. HTTP basic auth at the Caddy layer. Don't add a user model.
- **Plan-before-drive only.** No live in-drive UI in v1. Documented in `PLAN.md` §9 as a critical safety constraint, not a missing feature.

---

## Known Issues & Gotchas

- **WDGoWars auth is `X-API-Key`, NOT `Authorization: Bearer`.** Empirically probed 2026-05-11; Bearer/Token/raw all 401. See `DECISIONS.md` for the discovery + the full `/api/me` response shape.
- **`/api/me` does NOT return owned-cell IDs.** Only `reinforce: {zoom_level: count}` aggregates. The `cells.wdgowars_owner` column stays mostly NULL until we find the territory-enumeration endpoint (TBD; candidates: `/api/territory`, `/api/cells`, `/api/gang/{id}`).
- **WDGoWars `/api/me` has no `daily_quota_remaining`.** Derived as `20000 - recent_today` (the cap from PLAN.md §3.1).
- **Don't `payload.get(a) or payload.get(b)` for numeric/boolean fields.** Python `or` returns first *truthy*, not first non-`None`. Caused a real WDGoWars quota-bypass bug. Use the `_first_present(payload, *keys)` helper in `clients/wdgowars.py`.
- **`sqlite3.PARSE_DECLTYPES` chokes on ISO-T timestamps.** Removed from `db.connect()`. We parse timestamps manually elsewhere.
- WiGLE.net query API is rate-limited (1 req/sec free tier). Cell density lookups cached for 24h in SQLite.
- WDGoWars caps new APs at 20k per 24h per account. The uploader checks `/api/me` headroom (`recent_today` field) before posting; if the CSV would overflow, it raises `WdgowarsQuotaSkip` (logged but doesn't fail the run).
- ORS free tier: 2000 directions/day, 500 optimization/day. Watch quota on multi-plan days; Mapbox fallback wired but not yet implemented.
- WDGoWars is early-stage; pin endpoints, monitor changelog, keep the client thin.
- **Windows + Git Bash mangles `/path/like/this` CLI args** into `C:/Program Files/Git/path/like/this`. Prefix with `MSYS_NO_PATHCONV=1`.

---

## External Services & Integrations

| Service | Purpose | Auth | Notes |
|---------|---------|------|-------|
| WiGLE.net | AP database + density queries + CSV upload | HTTP Basic: `WIGLE_NAME` + `WIGLE_TOKEN` | 1 req/sec free tier |
| WDGoWars | Game state, territory, CSV upload | `X-API-Key: WDGOWARS_TOKEN` header | 20k AP/day cap |
| OpenRouteService | Routing + TSP optimization | Raw key in `Authorization` header (no `Bearer`) | 2000 dir/day, 500 opt/day free |
| Mapbox Directions | Routing fallback | `MAPBOX_API_KEY` (optional) | Wired in env, not yet used |
| Hetzner CPX | Prod hosting | SSH key | `5.161.250.8`, `warroute.darkhorseinfosec.com` |

---

## Session Startup

- Read `tasks/lessons.md` for recent corrections.
- Read `tasks/todo.md` to see where the current phase stands.
- Confirm which phase we're in (don't drift across phase boundaries).
- Don't touch `.env`. Reference variable names from `.env.example` if you need them in code.
