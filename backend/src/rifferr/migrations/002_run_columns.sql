-- F-API: the job row is the run record (API-6).
ALTER TABLE jobs ADD COLUMN workflow TEXT;
ALTER TABLE jobs ADD COLUMN workflow_hash TEXT;
ALTER TABLE jobs ADD COLUMN started_at TEXT;
