"""create_snapshot — agent-facing manual snapshot creation.

Mirrors the human path (api/backups) but for the AGENT. Because the
agent isn't a trusted human, it goes through the same approval_guard
contract as every other write tool: it must carry approved=true AND a
valid approval_id minted by request_approval and approved by a DBA.

Snapshot creation is non-destructive (adds a backup), but it's still a
mutating AWS API call and a cost-incurring resource, so it stays behind
the guard for consistency and auditability.
"""

import re
import time

import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient

_SNAPSHOT_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")


def _make_snapshot_id(cluster_id: str) -> str:
    tail = re.sub(r"[^a-zA-Z0-9]+", "-", cluster_id).strip("-")[-30:].strip("-") or "cluster"
    sid = re.sub(r"-{2,}", "-", f"manual-{tail}-{int(time.time())}").strip("-")
    return sid[:63].rstrip("-")


def create_snapshot_impl(
    cache: CacheClient,
    cluster_id: str,
    snapshot_id: str = "",
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "snapshot_id": snapshot_id or "(auto-generated)",
        }

    guard = verify_approval(
        approval_id, cluster_id, "create_snapshot", payload={"snapshot_id": snapshot_id}
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    sid = (snapshot_id or "").strip()
    if sid:
        if not _SNAPSHOT_ID_RE.match(sid) or len(sid) > 63:
            return {
                "status": "invalid_snapshot_id",
                "reason": "1-63 chars, letter-start, alphanumeric + single hyphens",
            }
    else:
        sid = _make_snapshot_id(cluster_id)

    rds = boto3.client("rds")
    try:
        resp = rds.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=sid,
            DBClusterIdentifier=cluster_id,
            Tags=[
                {"Key": "dbops:created-by", "Value": "agent"},
                {"Key": "dbops:type", "Value": "manual"},
            ],
        )
    except Exception as e:
        return {"status": "create_failed", "cluster_id": cluster_id, "error": str(e)[:300]}

    snap = resp.get("DBClusterSnapshot", {})
    return {
        "status": "creating",
        "cluster_id": cluster_id,
        "snapshot_id": sid,
        "snapshot_status": snap.get("Status", "creating"),
    }
