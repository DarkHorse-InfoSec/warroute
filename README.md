# WarRoute

Wardriving route planner and dual-uploader. Plans loop drives that maximize *new* AP discovery within a time budget, then auto-uploads the resulting [WigleWifi-1.6 CSV](https://wiki.wigle.net/index.php/File_format_for_uploads) to both [WiGLE.net](https://wigle.net) and [WDGoWars](https://wdgwars.pl) when the run finishes.

Single-tenant. Mobile-friendly web UI. Designed to be safe to use: plan in the driveway, drive with phone in pocket, review on return. **No live in-drive UI.**

See [`PLAN.md`](./PLAN.md) for the full design and build phases.

## Stack

- Python 3.11, FastAPI, SQLite
- HTMX + Leaflet (no React, no bundler)
- Routing: [OpenRouteService](https://openrouteservice.org) (worldwide, no self-hosted OSRM)
- Process management: systemd on prod
- Reverse proxy: Caddy (auto-TLS)

## Setup (dev)

```bash
# Install uv if you don't have it (https://docs.astral.sh/uv/)
# Then:
uv sync --all-extras
cp .env.example .env
# Fill in WIGLE_*, WDGOWARS_*, ORS_API_KEY in .env
```

## Commands

```bash
uv run pytest                 # tests
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy warroute          # type check
uv run warroute --help        # CLI
uv run uvicorn warroute.web.app:app --reload   # dev server
```

## Deploy (prod)

See `infra/` once Phase 1 lands. Targets a Hetzner CPX11 (`5.161.250.8`) at `warroute.darkhorseinfosec.com`.

## License

Proprietary. All rights reserved.
