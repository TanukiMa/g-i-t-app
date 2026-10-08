CREATE TABLE IF NOT EXISTS updates (
    id BIGSERIAL PRIMARY KEY,
    site_slug VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,
    commit_hash VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    summary_model TEXT,   -- who wrote the summary: a model name, 'rule' (no AI needed), NULL (unknown / older rows)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_updates_site_slug ON updates(site_slug);
CREATE INDEX IF NOT EXISTS idx_updates_created_at ON updates(created_at DESC);

-- URLs to register with the Internet Archive (drained by scripts/archive_worker.py).
CREATE TABLE IF NOT EXISTS archive_queue (
    id BIGSERIAL PRIMARY KEY,
    site_slug VARCHAR(255) NOT NULL,
    commit_hash VARCHAR(64) NOT NULL,
    url TEXT NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'attachment' CHECK (kind IN ('page', 'attachment')),
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    archive_url TEXT,
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    next_try_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (site_slug, commit_hash, url)
);

CREATE INDEX IF NOT EXISTS idx_archive_queue_pending ON archive_queue(status, next_try_at);
CREATE INDEX IF NOT EXISTS idx_archive_queue_commit ON archive_queue(site_slug, commit_hash);

-- Migration for an existing deployment:
-- ALTER TABLE updates DROP COLUMN IF EXISTS archive_url;
-- ALTER TABLE updates ADD COLUMN IF NOT EXISTS summary_model TEXT;   (sql/add-summary-model.sql)
