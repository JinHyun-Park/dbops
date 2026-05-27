-- v11: track alert acknowledgement state directly on alert_rules so the
-- Alerts page can render "acked X minutes ago by @user" inline without
-- joining against event_log. The columns are nullable — legacy rules and
-- rules that have never been acked just show no badge.
--
-- The ack flow originates from a Slack interactive button POSTed back to
-- /api/slack/interactive. The receiver Lambda verifies the Slack signing
-- secret, then sets these columns.

ALTER TABLE alert_rules
  ADD COLUMN IF NOT EXISTS last_acked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_acked_by TEXT;
