# WarRoute — Project Plan

**Project name:** WarRoute (placeholder; rename if you want)
**Owner:** Domenic Laurenzi (@DarkHorse-InfoSec)
**Status:** Spec, ready for build
**Author of plan:** Claude (architect role). Build agent: Claude Code.

---

## 1. What we're building

A wardriving route-planning and dual-upload toolchain that:

1. **Plans loop drives** from a home location that maximize *new* AP discovery within a time budget, using OSM road data, your historical WiGLE coverage, and WDGoWars territory data.
2. **Auto-uploads** WiGLE WiFi 1.6 CSV captures to **both** WiGLE.net and WDGoWars on completion of a run.
3. **Exposes a mobile-friendly UI** so you can plan a drive from your phone before leaving the house and view post-run stats.

Inspired by WDGoWars' gamified wardriving model. WDGoWars uses standard WigleWifi-1.6 CSV format, has a REST API at `/api/upload-csv`, and an `/api/me` endpoint for player state. WiGLE.net has a long-standing upload API and a query API for AP density.

### Non-goals (v1)

- No packet capture, handshake cracking, or anything beyond passive beacon-frame logging. WarRoute consumes CSVs that other tools (WiGLE Android app, Pineapple, Bruce, Flipper-with-firmware) produce — it does not do scanning itself.
- No mobile native app. Phone access is via responsive web UI on the Hetzner box.
- No multi-user. Single-tenant for Domenic's account.
- No Bluetooth/BLE territory logic — WDGoWars only counts WiFi for territory anyway.

---

## 2. Architecture

### Two environments

- **Dev:** Windows host, WSL2, `D:\Projects\warroute` (mounted as `/mnt/d/Projects/warroute` in WSL).
- **Prod:** New Hetzner VPS (CPX21 or CPX31 — see §4). Domain: `warroute.darkhorseinfosec.com` (subdomain to be added to existing DNS).

### Stack

