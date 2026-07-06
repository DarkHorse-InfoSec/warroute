-- WarRoute schema v8
-- Global daily counter for the shared-ORS GEOCODE key (DECISIONS.md 2026-07-05
-- security-pass). Routing already had shared_routing_usage (_v6) enforcing a
-- per-UTC-day cap; geocoding had only a per-IP/min limit and no global backstop,
-- so a distributed set of clients could drain the operator's ORS geocoding quota.
-- This mirrors shared_routing_usage: one row per UTC day, incremented per grant.

CREATE TABLE IF NOT EXISTS shared_geocode_usage (
    day   TEXT PRIMARY KEY,   -- 'YYYY-MM-DD' UTC
    count INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO schema_version (version) VALUES (8);
