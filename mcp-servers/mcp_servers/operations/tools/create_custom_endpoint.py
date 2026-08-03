"""create_custom_endpoint — agent-facing Aurora custom endpoint creation.

Custom cluster endpoints let a DBA route a chosen subset of readers behind a
stable DNS name (e.g. an analytics-only endpoint). Creating one is an RDS
control-plane mutation, so it rides the same approval_guard contract as every
other write tool: approved=true AND a DBA-approved approval_id minted by
request_approval, payload-hash-bound and single-use.

Aurora-only: the operations handler engine-gate (custom_endpoint capability)
refuses this tool on any non-relational engine before the impl runs. It also
validates that requested members are real cluster instances so a doomed create
never burns a consumed approval.

Failures return a STATIC Korean reason and log the detail with the module
logger: raw exception text must never reach a tool response (an AWS error
carries the hub account id, the platform role name and target ARNs).
"""

import logging
import re

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import demo_cluster_note, rds_client_for_cluster

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")
_VALID_TYPES = ("READER", "ANY")


def build_cli_preview(cluster_id, endpoint_identifier, endpoint_type, static_members, excluded_members):
    """The exact `aws rds create-db-cluster-endpoint ...` this operation runs.
    ASCII only (AWS-bound) — shown on the approval card so the operator reads
    precisely what will execute."""
    parts = [
        "aws rds create-db-cluster-endpoint",
        f"--db-cluster-identifier {cluster_id}",
        f"--db-cluster-endpoint-identifier {endpoint_identifier}",
        f"--endpoint-type {endpoint_type}",
    ]
    if static_members:
        parts.append("--static-members " + " ".join(static_members))
    if excluded_members:
        parts.append("--excluded-members " + " ".join(excluded_members))
    return " ".join(parts)


def create_custom_endpoint_impl(
    cache: CacheClient,
    cluster_id: str,
    endpoint_identifier: str = "",
    endpoint_type: str = "READER",
    static_members: list = None,
    excluded_members: list = None,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    etype = (endpoint_type or "READER").strip().upper()
    static_members = [str(m).strip() for m in (static_members or []) if str(m).strip()]
    excluded_members = [str(m).strip() for m in (excluded_members or []) if str(m).strip()]

    if not _ID_RE.match(endpoint_identifier or "") or len(endpoint_identifier) > 63:
        return {"status": "invalid_endpoint_identifier", "cluster_id": cluster_id,
                "reason": "endpoint_identifier: 1-63자, 영문 시작, 영숫자+단일 하이픈"}
    if etype not in _VALID_TYPES:
        return {"status": "invalid_endpoint_type", "cluster_id": cluster_id,
                "reason": f"endpoint_type은 {_VALID_TYPES} 중 하나여야 합니다 (커스텀 엔드포인트는 WRITER 불가)"}
    if static_members and excluded_members:
        return {"status": "invalid_members", "cluster_id": cluster_id,
                "reason": "static_members와 excluded_members는 상호 배타적입니다 — 하나만 지정하세요"}

    cli = build_cli_preview(cluster_id, endpoint_identifier, etype, static_members, excluded_members)

    # Validate members are real cluster instances BEFORE asking for approval —
    # AWS would reject a bogus member at create time, and the approval round-trip
    # would burn a consumed approval on a doomed call (same reasoning as
    # modify_scaling's pre-approval engine-mode check).
    rds = rds_client_for_cluster(cluster_id)
    try:
        cl = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception:
        logger.warning("describe_db_clusters failed for %s", cluster_id, exc_info=True)
        # "cluster_id를 확인하세요" is wrong for the built-in sample row: the id is
        # correct and the row is registered, it simply has no live AWS cluster to
        # describe. Only looked up on this failure path, so the happy path is unchanged.
        demo = demo_cluster_note(cluster_id)
        return {"status": "error", "cluster_id": cluster_id,
                "reason": demo or ("클러스터 조회에 실패해 멤버를 확인할 수 없어 중단했습니다. "
                                   "cluster_id를 확인하세요 (자세한 원인은 서버 로그를 확인하세요)."),
                "cli_preview": cli}
    members = {m.get("DBInstanceIdentifier") for m in cl.get("DBClusterMembers", [])}
    bad = [m for m in (static_members + excluded_members) if m not in members]
    if bad:
        return {"status": "invalid_members", "cluster_id": cluster_id,
                "reason": f"클러스터 인스턴스가 아닌 멤버가 있습니다: {bad}",
                "cluster_members": sorted(m for m in members if m),
                "cli_preview": cli}

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "endpoint_identifier": endpoint_identifier,
            "endpoint_type": etype,
            "static_members": static_members,
            "excluded_members": excluded_members,
            "cli_preview": cli,
        }

    guard = verify_approval(
        approval_id, cluster_id, "create_custom_endpoint",
        payload={"endpoint_identifier": endpoint_identifier, "endpoint_type": etype,
                 "static_members": static_members, "excluded_members": excluded_members},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    params = {
        "DBClusterIdentifier": cluster_id,
        "DBClusterEndpointIdentifier": endpoint_identifier,
        "EndpointType": etype,
    }
    if static_members:
        params["StaticMembers"] = static_members
    if excluded_members:
        params["ExcludedMembers"] = excluded_members
    try:
        resp = rds.create_db_cluster_endpoint(**params)
    except Exception:
        logger.warning(
            "create_db_cluster_endpoint failed for %s (endpoint=%s)",
            cluster_id, endpoint_identifier, exc_info=True,
        )
        # endpoint_identifier/type come from the CALLER, so echoing them is safe
        # and tells the DBA which create failed. cli_preview is likewise built
        # from known inputs, never from the exception.
        return {"status": "create_failed", "cluster_id": cluster_id,
                "endpoint_identifier": endpoint_identifier,
                "reason": "커스텀 엔드포인트 생성에 실패했습니다. 동일 이름의 엔드포인트가 이미 있거나 "
                          "클러스터가 available 상태가 아닐 수 있습니다 "
                          "(자세한 원인은 서버 로그를 확인하세요).",
                "cli_preview": cli}

    return {
        "status": "creating",
        "cluster_id": cluster_id,
        "endpoint_identifier": endpoint_identifier,
        "endpoint_type": etype,
        "endpoint_status": resp.get("Status", "creating"),
        "endpoint": resp.get("Endpoint"),
        "cli_preview": cli,
    }
