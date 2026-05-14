-- WarRoute schema v2
-- Phase 6a: multi-stop planner. Adds stops_json (JSON array of intermediate
-- destinations) to planned_routes. Old single-destination rows keep their
-- destination_lat/lon values; new multi-stop plans persist their full ordered
-- stop list as JSON for replay. stops_json is canonical when set; destination_*
-- is kept populated to the last stop for back-compat with v1 readers.

ALTER TABLE planned_routes ADD COLUMN stops_json TEXT;

-- Tracks the scheduled departure time for Phase 6b arrival-time plans. A
-- separate row per plan so the ntfy alarm job can poll without scanning
-- the full planned_routes table.
CREATE TABLE IF NOT EXISTS scheduled_departures (
    plan_id        INTEGER PRIMARY KEY REFERENCES planned_routes(id),
    departure_at   TIMESTAMP NOT NULL,
    arrive_by      TIMESTAMP NOT NULL,
    notified_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scheduled_departures_pending
    ON scheduled_departures(departure_at) WHERE notified_at IS NULL;

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
