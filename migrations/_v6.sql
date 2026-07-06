-- WarRoute schema v6
-- Daily counter for shared-ORS usage: the single system-key carve-out in the
-- stateless access model (DECISIONS.md 2026-07-04 design). WiGLE + WDGoWars keys
-- are strictly client-supplied; ORS is the one service most users lack, so ORS
-- operations (routing/geocoding/optimization) may fall back to the operator's
-- shared key, but only until this per-day counter nears the free-tier cap, after
-- which the user is told to add their own ORS key. day = 'YYYY-MM-DD' (UTC).

CREATE TABLE IF NOT EXISTS shared_routing_usage (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO schema_version (version) VALUES (6);
