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
warroute/                    # Python package
  __init__.py
  config.py                  # pydantic-settings, loads .env
  db.py                      # SQLite helpers
  cli.py                     # typer CLI entrypoint
  uploader/                  # Phase 1: dual-uploader
    parser.py
    wigle.py
    wdgowars.py
    watcher.py
  coverage/                  # Phase 2: cell ownership / density
    wdgowars_sync.py
    local.py
    grid.py
  router/                    # Phase 3: route planner
    ors_client.py            # OpenRouteService HTTP client
    scorer.py
    planner.py
  web/                       # Phase 4: FastAPI app + HTMX templates
    app.py
    templates/
    static/
migrations/                  # SQL migration files (_v1.sql, _v2.sql, ...)
infra/                       # bootstrap.sh, systemd units, Caddyfile
tests/
  fixtures/
  unit/
  integration/               # gated by RUN_INTEGRATION=1
tasks/
  todo.md
  lessons.md
PLAN.md                      # canonical design spec
README.md
CLAUDE.md                    # this file
.env                         # SECRETS - never commit
.env.example
pyproject.toml               # deps via uv
```

## Key Files & Paths

| File/Dir | Purpose |
|----------|---------|
| `PLAN.md` | Canonical design + build phases. Read first if you forget anything. |
| `.env` | Secrets (WIGLE, WDGOWARS, ORS keys + Hetzner IP). **Gitignored.** |
| `migrations/_v1.sql` | Initial SQLite schema (sessions, observations, cells, planned_routes). |
| `tasks/todo.md` | Active work for the current phase. |
| `tasks/lessons.md` | Patterns learned from corrections. Update after every redirect. |

## Environment & Commands

```bash
uv sync --all-extras                                  # install deps
uv run pytest                                         # tests
uv run ruff check .                                   # lint
uv run ruff format .                                  # format
uv run mypy warroute                                  # type check (strict)
uv run warroute --help                                # CLI
uv run uvicorn warroute.web.app:app --reload         # dev server (Phase 4+)
RUN_INTEGRATION=1 uv run pytest tests/integration    # real-API tests (rare)
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

- WiGLE.net query API is rate-limited (1 req/sec free tier). Cell density lookups must be cached for 24h in SQLite.
- WDGoWars caps new APs at 20k per 24h per account. The uploader checks `/api/me` headroom before posting and splits oversized CSVs.
- ORS free tier: 2000 directions/day, 500 optimization/day. Watch quota on multi-plan days; fall back to Mapbox if exceeded.
- WDGoWars is an early-stage service; API endpoints may change. Pin endpoints, monitor changelog, keep the client thin.

---

## External Services & Integrations

| Service | Purpose | Auth | Notes |
|---------|---------|------|-------|
| WiGLE.net | AP database + density queries + CSV upload | `WIGLE_NAME` + `WIGLE_TOKEN` | 1 req/sec free tier |
| WDGoWars | Game state, territory, CSV upload | `WDGOWARS_TOKEN` (Bearer) | 20k AP/day cap |
| OpenRouteService | Routing + TSP optimization | `ORS_API_KEY` | 2000 dir/day, 500 opt/day free |
| Mapbox Directions | Routing fallback | `MAPBOX_API_KEY` (optional) | Used only when ORS fails |
| Hetzner CPX | Prod hosting | SSH key | `5.161.250.8`, `warroute.darkhorseinfosec.com` |

---

## Session Startup

- Read `tasks/lessons.md` for recent corrections.
- Read `tasks/todo.md` to see where the current phase stands.
- Confirm which phase we're in (don't drift across phase boundaries).
- Don't touch `.env`. Reference variable names from `.env.example` if you need them in code.
