-- The tag grid's recent edits look up journal entries by ChangeSet (F-TAG-GRID).
CREATE INDEX journal_changeset ON journal (changeset_id, created_at);
