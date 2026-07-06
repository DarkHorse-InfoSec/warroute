-- WarRoute schema v4
-- Per-user API credentials for the tester program. Extends user_prefs with
-- nullable token columns. When a column is NULL, the app falls back to the
-- system .env credential for that service. See DECISIONS.md 2026-05-14 (very late).
--
-- Storage at rest: PLAINTEXT, same trust model as /etc/warroute/warroute.env
-- (filesystem-protected, warroute-user-owned). Surface this clearly in the UI.
-- A future migration can encrypt-at-rest if the trust model tightens (e.g. real
-- multi-tenant cloud deploy); for the current single-server tester program the
-- complexity is not warranted.

ALTER TABLE user_prefs ADD COLUMN wigle_name TEXT;
ALTER TABLE user_prefs ADD COLUMN wigle_token TEXT;
ALTER TABLE user_prefs ADD COLUMN wdgowars_name TEXT;
ALTER TABLE user_prefs ADD COLUMN wdgowars_token TEXT;
ALTER TABLE user_prefs ADD COLUMN ors_api_key TEXT;
ALTER TABLE user_prefs ADD COLUMN mapbox_api_key TEXT;
ALTER TABLE user_prefs ADD COLUMN ntfy_topic TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (4);
