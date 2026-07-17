CREATE TABLE IF NOT EXISTS asr_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    turn_index INTEGER NOT NULL CHECK (turn_index BETWEEN 1 AND 5),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    user_audio_path TEXT NOT NULL,
    user_audio_sha256 TEXT NOT NULL,
    asr_provider TEXT,
    asr_status TEXT NOT NULL CHECK (asr_status IN ('success', 'failed', 'timeout')),
    asr_text TEXT,
    asr_latency_ms INTEGER CHECK (asr_latency_ms IS NULL OR asr_latency_ms >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, turn_index, attempt_no),
    FOREIGN KEY (session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_asr_attempts_session_turn
ON asr_attempts (session_id, turn_index, id);
