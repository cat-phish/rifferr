-- S-TAGS-CHOOSE: albums waiting for a person to choose an outcome (TAGS-CHOOSE-3). ``body``
-- is the whole review as JSON, options included; ``claim`` is the job applying a decision,
-- so a review is decided only once.
CREATE TABLE choice_reviews (
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
CREATE INDEX choice_reviews_run ON choice_reviews (run_id, state);
CREATE INDEX choice_reviews_pending ON choice_reviews (state, created_at);
