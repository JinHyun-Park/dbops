"""modify_custom_endpoint — agent-facing Aurora custom endpoint member changes.

Approval-gated. Changes the StaticMembers or ExcludedMembers of an existing
CUSTOM endpoint (the two are mutually exclusive per endpoint). Reuses
find_custom_endpoint so the built-in writer/reader endpoints are never
modifiable through this tool.

Aurora-only: the operations handler engine-gate (custom_endpoint capability)
refuses this tool on any non-relational engine before the impl runs.

Failures return a STATIC Korean reason and log the detail with the module
logger: raw exception text must never reach a tool response.
"""

import logging

from mcp_servers.operations.tools.delete_custom_endpoint import find_custom_endpoint
from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

logger = logging.getLogger(__name__)


def build_cli_preview(endpoint_identifier, static_members, excluded_members):
    """Exact `aws rds modify-db-cluster-endpoint ...` this operation runs (ASCII
    only). delete/modify CLI address the endpoint by its own identifier, not the
    cluster id."""
    parts = [
        "aws rds modify-db-cluster-endpoint",
        f"--db-cluster-endpoint-identifier {endpoint_identifier}",
    ]
    if static_members:
        parts.append("--static-members " + " ".join(static_members))
    if excluded_members:
        parts.append("--excluded-members " + " ".join(excluded_members))
    return " ".join(parts)


def modify_custom_endpoint_impl(
    cache: CacheClient,
    cluster_id: str,
    endpoint_identifier: str = "",
    static_members: list = None,
    excluded_members: list = None,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    static_members = [str(m).strip() for m in (static_members or []) if str(m).strip()]
    excluded_members = [str(m).strip() for m in (excluded_members or []) if str(m).strip()]
    cli = build_cli_preview(endpoint_identifier, static_members, excluded_members)

    if not endpoint_identifier:
        return {"status": "invalid_endpoint_identifier", "cluster_id": cluster_id,
                "reason": "endpoint_identifier가 필요합니다"}
    if static_members and excluded_members:
        return {"status": "invalid_members", "cluster_id": cluster_id,
                "reason": "static_members와 excluded_members는 상호 배타적입니다 — 하나만 지정하세요"}
    if not static_members and not excluded_members:
        return {"status": "nothing_to_modify", "cluster_id": cluster_id,
                "reason": "static_members 또는 excluded_members 중 하나는 지정해야 합니다"}

    rds = rds_client_for_cluster(cluster_id)
    found = find_custom_endpoint(rds, cluster_id, endpoint_identifier)
    if found.get("status") != "ok":
        return {**found, "cli_preview": cli}

    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id,
                "endpoint_identifier": endpoint_identifier,
                "static_members": static_members, "excluded_members": excluded_members,
                "cli_preview": cli}

    guard = verify_approval(
        approval_id, cluster_id, "modify_custom_endpoint",
        payload={"endpoint_identifier": endpoint_identifier,
                 "static_members": static_members, "excluded_members": excluded_members},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    params = {"DBClusterEndpointIdentifier": endpoint_identifier}
    if static_members:
        params["StaticMembers"] = static_members
    if excluded_members:
        params["ExcludedMembers"] = excluded_members
    try:
        resp = rds.modify_db_cluster_endpoint(**params)
    except Exception:
        logger.warning(
            "modify_db_cluster_endpoint failed for %s (endpoint=%s)",
            cluster_id, endpoint_identifier, exc_info=True,
        )
        return {"status": "modify_failed", "cluster_id": cluster_id,
                "endpoint_identifier": endpoint_identifier,
                "reason": "커스텀 엔드포인트 멤버 변경에 실패했습니다. 엔드포인트가 "
                          "modifying 상태이면 잠시 후 다시 시도하세요 "
                          "(자세한 원인은 서버 로그를 확인하세요).",
                "cli_preview": cli}

    return {
        "status": "modifying",
        "cluster_id": cluster_id,
        "endpoint_identifier": endpoint_identifier,
        "static_members": static_members,
        "excluded_members": excluded_members,
        "endpoint_status": resp.get("Status", "modifying"),
        "cli_preview": cli,
    }
