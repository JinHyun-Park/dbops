"""task_scheduler — enqueue due recurring agent tasks.

Runs on an EventBridge schedule. Reads enabled rows from the `scheduled_tasks`
cache table, lets PostgreSQL decide which are due (NOW() - last_run_at vs the
row's interval_kind), and for each due row writes a pending row into the
agent-tasks DynamoDB table — the same single processing path the task_worker
drains — then stamps last_run_at so it won't re-fire until the next interval.

The scheduler only touches public endpoints (RDS Data API + DynamoDB), so it
lives in the data stack and never references the agent stack: the agent-tasks
table (foundation) is the decoupling point, identical to alert_evaluator.

See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.
"""

import os
import time
import uuid

import boto3

TTL_DAYS = 30

# Each enabled row is "due" when it has never run, or last ran longer ago than
# its cadence. PostgreSQL evaluates this so the scheduler stays a thin loop.
DUE_SQL = """
SELECT id, cluster_id, kind
FROM scheduled_tasks
WHERE enabled = TRUE AND (
    last_run_at IS NULL
    OR (interval_kind = 'hourly' AND last_run_at < NOW() - INTERVAL '1 hour')
    OR (interval_kind = 'daily'  AND last_run_at < NOW() - INTERVAL '1 day')
    OR (interval_kind = 'weekly' AND last_run_at < NOW() - INTERVAL '7 days')
)
"""


def _query(rds_data, cluster_arn, secret_arn, database, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=f"/* source=dbops-task-scheduler */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
            else:
                row[col] = None
        rows.append(row)
    return rows


def _enqueue(table, cluster_id, kind, schedule_id):
    now_ms = int(time.time() * 1000)
    task_id = str(uuid.uuid4())
    table.put_item(
        Item={
            "task_id": task_id,
            "record_type": "task",
            "cluster_id": cluster_id,
            "kind": kind or "scheduled_report",
            "trigger": f"schedule:{schedule_id}",
            "status": "pending",
            "created_at": str(now_ms),
            "title": f"예약 작업 · {cluster_id}",
            "ttl": int(time.time()) + TTL_DAYS * 24 * 60 * 60,
        }
    )
    return task_id


def lambda_handler(event, context):
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    tasks_table_name = os.environ.get("AGENT_TASKS_TABLE", "")

    rds_data = boto3.client("rds-data")

    def q(sql, params=None):
        return _query(rds_data, cluster_arn, secret_arn, database, sql, params)

    try:
        due = q(DUE_SQL)
    except Exception as e:
        print(f"[task-scheduler] due query failed: {type(e).__name__}: {e}")
        return {"enqueued": 0, "error": str(e)[:200]}

    if not tasks_table_name:
        return {"enqueued": 0, "due": len(due), "note": "AGENT_TASKS_TABLE unset"}

    table = boto3.resource("dynamodb").Table(tasks_table_name)
    enqueued = 0
    for row in due:
        sid = row.get("id")
        cluster_id = row.get("cluster_id")
        if not cluster_id:
            continue
        try:
            _enqueue(table, cluster_id, row.get("kind"), sid)
            # Stamp last_run_at so it won't re-fire until the next interval.
            q(
                "UPDATE scheduled_tasks SET last_run_at = NOW(), updated_at = NOW() WHERE id = :id",
                {"id": int(sid)},
            )
            enqueued += 1
        except Exception as e:
            print(f"[task-scheduler] schedule {sid} enqueue failed: {type(e).__name__}: {e}")

    print(f"[task-scheduler] due={len(due)} enqueued={enqueued}")
    return {"due": len(due), "enqueued": enqueued}
