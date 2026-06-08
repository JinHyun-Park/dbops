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

import os
import re
from datetime import datetime, timezone

import boto3

_INSTANCE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")


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


def lambda_handler(event, context):
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        print("[finalizer] CLUSTERS_TABLE not configured")
        return {"finalized": 0, "error": "CLUSTERS_TABLE missing"}

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
        return {"finalized": 0, "error": str(e)[:200]}

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
    return {"pending": len(pending), "finalized": finalized, "results": results}
