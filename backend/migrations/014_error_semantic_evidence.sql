ALTER TABLE experiment_sessions
ADD COLUMN manipulation_status TEXT NOT NULL DEFAULT 'unknown'
CHECK (manipulation_status IN ('unknown', 'pending', 'presented', 'failed'));

ALTER TABLE conversation_turns
ADD COLUMN error_mutation_json TEXT;

ALTER TABLE conversation_turns
ADD COLUMN error_semantic_attempt_count INTEGER NOT NULL DEFAULT 0
CHECK (error_semantic_attempt_count BETWEEN 0 AND 5);

ALTER TABLE conversation_turns
ADD COLUMN error_failure_reason TEXT;

ALTER TABLE conversation_turns
ADD COLUMN error_attempts_json TEXT;
