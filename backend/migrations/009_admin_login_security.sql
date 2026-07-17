CREATE TABLE admin_credentials (
    admin_user TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE admin_login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_token TEXT NOT NULL UNIQUE,
    username_key TEXT NOT NULL CHECK (length(username_key) = 64),
    client_address TEXT NOT NULL CHECK (length(client_address) BETWEEN 1 AND 255),
    state TEXT NOT NULL CHECK (state IN ('pending', 'failed')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_admin_login_attempts_username_expiry
ON admin_login_attempts (username_key, expires_at);

CREATE INDEX idx_admin_login_attempts_address_expiry
ON admin_login_attempts (client_address, expires_at);

CREATE INDEX idx_admin_login_attempts_expiry
ON admin_login_attempts (expires_at);
