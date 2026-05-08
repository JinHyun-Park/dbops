-- Phase 2 additional tables

CREATE TABLE IF NOT EXISTS event_log (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    source VARCHAR(50) NOT NULL,
    message TEXT,
    severity VARCHAR(20) DEFAULT 'info',
    raw_event JSONB
);

CREATE INDEX idx_event_log_lookup ON event_log (cluster_id, event_time);
CREATE INDEX idx_event_log_type ON event_log (event_type, event_time);

CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255),
    report_type VARCHAR(50) NOT NULL,
    report_date DATE NOT NULL,
    summary TEXT,
    data JSONB,
    s3_key TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_reports_lookup ON reports (cluster_id, report_type, report_date);
