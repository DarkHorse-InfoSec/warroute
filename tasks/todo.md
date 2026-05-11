# WarRoute - Active TODO

Current phase: **Phase 0 (bootstrap)**.

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

## Phase 1 - Dual-uploader (next)

Blocked on: Phase 0 acceptance.

- [ ] `uploader/parser.py` - WigleWifi-1.6 CSV parser, dedup within file
- [ ] `uploader/wigle.py` - POST to WiGLE.net upload endpoint, 429 backoff
- [ ] `uploader/wdgowars.py` - POST to wdgwars.pl/api/upload-csv, respect 20k/day cap
- [ ] `uploader/watcher.py` - watchdog daemon on `SPOOL_DIR`
- [ ] `cli.py`: add `warroute upload <file>` and `warroute watch`
- [ ] Unit tests against fixture CSV
- [ ] Integration tests gated on `RUN_INTEGRATION=1`

---

## Phase 2/3/4

See `PLAN.md` sections 3.2-3.4. Don't expand here until Phase 1 is shipping.
