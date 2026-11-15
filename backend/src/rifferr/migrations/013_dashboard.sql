-- F-DASHBOARD: write-ahead records of album removals by import undo (DASHBOARD-8),
-- inputs whose import was undone (DASHBOARD-9) and per-album gate decisions
-- (DASHBOARD-11).
CREATE TABLE album_removals (
    id           TEXT PRIMARY KEY,
    profile      TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    album_id     TEXT NOT NULL,
    label        TEXT NOT NULL,
    delete_files INTEGER NOT NULL,
    state        TEXT NOT NULL,
    deleted      TEXT NOT NULL DEFAULT '[]',
    error        TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX album_removals_run ON album_removals (run_id);
CREATE INDEX album_removals_state ON album_removals (profile, state);

ALTER TABLE trigger_inputs ADD COLUMN undone_at TEXT;

ALTER TABLE review_gates ADD COLUMN album_decisions TEXT NOT NULL DEFAULT '{}';
