-- WarRoute schema v3
-- Per-user preferences keyed by Caddy basic_auth username (forwarded via
-- X-Forwarded-User header). See DECISIONS.md 2026-05-14 (late evening) for the
-- "tell the app who's logged in" fork from PLAN.md §9.
--
-- Username is the basic_auth identity sanitized to [a-z0-9_.-]{1,32}. App-level
-- validation is the source of truth; the column accepts TEXT to keep the schema
-- forgiving if we widen the charset later.

CREATE TABLE IF NOT EXISTS user_prefs (
    username    TEXT PRIMARY KEY,
    home_lat    REAL NOT NULL,
    home_lon    REAL NOT NULL,
    home_label  TEXT,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_version (version) VALUES (3);
