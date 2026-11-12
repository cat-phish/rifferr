-- C-CHANGESET, C-JOURNAL: ChangeSets, review decisions and the journal.
CREATE TABLE changesets (
    id          TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    run_id      TEXT,
    step_id     TEXT,
    state       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE INDEX changesets_run ON changesets (run_id, state);
CREATE INDEX changesets_profile ON changesets (profile, state, created_at);
CREATE TABLE changeset_decisions (
    changeset_id TEXT NOT NULL REFERENCES changesets (id),
    idx          INTEGER NOT NULL,
    accepted     INTEGER NOT NULL,
    PRIMARY KEY (changeset_id, idx)
);
CREATE TABLE journal (
    id           TEXT PRIMARY KEY,
    profile      TEXT NOT NULL,
    changeset_id TEXT NOT NULL,
    run_id       TEXT,
    state        TEXT NOT NULL,
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    undone_by    TEXT
);
CREATE INDEX journal_profile ON journal (profile, state);
CREATE INDEX journal_run ON journal (run_id, created_at);
-- Jobs: besides runs (one row per run, the run record, API-6) the table now holds
-- jobs of other kinds that refer to a run (review decisions, undo). run_id stays
-- unique for kind 'run' only.
CREATE TABLE jobs_003 (
    id            TEXT PRIMARY KEY,
    profile       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    run_id        TEXT,
    state         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    claimed_at    TEXT,
    finished_at   TEXT,
    workflow      TEXT,
    workflow_hash TEXT,
    started_at    TEXT
);
INSERT INTO jobs_003 (id, profile, kind, run_id, state, payload, error, created_at,
                      claimed_at, finished_at, workflow, workflow_hash, started_at)
    SELECT id, profile, kind, run_id, state, payload, error, created_at,
           claimed_at, finished_at, workflow, workflow_hash, started_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_003 RENAME TO jobs;
CREATE UNIQUE INDEX jobs_run ON jobs (run_id) WHERE kind = 'run';
CREATE INDEX jobs_queue ON jobs (profile, state, created_at);
