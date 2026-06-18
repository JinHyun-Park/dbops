"""task_worker — executes agent-tasks rows as they are inserted.

Triggered by the ``agent-tasks`` DynamoDB stream. For each INSERTed row with
``status=pending`` it atomically claims the row (pending -> running), runs the
generator for the row's ``kind``, and writes the result back (done / failed)
plus a best-effort in-app WebSocket push.

This is the SINGLE processing path for all task sources — alert auto-RCA,
scheduled reports, and manual runs each just write a pending row; this worker is
the only thing that executes them. See
docs/superpowers/specs/2026-06-18-agent-tasks-design.md.

RCA is deterministic: it reuses the incident server's ``diagnose_root_cause``
tool (the same one the agent calls), so no LLM / model invocation happens here —
fast, cheap, and safe to run unattended in a Lambda.
"""

import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl
from mcp_servers.shared.cache_client import CacheClient

_DESER = TypeDeserializer()

# Lazily built so a cold container that only handles a malformed record never
# pays the cache-client init.
_cache = None


def _get_cache() -> CacheClient:
    global _cache
    if _cache is None:
        _cache = CacheClient()
    return _cache


def _table():
    return boto3.resource("dynamodb").Table(os.environ["AGENT_TASKS_TABLE"])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ddb_safe(value):
    """boto3's DynamoDB resource rejects Python floats ("Float types are not
    supported"). diagnose_root_cause returns scores/ratios as floats, so convert
    them to Decimal recursively before persisting the result. Mirrors
    request_approval._ddb_safe."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb_safe(v) for v in value]
    return value


def _deser_image(image: dict) -> dict:
    """DynamoDB stream NewImage is low-level ({"k": {"S": "v"}}); deserialize."""
    return {k: _DESER.deserialize(v) for k, v in (image or {}).items()}


def _broadcast(payload: dict) -> int:
    """Push `payload` to all connected WS clients. Best-effort, never raises.

    Copied from data-pipeline/.../ws_notify.broadcast (kept in sync) — the
    broadcasting Lambdas each carry their own copy rather than share a layer.
    """
    table_name = os.environ.get("WS_CONNECTIONS_TABLE")
    endpoint = os.environ.get("WS_MGMT_ENDPOINT")
    if not table_name or not endpoint:
        return 0  # push channel not configured on this deployment
    ddb = boto3.resource("dynamodb").Table(table_name)
    mgmt = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
    data = json.dumps(payload, default=str).encode("utf-8")

    items = []
    scan_kwargs = {"ProjectionExpression": "connection_id"}
    try:
        while True:
            resp = ddb.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        print(f"[task-worker] connections scan failed: {type(e).__name__}: {e}")
        return 0

    delivered = 0
    for it in items:
        cid = it.get("connection_id")
        if not cid:
            continue
        try:
            mgmt.post_to_connection(ConnectionId=cid, Data=data)
            delivered += 1
        except mgmt.exceptions.GoneException:
            try:
                ddb.delete_item(Key={"connection_id": cid})
            except Exception:
                pass
        except Exception as e:
            print(f"[task-worker] post_to_connection {cid[:8]} failed: {type(e).__name__}")
    return delivered


def _claim(task_id: str) -> bool:
    """Atomically move pending -> running. Returns True iff we won the claim.

    A stream record can be redelivered (shard retry, at-least-once) and the same
    INSERT can surface on a re-drive — the conditional write makes execution
    idempotent: only the first claimer runs the work."""
    try:
        _table().update_item(
            Key={"task_id": task_id},
            UpdateExpression="SET #s = :running, started_at = :ts",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":pending": "pending",
                ":ts": str(_now_ms()),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _finish(task_id, *, status, result=None, summary=None, error=None):
    names = {"#s": "status"}
    vals = {":s": status, ":ts": str(_now_ms())}
    sets = ["#s = :s", "completed_at = :ts"]
    if result is not None:
        vals[":r"] = _ddb_safe(result)
        sets.append("#r = :r")
        names["#r"] = "result"
    if summary is not None:
        vals[":sum"] = summary
        sets.append("summary = :sum")
    if error is not None:
        vals[":e"] = error[:500]
        sets.append("#err = :e")
        names["#err"] = "error"
    _table().update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


def _run_rca(cluster_id: str):
    """Deterministic RCA via the incident diagnose_root_cause tool.

    Returns (result_dict, one_line_summary). The summary is the top-ranked
    candidate's own summary line, so the toast / list reads meaningfully without
    the DBA opening the full result."""
    res = diagnose_root_cause_impl(_get_cache(), cluster_id)
    candidates = res.get("candidates", []) if isinstance(res, dict) else []
    if candidates:
        top = candidates[0]
        summary = top.get("summary") or top.get("category") or "신호 감지"
    else:
        summary = "자동 수집 신호에서 뚜렷한 원인 미발견 — 수동 점검 권장"
    return res, summary


def lambda_handler(event, context):
    processed = 0
    skipped = 0
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        img = _deser_image(rec.get("dynamodb", {}).get("NewImage", {}))
        task_id = img.get("task_id")
        kind = img.get("kind")
        cluster_id = img.get("cluster_id")
        status = img.get("status")
        if not task_id or status != "pending":
            skipped += 1
            continue
        if not _claim(task_id):
            skipped += 1  # someone else is handling it
            continue

        try:
            if kind in ("auto_rca", "manual_rca"):
                result, summary = _run_rca(cluster_id)
            else:
                # scheduled_report and any future kinds land here until their
                # generator ships — fail loudly so it shows in the task list
                # instead of hanging at "running" forever.
                raise NotImplementedError(f"task kind {kind!r} not yet supported by the worker")

            _finish(task_id, status="done", result=result, summary=summary)
            _broadcast({
                "type": "task",
                "task_kind": "rca_ready",
                "task_id": task_id,
                "cluster_id": cluster_id,
                "severity": "warning",
                "title": f"RCA 준비됨 · {cluster_id}: {summary}",
            })
            processed += 1
        except Exception as e:
            print(f"[task-worker] task {task_id} ({kind}) failed: {type(e).__name__}: {e}")
            try:
                _finish(
                    task_id,
                    status="failed",
                    summary=f"작업 실패: {type(e).__name__}",
                    error=str(e),
                )
            except Exception as e2:
                print(f"[task-worker] could not mark {task_id} failed: {type(e2).__name__}")

    return {"processed": processed, "skipped": skipped}
