"""restore_finalizer — async second half of the backup restore workflow.

Why this Lambda exists
----------------------
RestoreDBClusterFromSnapshot / RestoreDBClusterToPointInTime only restore
the *cluster volume*. Per the RDS docs you must call CreateDBInstance
SEPARATELY, and only AFTER the cluster reaches `available` — which takes
several minutes. That outlasts the synchronous request that kicked off
the restore, so instance provisioning + final registration happen here,
out of band.

Contract with the initiators (api/backups, operations.restore_cluster):
  - They restore the cluster and write a clusters-registry row with
    `pending_instance = true`, `is_restored = true`, `status =
    "restoring"`, plus restore provenance.
  - This Lambda runs on a fixed schedule, scans for `pending_instance`
    rows, and for each cluster that has become `available`:
      1. creates a single db.serverless writer instance (idempotent),
      2. backfills cluster_arn + secret_arn so RDS Data API / EXPLAIN
         work against the restored cluster,
      3. clears `pending_instance` and flips status to "available".

Idempotency: every step tolerates re-entry. If the instance already
exists, or the cluster already has members, we skip to backfill + clear.
A cluster that vanished (deleted mid-restore) gets its flag cleared and
status marked failed so we stop polling it.

Cross-account / cross-region: each pending row carries the `region` and
`spoke_role_arn` of the account the cluster was restored into (a restore
lands in the same account+region as its source). The finalizer builds a
per-row RDS client from those — assuming the spoke role when present — so it
can finalize restores in spoke accounts, not just the deploy account.
"""

import base64
import json
import os
import re
from datetime import datetime, timezone

import boto3

_INSTANCE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")

# Base64-encoded ClientContext so the operations Lambda's _extract_tool_name reads
# custom.tool_name == "prewarm_reader" (same shape a direct boto3 invoke sets).
_PREWARM_CLIENT_CONTEXT = base64.b64encode(
    json.dumps({"custom": {"tool_name": "prewarm_reader"}}).encode("utf-8")
).decode("utf-8")


def _rds_for(region: str = "", role_arn: str = ""):
    """RDS client targeting the restored cluster's account+region. With
    `role_arn` set (cross-account restore), assume the spoke role first. This
    Lambda lives in a separate package and can't import the mcp-servers shared
    helper, so it mirrors the minimal assume-role logic (see
    mcp_servers.shared.cluster_targets / api.clusters._session_for)."""
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.client("rds", region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"dbops-finalizer-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    ).client("rds")


def _make_instance_id(cluster_id: str) -> str:
    """Derive a valid writer-instance id from the cluster id:
    `<cluster-tail>-instance-1`, sanitised to RDS identifier rules."""
    tail = re.sub(r"[^a-zA-Z0-9]+", "-", cluster_id).strip("-")[-40:].strip("-") or "restored"
    sid = re.sub(r"-{2,}", "-", f"{tail}-instance-1").strip("-")
    if not _INSTANCE_ID_RE.match(sid):
        sid = "restored-instance-1"
    return sid[:63].rstrip("-")


def _event_log(cluster_id: str, severity: str, message: str):
    """Best-effort timeline event via RDS Data API (same shape the
    backups API uses). Failure here never blocks finalization."""
    cluster_arn = os.environ.get("CACHE_DB_CLUSTER_ARN", "")
    secret_arn = os.environ.get("CACHE_DB_SECRET_ARN", "")
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    if not cluster_arn or not secret_arn:
        return
    try:
        boto3.client("rds-data").execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=(
                "/* source=dbops-restore-finalizer */ "
                "INSERT INTO event_log (cluster_id, event_time, event_type, "
                "source, severity, message, raw_event) VALUES "
                "(:cid, NOW(), 'backup', 'dbops-restore-finalizer', :sev, :msg, "
                "'{}'::jsonb)"
            ),
            parameters=[
                {"name": "cid", "value": {"stringValue": cluster_id}},
                {"name": "sev", "value": {"stringValue": severity}},
                {"name": "msg", "value": {"stringValue": message}},
            ],
        )
    except Exception as e:  # pragma: no cover - best effort
        print(f"[finalizer] event_log write failed: {e}")


