CREATE TABLE clean_data_audits_replacement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL,
    attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('eligible', 'review_needed', 'excluded')),
    reasons_json TEXT NOT NULL,
    reviewer_note TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (participant_id, attempt_id),
    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE
);

INSERT INTO clean_data_audits_replacement (
    id,
    participant_id,
    attempt_id,
    status,
    reasons_json,
    reviewer_note,
    reviewed_by,
    reviewed_at,
    computed_at
)
SELECT
    id,
    participant_id,
    attempt_id,
    status,
    reasons_json,
    reviewer_note,
    reviewed_by,
    reviewed_at,
    computed_at
FROM clean_data_audits;

DROP TABLE clean_data_audits;
ALTER TABLE clean_data_audits_replacement RENAME TO clean_data_audits;

CREATE INDEX IF NOT EXISTS idx_clean_data_audits_attempt ON clean_data_audits (attempt_id);
