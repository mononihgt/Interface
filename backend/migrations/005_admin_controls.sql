CREATE TABLE IF NOT EXISTS admin_assignment_cells (
    participant_type TEXT NOT NULL CHECK (participant_type IN ('short', 'long')),
    condition TEXT NOT NULL CHECK (condition IN ('human', 'tool')),
    subcondition TEXT NOT NULL CHECK (subcondition IN ('qa', 'planning', 'chat', 'decision', 'execution')),
    cap INTEGER CHECK (cap IS NULL OR cap >= 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (participant_type, condition, subcondition)
);

CREATE TABLE IF NOT EXISTS admin_global_controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
