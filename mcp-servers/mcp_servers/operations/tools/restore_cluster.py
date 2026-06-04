"""restore_cluster — agent-facing cluster restore (snapshot or PITR).

This is the most consequential write the agent can initiate: it spins up a
brand-new Aurora cluster (cost-incurring) from either a snapshot or a
point in time. It NEVER touches the source cluster — RDS restore APIs are
inherently non-destructive to the source, and we additionally refuse to
reuse the source id as the target.

Gate: like every agent write, it requires approved=true AND a
server-verified approval_id from request_approval. Restore is classified
high risk on the approval card (vs create_snapshot's low) because the
result is a live, billable cluster.

The agent only kicks off the restore and registers the new cluster with
`pending_instance=true`; the restore_finalizer Lambda adds the writer
instance and finalizes registration once the cluster is available
(CreateDBInstance can't run until then — see that handler).
"""

import os
import re

import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient

_CLUSTER_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")


def _source_restore_kwargs(rds, source_id: str) -> dict:
    """Read network + scaling config off the source cluster so the restored
    cluster lands in the same VPC and keeps a Serverless v2 profile. Missing
    pieces fall back to safe defaults."""
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
                      region: str, engine: str, cluster_arn: str, who: str):
    """Write a clusters-registry row the restore_finalizer will pick up."""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return False
    from datetime import datetime, timezone

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
        print(f"[restore_cluster] registry put failed: {e}")
        return False


def restore_cluster_impl(
    cache: CacheClient,
    cluster_id: str,
    new_cluster_id: str,
    mode: str = "snapshot",
    snapshot_id: str = "",
    restore_to_time: str = "",
    use_latest: bool = False,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "new_cluster_id": new_cluster_id,
            "mode": mode,
        }

    guard = verify_approval(approval_id, cluster_id, "restore_cluster")
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    # Target id must be valid AND distinct from the source — restore always
    # produces a NEW cluster; we never restore in place.
    nid = (new_cluster_id or "").strip()
    if not nid or not _CLUSTER_ID_RE.match(nid) or len(nid) > 63:
        return {"status": "invalid_new_cluster_id",
                "reason": "1-63 chars, letter-start, alphanumeric + single hyphens"}
    if nid == cluster_id:
        return {"status": "invalid_new_cluster_id",
                "reason": "new_cluster_id must differ from the source cluster"}

    rds = boto3.client("rds")
    base_kwargs, engine = _source_restore_kwargs(rds, cluster_id)
    tags = [
        {"Key": "dbops:type", "Value": "restored"},
        {"Key": "dbops:restored-from", "Value": cluster_id},
        {"Key": "dbops:created-by", "Value": "agent"},
    ]

    try:
        if mode == "pitr":
            pitr_kwargs = dict(base_kwargs)
            if use_latest:
                pitr_kwargs["UseLatestRestorableTime"] = True
            elif restore_to_time:
                pitr_kwargs["RestoreToTime"] = restore_to_time
            else:
                return {"status": "invalid_request",
                        "reason": "pitr mode needs restore_to_time or use_latest=true"}
            resp = rds.restore_db_cluster_to_point_in_time(
                DBClusterIdentifier=nid,
                SourceDBClusterIdentifier=cluster_id,
                Tags=tags,
                **pitr_kwargs,
            )
            restore_source = "pitr:latest" if use_latest else f"pitr:{restore_to_time}"
        else:  # snapshot
            sid = (snapshot_id or "").strip()
            if not sid:
                return {"status": "invalid_request", "reason": "snapshot mode needs snapshot_id"}
            resp = rds.restore_db_cluster_from_snapshot(
                DBClusterIdentifier=nid,
                SnapshotIdentifier=sid,
                Engine=engine,
                Tags=tags,
                **base_kwargs,
            )
            restore_source = f"snapshot:{sid}"
    except rds.exceptions.DBClusterAlreadyExistsFault:
        return {"status": "already_exists", "new_cluster_id": nid}
    except Exception as e:
        return {"status": "restore_failed", "cluster_id": cluster_id, "error": str(e)[:300]}

    new_cluster = resp.get("DBCluster", {})
    new_arn = new_cluster.get("DBClusterArn", "")
    region = (new_arn.split(":")[3] if new_arn.count(":") >= 4 else os.environ.get("AWS_REGION", ""))
    registered = _register_pending(nid, cluster_id, restore_source, region, engine, new_arn, "agent")

    return {
        "status": "restoring",
        "cluster_id": cluster_id,
        "new_cluster_id": nid,
        "mode": mode,
        "restore_source": restore_source,
        "registered": registered,
        "note": (
            "복원된 클러스터가 생성 중입니다. available 상태가 되면 "
            "restore_finalizer가 writer 인스턴스를 자동 생성하고 등록을 "
            "마무리합니다 (수 분 소요)."
        ),
    }
