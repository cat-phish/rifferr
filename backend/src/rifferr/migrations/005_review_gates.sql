-- S-REVIEW: review gates. body is the ReviewGate JSON, fixed at creation (REVIEW-6);
-- the decision columns are the source of truth for state and who decided.
CREATE TABLE review_gates (
    id         TEXT PRIMARY KEY,
    profile    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    state      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline   TEXT,
    decided_at TEXT,
    decided_by TEXT,
    note       TEXT
);
CREATE INDEX review_gates_run ON review_gates (run_id, state);
CREATE INDEX review_gates_deadline ON review_gates (state, deadline);
