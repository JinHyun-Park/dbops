"""Agent Tasks REST API — list / get / create.

Backs the /tasks UI and the manual "run agent task" button. Reads the
agent-tasks DynamoDB table populated by alert_evaluator (auto-RCA), the
scheduler, and this handler's own POST (manual runs). Creating a task just
writes a ``pending`` row — the table's stream drives the task_worker that
actually executes it.

See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.
"""

import base64
import json
import os
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TTL_DAYS = 30
KINDS_MANUAL = {"manual_rca"}  # kinds a user may trigger by hand (read-only RCA)


def _table():
    return boto3.resource("dynamodb").Table(os.environ["AGENT_TASKS_TABLE"])


def _clusters_table():
    name = os.environ.get("CLUSTERS_TABLE", "")
    return boto3.resource("dynamodb").Table(name) if name else None


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _caller_name(event: dict) -> str:
    hdrs = event.get("headers") or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return "unknown"
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return (
        claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("email")
        or "unknown"
    )


def _cluster_exists(cluster_id: str) -> bool:
    t = _clusters_table()
    if t is None:
        return True  # registry not wired on this deployment — don't block
    try:
        return "Item" in t.get_item(Key={"cluster_id": cluster_id})
    except Exception:
        return True  # fail-open on a registry read error — POST isn't destructive


def _recent_for_stats(limit=500):
    resp = _table().query(
        IndexName="recency-index",
        KeyConditionExpression=Key("record_type").eq("task"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def _stats():
    rows = _recent_for_stats()
    by_status, by_kind, durs = {}, {}, []
    for r in rows:
        st = str(r.get("status", "unknown"))
        by_status[st] = by_status.get(st, 0) + 1
        kd = str(r.get("kind", "unknown"))
        by_kind[kd] = by_kind.get(kd, 0) + 1
        if st == "done" and r.get("duration_ms") is not None:
            try:
                durs.append(int(r["duration_ms"]))
            except (TypeError, ValueError):
                pass
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    finished = done + failed
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_kind": by_kind,
        "success_rate": round(done / finished, 4) if finished else 0,
        "avg_duration_ms": int(sum(durs) / len(durs)) if durs else 0,
        "recent_failures": failed,
    }


def _list(qsp: dict) -> list:
    cluster = (qsp.get("cluster") or "").strip()
    status = (qsp.get("status") or "").strip()
    try:
        limit = min(200, max(1, int(qsp.get("limit", "50"))))
    except (TypeError, ValueError):
        limit = 50

    table = _table()
    kwargs = {"ScanIndexForward": False, "Limit": limit}
    if status:
        # A Limit applies before the FilterExpression, so over-fetch then trim
        # to avoid returning fewer than `limit` matches when filtering.
        kwargs["Limit"] = limit * 4
        kwargs["FilterExpression"] = Attr("status").eq(status)

    if cluster:
        kwargs["IndexName"] = "cluster-created-index"
        kwargs["KeyConditionExpression"] = Key("cluster_id").eq(cluster)
    else:
        kwargs["IndexName"] = "recency-index"
        kwargs["KeyConditionExpression"] = Key("record_type").eq("task")

    # conditions.Attr("status") emits a reserved-word-safe expression via the
    # SDK (auto #status alias), so the FilterExpression is safe despite "status"
    # being a DynamoDB reserved word.
    resp = table.query(**kwargs)
    items = resp.get("Items", [])
    return items[:limit]


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path_params = event.get("pathParameters") or {}
    qsp = event.get("queryStringParameters") or {}
    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    task_id = path_params.get("id")
    raw_path = event.get("rawPath") or event.get("path") or ""

    if method == "GET" and raw_path.endswith("/stats"):
        try:
            return {"statusCode": 200, "headers": headers, "body": json.dumps(_stats(), default=str)}
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}

    if method == "GET" and task_id:
        try:
            item = _table().get_item(Key={"task_id": task_id}).get("Item")
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
        return {
            "statusCode": 200 if item else 404,
            "headers": headers,
            "body": json.dumps(item or {"error": "not found"}, default=str),
        }

    if method == "GET":
        try:
            items = _list(qsp)
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
        return {"statusCode": 200, "headers": headers, "body": json.dumps({"tasks": items}, default=str)}

    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "invalid JSON body"})}
        cluster_id = (body.get("cluster_id") or "").strip()
        kind = (body.get("kind") or "manual_rca").strip()
        if not cluster_id:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "cluster_id required"})}
        if kind not in KINDS_MANUAL:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"kind must be one of {sorted(KINDS_MANUAL)}"})}
        if not _cluster_exists(cluster_id):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"unknown cluster {cluster_id}"})}

        now_ms = int(time.time() * 1000)
        new_task_id = str(uuid.uuid4())
        item = {
            "task_id": new_task_id,
            "record_type": "task",
            "cluster_id": cluster_id,
            "kind": kind,
            "trigger": f"manual:{_caller_name(event)}",
            "status": "pending",
            "created_at": str(now_ms),
            "title": f"수동 RCA · {cluster_id}",
            "ttl": int(time.time()) + TTL_DAYS * 24 * 60 * 60,
        }
        try:
            _table().put_item(Item=item)
        except ClientError as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
        return {"statusCode": 201, "headers": headers, "body": json.dumps(item, default=str)}

    return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "Method not allowed"})}
