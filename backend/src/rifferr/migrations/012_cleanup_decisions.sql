-- S-FILES-CLEANUP: folder listings waiting for Delete or Keep (FILES-CLEANUP-14). ``body``
-- is the whole decision as JSON; ``state``, ``deadline`` and ``claim`` are columns for
-- queries. ``claim`` is the job applying the decision, so it is decided only once.
CREATE TABLE cleanup_decisions (
    id         TEXT PRIMARY KEY,
    profile    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    state      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline   TEXT,
    claim      TEXT
);
CREATE INDEX cleanup_decisions_run ON cleanup_decisions (run_id, state);
CREATE INDEX cleanup_decisions_pending ON cleanup_decisions (state, deadline);
