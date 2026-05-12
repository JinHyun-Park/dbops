-- v7: Installed extension catalog per cluster.
--
-- pg_extension is small (a few dozen rows max) but we cache it so the
-- Extensions card on the dashboard doesn't need to hit the live cluster on
-- every render. ETL updates this on each cycle; entries older than the
-- snapshot window mean the extension was dropped.

CREATE TABLE IF NOT EXISTS cluster_extensions (
    cluster_id VARCHAR(255) NOT NULL,
    extname VARCHAR(255) NOT NULL,
    extversion VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, extname)
);

CREATE INDEX IF NOT EXISTS idx_cluster_extensions_cluster
    ON cluster_extensions (cluster_id);