- **Backend:** Python 3.11, FastAPI, uvicorn, SQLite (single-file DB, no Postgres needed for v1).
- **Geo:** `osmnx` for road graph extraction, OSRM (Docker) for routing, `shapely` for geometry, `geopandas` for spatial joins.
- **Frontend:** Server-rendered Jinja2 templates + HTMX for interactivity + Leaflet for map display. **No React.** Keeps it simple, mobile-fast, no build step.
- **Process management:** systemd units on prod. `tmux` is fine for dev iteration but not for production.
- **Reverse proxy:** Caddy (auto-TLS via Let's Encrypt, simpler than nginx for a single-service deploy).
- **Containerization:** OSRM runs in Docker. The FastAPI app runs natively in a venv (simpler debugging, faster iteration). Don't over-Dockerize.

### Why these choices

- **FastAPI over Flask:** async support matters when the upload endpoint is hitting two external APIs in parallel.
- **HTMX over React:** mobile-first, no bundler, no `npm install` hell. Domenic has explicitly identified marketing/sales — not frontend complexity — as a weak point. Don't add weight.
- **SQLite over Postgres:** single-user app, all data fits in memory, backups are `cp warroute.db warroute.db.bak`.
- **osmnx over raw Overpass:** built-in caching and graph algorithms save ~200 lines of code.
- **OSRM over Valhalla or GraphHopper:** OSRM is the lightest to host, has a clean HTTP API, and supports the `trip` service (open/closed TSP) natively. Vermont-sized region fits in <2GB RAM.

### Data flow

```
                         ┌─────────────────────────────┐
                         │  WiGLE Android app on phone │
                         └──────────────┬──────────────┘
                                        │ CSV export (manual or scheduled)
                                        ▼
                              ┌─────────────────┐
                              │ Sync to Hetzner │  (rsync over SSH or
                              │   /var/spool/   │   Syncthing — see §6.1)
                              │   warroute/in/  │
                              └────────┬────────┘
                                       │ inotify watch
                                       ▼
                            ┌──────────────────────┐
                            │  Dual-uploader       │
                            │  - parse CSV         │
                            │  - dedup vs prior    │
                            │  - POST WiGLE.net    │
                            │  - POST WDGoWars     │
                            │  - record in SQLite  │
                            └────────┬─────────────┘
                                     │
                                     ▼
        ┌────────────────────────────────────────────────────┐
        │                    SQLite DB                       │
        │  observations, sessions, uploads, cells, owned     │
        └────────────────────────────────────────────────────┘
                                     ▲
                                     │
        ┌────────────────────────────┴────────────────────────┐
        │                                                      │
        ▼                                                      ▼
┌──────────────────┐                                ┌──────────────────────┐
│ Coverage         │                                │ Route Planner        │
│ Analyzer         │                                │ - osmnx road graph   │
│ - cell ownership │                                │ - score cells        │
│ - WDGoWars sync  │                                │ - OSRM trip API      │
│   via /api/me    │                                │ - emit GPX + gmaps   │
└──────────────────┘                                └──────────────────────┘
                                     ▲
                                     │
                              ┌──────┴──────┐
                              │  FastAPI UI │
                              │  + HTMX     │
                              │  + Leaflet  │
                              └─────────────┘
```

---

## 3. Build phases

Each phase ships independently working software. Don't move to phase N+1 until phase N passes its acceptance criteria.

### Phase 0 — Bootstrap (target: 30 min)

- Create repo `DarkHorse-InfoSec/warroute` on GitHub. Private initially.
- `README.md` with one-paragraph description.
- `.gitignore` (Python + IDE + `.env` + `*.db` + `osm-cache/`).
- `pyproject.toml` with `uv` for dep management (faster than pip).
- `LICENSE` — MIT or proprietary, Domenic's call. Default to **proprietary "All rights reserved"** for v1 since it touches DarkHorse infra.
- `CLAUDE.md` at repo root: code style, never commit secrets, all secrets in `.env`, prod paths, dev paths.
- Pre-commit hook: `ruff check`, `ruff format`, `mypy --strict` on changed files.

**Acceptance:** `git clone`, `uv sync`, `pytest` (no tests yet, just confirms the harness works) all pass on both WSL and a fresh Hetzner box.

### Phase 1 — Dual-uploader (target: 2–3 hours)

The fastest-to-value piece. Build this first because it's useful even without anything else.

**Components:**

- `warroute/uploader/parser.py` — parses WigleWifi-1.6 CSV. Validates header, extracts session metadata, deduplicates within file (BSSID+location bucketed to ~10m grid).
- `warroute/uploader/wigle.py` — POSTs to WiGLE.net file upload endpoint. Auth via `WIGLE_API_TOKEN` env. Handles 429 rate limit with exponential backoff.
- `warroute/uploader/wdgwars.py` — POSTs to `https://wdgwars.pl/api/upload-csv`. Auth via `WDGWARS_API_KEY`. Respects the 20k new-AP-per-24h cap by checking `/api/me` first; if today's headroom is <CSV's new-AP count, splits the file and queues the remainder for tomorrow.
- `warroute/uploader/watcher.py` — `watchdog` library, monitors `/var/spool/warroute/in/`. On new `.csv` close-write event, calls `parser` then both uploaders in parallel via `asyncio.gather`.
- `warroute/db.py` — SQLite schema (see §5), connection helper, migrations via `alembic` or simple `_v1.sql`/`_v2.sql` files.
- `warroute/cli.py` — `warroute upload <file>` for manual runs, `warroute watch` to start the daemon.

**Tests:** Unit tests for parser (against a known-good fixture CSV), integration tests for both upload clients (mock the HTTP layer with `respx`).

**Acceptance:**
- `warroute upload tests/fixtures/sample.csv` exits 0, both APIs return 200, SQLite has the session recorded.
- `warroute watch` running as systemd unit on Hetzner. Drop a real CSV from a real wardrive into the spool dir; within 30 seconds, both uploads complete and the WDGoWars profile shows new APs.

### Phase 2 — Coverage analyzer (target: 3–4 hours)

**Components:**

- `warroute/coverage/wdgwars_sync.py` — pulls `/api/me`, owned-territory cells, daily quota. Caches in SQLite, refreshes every 15 min when the UI is active.
- `warroute/coverage/local.py` — reads the local WiGLE SQLite (the Android app's `wiglewifi.sqlite`, copied from phone over `adb pull` or via the watch-folder sync). Builds a "GPS track buffer" polygon from logged route points using `shapely.buffer` at 100m radius.
- `warroute/coverage/grid.py` — generates a 2×3 km cell grid (matching WDGoWars' grid) over a configurable home-centered radius (default 50 km). For each cell: WDGoWars owned status, owner if not you, your historical AP count from local DB, estimated AP density from WiGLE.net query API.
- CLI: `warroute coverage report` prints a text summary; `warroute coverage export <geojson>` for offline inspection in QGIS or geojson.io.

**Tests:** Fixture-based — fake `/api/me` response, fake local DB, assert correct cell scoring.

**Acceptance:**
- `warroute coverage report` prints something like:
  ```
  Home: 44.94, -72.21 (Newport, VT)
  Radius: 50 km
  Cells in radius: 287
    Owned by you:        18  (6%)
    Owned by rivals:      4  (1%)
    Uncaptured:         265  (92%)
  Top 5 unexplored cells by estimated yield:
    1. Cell 44.96/-72.34 — ~127 APs estimated, 8 min from home
    ...
  ```

### Phase 3 — Route planner (target: 5–7 hours)

**The actual game-changer.** This is where most of the design risk lives.

**Components:**

- `warroute/router/osm.py` — uses `osmnx.graph_from_point` to fetch and cache the drivable road graph for the home + radius region. Cached on disk; refreshed monthly.
- `warroute/router/osrm_client.py` — thin client for the OSRM HTTP API. Two endpoints used: `route` (point-to-point) and `trip` (TSP through waypoints).
- `warroute/router/scorer.py` — for each candidate cell:
  ```
  score = (estimated_new_APs - your_existing_APs_in_cell)
        / (extra_drive_minutes_to_include_it)
  ```
  Cells you already own get a small positive base score (revisit bonus for refreshing observations) but are heavily down-weighted vs unexplored cells.
- `warroute/router/planner.py` — main solver. Algorithm:
  1. Take home, time budget T, mode (loop=start/end same | one-way to dest D).
  2. Compute reachable radius R given T (assume avg 40 km/h on rural Vermont roads → R ≈ T × 20 km if loop).
  3. Rank all cells in reachable radius by score.
  4. Greedy-pick top-K cells where `K` is chosen so OSRM trip API stays under its complexity limit (≤25 waypoints).
  5. Hand waypoints + start + end to OSRM `/trip`. OSRM returns optimal ordering and route geometry.
  6. Verify total drive time ≤ T × 1.05 (allow 5% slack). If over, drop the lowest-scoring waypoint and re-solve.
  7. Output: GPX file, Google Maps directions URL (multi-stop), turn-by-turn JSON, expected new-AP count.

- CLI: `warroute plan --home 44.94,-72.21 --duration 90m --mode loop --out drive.gpx`

**Edge cases:**
- Time budget too small to leave home cell → return helpful error, suggest minimum.
- Home is on the edge of mapped area → expand graph radius dynamically.
- All nearby cells already saturated → return "diminishing returns" warning, suggest driving 30+ min outbound first.
- OSRM `/trip` doesn't natively support fixed start/end (it's symmetric TSP). Workaround: use OSRM's `roundtrip=true` + `source=first&destination=last` parameters.

**Tests:** Snapshot test against a fixed home + seed (Newport, VT, 90 min loop) — assert the route output is stable across runs.

**Acceptance:**
- `warroute plan` produces a GPX you can import into Google Maps or OSMAnd.
- The planned route, when driven, produces a CSV that scores ≥3× more new APs per minute than a same-duration drive on Domenic's normal commute. (Validate empirically on a real drive.)

### Phase 4 — Mobile-friendly UI (target: 4–5 hours)

**Pages:**

- `/` — dashboard. Today's quota, recent runs (last 10), badges earned, WDGoWars rank.
- `/plan` — input form: time budget slider, mode toggle (loop/one-way), optional destination. Submit → server runs planner → renders Leaflet map with route + cell scores overlaid + "Open in Google Maps" deep link + GPX download.
- `/coverage` — Leaflet map of all cells in radius, color-coded by ownership (yours/rival/unclaimed). Tap a cell → see AP estimate and historical visits.
- `/runs/<id>` — post-run breakdown. New APs added, points earned, comparison to predicted yield.
- `/settings` — API keys (read-only display, edited via `.env`), home location, default radius, default duration.

**Mobile UX rules:**
- No horizontal scroll at 360px width.
- All CTAs ≥ 44px tap target.
- Map gestures: pinch zoom, two-finger pan (one-finger = page scroll, like Google Maps embed).
- Service worker caches static assets so the dashboard loads even on flaky cell signal in northern VT.

**Auth:** Single-user, so HTTP basic auth at the Caddy reverse proxy layer is enough. No login form, no session management, no JWT. KISS.

**Acceptance:**
- Open `https://warroute.darkhorseinfosec.com` on Pixel 9 XL, plan a 90-min loop, tap "Open in Google Maps", drive it, return home, refresh dashboard, see new run logged with correct point delta.

### Phase 5 (stretch, post-v1) — Auto-trigger and notifications

- Watcher detects new run finished → push notification to phone (via existing DarkHorse Twilio account or ntfy.sh — Domenic's call).
- Notification body: "Run complete. +47 new APs, +1 cell captured (Derby Line North), 312 points. Tap to view."

Defer this to v1.1. Don't let it block Phase 4 acceptance.

### Phase 6 (post-v1) — Multi-leg planner

**Motivation:** v1 plans either a loop (home → home) or a one-way (home → destination). Real driving has shape: drop kid at daycare → go to work → come home. Road trips chain cities with overnight stops. Errands stack. The single-destination model can't express any of this. Phase 6 generalizes the planner to N waypoints with optional dwell times, optional arrival deadlines, and multi-day segmenting.

The three asks layer; do them in order, each shippable on its own:

**6a. Multi-stop core** (foundation for 6b and 6c)

Replace single `destination` with an ordered list of stops. Wardriving cells slot between consecutive stops via the existing corridor filter, run per segment.

- Schema: add `stops_json` column to `planned_routes` (JSON array of `{lat, lon, label, dwell_min}`). Keep existing `destination_lat`/`lon` populated to the *last* stop for backward compat with existing rows.
- `PlanRequest`: replace `destination_lat/lon` with `stops: list[Stop]`. Loop mode = no stops; oneway = one stop (today's behavior); multi-stop = 2+ stops.
- Planner: for each consecutive (stop[i], stop[i+1]) pair, run the corridor filter independently and pick cells. ORS optimization call per segment, capped by ORS's 25-jobs-per-call limit. Total ORS calls = N segments × {1 directions + 1 optimization} per segment.
- UI (`/plan`):
  - Stops list with drag-reorder (Sortable.js or HTMX swap). "+ Add stop" appends a stop with geocoder type-ahead. "X" removes.
  - Per-stop: address (type-ahead), optional dwell-minutes input ("How long are you stopped?").
  - Result page: stop list with cumulative drive + dwell time, GMaps URL that chains all stops + cells.
- Constraints: max 8 stops (ORS optimization VRP cap is 25 jobs; reserve room for cells per segment). Beyond 8, route to /roadtrip (see 6c).

**6b. Arrival-time backward planning**

User specifies "be at the last stop by HH:MM" instead of (or in addition to) duration. Planner computes departure time, alerts user if budget too tight.

- `PlanRequest` adds `arrive_by: datetime | None`.
- Logic: planner computes total drive_min + sum(dwell_min). `departure = arrive_by - total_min`. If `departure < now() + 5min`, return error: "Not enough time — leave immediately or sooner, OR drop a stop."
- UI: new mode toggle "Plan by duration" vs "Plan by arrival time". When "arrival time" selected, replace the duration input with a datetime picker.
- Optional: ntfy push at `departure - 5min`: "Leave in 5 min for [first stop]". Wire via existing Phase 5 ntfy infra. New table `scheduled_departures(plan_id, departure_at, notified_at)`; new systemd timer job polls it every minute.

**6c. Roadtrip mode**

Long-distance, multi-day, multi-state plans. Same multi-stop primitive, with overnight markers and per-day segmenting.

- Add `overnight_after: bool` to each stop. When set, planner ends a day at that stop and starts the next day from it.
- Per-segment density routing: corridor filter using a wider half-width (10-20 km for highway corridors) since long-distance trips can deviate further for high-value wardriving.
- New page `/roadtrip` (or a "Multi-day" toggle on `/plan`). Result UI groups stops by day with totals: "Day 1: 6 stops, 4.5h drive, ~120 new APs."
- ORS quota awareness: a 10-day roadtrip with 3 stops/day = 30 segments × 2 ORS calls = 60 calls. Free tier (500 opt/day) handles a couple of trips per day. Display estimated ORS calls before submit; refuse plans that would exceed daily quota.
- GPX output: one GPX file per day (so phone navigation only loads the current day's segment).

**Sequencing:**

1. **6a first** (1-2 sessions). Multi-stop core. Ship. Drive a 3-stop errand run to validate.
2. **6b after 6a is in prod** (1 session). Arrival-time mode + ntfy alarm.
3. **6c last** (2-3 sessions). Roadtrip mode. Most UI work, most edge cases, biggest payoff.

**Non-goals for Phase 6:**

- No traffic-aware ETAs (ORS doesn't return live traffic on free tier).
- No collaborative trip planning (still single-tenant).
- No "leg home" auto-insert. If the user wants to end at home, they add a "Home" stop.

**Acceptance:**

- 6a: User plans a 3-stop route (daycare → work → coffee shop → home), drives it, GPX navigates all 4 legs correctly, dashboard logs the run.
- 6b: User says "Be at work by 09:00", planner replies "Leave by 08:23", ntfy fires at 08:18. Drives, arrives within ±5 min.
- 6c: User plans a 3-day VT → NH → ME roadtrip with 2 overnights, 18 stops total, 6 wardriving cells per day. GPX per day. Total drive matches sum of segments.

---

## 4. Hetzner provisioning

Spin up a **fresh CPX21** (the existing pentest VPS at 178.156.232.180 should not host this — different security posture, different uptime requirements, and the pentest box gets reimaged occasionally).

```
Type:     CPX21 (3 vCPU, 4 GB RAM, 80 GB SSD) — €8.46/mo
Image:    Debian 12
Location: Falkenstein or Helsinki (latency to VT is similar; pick on price)
SSH key:  domenic@darkhorse (existing key from D:\Projects\.ssh\)
Hostname: warroute
Firewall: 22 (SSH, your IPs only), 80, 443 (Caddy)
```

**Initial setup script** — Claude Code should write this as `infra/bootstrap.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3-pip git docker.io \
               caddy ufw fail2ban inotify-tools rsync sqlite3
useradd -m -s /bin/bash warroute
ufw default deny incoming
ufw allow 22/tcp
ufw allow 80,443/tcp
ufw enable
systemctl enable --now docker fail2ban
# OSRM container with Vermont + adjacent NH/NY/QC OSM extract
mkdir -p /var/lib/osrm
# (downloads handled by separate infra/setup-osrm.sh)
```

DNS: add `warroute.darkhorseinfosec.com` A record pointing to the new VPS.

---

## 5. Database schema

```sql
-- sessions: one row per CSV uploaded
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,           -- 'wigle-android', 'pineapple-pager', 'bruce', 'manual'
    csv_path TEXT NOT NULL,
    csv_sha256 TEXT NOT NULL UNIQUE,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP NOT NULL,
    distance_km REAL,
    new_aps INTEGER,
    total_aps INTEGER,
    uploaded_wigle_at TIMESTAMP,
    uploaded_wdgwars_at TIMESTAMP,
    wdgwars_run_id TEXT,
    points_earned INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- observations: deduplicated AP sightings (one row per unique BSSID)
CREATE TABLE observations (
    bssid TEXT PRIMARY KEY,
    ssid TEXT,
    encryption TEXT,
    first_seen_session INTEGER REFERENCES sessions(id),
    first_seen_lat REAL,
    first_seen_lon REAL,
    last_seen_at TIMESTAMP,
    times_seen INTEGER DEFAULT 1
);

-- cells: 2x3km grid cells, materialized for the home radius
CREATE TABLE cells (
    id TEXT PRIMARY KEY,            -- 'lat_lon' rounded to grid
    center_lat REAL,
    center_lon REAL,
    bbox_geojson TEXT,
    your_ap_count INTEGER DEFAULT 0,
    estimated_total_aps INTEGER,    -- from WiGLE.net density
    wdgwars_owner TEXT,             -- null = uncaptured, 'me' = mine, else rival username
    wdgwars_last_capture TIMESTAMP,
    last_refreshed TIMESTAMP
);

-- planned_routes: history of generated plans (so you can compare predicted vs actual)
CREATE TABLE planned_routes (
    id INTEGER PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    home_lat REAL,
    home_lon REAL,
    duration_min INTEGER,
    mode TEXT,                      -- 'loop' or 'oneway'
    destination_lat REAL,
    destination_lon REAL,
    waypoints_json TEXT,
    gpx_path TEXT,
    estimated_new_aps INTEGER,
    estimated_drive_min REAL,
    actual_session_id INTEGER REFERENCES sessions(id)
);
```

---

## 6. Resolved decisions (2026-05-10)

These were the open questions; Domenic resolved them as follows.

### 6.1 Phone → Hetzner CSV sync — **Syncthing**

Pixel runs Syncthing; the WiGLE-exports folder syncs to `/var/spool/warroute/in/` on the Hetzner box. Watcher picks up new CSVs via `inotify`. Encrypted, automatic, no manual scp every drive.

Alternatives ruled out: pulling CSVs back from WiGLE.net post-upload (WiGLE only exposes per-BSSID query, not raw-observation export); IMAP polling (roundabout, latency).

### 6.2 Routing engine and geographic scope — **OpenRouteService API, worldwide**

Original plan was self-hosted OSRM with a regional OSM extract (VT + NH + QC). Domenic wants WarRoute usable anywhere in the world, not just New England. A worldwide OSM extract is ~120 GB on disk and needs ~32 GB RAM to serve — a CPX21 cannot host it.

**Decision: drop self-hosted OSRM.** Use **OpenRouteService** (https://openrouteservice.org) as the routing backend:
- Free tier: 2000 directions requests/day, 500 optimization requests/day. Sufficient for single-user planning.
- Worldwide coverage out of the box, no extract management.
- Has both `directions` (point-to-point) and `optimization` (vehicle routing problem / TSP with constraints) endpoints — covers everything OSRM's `trip` was going to.
- Falls back to Mapbox Directions API if ORS is down or quota exceeded.

**New env vars:** `ORS_API_KEY` (required), `MAPBOX_API_KEY` (optional fallback).
**Removed from plan:** OSRM Docker container, OSM PBF downloads, `infra/setup-osrm.sh`.
**`osmnx` is still useful** for fetching the road graph as a visualization layer and for validating route segments locally, but is no longer the routing engine.

This also means the Hetzner box can be a **CPX11** instead of CPX21 (1 vCPU, 2 GB RAM, €4.51/mo). Domenic already provisioned at `5.161.250.8` — keep whatever was provisioned, just don't bother with the OSRM piece.

### 6.3 AP density source — **WiGLE query API + 24h SQLite cache**

Per-cell BSSID counts via WiGLE's search API, cached for 24 hours per cell. Cells don't churn quickly enough to matter. Avoids the precomputed-tile parsing route.

### 6.4 Scoring — **Use WDGoWars and WiGLE native numbers, not a custom formula**

Original plan proposed `score = 0.6 × new_to_you + 0.4 × new_territory_cell`. Domenic prefers WarRoute to be a thin orchestration layer over the existing services rather than a competing scoring system.

**Revised approach:**
- Pull cell ownership and game value from WDGoWars (`/api/me` + territory endpoints).
- Pull AP density from WiGLE query API.
- Score combines those native numbers (e.g. `wdgwars_capture_value × wigle_ap_density`) with no hand-tuned weights of our own.
- If WDGoWars exposes a "points-if-captured" number per cell, use it directly. If not, derive from ownership status (uncaptured > rival-owned > self-owned).

**Implication:** the `cells` table still caches what we pulled, but `your_existing_APs_in_cell` is no longer a primary scoring input — it's only used for revisit decisions.

---

## 7. Secrets and config

All secrets in `/home/warroute/.env` on prod, `.env` (gitignored) in dev. Loaded via `pydantic-settings`.

```
# .env.example — commit this, with placeholders only
WIGLE_API_TOKEN=         # from wigle.net account API page
WIGLE_API_USER=
WDGWARS_API_KEY=         # from wdgwars.pl/profile → Generate API key
HOME_LAT=44.9367
HOME_LON=-72.2051
HOME_RADIUS_KM=50
DEFAULT_DURATION_MIN=90
OSRM_URL=http://localhost:5000
DATABASE_URL=sqlite:///var/lib/warroute/warroute.db
SPOOL_DIR=/var/spool/warroute/in
GPX_OUT_DIR=/var/lib/warroute/gpx
NTFY_TOPIC=              # optional, phase 5
```

Domenic must obtain `WIGLE_API_TOKEN` and `WDGWARS_API_KEY` manually before Phase 1 testing — Claude Code cannot do this.

---

## 8. Testing & validation

- **Unit:** `pytest` for everything that doesn't touch the network. `respx` for HTTP mocks.
- **Integration:** A `tests/integration/` directory with tests gated behind `RUN_INTEGRATION=1`. These hit real WiGLE.net and WDGoWars endpoints with a tiny throwaway CSV.
- **Empirical:** the Phase 3 acceptance criterion (3× new APs/min vs commute baseline) is the real test. Run it. Capture the number. If it's <2×, the scorer is wrong.

---

## 9. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| WDGoWars API changes (early-stage project) | High | Pin to specific endpoints, version the client, monitor changelog page weekly |
| WiGLE rate limits cause cascade failures | Medium | Aggressive caching, exponential backoff, queue uploads if quota hit |
| OSRM gives bad routes in rural VT (sparse OSM data) | Medium | Fallback to Mapbox Directions API for any leg OSRM can't solve; flag low-confidence segments in UI |
| Daily 20k AP cap on WDGoWars throttles big runs | Low | Already handled in uploader: split + queue |
| Driving while looking at phone UI = unsafe | Critical | UI is plan-before-driving + post-drive review only. **No live in-drive UI in v1.** Plan in driveway, drive with phone in pocket, review on return. State this in the README. |

---

## 10. How to use this plan

1. **Domenic:** sign up at https://wdgwars.pl, generate API key, save it. Confirm your WiGLE.net API token from `wigle.net/account`. Provision the Hetzner CPX21 and set up DNS for `warroute.darkhorseinfosec.com`.
2. **Open a fresh Claude Code session** at `D:\Projects\warroute` (or `cd D:/Projects && claude code` from WSL).
3. **First message to Claude Code:**
   > Read `PLAN.md`. Confirm you understand it. Ask any clarifying questions. Then begin Phase 0. Stop and report back when Phase 0 acceptance criteria pass — do not proceed to Phase 1 without my go-ahead.
4. **Iterate phase by phase.** Don't let it run all five phases unattended; the value of human checkpoints between phases is high.
5. **When Claude Code hits a real architectural fork** (something not covered in this plan, or where the plan turns out to be wrong), have it write the question + its proposed answer to `DECISIONS.md` and ping Domenic. Domenic routes the question to Claude-the-architect (this chat or a new one with this plan attached) for sign-off.
6. **After v1 ships:** write a `RETROSPECTIVE.md`. What took longer than expected? What was wrong in this plan? Use it to write the next plan better.

---

## 11. Definition of done (v1)

- [ ] Repo on `@DarkHorse-InfoSec`, CI green (lint + tests).
- [ ] Hetzner box live at `warroute.darkhorseinfosec.com`, TLS valid, basic auth gated.
- [ ] Plan a 90-min loop from Newport, drive it, return home, see auto-uploaded run with correct stats — without touching a keyboard mid-drive.
- [ ] WDGoWars profile shows the run, points awarded, any cells captured.
- [ ] WiGLE.net profile shows the same CSV uploaded.
- [ ] README documents the full setup so it's reproducible if the VPS dies.

---

*End of plan. Total expected build time: 15–20 hours of Claude Code wall-clock, spread across 4–7 sessions. Phase 1 alone delivers most of the day-to-day value; Phase 3 delivers the magic.*
