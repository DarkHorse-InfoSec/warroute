-- WarRoute schema v5
-- Per-user preferred navigation app for the plan-result hand-off. Nullable;
-- NULL -> the app default ("google"). Valid values are enforced in the app
-- layer (warroute.web.user_prefs.VALID_NAV_APPS), not by a CHECK constraint,
-- so adding a new nav target later needs no migration. See tasks/todo.md
-- ACTIVE PLAN 2026-07-04 and DECISIONS.md 2026-07-04.

ALTER TABLE user_prefs ADD COLUMN preferred_nav_app TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (5);
