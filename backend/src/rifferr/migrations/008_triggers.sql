-- F-TRIGGERS: what started a run and with which input (TRIGGERS-24), dedup records
-- (TRIGGERS-19 to -22) and webhook secret hashes (TRIGGERS-17).
ALTER TABLE jobs ADD COLUMN trigger TEXT;
ALTER TABLE jobs ADD COLUMN input TEXT;

CREATE TABLE trigger_inputs (
    profile     TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    input_path  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (profile, workflow, input_path)
);

CREATE TABLE webhook_secrets (
    profile    TEXT NOT NULL,
    name       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile, name)
);
