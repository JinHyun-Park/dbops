"""Backup write operations — manual snapshot creation (phase 2).

The read tier (snapshot inventory + PITR window) lives in the dashboard
handler at GET /api/dashboard/{cluster_id}/backups. This handler owns
the *write* tier: creating a manual snapshot on demand.

Authority model
---------------
This is a HUMAN-initiated write, distinct from the agent-proposed write
path (execute_sql / modify_parameter / ...) which routes through the
DDB approval_guard. The DBA clicking "Create snapshot" in the console
IS the trusted authority the Approval Center exists to represent, so we
gate on the Cognito admin role directly — the same pattern used by
cluster registration and alert-rule creation. No second approval round
is required for a human admin's own click.

Why this is safe to expose directly: CreateDBClusterSnapshot is
NON-DESTRUCTIVE — it only adds a backup, never mutates or deletes data.
It is the safest possible write in the backup workflow. (Restore, the
phase-3 destructive-adjacent operation, gets a stronger gate.)

Every snapshot creation is recorded to audit_log + event_log so it
surfaces on /activity and /timeline.

Route: POST /api/dashboard/{cluster_id}/snapshot
Body:  { "snapshot_id": "optional-custom-id" }
"""

from __future__ import annotations

import base64
import json
import os
import re
import time

import boto3

# RDS snapshot identifier rules: 1-63 chars, must start with a letter,
# alphanumeric + hyphens, no consecutive hyphens, no trailing hyphen.
_SNAPSHOT_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")
_CLUSTER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,254}$")


def _resp(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _caller(event: dict) -> tuple[bool, str]:
    """Return (is_admin, username). Mirrors clusters/_is_admin: a token
    not explicitly in dbops-viewer counts as admin (one-admin deploys).
    Anonymous (no bearer) is NOT admin."""
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False, "anonymous"
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    name = (
        claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("email")
        or "unknown"
    )
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False, name
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False, name
    return True, name


def _audit(cluster_id: str, username: str, snapshot_id: str, status: str, result: str = ""):
    """Best-effort write to audit_log (PG) + event_log (PG) via RDS Data
    API. Failures here don't fail the snapshot — the snapshot already
    happened; the audit trail is secondary. Logged so /activity and
    /timeline pick it up."""
    cluster_arn = os.environ.get("CACHE_DB_CLUSTER_ARN", "")
    secret_arn = os.environ.get("CACHE_DB_SECRET_ARN", "")
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    if not cluster_arn or not secret_arn:
        return
    rds_data = boto3.client("rds-data")

    def _exec(sql, params):
        sql_params = [
            {"name": k, "value": ({"isNull": True} if v is None else {"stringValue": str(v)})}
            for k, v in params.items()
        ]
        try:
            rds_data.execute_statement(
                resourceArn=cluster_arn,
                secretArn=secret_arn,
                database=database,
                sql=f"/* source=dbops-backups-api */ {sql}",
                parameters=sql_params,
            )
        except Exception as e:
            print(f"[backups] audit write failed: {e}")

    _exec(
        "INSERT INTO audit_log (cluster_id, action_type, tool_name, "
        "requested_by, approved_by, parameters, result, status, resolved_at) "
        "VALUES (:cid, 'create_snapshot', 'create_snapshot', :who, :who, "
        ":params::jsonb, :result, :status, NOW())",
        {
            "cid": cluster_id,
            "who": username,
            "params": json.dumps({"snapshot_id": snapshot_id}),
            "result": result[:500],
            "status": status,
        },
    )
    # event_log → timeline. Use a 'backup' event_type so it gets the
    # rds_event chip family on the timeline.
    severity = "info" if status == "executed" else "warning"
    _exec(
        "INSERT INTO event_log (cluster_id, event_time, event_type, source, "
        "severity, message, raw_event) "
        "VALUES (:cid, NOW(), 'backup', 'dbops-backups-api', :sev, :msg, :raw::jsonb)",
        {
            "cid": cluster_id,
            "sev": severity,
            "msg": f"Manual snapshot {snapshot_id} {status} by {username}",
            "raw": json.dumps({"snapshot_id": snapshot_id, "status": status, "by": username}),
        },
    )


def _make_snapshot_id(cluster_id: str) -> str:
    """Auto-generate a valid manual snapshot id when the caller didn't
    supply one: manual-<cluster-tail>-<epoch>. Sanitised to satisfy the
    RDS identifier rules (letter-start, no double/trailing hyphen)."""
    tail = re.sub(r"[^a-zA-Z0-9]+", "-", cluster_id).strip("-")
    # Keep the last segment short so the whole id stays under 63 chars.
    tail = tail[-30:].strip("-") or "cluster"
    ts = int(time.time())
    sid = f"manual-{tail}-{ts}"
    # Collapse any accidental double hyphens.
    sid = re.sub(r"-{2,}", "-", sid).strip("-")
    return sid[:63].rstrip("-")


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    )
    if method == "OPTIONS":
        return _resp(200, {"ok": True})
    if method != "POST":
        return _resp(405, {"error": f"method {method} not allowed"})

    path_params = event.get("pathParameters") or {}
    cluster_id = path_params.get("cluster_id") or ""
    if not cluster_id or not _CLUSTER_ID_RE.match(cluster_id):
        return _resp(400, {"error": "invalid cluster_id"})

    is_admin, username = _caller(event)
    if not is_admin:
        return _resp(403, {"error": "forbidden", "reason": "admin role required to create snapshots"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "body must be valid JSON"})

    snapshot_id = (body.get("snapshot_id") or "").strip()
    if snapshot_id:
        if not _SNAPSHOT_ID_RE.match(snapshot_id) or len(snapshot_id) > 63:
            return _resp(400, {
                "error": (
                    "invalid snapshot_id — must be 1-63 chars, start with a "
                    "letter, alphanumeric + single hyphens (no leading/trailing "
                    "or consecutive hyphens)"
                ),
            })
    else:
        snapshot_id = _make_snapshot_id(cluster_id)

    rds = boto3.client("rds")
    try:
        resp = rds.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snapshot_id,
            DBClusterIdentifier=cluster_id,
            Tags=[
                {"Key": "dbops:created-by", "Value": username},
                {"Key": "dbops:type", "Value": "manual"},
            ],
        )
    except rds.exceptions.DBClusterSnapshotAlreadyExistsFault:
        return _resp(409, {"error": f"snapshot id '{snapshot_id}' already exists"})
    except Exception as e:
        _audit(cluster_id, username, snapshot_id, "failed", str(e)[:300])
        return _resp(502, {"error": "create_snapshot failed", "message": str(e)[:300]})

    snap = resp.get("DBClusterSnapshot", {})
    _audit(cluster_id, username, snapshot_id, "executed", snap.get("Status", "creating"))

    return _resp(201, {
        "ok": True,
        "cluster_id": cluster_id,
        "snapshot_id": snapshot_id,
        "status": snap.get("Status", "creating"),
        "created_by": username,
        "message": (
            f"스냅샷 '{snapshot_id}' 생성을 시작했습니다. 완료까지 수 분 걸릴 수 "
            "있으며, Backup 패널에서 상태가 available 로 바뀌면 사용 가능합니다."
        ),
    })
