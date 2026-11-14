-- S-NOTIFY: rule notifications, one row per target (NOTIFY-9). A row is written before
-- the send, so a server restart retries ``pending`` rows instead of sending twice.
-- ``dedup_key`` names the transition (run, rule event, pass or hold refs) it is about.
-- Reminders (NOTIFY-15) are rows of their own with ``parent`` set.
CREATE TABLE notifications (
    id               TEXT PRIMARY KEY,
    profile          TEXT NOT NULL,
    run_id           TEXT,
    event            TEXT NOT NULL,
    dedup_key        TEXT NOT NULL,
    target           TEXT NOT NULL,
    state            TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    message          TEXT NOT NULL,
    items            TEXT NOT NULL DEFAULT '[]',
    remind_after     INTEGER,
    remind_limit     INTEGER NOT NULL DEFAULT 0,
    reminders        INTEGER NOT NULL DEFAULT 0,
    reminding        INTEGER NOT NULL DEFAULT 0,
    parent           TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT,
    last_reminded_at TEXT,
    error            TEXT,
    UNIQUE (dedup_key, target)
);
CREATE INDEX notifications_pending ON notifications (state);
CREATE INDEX notifications_reminding ON notifications (reminding);
