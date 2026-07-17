CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    phone_hash TEXT NOT NULL,
    participant_type TEXT NOT NULL CHECK (participant_type IN ('short', 'long')),
    condition TEXT NOT NULL CHECK (condition IN ('human', 'tool')),
    subcondition TEXT NOT NULL CHECK (subcondition IN ('qa', 'planning', 'chat', 'decision', 'execution')),
    topic_key TEXT NOT NULL,
    error_type_id TEXT NOT NULL CHECK (error_type_id IN ('factual_minor', 'factual_major', 'logic_minor', 'logic_major', 'social_minor', 'social_major', 'system_failure')),
    target_days INTEGER NOT NULL CHECK (
        target_days IN (1, 3)
        AND (
            (participant_type = 'short' AND target_days = 1)
            OR (participant_type = 'long' AND target_days = 3)
        )
    ),
    current_status TEXT NOT NULL CHECK (current_status IN ('active', 'completed', 'blocked', 'withdrawn')),
    blocked_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS participant_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL,
    day_index INTEGER NOT NULL CHECK (day_index IN (1, 2, 3)),
    calendar_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('not_started', 'pretest', 'in_experiment', 'completed', 'missed', 'blocked')),
    started_at TEXT,
    completed_at TEXT,
    missed_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (participant_id, day_index),
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pretest_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL,
    day_index INTEGER NOT NULL CHECK (day_index IN (1, 2, 3)),
    status TEXT NOT NULL CHECK (status IN ('draft', 'final')),
    payload_json TEXT NOT NULL,
    autosave_count INTEGER NOT NULL DEFAULT 0 CHECK (autosave_count >= 0),
    last_saved_at TEXT,
    submitted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS experiment_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL,
    participant_day_id INTEGER NOT NULL,
    session_uuid TEXT NOT NULL UNIQUE,
    condition TEXT NOT NULL CHECK (condition IN ('human', 'tool')),
    subcondition TEXT NOT NULL CHECK (subcondition IN ('qa', 'planning', 'chat', 'decision', 'execution')),
    topic_key TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    agent_graph_version TEXT NOT NULL,
    error_type_id TEXT NOT NULL CHECK (error_type_id IN ('factual_minor', 'factual_major', 'logic_minor', 'logic_major', 'social_minor', 'social_major', 'system_failure')),
    planned_error_turn INTEGER NOT NULL CHECK (planned_error_turn BETWEEN 1 AND 5),
    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'abandoned', 'invalid', 'interrupted')),
    started_at TEXT,
    completed_at TEXT,
    client_info_json TEXT,
    is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE,
    FOREIGN KEY (participant_day_id) REFERENCES participant_days(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    turn_index INTEGER NOT NULL CHECK (turn_index BETWEEN 1 AND 5),
    user_text TEXT,
    user_input_mode TEXT NOT NULL CHECK (user_input_mode IN ('voice', 'text_test_only')),
    user_audio_path TEXT,
    user_audio_sha256 TEXT,
    asr_provider TEXT,
    asr_status TEXT NOT NULL CHECK (asr_status IN ('not_used', 'success', 'failed', 'timeout')),
    asr_text TEXT,
    asr_latency_ms INTEGER CHECK (asr_latency_ms IS NULL OR asr_latency_ms >= 0),
    assistant_text TEXT,
    response_latency_ms INTEGER CHECK (response_latency_ms IS NULL OR response_latency_ms >= 0),
    llm_provider TEXT,
    llm_model TEXT,
    llm_route TEXT,
    llm_attempts_json TEXT,
    error_planned INTEGER NOT NULL DEFAULT 0 CHECK (error_planned IN (0, 1)),
    error_type_id TEXT CHECK (error_type_id IS NULL OR error_type_id IN ('factual_minor', 'factual_major', 'logic_minor', 'logic_major', 'social_minor', 'social_major', 'system_failure')),
    error_presented INTEGER NOT NULL DEFAULT 0 CHECK (error_presented IN (0, 1)),
    error_presentation TEXT NOT NULL CHECK (error_presentation IN ('assistant_text', 'simulated_ui', 'system_failure', 'none')),
    error_evaluator_provider TEXT,
    error_evaluator_model TEXT,
    error_evaluator_result_json TEXT,
    agent_state_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, turn_index),
    FOREIGN KEY (session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turn_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL UNIQUE,
    stance_score INTEGER NOT NULL CHECK (stance_score BETWEEN 1 AND 5),
    trust_score INTEGER NOT NULL CHECK (trust_score BETWEEN 1 AND 7),
    submitted_at TEXT NOT NULL,
    client_elapsed_ms INTEGER CHECK (client_elapsed_ms IS NULL OR client_elapsed_ms >= 0),
    FOREIGN KEY (turn_id) REFERENCES conversation_turns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('table', 'copy_versions', 'decision_matrix', 'preference_cards', 'plan_card', 'weather_card')),
    status TEXT NOT NULL CHECK (status IN ('draft', 'completed', 'failed')),
    payload_json TEXT NOT NULL,
    visible_to_participant INTEGER NOT NULL DEFAULT 1 CHECK (visible_to_participant IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (turn_id) REFERENCES conversation_turns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_call_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    route TEXT NOT NULL CHECK (route IN ('chat', 'evaluator', 'asr')),
    provider TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'timeout', 'http_error', 'invalid_response', 'local_fallback')),
    http_status INTEGER,
    error_code TEXT,
    error_message_summary TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    cooldown_applied INTEGER NOT NULL DEFAULT 0 CHECK (cooldown_applied IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('login', 'update_assignment_cap', 'block_participant', 'export_data', 'test_agent')),
    target_type TEXT,
    target_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_risk_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    flag TEXT NOT NULL CHECK (flag IN ('api_failure', 'local_fallback', 'asr_failed', 'asr_repeated_failure', 'missing_rating', 'error_not_presented', 'artifact_schema_error', 'abandoned', 'long_term_missed_day')),
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_participants_phone_hash ON participants (phone_hash);
CREATE INDEX IF NOT EXISTS idx_participants_created_at ON participants (created_at);
CREATE INDEX IF NOT EXISTS idx_participant_days_created_at ON participant_days (created_at);
CREATE INDEX IF NOT EXISTS idx_pretest_responses_created_at ON pretest_responses (created_at);
CREATE INDEX IF NOT EXISTS idx_experiment_sessions_session_uuid ON experiment_sessions (session_uuid);
CREATE INDEX IF NOT EXISTS idx_experiment_sessions_is_test ON experiment_sessions (is_test);
CREATE INDEX IF NOT EXISTS idx_experiment_sessions_created_at ON experiment_sessions (created_at);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_created_at ON conversation_turns (created_at);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_created_at ON task_artifacts (created_at);
CREATE INDEX IF NOT EXISTS idx_api_call_logs_created_at ON api_call_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_admin_events_created_at ON admin_events (created_at);
CREATE INDEX IF NOT EXISTS idx_session_risk_flags_created_at ON session_risk_flags (created_at);
