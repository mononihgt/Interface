CREATE TABLE IF NOT EXISTS external_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    participant_id INTEGER NOT NULL,
    attempt_id INTEGER,
    session_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('turn', 'asr')),
    turn_index INTEGER NOT NULL CHECK (turn_index BETWEEN 1 AND 5),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    result_entity_id INTEGER,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE,
    FOREIGN KEY (attempt_id) REFERENCES participant_attempts(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_operations_scope
ON external_operations (
    participant_id,
    COALESCE(attempt_id, 0),
    session_id,
    kind,
    turn_index,
    operation_id
);

CREATE INDEX IF NOT EXISTS idx_external_operations_status
ON external_operations (status, updated_at);
