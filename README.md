# WarRoute

Wardriving route planner and dual-uploader. WarRoute plans loop drives that maximize *new* access-point discovery within a time budget, then uploads the resulting [WigleWifi-1.6 CSV](https://wiki.wigle.net/index.php/File_format_for_uploads) to both [WiGLE.net](https://wigle.net) and [WDGoWars](https://wdgwars.pl) when the run finishes. It plans and uploads only; it never captures wireless data itself and never connects to any network.

Mobile-friendly web UI. Designed to be safe to use: plan in the driveway, drive with the phone in your pocket, review on return. **No live in-drive UI.**

See [`PLAN.md`](./PLAN.md) for the full design and build phases, and [`DECISIONS.md`](./DECISIONS.md) for what changed during build.

## Features

- **Route planning.** Greedy pick plus OpenRouteService optimization builds a loop (or one-way) drive that packs the most unseen APs into a time budget you set. For one-way trips the budget is optional: leave it blank to just route to your destination (a road trip, or a run to a fixed address), or set one to weave AP-scanning detours in along the way.
- **Dual upload to WiGLE + WDGoWars.** Parse a WigleWifi CSV once, dedup by sha256, and push it to both services in parallel. WDGoWars quota headroom is checked before posting so a run never blows the daily cap.
- **Coverage analyzer.** A 2x3 km aligned grid painted by ownership (mine, rival, uncaptured) and by WiGLE density, so you can see where new drives actually pay off.
- **Post-drive review map.** After a run's CSV is uploaded, `/runs/{id}` plots every AP you discovered on a Leaflet map, colored by encryption (open, WEP, WPA/WPA2, WPA3). Post-drive only, in keeping with the no-live-in-drive-UI safety rule.
- **Per-user navigation-app choice.** Hand a finished plan to Google Maps, a GPX file, Apple Maps, Waze, or your device-default map app. See [Navigation](#navigation) for the full-route vs first-stop distinction.
- **Push notifications via ntfy.** Optional departure alerts push to your phone through [ntfy](https://ntfy.sh) when a scheduled departure is near.

## Stack

- Python 3.11, FastAPI, SQLite (single file)
- HTMX + Leaflet via CDN (no React, no bundler, no build step)
- Routing: [OpenRouteService](https://openrouteservice.org) (worldwide, no self-hosted OSRM)
- Dependency management: [`uv`](https://docs.astral.sh/uv/)
- Reverse proxy: Caddy (auto-TLS; authentication lives at this edge, not in the app)

## Quick start (self-host)

```bash
git clone https://github.com/DarkHorse-InfoSec/warroute.git
cd warroute
uv sync --all-extras
cp .env.example .env
# Edit .env and fill in your own API keys (see "Bring your own API keys" below)
uv run warroute serve       # web UI at http://127.0.0.1:8000
```

Useful follow-ups:

```bash
uv run warroute doctor      # confirm all required env vars are present
uv run warroute migrate     # apply the SQL schema to ./warroute.db
uv run warroute --help      # full CLI
uv run pytest               # run the test suite
```

For a full production deployment (Hetzner VPS, systemd, Caddy, TLS), see [`infra/README.md`](./infra/README.md).

## Bring your own API keys

WarRoute is not-for-profit. Every user brings their own API keys. Self-hosters put the keys in `.env` (copy from `.env.example`). On the hosted instance, you paste your keys into Settings and they are kept **in your browser only** (never on the server). The one exception is routing: an operator may run a shared OpenRouteService key (behind a rate limit + daily cap) so users who lack an ORS key can still plan.

| Service | Purpose | Where to get a key |
|---|---|---|
| WiGLE.net | AP database, density queries, CSV upload | https://wigle.net/account |
| WDGoWars | Game state, territory, CSV upload | https://wdgwars.pl/profile |
| OpenRouteService | Routing and TSP optimization | https://openrouteservice.org/dev/#/signup |
| Mapbox | Optional routing fallback | https://account.mapbox.com |
| ntfy | Optional push notifications | https://ntfy.sh (pick a topic name) |

## Accounts, keys, and privacy

WarRoute has **no accounts and no login.** It is stateless by design: your WiGLE /
WDGoWars / ORS keys and preferences live in **your browser** (localStorage) and are
sent with each request so the app can talk to those services on your behalf. **The
server stores nothing** (it handles your keys transiently to make the upstream call,
but never persists them). See DECISIONS.md 2026-07-04 for the full model.

Two tiers, by how private you want to be:

- **Hosted: enter your keys.** Open the hosted instance, add your keys in Settings
  (they stay in your browser), and go. Nobody provisions an account for you. Optional
  **end-to-end-encrypted sync**: turn it on in Settings to get a one-time code that
  backs your keys up (encrypted in your browser first; the server stores an opaque
  blob it cannot read). Restore on another device, or after your browser clears its
  storage (iOS Safari does this after about a week), by pasting that code. No login,
  no email, no server-readable keys.
- **Self-host: full privacy.** Run your own copy (see Quick start). Your keys never
  leave your machine and nothing about you touches anyone else's server. This is the
  most private option and it is why the project is open source.

**Operators** choose whether the hosted instance is open to everyone or gated to a
known group, by which Caddyfile they deploy: `infra/Caddyfile.public` (no gate,
intended for a public beta), `infra/Caddyfile` (basic_auth), or
`infra/Caddyfile.cloudflare` (Cloudflare Access SSO; requires locking the origin to
Cloudflare IPs, see its header). The app is identical under all three. Details in
[`infra/README.md`](./infra/README.md) "Auth modes". Routing (OpenRouteService) is
the one key most wardrivers lack, so an operator may configure a shared ORS key for
routing behind a per-IP rate limit + daily cap; users who add their own ORS key skip
the limit.

## Navigation

A finished plan can be handed off to whichever navigation app you prefer. The apps differ in how much of the route they can carry:

- **Full multi-stop loop:** Google Maps and GPX (GPX imports into OsmAnd, Organic Maps, and similar). These carry every stop in order.
- **First stop only:** Apple Maps, Waze, and the device-default `geo:` link. These route only to the first stop; drive the rest manually or switch to a full-route app.

Choose your default at `/settings`.

## Web routes

| Route | Purpose |
|---|---|
| `/`           | Dashboard: today's quota, recent runs, coverage stats |
| `/plan`       | Plan a drive (form + Leaflet result + GPX download + nav-app hand-off) |
| `/coverage`   | Leaflet cell map colored by ownership (mine, rival, uncaptured) |
| `/runs/{id}`  | Post-run breakdown + a Leaflet map of the APs you discovered (colored by encryption); predicted vs actual when a plan is linked |
| `/settings`   | Per-user API keys, home location, nav-app choice |

`/docs` and `/openapi.json` are intentionally disabled (single-tenant; no public API surface).

## Responsible use and legality

Wardriving means mapping wireless networks while driving. WarRoute deals only with passive observation: it plans routes and uploads CSVs you collected yourself. It never captures wireless data, and it never connects to any network. Connecting to a network you do not own is not part of this tool and would be illegal.

Passive observation of broadcast beacons is legal in most of the United States, but laws vary by jurisdiction and country. You are solely responsible for complying with the laws that apply where you drive, and for the terms of service of WiGLE, WDGoWars, and any other service you upload to. Read them and stay within them.

## Quality bar

```bash
uv run ruff check .       # lint
uv run ruff format .      # format
uv run mypy warroute      # strict type check
uv run pytest             # all tests (respx mocks all external calls)
```

A change is not done until those four commands pass.

## Windows note

Git Bash on Windows mangles Unix-style path arguments (e.g. `/api/me` becomes `C:/Program Files/Git/api/me`). Prefix any CLI call that takes a Unix-looking path with `MSYS_NO_PATHCONV=1`:

```bash
MSYS_NO_PATHCONV=1 uv run warroute coverage probe-wdgowars /api/me
```

## License

Licensed under the Apache License 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
