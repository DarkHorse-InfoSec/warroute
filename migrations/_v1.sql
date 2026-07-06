-- WarRoute schema v1
-- Initial migration. Apply via warroute.db.run_migrations().

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- One row per CSV uploaded.
CREATE TABLE IF NOT EXISTS sessions (
    id                    INTEGER PRIMARY KEY,
    source                TEXT NOT NULL,            -- 'wigle-android' | 'pineapple-pager' | 'bruce' | 'manual'
    csv_path              TEXT NOT NULL,
    csv_sha256            TEXT NOT NULL UNIQUE,
    started_at            TIMESTAMP NOT NULL,
    ended_at              TIMESTAMP NOT NULL,
    distance_km           REAL,
    new_aps               INTEGER,
    total_aps             INTEGER,
    uploaded_wigle_at     TIMESTAMP,
    uploaded_wdgowars_at  TIMESTAMP,
    wdgowars_run_id       TEXT,
    points_earned         INTEGER,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Deduplicated AP sightings (one row per unique BSSID).
CREATE TABLE IF NOT EXISTS observations (
    bssid                 TEXT PRIMARY KEY,
    ssid                  TEXT,
    encryption            TEXT,
    first_seen_session    INTEGER REFERENCES sessions(id),
    first_seen_lat        REAL,
    first_seen_lon        REAL,
    last_seen_at          TIMESTAMP,
    times_seen            INTEGER DEFAULT 1
);

-- 2x3 km grid cells, materialized for the home radius (or any region we plan in).
CREATE TABLE IF NOT EXISTS cells (
    id                    TEXT PRIMARY KEY,         -- canonical 'lat_lon' rounded to grid
    center_lat            REAL NOT NULL,
    center_lon            REAL NOT NULL,
    bbox_geojson          TEXT NOT NULL,
    your_ap_count         INTEGER DEFAULT 0,
    estimated_total_aps   INTEGER,                  -- from WiGLE.net density (cached)
    wdgowars_owner        TEXT,                     -- NULL = uncaptured, 'me' = mine, else rival username
    wdgowars_capture_value INTEGER,                 -- points-if-captured per WDGoWars (when exposed)
    wdgowars_last_capture TIMESTAMP,
    last_refreshed        TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cells_owner   ON cells(wdgowars_owner);
CREATE INDEX IF NOT EXISTS idx_cells_refresh ON cells(last_refreshed);

-- History of generated plans (compare predicted vs actual).
CREATE TABLE IF NOT EXISTS planned_routes (
    id                    INTEGER PRIMARY KEY,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    home_lat              REAL NOT NULL,
    home_lon              REAL NOT NULL,
    duration_min          INTEGER NOT NULL,
    mode                  TEXT NOT NULL,            -- 'loop' | 'oneway'
    destination_lat       REAL,
    destination_lon       REAL,
    waypoints_json        TEXT NOT NULL,
    gpx_path              TEXT,
    estimated_new_aps     INTEGER,
    estimated_drive_min   REAL,
    actual_session_id     INTEGER REFERENCES sessions(id)
);

-- Schema version tracking.
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
