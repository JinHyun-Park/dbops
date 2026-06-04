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
# A NEW cluster id must satisfy the stricter RDS *create* rules (letter-start,
# <=63, no leading/trailing/consecutive hyphens) — same shape as a snapshot id.
_NEW_CLUSTER_ID_RE = _SNAPSHOT_ID_RE


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


def _audit(cluster_id: str, username: str, action_type: str, params: dict,
           status: str, result: str = "", message: str = ""):
    """Best-effort write to audit_log (PG) + event_log (PG) via RDS Data
    API. Failures here don't fail the operation — the AWS action already
    happened; the audit trail is secondary. Logged so /activity and
    /timeline pick it up. Generic over action_type so both snapshot
    creation and cluster restore share one writer."""
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
        "VALUES (:cid, :atype, :atype, :who, :who, "
        ":params::jsonb, :result, :status, NOW())",
        {
            "cid": cluster_id,
            "atype": action_type,
            "who": username,
            "params": json.dumps(params),
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
            "msg": message or f"{action_type} {status} by {username}",
            "raw": json.dumps({**params, "status": status, "by": username}),
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


def _source_restore_kwargs(rds, source_id: str):
    """Read network + scaling config off the source cluster so the restored
    cluster lands in the same VPC and keeps a Serverless v2 profile. Returns
    (kwargs, engine). Missing pieces fall back to safe defaults."""
    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=source_id)
        c = (resp.get("DBClusters") or [{}])[0]
    except Exception:
        c = {}
    kwargs: dict = {}
    if c.get("DBSubnetGroup"):
        kwargs["DBSubnetGroupName"] = c["DBSubnetGroup"]
    sgs = [g.get("VpcSecurityGroupId") for g in (c.get("VpcSecurityGroups") or []) if g.get("VpcSecurityGroupId")]
    if sgs:
        kwargs["VpcSecurityGroupIds"] = sgs
    scaling = c.get("ServerlessV2ScalingConfiguration") or {}
    kwargs["ServerlessV2ScalingConfiguration"] = {
        "MinCapacity": scaling.get("MinCapacity", 0.5),
        "MaxCapacity": scaling.get("MaxCapacity", 4),
    }
    return kwargs, c.get("Engine", "aurora-postgresql")


def _register_pending(cluster_id: str, source_id: str, restore_source: str,
                      region: str, engine: str, cluster_arn: str, who: str) -> bool:
    """Write a clusters-registry row the restore_finalizer will pick up once
    the cluster is available. pending_instance=true is the finalizer's
    work signal."""
    from datetime import datetime, timezone

    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return False
    try:
        boto3.resource("dynamodb").Table(table_name).put_item(
            Item={
                "cluster_id": cluster_id,
                "account_id": (cluster_arn.split(":")[4] if cluster_arn.count(":") >= 4 else ""),
                "region": region,
                "engine": engine,
                "spoke_role_arn": "",
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "connection_status": "untested",
                "connection_error": "",
                "is_restored": True,
                "restored_from": source_id,
                "restore_source": restore_source,
                "pending_instance": True,
                "status": "restoring",
                "created_by": who,
                **({"cluster_arn": cluster_arn} if cluster_arn else {}),
            }
        )
        return True
    except Exception as e:
        print(f"[backups] registry put failed: {e}")
        return False


def _handle_snapshot(cluster_id: str, username: str, body: dict):
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
        _audit(cluster_id, username, "create_snapshot", {"snapshot_id": snapshot_id},
               "failed", str(e)[:300], f"Manual snapshot {snapshot_id} failed by {username}")
        return _resp(502, {"error": "create_snapshot failed", "message": str(e)[:300]})

    snap = resp.get("DBClusterSnapshot", {})
    _audit(cluster_id, username, "create_snapshot", {"snapshot_id": snapshot_id},
           "executed", snap.get("Status", "creating"),
           f"Manual snapshot {snapshot_id} executed by {username}")

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


