CREATE TABLE IF NOT EXISTS recruitment_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL CHECK (status IN ('closed', 'open')),
    updated_by TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO recruitment_control (id, status) VALUES (1, 'closed');

CREATE TABLE admin_events_replacement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('login', 'update_assignment_cap', 'block_participant', 'export_data', 'test_agent', 'set_recruitment')),
    target_type TEXT,
    target_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO admin_events_replacement (
    id,
    admin_user,
    action,
    target_type,
    target_id,
    payload_json,
    created_at
)
SELECT
    id,
    admin_user,
    action,
    target_type,
    target_id,
    payload_json,
    created_at
FROM admin_events;

DROP TABLE admin_events;
ALTER TABLE admin_events_replacement RENAME TO admin_events;

CREATE INDEX idx_admin_events_created_at ON admin_events (created_at);
