-- v25: alert rule snooze (P2-⑦). A rule with snooze_until in the future is
-- skipped by the evaluator without needing to be disabled; once the
-- timestamp passes it fires normally again with no manual un-snooze step.
-- NULL (the default) means "not snoozed" — existing rows are unaffected.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS snooze_until TIMESTAMPTZ;
