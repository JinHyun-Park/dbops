"""
Schema migrator invoked by CDK Custom Resource on stack create/update.

Reads all schema_v*.sql files bundled into the Lambda asset and executes each
statement via the RDS Data API. Idempotent: every DDL uses IF NOT EXISTS / IF
EXISTS / ON CONFLICT, so reruns are safe.
"""

import os
import re
import time

import boto3

# Data API errors that mean "try again", not "this DDL is wrong". A cache writer
# scaled to its 0.5-ACU floor (or auto-paused) answers the first statement with
# DatabaseResumingException, and a 25-file migration can trip request throttling.
# Neither is a schema defect, so neither may roll back a legitimate deploy.
_TRANSIENT_MARKERS = (
    "databaseresuming",
    "resuming after being auto-paused",
    "communications link failure",
    "throttling",
    "rate exceeded",
    "too many requests",
    "serviceunavailable",
    "service unavailable",
    "internalservererror",
    "internal server error",
)
# ponytail: fixed backoff, ~67s of patience, which covers a Serverless v2 resume
# well inside the 5-minute Lambda timeout. Switch to jittered exponential only if
# a real deploy is seen exhausting it.
_RETRY_SLEEPS = (2, 5, 10, 20, 30)


def _is_transient(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _split_statements(sql_text: str):
    # Strip line comments, then split on `;` not inside quotes. Aurora Data API
    # only accepts one statement per call.
    cleaned = "\n".join(
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    )
    raw = [s.strip() for s in re.split(r";\s*\n|;\s*$", cleaned) if s.strip()]
    return raw


def _run_statement(rds_data, cluster_arn, secret_arn, database, sql):
    rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=f"/* source=dbops-schema-migrator */ {sql}",
    )


def lambda_handler(event, context):
    request_type = (event or {}).get("RequestType", "Create")
    print(f"SchemaMigrator invoked: {request_type}")
    if request_type == "Delete":
        # Nothing to migrate on delete: the cache DB goes away with the stack.
        # Without this the fail-hard raise below fires on `cdk destroy` too and
        # parks the stack in DELETE_FAILED. Same shape as the sibling
        # inference_profile_setup custom resource.
        return {"PhysicalResourceId": "dbops-schema-migrator", "Data": {"status": "skipped"}}

    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    rds_data = boto3.client("rds-data")

    sql_dir = os.path.join(os.path.dirname(__file__), "sql")

    # Numeric-aware sort: schema.sql, schema_v2.sql, schema_v3.sql, ...,
    # schema_v10.sql. Lexical sort would put v10 before v2 and break any
    # fresh install where v10 depends on tables created in earlier versions.
    def _ver_key(fname: str) -> tuple[int, int]:
        m = re.match(r"^schema(?:_v(\d+))?\.sql$", fname)
        if not m:
            return (1, 0)  # unknown → last
        return (0, int(m.group(1) or 0))

    schemas = sorted(
        (f for f in os.listdir(sql_dir) if f.startswith("schema") and f.endswith(".sql")),
        key=_ver_key,
    )

    results = []
    for fname in schemas:
        path = os.path.join(sql_dir, fname)
        with open(path) as fh:
            statements = _split_statements(fh.read())
        ok = 0
        skipped = 0
        errors = 0
        transient = 0
        idempotent_markers = (
            "already exists",
            "duplicate",
            "already defined",
            "duplicate_object",
            "duplicate_table",
            "duplicate_column",
        )
        for stmt in statements:
            for attempt in range(len(_RETRY_SLEEPS) + 1):
                try:
                    _run_statement(rds_data, cluster_arn, secret_arn, database, stmt)
                    ok += 1
                    break
                except Exception as e:
                    # idempotent reruns hit harmless "already exists" / "duplicate"
                    # conflicts; classify and move on so brand-new DDL still applies.
                    msg = str(e).lower()
                    if any(m in msg for m in idempotent_markers):
                        skipped += 1
                        break
                    if _is_transient(e) and attempt < len(_RETRY_SLEEPS):
                        print(f"[{fname}] transient, retrying in {_RETRY_SLEEPS[attempt]}s: {e}")
                        time.sleep(_RETRY_SLEEPS[attempt])
                        continue
                    # Full text goes to the log group ONLY. The raise below is a
                    # CloudFormation Reason and gets a static message.
                    print(f"[{fname}] statement failed: {e}")
                    if _is_transient(e):
                        transient += 1
                    else:
                        errors += 1
                    break
        results.append(
            {"file": fname, "ok": ok, "skipped": skipped, "errors": errors, "transient": transient}
        )
        print(f"[{fname}] ok={ok} skipped={skipped} errors={errors} transient={transient}")

    # RAISE. This runs as a CDK Provider on_event, and a Custom Resource that
    # returns without raising is a CloudFormation SUCCESS no matter what it puts in
    # Data. Returning Data.status="failed" therefore reported a migration failure as
    # a successful deploy: the cache DB ends up missing tables while every stack
    # shows CREATE/UPDATE_COMPLETE, and the first symptom is a REST route or
    # collector failing much later on a table that was never created. Idempotent
    # "already exists" conflicts are `skipped`, so only genuine DDL failures and
    # exhausted-retry transients get here, and they raise separate messages so a
    # throttled/resuming cache DB is not read as broken schema.
    # The message carries file names + counts only: a Custom Resource failure Reason
    # surfaces in stack events and `cdk deploy` output, so no exception text (which
    # can quote the secret ARN or row data) may go into it.
    _LOG_POINTER = (
        "Full SQL error text is in the schema_migrator Lambda log group "
        "(/aws/lambda/*SchemaMigrator*)."
    )

    def _detail(rows, key):
        # CloudFormation truncates the failure Reason (~1KB), and a cold DB fails
        # all 25 files at once. List a few and let the log group carry the rest.
        head = ", ".join(f"{r['file']} ({r[key]} statement(s))" for r in rows[:5])
        return head + (f", and {len(rows) - 5} more file(s)" if len(rows) > 5 else "")

    failed = [r for r in results if r["errors"]]
    if failed:
        raise RuntimeError(
            f"schema migration failed for {len(failed)} file(s): "
            f"{_detail(failed, 'errors')}. {_LOG_POINTER}"
        )
    stalled = [r for r in results if r["transient"]]
    if stalled:
        detail = _detail(stalled, "transient")
        raise RuntimeError(
            "schema migration could not reach the cache DB: transient Data API errors "
            f"(throttling / serverless resume) survived {len(_RETRY_SLEEPS)} retries for "
            f"{len(stalled)} file(s): {detail}. The schema is NOT a suspect, re-run "
            f"`cdk deploy` once the cache DB is warm. {_LOG_POINTER}"
        )
    return {
        "PhysicalResourceId": "dbops-schema-migrator",
        "Data": {
            "results": str(results),
            "status": "ok",
        },
    }
