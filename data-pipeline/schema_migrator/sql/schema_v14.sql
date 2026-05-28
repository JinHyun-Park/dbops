-- v14: audit_log immutability.
--
-- audit_log records every executed write operation. Compliance auditors
-- and post-incident retros depend on its integrity — a row stamped with
-- "Alice approved DROP TABLE on prod" must stay that way forever.
-- Enforcing append-only at the DB level so even a privileged accidental
-- UPDATE or DELETE gets refused with a clear error.
--
-- Trigger approach (vs REVOKE on the app role): keeps the existing
-- migrator and seeder paths working — they can still INSERT and SELECT.
-- A future operations-side migration (e.g. legitimate archival) can
-- temporarily DROP this trigger, batch the change, and re-create it,
-- which is the right kind of "deliberate friction."
--
-- NOTE: the migrator splits on ";\n", so the function body is one line
-- with no internal newlines — keeps the splitter from chopping the
-- BEGIN/END inside $$.

CREATE OR REPLACE FUNCTION audit_log_block_mutations() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'audit_log is append-only — % refused. Drop trigger audit_log_immutable to override, but consider whether you actually want to silently rewrite history first.', TG_OP USING HINT = 'data-pipeline/schema_migrator/sql/schema_v14.sql'; END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log;

CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutations();

-- approvals isn't here too — that table lives in DynamoDB, and the
-- approval_guard module already enforces atomic consume-on-use via
-- ConditionExpression. DDB has no analog of a SQL trigger, but the
-- application code path is the only place that touches it, and that
-- code path refuses to overwrite a consumed row.
