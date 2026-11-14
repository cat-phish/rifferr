-- C-LANES: one album's path through a run (LANES-1, -2) and holds resolved through
-- Engine.resolve_hold (LANES-14).
CREATE TABLE run_lanes (
    run_id     TEXT NOT NULL,
    key        TEXT NOT NULL,
    label      TEXT NOT NULL,
    state      TEXT NOT NULL,
    position   TEXT,
    holds      TEXT NOT NULL DEFAULT '[]',
    steps      TEXT NOT NULL DEFAULT '{}',
    passes     TEXT NOT NULL DEFAULT '{}',
    seq        INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, key)
);

CREATE TABLE resolved_holds (
    run_id TEXT NOT NULL,
    ref    TEXT NOT NULL,
    PRIMARY KEY (run_id, ref)
);
