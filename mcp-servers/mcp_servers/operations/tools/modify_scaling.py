from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster


def modify_scaling_impl(
    cache: CacheClient,
    cluster_id: str,
    min_capacity: float = None,
    max_capacity: float = None,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    rds = rds_client_for_cluster(cluster_id)

    # ACU 범위는 Serverless v2 전용. 프로비저닝 클러스터에도 RDS API는
    # ServerlessV2ScalingConfiguration을 조용히 수용할 수 있는데, 그 경우
    # "modified"라고 보고해도 실제 인스턴스(r6g 등)에는 아무 효과가 없다.
    # 이 검사는 approval_required 분기보다 먼저 — 어차피 적용 불가한 작업에
    # 승인 왕복(요청→DBA 검토→재실행)을 시키고 승인만 소비하는 낭비를 막는다.
    try:
        info = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        return {
            "status": "error",
            "reason": f"클러스터 조회 실패 — 적용 전 엔진 모드를 확인할 수 없어 중단합니다: {str(e)[:200]}",
            "cluster_id": cluster_id,
        }
    if not info.get("ServerlessV2ScalingConfiguration"):
        return {
            "status": "not_applicable",
            "reason": (
                "이 클러스터는 프로비저닝(고정 인스턴스) 모드라 ACU 범위 변경이 적용되지 않습니다. "
                "인스턴스 클래스 변경은 simulate_scaling(new_instance_class)으로 비용 영향을 검토한 뒤 "
                "RDS modify-db-instance로 진행하세요."
            ),
            "cluster_id": cluster_id,
            "engine_mode": info.get("EngineMode", "provisioned"),
        }

    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "min_capacity": min_capacity, "max_capacity": max_capacity}

    guard = verify_approval(
        approval_id,
        cluster_id,
        "modify_scaling",
        payload={"min_capacity": min_capacity, "max_capacity": max_capacity},
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    params = {"DBClusterIdentifier": cluster_id}
    sc = {}
    if min_capacity is not None:
        sc["MinCapacity"] = min_capacity
    if max_capacity is not None:
        sc["MaxCapacity"] = max_capacity
    if sc:
        params["ServerlessV2ScalingConfiguration"] = sc

    rds.modify_db_cluster(**params)
    return {"status": "modified", "cluster_id": cluster_id, "scaling": sc}
