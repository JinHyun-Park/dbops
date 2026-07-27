"""create_snapshot — agent-facing manual snapshot creation.

Mirrors the human path (api/backups) but for the AGENT. Because the
agent isn't a trusted human, it goes through the same approval_guard
contract as every other write tool: it must carry approved=true AND a
valid approval_id minted by request_approval and approved by a DBA.

Snapshot creation is non-destructive (adds a backup), but it's still a
mutating AWS API call and a cost-incurring resource, so it stays behind
the guard for consistency and auditability.
"""

import logging
import re
import time

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

logger = logging.getLogger(__name__)

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

    rds = rds_client_for_cluster(cluster_id)
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
        # The approval is ALREADY consumed at this point, so this path only
        # reports: it must not re-run the guard or change the status.
        #
        # RDS exception TEXT spells out the hub account id, the platform role and
        # the cluster ARN, and this reason lands in the agent transcript the DBA
        # reads. The error CODE is a bounded AWS enum and is the actionable part
        # (id collision vs cluster mid-modify vs quota), so keep the code and
        # send the full exception to CloudWatch only.
        # .get chain, not ["Error"]["Code"]: a KeyError raised INSIDE this
        # handler would escape the tool and lose the create_failed status.
        code = str((e.response.get("Error") or {}).get("Code") or "")[:60] \
            if isinstance(e, ClientError) else ""
        code_part = f" ({code})" if code else ""
        logger.warning(
            "create_db_cluster_snapshot failed for %s (snapshot=%s)", cluster_id, sid, exc_info=True
        )
        return {
            "status": "create_failed",
            "cluster_id": cluster_id,
            "error": (
                f"스냅샷 생성 요청이 실패했습니다{code_part}. "
                "스냅샷 식별자 중복, 클러스터 상태(변경 중), 스냅샷 할당량을 확인하세요 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
        }

    snap = resp.get("DBClusterSnapshot", {})
    return {
        "status": "creating",
        "cluster_id": cluster_id,
        "snapshot_id": sid,
        "snapshot_status": snap.get("Status", "creating"),
    }