def _finalize_one(rds, table, row: dict) -> dict:
    """Advance one pending cluster. Returns a small status dict for logs."""
    cluster_id = row.get("cluster_id") or ""
    if not cluster_id:
        return {"cluster_id": "(missing)", "result": "skipped_no_id"}

    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = resp.get("DBClusters") or []
    except rds.exceptions.DBClusterNotFoundFault:
        _clear_pending(table, cluster_id, status="restore_failed")
        _event_log(cluster_id, "warning", f"Restore target {cluster_id} disappeared before finalization")
        return {"cluster_id": cluster_id, "result": "not_found"}
    except Exception as e:
        # Transient describe error — leave the flag set, retry next tick.
        return {"cluster_id": cluster_id, "result": f"describe_error:{str(e)[:80]}"}

    if not clusters:
        _clear_pending(table, cluster_id, status="restore_failed")
        return {"cluster_id": cluster_id, "result": "not_found"}

    cluster = clusters[0]
    status = cluster.get("Status", "")
    if status != "available":
        # Still creating / backing-up / migrating — try again next tick.
        return {"cluster_id": cluster_id, "result": f"waiting:{status}"}

    members = cluster.get("DBClusterMembers") or []
    engine = cluster.get("Engine", "aurora-postgresql")

    if not members:
        instance_id = _make_instance_id(cluster_id)
        try:
            rds.create_db_instance(
                DBInstanceIdentifier=instance_id,
                DBClusterIdentifier=cluster_id,
                Engine=engine,
                DBInstanceClass="db.serverless",
                Tags=[{"Key": "dbops:type", "Value": "restored"}],
            )
            _event_log(
                cluster_id, "info",
                f"Restored cluster {cluster_id} available — created writer instance {instance_id}",
            )
        except rds.exceptions.DBInstanceAlreadyExistsFault:
            pass  # idempotent: instance already created on a prior tick
        except Exception as e:
            # Cluster is available but instance creation failed — surface it
            # and clear the flag so we don't spin forever. The DBA can add an
            # instance manually from the restored cluster.
            _clear_pending(table, cluster_id, status="available_no_instance")
            _event_log(cluster_id, "warning", f"Instance creation failed for {cluster_id}: {str(e)[:200]}")
            return {"cluster_id": cluster_id, "result": f"instance_error:{str(e)[:80]}"}

    # Backfill the connection coordinates so the restored cluster is queryable
    # through the same RDS Data API path every other endpoint uses.
    cluster_arn = cluster.get("DBClusterArn", "")
    secret_arn = (cluster.get("MasterUserSecret") or {}).get("SecretArn", "")
    _clear_pending(
        table, cluster_id, status="available",
        cluster_arn=cluster_arn, secret_arn=secret_arn,
        engine_version=cluster.get("EngineVersion", ""),
    )
    return {"cluster_id": cluster_id, "result": "finalized"}


