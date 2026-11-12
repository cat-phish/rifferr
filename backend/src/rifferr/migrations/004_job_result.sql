-- C-JOURNAL: what a finished job did (e.g. which entries an undo undid), as JSON.
ALTER TABLE jobs ADD COLUMN result TEXT;
