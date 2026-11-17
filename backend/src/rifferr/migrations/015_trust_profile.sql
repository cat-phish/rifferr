-- SEC-63: trust is kept per profile. ``profile`` is the profile the user trusted the
-- content in; '' means every profile. Rows written before this migration had no profile,
-- so they keep their meaning ('' : every profile) and no stored trust is lost. New trust
-- through the API, the triggers page or ``rifferr trust --profile`` names its profile.
CREATE TABLE trusted_files_new (
    sha256     TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT '',
    path_hint  TEXT NOT NULL,
    trusted_at TEXT NOT NULL,
    trusted_by TEXT NOT NULL,
    PRIMARY KEY (sha256, profile)
);
INSERT INTO trusted_files_new (sha256, profile, path_hint, trusted_at, trusted_by)
    SELECT sha256, '', path_hint, trusted_at, trusted_by FROM trusted_files;
DROP TABLE trusted_files;
ALTER TABLE trusted_files_new RENAME TO trusted_files;
