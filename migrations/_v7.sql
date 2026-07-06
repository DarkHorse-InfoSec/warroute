-- WarRoute schema v7
-- Opt-in end-to-end-encrypted config sync (DECISIONS.md 2026-07-04 sync entry).
-- Solves iOS Safari's ~7-day localStorage eviction + cross-device: a user can
-- back up their in-browser config (keys + prefs) to the server so it survives
-- eviction and restores on another device.
--
-- ZERO-KNOWLEDGE: the client encrypts the config in the BROWSER with a key derived
-- from a user-held sync code before upload; the server stores only opaque
-- ciphertext and never sees the code or the plaintext keys. `sync_id` is a SHA-256
-- derived from the code (not the code itself), so it is not reversible to the code.

CREATE TABLE IF NOT EXISTS synced_configs (
    sync_id    TEXT PRIMARY KEY,   -- hex SHA-256 derived from the user's sync code
    ciphertext TEXT NOT NULL,      -- base64(iv || AES-GCM ciphertext), opaque to the server
    updated_at TEXT NOT NULL       -- ISO8601 UTC
);

INSERT OR IGNORE INTO schema_version (version) VALUES (7);
