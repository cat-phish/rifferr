-- S-REVIEW REVIEW-14: edits held on an `edit: true` gate until its albums are continued.
-- JSON list of HeldEdit (target, album, changes); written only on continue (REVIEW-15).
ALTER TABLE review_gates ADD COLUMN edits TEXT NOT NULL DEFAULT '[]';