def _handle_restore(cluster_id: str, username: str, body: dict):
    """Restore the source cluster into a NEW cluster (snapshot or PITR).

    Stronger gate than snapshot creation: beyond the admin role, the caller
    must echo the target cluster id in `confirm` (type-to-confirm), since a
    restore stands up a billable cluster. The source is never modified —
    RDS restore APIs only read it, and we refuse target == source.
    """
    new_id = (body.get("new_cluster_id") or "").strip()
    mode = (body.get("mode") or "snapshot").strip().lower()
    confirm = (body.get("confirm") or "").strip()

    if not new_id or not _NEW_CLUSTER_ID_RE.match(new_id) or len(new_id) > 63:
        return _resp(400, {"error": (
            "invalid new_cluster_id — 1-63 chars, start with a letter, "
            "alphanumeric + single hyphens"
        )})
    if new_id == cluster_id:
        return _resp(400, {"error": (
            "new_cluster_id must differ from the source — restore always "
            "creates a NEW cluster"
        )})
    if confirm != new_id:
        return _resp(400, {"error": (
            "confirmation failed — 'confirm' must exactly match new_cluster_id"
        )})

    rds = boto3.client("rds")
    base_kwargs, engine = _source_restore_kwargs(rds, cluster_id)
    tags = [
        {"Key": "dbops:type", "Value": "restored"},
        {"Key": "dbops:restored-from", "Value": cluster_id},
        {"Key": "dbops:created-by", "Value": username},
    ]

    try:
        if mode == "pitr":
            pitr_kwargs = dict(base_kwargs)
            use_latest = bool(body.get("use_latest"))
            restore_to_time = (body.get("restore_to_time") or "").strip()
            if use_latest:
                pitr_kwargs["UseLatestRestorableTime"] = True
            elif restore_to_time:
                pitr_kwargs["RestoreToTime"] = restore_to_time
            else:
                return _resp(400, {"error": "pitr mode requires restore_to_time or use_latest=true"})
            resp = rds.restore_db_cluster_to_point_in_time(
                DBClusterIdentifier=new_id,
                SourceDBClusterIdentifier=cluster_id,
                Tags=tags,
                **pitr_kwargs,
            )
            restore_source = "pitr:latest" if use_latest else f"pitr:{restore_to_time}"
        else:
            snapshot_id = (body.get("snapshot_id") or "").strip()
            if not snapshot_id:
                return _resp(400, {"error": "snapshot mode requires snapshot_id"})
            resp = rds.restore_db_cluster_from_snapshot(
                DBClusterIdentifier=new_id,
                SnapshotIdentifier=snapshot_id,
                Engine=engine,
                Tags=tags,
                **base_kwargs,
            )
            restore_source = f"snapshot:{snapshot_id}"
    except rds.exceptions.DBClusterAlreadyExistsFault:
        return _resp(409, {"error": f"cluster id '{new_id}' already exists"})
    except Exception as e:
        _audit(cluster_id, username, "restore_cluster",
               {"new_cluster_id": new_id, "mode": mode}, "failed", str(e)[:300],
               f"Restore to {new_id} failed by {username}")
        return _resp(502, {"error": "restore failed", "message": str(e)[:300]})

    new_cluster = resp.get("DBCluster", {})
    new_arn = new_cluster.get("DBClusterArn", "")
    region = new_arn.split(":")[3] if new_arn.count(":") >= 4 else os.environ.get("AWS_REGION", "")
    registered = _register_pending(new_id, cluster_id, restore_source, region, engine, new_arn, username)

    _audit(cluster_id, username, "restore_cluster",
           {"new_cluster_id": new_id, "mode": mode, "restore_source": restore_source},
           "executed", new_cluster.get("Status", "creating"),
           f"Restore to {new_id} ({restore_source}) started by {username}")

    return _resp(201, {
        "ok": True,
        "cluster_id": cluster_id,
        "new_cluster_id": new_id,
        "mode": mode,
        "restore_source": restore_source,
        "registered": registered,
        "created_by": username,
        "message": (
            f"'{cluster_id}' → 새 클러스터 '{new_id}' 복원을 시작했습니다 "
            f"({restore_source}). 클러스터가 available 되면 writer 인스턴스가 "
            "자동 생성되고 DBOps 등록이 마무리됩니다 (수 분 소요). 소스 "
            "클러스터는 변경되지 않습니다."
        ),
    })


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

    raw_path = (
        event.get("rawPath")
        or event.get("requestContext", {}).get("http", {}).get("path", "")
        or ""
    )
    path_params = event.get("pathParameters") or {}
    cluster_id = path_params.get("cluster_id") or ""
    if not cluster_id or not _CLUSTER_ID_RE.match(cluster_id):
        return _resp(400, {"error": "invalid cluster_id"})

    is_admin, username = _caller(event)
    if not is_admin:
        return _resp(403, {"error": "forbidden", "reason": "admin role required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "body must be valid JSON"})

    # Two write actions share this Lambda, dispatched by path:
    #   POST .../snapshot  → create a manual snapshot (low risk)
    #   POST .../restore   → restore into a NEW cluster (type-to-confirm)
    if raw_path.endswith("/restore"):
        return _handle_restore(cluster_id, username, body)
    return _handle_snapshot(cluster_id, username, body)
