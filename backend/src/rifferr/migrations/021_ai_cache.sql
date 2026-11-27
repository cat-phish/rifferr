-- C-AI-CORE: cached AI answers (AI-CORE-16, -17). ``key`` is the SHA-256 of everything that
-- decides the answer (task, prompt version, provider type, model, temperature, the text
-- sent); ``answer`` is the schema-validated answer as JSON. Per profile. Failed attempts
-- are never stored.
CREATE TABLE ai_cache (
    profile        TEXT NOT NULL,
    key            TEXT NOT NULL,
    task           TEXT NOT NULL,
    provider_type  TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    answer         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT NOT NULL,
    PRIMARY KEY (profile, key)
);
CREATE INDEX ai_cache_lru ON ai_cache (profile, last_used_at);
