ALTER TABLE conversation_turns
ADD COLUMN client_message_sent_at TEXT;

ALTER TABLE conversation_turns
ADD COLUMN assistant_render_completed_at TEXT;

ALTER TABLE conversation_turns
ADD COLUMN client_response_latency_ms INTEGER
CHECK (client_response_latency_ms IS NULL OR client_response_latency_ms >= 0);

ALTER TABLE conversation_turns
ADD COLUMN client_timing_interrupted INTEGER
CHECK (client_timing_interrupted IS NULL OR client_timing_interrupted IN (0, 1));

ALTER TABLE conversation_turns
ADD COLUMN render_timing_received_at TEXT;
