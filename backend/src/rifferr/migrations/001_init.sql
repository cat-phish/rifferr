-- Tables introduced by F-WORKER (jobs, events) and F-SECURITY (trusted_files).
-- IF NOT EXISTS: app DBs created before migrations existed already have them.
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    run_id      TEXT NOT NULL UNIQUE,
    state       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    claimed_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (profile, state, created_at);
CREATE TABLE IF NOT EXISTS trusted_files (
    sha256     TEXT PRIMARY KEY,
    path_hint  TEXT NOT NULL,
    trusted_at TEXT NOT NULL,
    trusted_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    step_id TEXT,
    data    TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
