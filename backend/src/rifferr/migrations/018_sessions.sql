-- F-SECURITY SECURITY-15: browser sessions survive a restart. Only the SHA-256 of a
-- session id is stored, so the table holds nothing a browser could sign in with.
-- Times are Unix seconds.
CREATE TABLE sessions (
    id_sha256    TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    last_used_at REAL NOT NULL,
    expires_at   REAL NOT NULL,
    started_by   TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX sessions_expires ON sessions (expires_at);
