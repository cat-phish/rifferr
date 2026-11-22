-- S-LIBRARY-PRUNE: albums whose files are gone, waiting for Accept or Reject
-- (LIBRARY-PRUNE-3). ``body`` is the decision as JSON; ``claim`` is the job applying it.
CREATE TABLE prune_decisions (
    id         TEXT PRIMARY KEY,
    profile    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    state      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claim      TEXT
);
CREATE INDEX prune_decisions_run ON prune_decisions (run_id, state);

-- LIBRARY-PRUNE-4: removal records keep the album's snapshot.
ALTER TABLE album_removals ADD COLUMN snapshot TEXT;
