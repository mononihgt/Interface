CREATE TABLE IF NOT EXISTS provider_cooldowns (
    route TEXT NOT NULL CHECK (route IN ('chat', 'evaluator', 'asr')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cooldown_until TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (route, provider, model)
);

CREATE INDEX IF NOT EXISTS idx_provider_cooldowns_until
ON provider_cooldowns (cooldown_until);
