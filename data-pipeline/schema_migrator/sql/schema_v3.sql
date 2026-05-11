-- Phase 3 additional tables

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    tool_name VARCHAR(100),
    requested_by VARCHAR(255),
    approved_by VARCHAR(255),
    sql_text TEXT,
    parameters JSONB,
    result TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX idx_audit_log_cluster ON audit_log (cluster_id, created_at);
CREATE INDEX idx_audit_log_status ON audit_log (status, created_at);
