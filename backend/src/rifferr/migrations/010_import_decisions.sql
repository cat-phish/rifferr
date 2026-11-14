-- F-IMPORT-UI: albums an import left for a person (IMPORT-UI-4). ``body`` is the whole
-- decision as JSON; ``state``, ``deadline`` and ``claim`` are kept as columns for queries.
-- ``claim`` is the job applying the decision right now, so it is decided only once.
CREATE TABLE import_decisions (
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
CREATE INDEX import_decisions_run ON import_decisions (run_id, state);
CREATE INDEX import_decisions_pending ON import_decisions (state, deadline);
