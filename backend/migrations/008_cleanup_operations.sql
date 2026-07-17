CREATE TABLE cleanup_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('relocate', 'delete')),
    source_path TEXT NOT NULL,
    staging_path TEXT UNIQUE,
    destination_path TEXT,
    expected_sha256 TEXT,
    preserve_source INTEGER NOT NULL DEFAULT 0 CHECK (preserve_source IN (0, 1)),
    worker_token TEXT,
    lease_expires_at TEXT,
    state TEXT NOT NULL CHECK (
        state IN (
            'planned',
            'staged',
            'database_committed',
            'completed',
            'rolled_back',
            'review_needed'
        )
    ),
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attempt_id) REFERENCES participant_attempts(id) ON DELETE CASCADE
);

CREATE TABLE cleanup_operation_owners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL,
    owner_table TEXT NOT NULL CHECK (owner_table IN ('conversation_turns', 'asr_attempts')),
    owner_row_id INTEGER NOT NULL,
    owner_field TEXT NOT NULL CHECK (owner_field = 'user_audio_path'),
    original_path TEXT NOT NULL,
    destination_path TEXT,
    original_sha256 TEXT,
    FOREIGN KEY (operation_id) REFERENCES cleanup_operations(id) ON DELETE CASCADE,
    UNIQUE (operation_id, owner_table, owner_row_id, owner_field)
);

CREATE INDEX idx_cleanup_operations_state
ON cleanup_operations (state, operation_kind, id);

CREATE INDEX idx_cleanup_operation_owners_operation
ON cleanup_operation_owners (operation_id, id);
