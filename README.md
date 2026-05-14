# WarRoute

Wardriving route planner and dual-uploader. Plans loop drives that maximize *new* AP discovery within a time budget, then auto-uploads the resulting [WigleWifi-1.6 CSV](https://wiki.wigle.net/index.php/File_format_for_uploads) to both [WiGLE.net](https://wigle.net) and [WDGoWars](https://wdgwars.pl) when the run finishes.

Single-tenant. Mobile-friendly web UI. Designed to be safe to use: plan in the driveway, drive with phone in pocket, review on return. **No live in-drive UI.**

See [`PLAN.md`](./PLAN.md) for the full design + build phases, and [`DECISIONS.md`](./DECISIONS.md) for what changed during build.

## Stack

- Python 3.11, FastAPI, SQLite (single file)
- HTMX + Leaflet via CDN (no React, no bundler, no build step)
- Routing: [OpenRouteService](https://openrouteservice.org) (worldwide, no self-hosted OSRM)
- Phone -> Hetzner CSV sync via Syncthing (drop into `SPOOL_DIR`, watcher daemon picks up)
- Process management: systemd on prod
- Reverse proxy: Caddy (auto-TLS, HTTP basic auth at this layer = our only auth)

## Setup (dev)

```bash
uv sync --all-extras
cp .env.example .env
# Fill in WIGLE_NAME, WIGLE_TOKEN, WDGOWARS_TOKEN, ORS_API_KEY in .env
uv run warroute doctor      # confirm all required env vars present
uv run warroute migrate     # apply SQL schema to ./warroute.db
```

## CLI

```bash
uv run warroute --help

# planning
uv run warroute plan --duration 90 --mode loop --out drive.gpx
uv run warroute plan --duration 120 --mode oneway --destination 45.0,-72.0

# coverage
uv run warroute coverage refresh                # paint grid + sync WDGoWars + WiGLE density
uv run warroute coverage report                 # text summary
uv run warroute coverage probe-wdgowars /api/me # endpoint discovery (raw JSON dump)

# ingest (dual-upload)
uv run warroute upload tests/fixtures/sample_wiglewifi.csv
uv run warroute watch                           # daemon: SPOOL_DIR -> auto-ingest

# web UI
uv run warroute serve                           # http://127.0.0.1:8000
uv run warroute serve --host 0.0.0.0 --reload   # dev: bind LAN, auto-reload on edits
```

## Web routes

| Route | Purpose |
|---|---|
| `/`           | Dashboard: today's quota, recent runs, coverage stats |
| `/plan`       | Plan a drive (form + Leaflet result + GPX download + Google Maps deep-link) |
| `/coverage`   | Leaflet cell map colored by ownership (mine/rival/uncaptured) |
| `/runs/{id}`  | Post-run breakdown + predicted-vs-actual when a planned route is associated |
| `/settings`   | Read-only config display; secrets masked to last4 |

`/docs` and `/openapi.json` are intentionally disabled (single-tenant; no API surface to publish).

## Quality bar

```bash
uv run ruff check .       # lint
uv run ruff format .      # format
uv run mypy warroute      # strict type check
uv run pytest             # all tests (currently 100+; respx mocks all external calls)
```

A change isn't done until those four commands pass.

## Windows note

Git Bash on Windows mangles Unix-style path arguments (e.g. `/api/me` -> `C:/Program Files/Git/api/me`). Prefix any CLI call that takes a Unix-looking path with `MSYS_NO_PATHCONV=1`:

```bash
MSYS_NO_PATHCONV=1 uv run warroute coverage probe-wdgowars /api/me
```

## Deploy (prod)

Hetzner box at `5.161.250.8` (DNS: `warroute.darkhorseinfosec.com`).
Bootstrap script + systemd unit + Caddyfile live under `infra/` (TBD - not built yet).

## License

Proprietary. All rights reserved.
