"""
Schema migrator invoked by CDK Custom Resource on stack create/update.

Reads all schema_v*.sql files bundled into the Lambda asset and executes each
statement via the RDS Data API. Idempotent: every DDL uses IF NOT EXISTS / IF
EXISTS / ON CONFLICT, so reruns are safe.
"""

import os
import re
import boto3


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
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    rds_data = boto3.client("rds-data")

    sql_dir = os.path.join(os.path.dirname(__file__), "sql")
    schemas = sorted(
        f for f in os.listdir(sql_dir) if f.startswith("schema") and f.endswith(".sql")
    )

    results = []
    for fname in schemas:
        path = os.path.join(sql_dir, fname)
        with open(path) as fh:
            statements = _split_statements(fh.read())
        ok = 0
        skipped = 0
        errors = []
        idempotent_markers = (
            "already exists",
            "duplicate",
            "already defined",
            "duplicate_object",
            "duplicate_table",
            "duplicate_column",
        )
        for stmt in statements:
            try:
                _run_statement(rds_data, cluster_arn, secret_arn, database, stmt)
                ok += 1
            except Exception as e:
                # idempotent reruns hit harmless "already exists" / "duplicate"
                # conflicts; classify and move on so brand-new DDL still applies.
                msg = str(e).lower()
                if any(m in msg for m in idempotent_markers):
                    skipped += 1
                else:
                    errors.append(str(e)[:300])
        results.append({"file": fname, "ok": ok, "skipped": skipped, "errors": errors})
        print(f"[{fname}] ok={ok} skipped={skipped} errors={len(errors)}")

    failed = any(r["errors"] for r in results)
    return {
        "PhysicalResourceId": "dbops-schema-migrator",
        "Data": {
            "results": str(results),
            "status": "failed" if failed else "ok",
        },
    }
