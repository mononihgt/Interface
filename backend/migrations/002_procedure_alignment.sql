CREATE TABLE IF NOT EXISTS admin_assignment_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_type TEXT NOT NULL CHECK (participant_type IN ('short', 'long')),
    condition TEXT NOT NULL CHECK (condition IN ('human', 'tool')),
    subcondition TEXT NOT NULL CHECK (subcondition IN ('qa', 'planning', 'chat', 'decision', 'execution')),
    error_type_id TEXT NOT NULL CHECK (error_type_id IN ('factual_minor', 'factual_major', 'logic_minor', 'logic_major', 'social_minor', 'social_major', 'system_failure')),
    cap INTEGER CHECK (cap IS NULL OR cap >= 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (participant_type, condition, subcondition, error_type_id)
);

CREATE TABLE IF NOT EXISTS pending_assignment_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone_hash TEXT NOT NULL,
    participant_type TEXT NOT NULL,
    condition TEXT NOT NULL,
    subcondition TEXT NOT NULL,
    error_type_id TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clean_data_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('eligible', 'review_needed', 'excluded')),
    reasons_json TEXT NOT NULL,
    reviewer_note TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (participant_id),
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
);

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
