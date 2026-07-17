CREATE TABLE IF NOT EXISTS export_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL UNIQUE,
    export_type TEXT NOT NULL CHECK (export_type IN ('experiment_data', 'complete_no_external_error_data', 'reimbursement')),
    filters_json TEXT NOT NULL,
    include_test INTEGER NOT NULL DEFAULT 0 CHECK (include_test IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    progress_message TEXT,
    output_path TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT
);

ALTER TABLE export_jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE export_jobs ADD COLUMN lease_token TEXT;
ALTER TABLE export_jobs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE export_jobs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE export_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0);
ALTER TABLE export_jobs ADD COLUMN failure_kind TEXT CHECK (failure_kind IN ('recoverable', 'terminal'));
ALTER TABLE export_jobs ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'unpublished' CHECK (publication_state IN ('unpublished', 'publishing', 'published'));
ALTER TABLE export_jobs ADD COLUMN publication_token TEXT;
ALTER TABLE export_jobs ADD COLUMN staging_path TEXT;
ALTER TABLE export_jobs ADD COLUMN canonical_path TEXT;
ALTER TABLE export_jobs ADD COLUMN archive_sha256 TEXT CHECK (archive_sha256 IS NULL OR length(archive_sha256) = 64);

UPDATE export_jobs
SET publication_state = 'published',
    canonical_path = output_path
WHERE status = 'succeeded'
  AND output_path IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_export_jobs_claimable
ON export_jobs (status, id);

CREATE INDEX IF NOT EXISTS idx_export_jobs_expired_lease
ON export_jobs (status, lease_expires_at);
