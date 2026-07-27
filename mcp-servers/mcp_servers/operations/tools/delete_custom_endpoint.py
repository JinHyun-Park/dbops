"""delete_custom_endpoint — agent-facing Aurora custom endpoint deletion.

Approval-gated like every write tool. Before touching anything it verifies via
DescribeDBClusterEndpoints that the target exists and is a CUSTOM endpoint —
the built-in writer/reader endpoints (EndpointType WRITER/READER) are NEVER
deletable through this tool, so a typo'd or malicious identifier can't drop the
cluster's real connection endpoints.

Aurora-only: the operations handler engine-gate (custom_endpoint capability)
refuses this tool on any non-relational engine before the impl runs.

Failures return a STATIC Korean reason and log the detail with the module
logger: raw exception text must never reach a tool response.
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

logger = logging.getLogger(__name__)


def find_custom_endpoint(rds, cluster_id: str, endpoint_identifier: str) -> dict:
    """Look up one cluster endpoint and enforce that it is CUSTOM. Returns
    {"status":"ok","endpoint":{...}} or an error-shaped dict. Shared by delete +
    modify so the built-in-endpoint protection has a single source of truth.

    No pagination: a single DBClusterEndpointIdentifier scopes the describe to
    exactly one endpoint, so there is no Marker loop (and no MagicMock-pagination
    hang in tests)."""
    try:
        resp = rds.describe_db_cluster_endpoints(
            DBClusterIdentifier=cluster_id,
            DBClusterEndpointIdentifier=endpoint_identifier,
        )
    except Exception as e:
        # `msg` stays LOCAL: it classifies the fault (NotFound vs anything else)
        # and never reaches the returned dict. Both branches carry a static reason
        # plus the caller-supplied identifier only.
        msg = str(e)
        logger.warning(
            "describe_db_cluster_endpoints failed for %s (endpoint=%s)",
            cluster_id, endpoint_identifier, exc_info=True,
        )
        if "NotFound" in msg or "not found" in msg.lower():
            return {"status": "not_found", "cluster_id": cluster_id,
                    "reason": f"엔드포인트 {endpoint_identifier!r} 를 찾을 수 없습니다"}
        return {"status": "error", "cluster_id": cluster_id,
                "reason": "엔드포인트 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요)."}
    eps = resp.get("DBClusterEndpoints") or []
    if not eps:
        return {"status": "not_found", "cluster_id": cluster_id,
                "reason": f"엔드포인트 {endpoint_identifier!r} 를 찾을 수 없습니다"}
    ep = eps[0]
    if (ep.get("EndpointType") or "").upper() != "CUSTOM":
        return {"status": "builtin_protected", "cluster_id": cluster_id,
                "endpoint_type": ep.get("EndpointType"),
                "reason": "writer/reader 내장 엔드포인트는 삭제/수정할 수 없습니다 (CUSTOM 전용)"}
    return {"status": "ok", "endpoint": ep}


def delete_custom_endpoint_impl(
    cache: CacheClient,
    cluster_id: str,
    endpoint_identifier: str = "",
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    cli = f"aws rds delete-db-cluster-endpoint --db-cluster-endpoint-identifier {endpoint_identifier}"
    if not endpoint_identifier:
        return {"status": "invalid_endpoint_identifier", "cluster_id": cluster_id,
                "reason": "endpoint_identifier가 필요합니다"}

    rds = rds_client_for_cluster(cluster_id)
    found = find_custom_endpoint(rds, cluster_id, endpoint_identifier)
    if found.get("status") != "ok":
        return {**found, "cli_preview": cli}

    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id,
                "endpoint_identifier": endpoint_identifier, "cli_preview": cli}

    guard = verify_approval(
        approval_id, cluster_id, "delete_custom_endpoint",
        payload={"endpoint_identifier": endpoint_identifier},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    try:
        resp = rds.delete_db_cluster_endpoint(DBClusterEndpointIdentifier=endpoint_identifier)
    except Exception:
        logger.warning(
            "delete_db_cluster_endpoint failed for %s (endpoint=%s)",
            cluster_id, endpoint_identifier, exc_info=True,
        )
        return {"status": "delete_failed", "cluster_id": cluster_id,
                "endpoint_identifier": endpoint_identifier,
                "reason": "커스텀 엔드포인트 삭제에 실패했습니다. 엔드포인트가 modifying "
                          "상태이면 잠시 후 다시 시도하세요 "
                          "(자세한 원인은 서버 로그를 확인하세요).",
                "cli_preview": cli}

    return {
        "status": "deleting",
        "cluster_id": cluster_id,
        "endpoint_identifier": endpoint_identifier,
        "endpoint_status": resp.get("Status", "deleting"),
        "cli_preview": cli,
    }
