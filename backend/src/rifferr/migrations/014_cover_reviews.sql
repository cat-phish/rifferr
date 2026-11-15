-- S-COVERS-SEARCH: albums waiting for a person to pick a cover (COVERS-SEARCH-14). ``body``
-- is the whole review as JSON, candidates included; ``claim`` is the job applying a choice,
-- so a review is decided only once.
CREATE TABLE cover_reviews (
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
CREATE INDEX cover_reviews_run ON cover_reviews (run_id, state);
CREATE INDEX cover_reviews_pending ON cover_reviews (state, created_at);
