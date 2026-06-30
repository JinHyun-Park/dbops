-- data-pipeline/schema_migrator/sql/schema_v24.sql
-- Remediation Outcome Loop: cases + learned aggregate. See
-- docs/superpowers/specs/2026-06-30-remediation-outcome-loop-design.md

CREATE TABLE IF NOT EXISTS remediation_cases (
    case_id             BIGSERIAL PRIMARY KEY,
    cluster_id          VARCHAR(255) NOT NULL,
    opened_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    symptom_class       VARCHAR(80)  NOT NULL,
    symptom_subject     VARCHAR(255) NOT NULL DEFAULT '',
    watch_metric        VARCHAR(80),
    severity_at_open    VARCHAR(20),
    recommendation_text TEXT,
    action_class        VARCHAR(40)  NOT NULL DEFAULT 'manual',
    source              VARCHAR(40)  NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'open',
    evaluate_after      TIMESTAMPTZ  NOT NULL,
    evaluated_at        TIMESTAMPTZ,
    details             JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- At most one OPEN case per (cluster, symptom_class, subject); re-emission while
-- open only bumps last_seen_at (see case_opener ON CONFLICT).
CREATE UNIQUE INDEX IF NOT EXISTS ux_remediation_cases_open
    ON remediation_cases (cluster_id, symptom_class, symptom_subject)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS ix_remediation_cases_due
    ON remediation_cases (status, evaluate_after);

CREATE TABLE IF NOT EXISTS remediation_outcomes_agg (
    cluster_id      VARCHAR(255) NOT NULL,   -- '*' = fleet rollup (cold-start prior)
    symptom_class   VARCHAR(80)  NOT NULL,
    action_class    VARCHAR(40)  NOT NULL,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    successes       INTEGER      NOT NULL DEFAULT 0,
    last_outcome    VARCHAR(20),
    last_success_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, symptom_class, action_class)
);
