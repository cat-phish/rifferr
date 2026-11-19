-- S-LIBRARY-MOVE: albums that can't move yet, waiting in the review queue (LIBRARY-MOVE-5a).
-- ``body`` is the whole conflict as JSON, the step's settings included; ``claim`` is the job
-- retrying it, so a conflict is decided only once at a time.
CREATE TABLE move_conflicts (
    id         TEXT PRIMARY KEY,
    profile    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    album_id   TEXT NOT NULL,
    state      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claim      TEXT
);
CREATE INDEX move_conflicts_run ON move_conflicts (run_id, state);
CREATE INDEX move_conflicts_pending ON move_conflicts (state, created_at);