def _clear_pending(table, cluster_id: str, status: str, cluster_arn: str = "",
                   secret_arn: str = "", engine_version: str = ""):
    """Flip pending_instance off and stamp the outcome on the registry row.
    Only sets connection coordinates when we actually have them."""
    expr = ["pending_instance = :f", "#st = :s", "finalized_at = :ts"]
    names = {"#st": "status"}
    values = {
        ":f": False,
        ":s": status,
        ":ts": _now_iso(),
    }
    if cluster_arn:
        expr.append("cluster_arn = :ca")
        values[":ca"] = cluster_arn
    if secret_arn:
        expr.append("secret_arn = :sa")
        values[":sa"] = secret_arn
        # Having a master secret means RDS Data API can connect.
        expr.append("connection_status = :cs")
        values[":cs"] = "ok"
    if engine_version:
        expr.append("engine_version = :ev")
        values[":ev"] = engine_version
    try:
        table.update_item(
            Key={"cluster_id": cluster_id},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as e:  # pragma: no cover - best effort
        print(f"[finalizer] registry update failed for {cluster_id}: {e}")


def _now_iso() -> str:
    # datetime.utcnow via boto-free path; importing here keeps top clean.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ===== Second pass: scale-out prewarm approvals (N-④ Phase 1) ================
# The prewarm approval ROW is the whole state machine — this Lambda only moves it
# between states and fires a Lambda invoke. It lives in its own package and CANNOT
# import mcp_servers, so it never computes payload hashes and never connects to a
# DB: awaiting_instance → (reader available) → pending → (DBA approves) →
# approved → (invoke prewarm_reader) → consumed (by the prewarm tool itself).


def _scan_scaleout_prewarms(table) -> list:
    """Paginate the approvals table for scale-out prewarm rows. Guarded against
    the bare-MagicMock infinite-scan: LastEvaluatedKey must be a real, non-empty
    dict to continue (a MagicMock isn't a dict → we stop)."""
    kwargs = {
        "FilterExpression": "scaleout = :t AND action_type = :pw",
        "ExpressionAttributeValues": {":t": True, ":pw": "prewarm_reader"},
    }
    items = []
    try:
        while True:
            resp = table.scan(**kwargs)
            page = resp.get("Items")
            if isinstance(page, list):
                items.extend(page)
            lek = resp.get("LastEvaluatedKey")
            if not isinstance(lek, dict) or not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    except Exception as e:
        print(f"[finalizer] approvals scan failed: {e}")
    return items


def _set_approval(table, approval_id: str, created_at: str, **attrs):
    """Set arbitrary attributes on an approval row (composite key). Best-effort."""
    if not attrs:
        return
    expr = ", ".join(f"#{k} = :{k}" for k in attrs)
    names = {f"#{k}": ("approval_status" if k == "status" else k) for k in attrs}
    values = {f":{k}": v for k, v in attrs.items()}
    try:
        table.update_item(
            Key={"approval_id": approval_id, "created_at": created_at},
            UpdateExpression="SET " + expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as e:  # pragma: no cover - best effort
        print(f"[finalizer] approval update failed for {approval_id}: {e}")


def _advance_prewarm(table, ops_fn: str, row: dict, may_dispatch: bool = True) -> dict:
    approval_id = row.get("approval_id", "")
    created_at = row.get("created_at", "")
    cluster_id = row.get("cluster_id", "")
    reader_id = row.get("reader_instance_id", "")
    status = row.get("approval_status", "")

    # Never touch rejected/consumed (or anything unexpected) — only the two
    # states this pass owns.
    if status not in ("awaiting_instance", "approved"):
        return {"approval_id": approval_id, "result": f"skip:{status}"}

    if status == "awaiting_instance":
        rds = _rds_for(row.get("region", ""), row.get("spoke_role_arn", ""))
        try:
            di = rds.describe_db_instances(DBInstanceIdentifier=reader_id)
        except Exception as e:
            msg = str(e)
            # The reader vanished (deleted before it ever came up) → stop polling.
            if "NotFound" in msg or "DBInstanceNotFound" in msg:
                _set_approval(table, approval_id, created_at, status="awaiting_instance_failed")
                _event_log(cluster_id, "warning",
                           f"scale-out 예열 대상 리더 {reader_id} 소멸 — 예열 승인 취소")
                return {"approval_id": approval_id, "result": "instance_vanished"}
            # Transient describe error — leave for next tick.
            return {"approval_id": approval_id, "result": f"describe_error:{msg[:60]}"}
        insts = di.get("DBInstances") or []
        if not insts:
            _set_approval(table, approval_id, created_at, status="awaiting_instance_failed")
            _event_log(cluster_id, "warning",
                       f"scale-out 예열 대상 리더 {reader_id} 없음 — 예열 승인 취소")
            return {"approval_id": approval_id, "result": "instance_vanished"}
        inst_status = insts[0].get("DBInstanceStatus", "")
        if inst_status == "available":
            # Now DBA-visible in the Approval Center.
            _set_approval(table, approval_id, created_at, status="pending")
            _event_log(cluster_id, "info",
                       f"리더 {reader_id} available — 예열 승인 대기열 등록")
            return {"approval_id": approval_id, "result": "queued_pending"}
        return {"approval_id": approval_id, "result": f"waiting:{inst_status}"}

    # status == "approved" → dispatch the actual warm via the operations Lambda.
    if row.get("warm_dispatched"):
        return {"approval_id": approval_id, "result": "already_dispatched"}
    if not ops_fn:
        return {"approval_id": approval_id, "result": "no_operations_fn"}
    # One synchronous warm per tick (see caller): a sync invoke blocks this
    # Lambda for the operations Lambda's runtime, so we cap to one and let the
    # rest ride the next 5-min tick.
    if not may_dispatch:
        return {"approval_id": approval_id, "result": "dispatch_deferred"}
    ad = row.get("action_details")
    if not isinstance(ad, dict):
        ad = {}
    # Pass the SAME endpoint_identifier + top_n that were hashed into the approval
    # — prewarm_reader.verify_approval re-projects them and refuses any mismatch.
    payload = {
        "cluster_id": cluster_id,
        "reader_instance_id": reader_id,
        "endpoint_identifier": ad.get("endpoint_identifier", "") or "",
        "top_n": int(ad.get("top_n", 20) or 20),
        "approved": True,
        "approval_id": approval_id,
    }
    try:
        # SYNCHRONOUS (RequestResponse): Lambda only delivers ClientContext —
        # which carries custom.tool_name so the operations handler routes to
        # prewarm_reader — for RequestResponse, NOT for async Event invokes.
        # (The finalizer timeout is raised to accommodate the operations Lambda's
        # 120s, and we dispatch at most one warm per tick.)
        resp = boto3.client("lambda").invoke(
            FunctionName=ops_fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
            ClientContext=_PREWARM_CLIENT_CONTEXT,
        )
    except Exception as e:
        # The invoke itself failed (throttle/network) — prewarm never started,
        # the approval is NOT consumed, so leave warm_dispatched unset to retry.
        return {"approval_id": approval_id, "result": f"invoke_error:{str(e)[:60]}"}

    # We got a response → the operations handler ran and prewarm_reader's
    # verify_approval has consumed the approval (or refused it). Either way the
    # attempt is made; set warm_dispatched so we never re-invoke a consumed
    # approval (which would just come back approval_denied and loop forever).
    warm_status = "unknown"
    try:
        body = json.loads(resp["Payload"].read().decode("utf-8"))
        text = (body.get("content") or [{}])[0].get("text") if isinstance(body, dict) else None
        warm_status = (json.loads(text).get("status") if text else None) or warm_status
    except Exception:  # pragma: no cover - best effort result parse
        pass
    fn_error = resp.get("FunctionError")
    _set_approval(table, approval_id, created_at, warm_dispatched=True)
    _event_log(
        cluster_id,
        "info" if warm_status == "prewarmed" else "warning",
        f"리더 {reader_id} 예열 실행 결과: {warm_status}"
        + (f" (fn_error={fn_error})" if fn_error else "") + f" (approval {approval_id})",
    )
    return {"approval_id": approval_id, "result": "warm_dispatched", "warm_status": warm_status}


def _process_scaleout_prewarms() -> dict:
    """Drive every scale-out prewarm approval one step. Independent of the
    restore pass so a CLUSTERS_TABLE misconfig can't disable it."""
    table_name = os.environ.get("APPROVALS_TABLE", "")
    if not table_name:
        return {"scanned": 0}
    ops_fn = os.environ.get("OPERATIONS_FUNCTION_NAME", "")
    table = boto3.resource("dynamodb").Table(table_name)
    rows = _scan_scaleout_prewarms(table)
    # Cap to ONE synchronous warm dispatch per tick — each blocks this Lambda
    # for the operations Lambda's runtime. State-only transitions
    # (awaiting_instance → pending) are cheap and always run; only the
    # approved → invoke step consumes the budget, so once one fires the rest
    # defer to the next 5-min tick.
    results = []
    dispatched = False
    for row in rows:
        res = _advance_prewarm(table, ops_fn, row, may_dispatch=not dispatched)
        results.append(res)
        if res.get("result") == "warm_dispatched":
            dispatched = True
    if results:
        print(f"[finalizer] scale-out prewarms: {results}")
    return {"scanned": len(rows), "results": results}


def lambda_handler(event, context):
    # Independent second pass — runs even if the restore pass early-returns.
    scaleout = _process_scaleout_prewarms()

    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        print("[finalizer] CLUSTERS_TABLE not configured")
        return {"finalized": 0, "error": "CLUSTERS_TABLE missing", "scaleout": scaleout}

    table = boto3.resource("dynamodb").Table(table_name)

    # Scan for pending restores. The registry is small (one row per managed
    # cluster) so a filtered scan every few minutes is cheap; in steady
    # state there are zero pending rows and this is a no-op.
    try:
        resp = table.scan(
            FilterExpression="pending_instance = :t",
            ExpressionAttributeValues={":t": True},
        )
        pending = resp.get("Items", [])
    except Exception as e:
        print(f"[finalizer] scan failed: {e}")
        return {"finalized": 0, "error": str(e)[:200], "scaleout": scaleout}

    # Build the RDS client PER ROW from its account+region (cross-account
    # restores carry a spoke_role_arn) — a single hub client can't reach a
    # cluster that was restored into a spoke account.
    results = [
        _finalize_one(
            _rds_for(row.get("region", ""), row.get("spoke_role_arn", "")),
            table,
            row,
        )
        for row in pending
    ]
    finalized = sum(1 for r in results if r.get("result") == "finalized")
    if results:
        print(f"[finalizer] processed {len(results)} pending: {results}")
    return {"pending": len(pending), "finalized": finalized, "results": results,
            "scaleout": scaleout}
