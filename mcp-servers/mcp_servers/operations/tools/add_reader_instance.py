"""add_reader_instance — approval-gated Aurora reader scale-OUT (N-③).

Adds a new READER instance to an Aurora cluster (PG or MySQL — this is
instance-level, not engine-specific). The handler positive-gates this tool on
the relational-only `scale_instance` capability, so non-relational engines get
unsupported_engine before the impl runs.

FAIL-CLOSED like every write tool: verify_approval must pass before any RDS
write, and no str(e) internals leak into a return shown to users (errors are
logged to CloudWatch).
"""

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster


def _writer_instance_class(rds, dbc):
    """Resolve the WRITER's current DBInstanceClass so a scale-out defaults to a
    same-shape reader. IsClusterWriter lives on the cluster's member list; the
    class lives on describe_db_instances. Serverless v2 writers report
    `db.serverless`. Returns "" if it can't be resolved."""
    writer_id = None
    for m in dbc.get("DBClusterMembers") or []:
        if m.get("IsClusterWriter"):
            writer_id = m.get("DBInstanceIdentifier")
            break
    if not writer_id:
        return ""
    di = rds.describe_db_instances(
        Filters=[{"Name": "db-cluster-id", "Values": [dbc.get("DBClusterIdentifier", "")]}]
    )
    for inst in di.get("DBInstances") or []:
        if inst.get("DBInstanceIdentifier") == writer_id:
            return inst.get("DBInstanceClass") or ""
    return ""


def add_reader_instance_impl(
    cache: CacheClient,
    cluster_id: str,
    new_instance_id: str = "",
    instance_class: str = "",
    availability_zone: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    new_instance_id = (new_instance_id or "").strip()
    instance_class = (instance_class or "").strip()
    availability_zone = (availability_zone or "").strip()

    # The caller must name the new reader — the approval payload hash binds it,
    # so an auto-generated name (different each call) could never hash-match.
    if not new_instance_id:
        return {"status": "invalid_instance", "cluster_id": cluster_id,
                "reason": "new_instance_id가 필요합니다."}

    if not approved:
        # Resolve the concrete class NOW so the approval payload hash binds the
        # exact billable class the DBA sees — execute never picks a class after
        # approval.
        if not instance_class:
            try:
                rds = client_for_cluster(cluster_id, "rds")
                dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
                instance_class = _writer_instance_class(rds, dbc)
            except Exception as e:
                print(f"[add_reader_instance] preview writer-class lookup failed for {cluster_id}: {e}")
                instance_class = ""
            if not instance_class:
                return {"status": "needs_instance_class", "cluster_id": cluster_id,
                        "reason": "instance_class를 결정할 수 없습니다 — 명시해 주세요 (예: db.serverless)."}
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "new_instance_id": new_instance_id,
            "instance_class": instance_class,
            "availability_zone": availability_zone,
            "cli_preview": (
                f"리더 인스턴스 추가 (scale-out): 클러스터 {cluster_id}에 "
                f"{new_instance_id!r} 리더를 생성합니다 (클래스 {instance_class})"
                + (f", AZ {availability_zone}" if availability_zone else "")
                + ". 신규 인스턴스는 과금 대상이며 생성에 수 분이 걸립니다."
            ),
        }

    guard = verify_approval(
        approval_id, cluster_id, "add_reader_instance",
        payload={"cluster_id": cluster_id, "new_instance_id": new_instance_id,
                 "instance_class": instance_class, "availability_zone": availability_zone},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # The class was resolved in PREVIEW and hash-bound by the approval, so execute
    # uses the exact class the DBA approved — never a post-approval lookup.
    if not instance_class:
        return {"status": "add_failed", "cluster_id": cluster_id,
                "reason": "instance_class가 승인에 바인딩되지 않았습니다 — 미리보기가 제안한 클래스로 다시 승인 요청하세요."}

    rds = client_for_cluster(cluster_id, "rds")
    try:
        dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        print(f"[add_reader_instance] describe_db_clusters failed for {cluster_id}: {e}")
        return {"status": "add_failed", "cluster_id": cluster_id,
                "reason": "클러스터 조회에 실패했습니다 — 대상 클러스터 식별자를 확인하세요."}

    real_cluster_id = dbc.get("DBClusterIdentifier") or cluster_id
    engine = dbc.get("Engine")

    params = {
        "DBInstanceIdentifier": new_instance_id,
        "DBClusterIdentifier": real_cluster_id,
        "Engine": engine,
        "DBInstanceClass": instance_class,
        "Tags": [{"Key": "dbops:managed", "Value": "scale-out"}],
    }
    if availability_zone:
        params["AvailabilityZone"] = availability_zone

    try:
        rds.create_db_instance(**params)
    except Exception as e:
        print(f"[add_reader_instance] create_db_instance failed for {new_instance_id}: {e}")
        return {"status": "add_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 추가에 실패했습니다 (식별자 중복·용량·권한 확인)."}

    return {
        "status": "instance_added",
        "cluster_id": cluster_id,
        "instance_id": new_instance_id,
        "instance_class": instance_class,
        "availability_zone": availability_zone or None,
        "db_status": "creating",
    }
