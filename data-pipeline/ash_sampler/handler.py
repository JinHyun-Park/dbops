"""High-resolution active-session sampler (~5s "near-ASH").

The 5-min ETL misses transient active-session / wait spikes. This Lambda runs on
a 1-min schedule and internally loops, sampling each relational cluster's active
session count + dominant wait every ~5s (≈10 samples/run), writing them to the
active_session_samples table. It also prunes that table to a 7-day window each
run (the table has its own retention because 5s sampling is far higher volume
than metric_snapshots, which has no purge).

Same-account targets only for v1 (uses the registry's cluster_arn/secret_arn
directly via RDS Data API). Cross-account spoke-role sampling is a follow-up,
mirroring the ETL collector's assume-role path.

ponytail: self-looping Lambda (sleeps between samples) is the lazy fit for the
existing Lambda-based stack — it bills ~50s/min of mostly-idle wait, single-digit
$/mo for a small fleet. A continuously-connected Fargate task is cheaper only at
large fleet scale; revisit then.
"""

import os
import time

import boto3

INTERVAL_SEC = 5
SAMPLES_PER_RUN = 10
# Hard wall-clock budget per run — must stay below the 60s Lambda timeout AND the
# 1-min schedule so a run never gets killed mid-sample or overlaps the next. The
# loop stops early when the budget is spent, so a large fleet just takes fewer
# samples/run (graceful) instead of running over.
MAX_RUN_SEC = 50
RETENTION_DAYS = 7

# Active sessions + the single dominant wait, one fast query.
PG_SQL = """
WITH a AS (
  SELECT COALESCE(wait_event_type || ':' || wait_event, 'CPU') AS w
  FROM pg_stat_activity
  WHERE state = 'active' AND pid <> pg_backend_pid()
),
top AS (SELECT w, count(*) AS c FROM a GROUP BY w ORDER BY c DESC LIMIT 1)
SELECT (SELECT count(*) FROM a)        AS active,
       (SELECT w FROM top)             AS top_wait,
       (SELECT c FROM top)             AS top_wait_count
"""

MYSQL_SQL = """
WITH a AS (
  SELECT COALESCE(NULLIF(state, ''), 'executing') AS w
  FROM information_schema.processlist
  WHERE command <> 'Sleep' AND id <> connection_id()
),
top AS (SELECT w, count(*) AS c FROM a GROUP BY w ORDER BY c DESC LIMIT 1)
SELECT (SELECT count(*) FROM a)        AS active,
       (SELECT w FROM top)             AS top_wait,
       (SELECT c FROM top)             AS top_wait_count
"""

INSERT_SQL = (
    "INSERT INTO active_session_samples (cluster_id, ts, active_sessions, top_wait, top_wait_count) "
    "VALUES (:cid, NOW(), :active, :tw, :twc)"
)


def _scan_relational(table):
    """All registered Aurora MySQL/PostgreSQL clusters with target ARNs."""
    targets, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        for it in resp.get("Items", []):
            eng = str(it.get("engine", "")).lower()
            if ("postgresql" in eng or "mysql" in eng) and it.get("cluster_arn") and it.get("secret_arn"):
                targets.append({
                    "cluster_id": it["cluster_id"], "engine": eng,
                    "arn": it["cluster_arn"], "sec": it["secret_arn"],
                    "db": it.get("db_name", "sampledb"),
                })
        if "LastEvaluatedKey" not in resp:
            return targets
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _first_row(rds, arn, sec, db, sql):
    resp = rds.execute_statement(
        resourceArn=arn, secretArn=sec, database=db,
        sql=f"/* source=dbops-ash */ {sql}", includeResultMetadata=True,
    )
    recs = resp.get("records", [])
    if not recs:
        return None
    cols = [c.get("name", "") for c in resp.get("columnMetadata", [])]
    row = {}
    for i, f in enumerate(recs[0]):
        col = cols[i] if i < len(cols) else f"c{i}"
        row[col] = None if f.get("isNull") else next(
            (f[t] for t in ("longValue", "stringValue", "doubleValue") if t in f), None)
    return row


def lambda_handler(event, context):
    rds = boto3.client("rds-data")
    cache_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    cache_sec = os.environ["CACHE_DB_SECRET_ARN"]
    cache_db = os.environ.get("CACHE_DB_NAME", "dbops")
    table = boto3.resource("dynamodb").Table(os.environ["CLUSTERS_TABLE"])

    targets = _scan_relational(table)
    inserted = 0
    deadline = time.monotonic() + MAX_RUN_SEC
    for i in range(SAMPLES_PER_RUN):
        if time.monotonic() >= deadline:
            break
        for t in targets:
            try:
                sql = PG_SQL if "postgresql" in t["engine"] else MYSQL_SQL
                row = _first_row(rds, t["arn"], t["sec"], t["db"], sql)
                if row is None:
                    continue
                rds.execute_statement(
                    resourceArn=cache_arn, secretArn=cache_sec, database=cache_db,
                    sql=f"/* source=dbops-ash */ {INSERT_SQL}",
                    parameters=[
                        {"name": "cid", "value": {"stringValue": t["cluster_id"]}},
                        {"name": "active", "value": {"longValue": int(row.get("active") or 0)}},
                        {"name": "tw", "value": ({"stringValue": str(row["top_wait"])} if row.get("top_wait") else {"isNull": True})},
                        {"name": "twc", "value": {"longValue": int(row.get("top_wait_count") or 0)}},
                    ],
                )
                inserted += 1
            except Exception as e:
                print(f"[ash] sample failed for {t['cluster_id']}: {type(e).__name__}: {e}")
        # Sleep only if another full iteration still fits within the budget.
        if i < SAMPLES_PER_RUN - 1 and time.monotonic() + INTERVAL_SEC < deadline:
            time.sleep(INTERVAL_SEC)
        else:
            break

    # Retention: keep ~7d of high-res samples.
    try:
        rds.execute_statement(
            resourceArn=cache_arn, secretArn=cache_sec, database=cache_db,
            sql=f"/* source=dbops-ash */ DELETE FROM active_session_samples "
                f"WHERE ts < NOW() - INTERVAL '{RETENTION_DAYS} days'",
        )
    except Exception as e:
        print(f"[ash] prune failed: {type(e).__name__}: {e}")

    return {"targets": len(targets), "samples_inserted": inserted}
