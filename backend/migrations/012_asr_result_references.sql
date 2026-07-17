UPDATE asr_attempts
SET result_ref = lower(hex(randomblob(32)))
WHERE result_ref IS NULL;

CREATE UNIQUE INDEX idx_asr_attempts_result_ref
ON asr_attempts (result_ref);
