-- v5: DBOps-managed alert subscribers for Slack / PagerDuty
--
-- The existing /api/alert-subscriptions flow uses SNS native subscribers
-- (email, sms, https). Slack incoming webhooks and PagerDuty events-v2 need
-- a structured JSON body — neither plays well with SNS's default HTTPS
-- envelope — so we keep them out of SNS and let the alert_evaluator POST
-- directly using a payload format that each platform expects.

CREATE TABLE IF NOT EXISTS alert_subscribers_managed (
    id BIGSERIAL PRIMARY KEY,
    protocol VARCHAR(40) NOT NULL,        -- 'slack-webhook' | 'pagerduty-events-v2'
    endpoint VARCHAR(2048) NOT NULL,      -- webhook URL or integration key
    label VARCHAR(255),                   -- optional human label ("#dba-alerts")
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_subscribers_managed_enabled
    ON alert_subscribers_managed (enabled);
