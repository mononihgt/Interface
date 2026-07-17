CREATE TABLE IF NOT EXISTS participant_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    participant_type TEXT NOT NULL CHECK (participant_type IN ('short', 'long')),
    condition TEXT NOT NULL CHECK (condition IN ('human', 'tool')),
    subcondition TEXT NOT NULL CHECK (subcondition IN ('qa', 'planning', 'chat', 'decision', 'execution')),
    topic_key TEXT NOT NULL,
    error_type_id TEXT NOT NULL CHECK (error_type_id IN ('factual_minor', 'factual_major', 'logic_minor', 'logic_major', 'social_minor', 'social_major', 'system_failure')),
    target_days INTEGER NOT NULL CHECK (target_days IN (1, 3)),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'blocked', 'abandoned', 'converted_to_short')),
    valid_for_export INTEGER NOT NULL DEFAULT 1 CHECK (valid_for_export IN (0, 1)),
    source_attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE SET NULL,
    export_role TEXT NOT NULL CHECK (export_role IN ('normal_short', 'normal_long', 'converted_short')),
    blocked_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (participant_id, attempt_no),
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
);

ALTER TABLE participants ADD COLUMN current_attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE SET NULL;
ALTER TABLE participant_days ADD COLUMN attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE;
ALTER TABLE participant_days ADD COLUMN valid_for_export INTEGER NOT NULL DEFAULT 1 CHECK (valid_for_export IN (0, 1));
ALTER TABLE participant_days ADD COLUMN export_scope_note TEXT;
ALTER TABLE pretest_responses ADD COLUMN attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE;
ALTER TABLE pretest_responses ADD COLUMN source_pretest_response_id INTEGER REFERENCES pretest_responses(id) ON DELETE SET NULL;
ALTER TABLE experiment_sessions ADD COLUMN attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE;
ALTER TABLE experiment_sessions ADD COLUMN valid_for_export INTEGER NOT NULL DEFAULT 1 CHECK (valid_for_export IN (0, 1));
ALTER TABLE experiment_sessions ADD COLUMN export_scope_note TEXT;
ALTER TABLE clean_data_audits ADD COLUMN attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE;

INSERT INTO participant_attempts (
    participant_id,
    attempt_no,
    participant_type,
    condition,
    subcondition,
    topic_key,
    error_type_id,
    target_days,
    status,
    valid_for_export,
    source_attempt_id,
    export_role,
    blocked_reason,
    created_at,
    updated_at
)
SELECT
    id,
    1,
    participant_type,
    condition,
    subcondition,
    topic_key,
    error_type_id,
    target_days,
    CASE current_status
        WHEN 'completed' THEN 'completed'
        WHEN 'blocked' THEN 'blocked'
        ELSE 'active'
    END,
    CASE current_status
        WHEN 'withdrawn' THEN 0
        ELSE 1
    END,
    NULL,
    CASE participant_type
        WHEN 'long' THEN 'normal_long'
        ELSE 'normal_short'
    END,
    blocked_reason,
    created_at,
    updated_at
FROM participants
WHERE NOT EXISTS (
    SELECT 1
    FROM participant_attempts existing
    WHERE existing.participant_id = participants.id
);

UPDATE participants
SET current_attempt_id = (
    SELECT pa.id
    FROM participant_attempts pa
    WHERE pa.participant_id = participants.id
    ORDER BY pa.attempt_no DESC
    LIMIT 1
)
WHERE current_attempt_id IS NULL;

UPDATE participant_days
SET attempt_id = (
    SELECT p.current_attempt_id
    FROM participants p
    WHERE p.id = participant_days.participant_id
)
WHERE attempt_id IS NULL;

PRAGMA defer_foreign_keys = ON;

CREATE TABLE participant_days_replacement (
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
    attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE,
    valid_for_export INTEGER NOT NULL DEFAULT 1 CHECK (valid_for_export IN (0, 1)),
    export_scope_note TEXT,
    UNIQUE (participant_id, attempt_id, day_index),
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
);

INSERT INTO participant_days_replacement (
    id,
    participant_id,
    day_index,
    calendar_date,
    status,
    started_at,
    completed_at,
    missed_reason,
    created_at,
    updated_at,
    attempt_id,
    valid_for_export,
    export_scope_note
)
SELECT
    id,
    participant_id,
    day_index,
    calendar_date,
    status,
    started_at,
    completed_at,
    missed_reason,
    created_at,
    updated_at,
    attempt_id,
    valid_for_export,
    export_scope_note
FROM participant_days;

DROP TABLE participant_days;
ALTER TABLE participant_days_replacement RENAME TO participant_days;

UPDATE pretest_responses
SET attempt_id = (
    SELECT p.current_attempt_id
    FROM participants p
    WHERE p.id = pretest_responses.participant_id
)
WHERE attempt_id IS NULL;

UPDATE experiment_sessions
SET attempt_id = (
    SELECT p.current_attempt_id
    FROM participants p
    WHERE p.id = experiment_sessions.participant_id
)
WHERE attempt_id IS NULL;

UPDATE clean_data_audits
SET attempt_id = (
    SELECT p.current_attempt_id
    FROM participants p
    WHERE p.id = clean_data_audits.participant_id
)
WHERE attempt_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_participant_attempts_participant ON participant_attempts (participant_id);
CREATE INDEX IF NOT EXISTS idx_participant_attempts_status ON participant_attempts (status);
CREATE INDEX IF NOT EXISTS idx_participant_attempts_source ON participant_attempts (source_attempt_id);
CREATE INDEX IF NOT EXISTS idx_participant_days_created_at ON participant_days (created_at);
CREATE INDEX IF NOT EXISTS idx_participant_days_attempt ON participant_days (attempt_id);
CREATE INDEX IF NOT EXISTS idx_pretest_responses_attempt ON pretest_responses (attempt_id);
CREATE INDEX IF NOT EXISTS idx_experiment_sessions_attempt ON experiment_sessions (attempt_id);
CREATE INDEX IF NOT EXISTS idx_clean_data_audits_attempt ON clean_data_audits (attempt_id);
