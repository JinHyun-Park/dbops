"""plan_az_scaleout — READ-ONLY preview for the AZ scale-out runbook (P2-⑥).

Plans `count` Aurora reader instances spread round-robin over the cluster's
healthy AZs, EXCLUDING one chosen AZ (preemptive spread away from an at-risk
AZ). READ-ONLY: no RDS writes, no approval, never in approval_guard.

The trusted API (POST /api/scaleout-az) turns each planned reader into an
add_reader_instance approval (origin="ui"), so the plan resolves a CONCRETE
instance_class + availability_zone for every reader — that's exactly what the
approval payload hash binds, so add_reader_instance's execute refuses an empty
class and never picks one post-approval.

The handler positive-gates this on the relational-only `scale_instance`
capability, so non-relational engines get unsupported_engine before the impl
runs. No str(e) leaks into a returned reason (errors log to CloudWatch).
"""

import re

from mcp_servers.operations.tools.add_reader_instance import _writer_instance_class
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

_MAX_COUNT = 10
# Same RDS identifier rule the restore_finalizer validates against.
_INSTANCE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$")


def _tail(cluster_id: str) -> str:
    """Sanitized cluster-id tail for building reader ids (mirrors
    restore_finalizer._make_instance_id sanitization)."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", cluster_id).strip("-")[-40:].strip("-") or "reader"


def _make_reader_id(tail: str, idx: int, taken: set) -> str:
    """`<cluster-tail>-az<n>` sanitized to RDS id rules, bumped until unique
    across the batch AND not colliding with an existing member id."""
    n = idx
    while True:
        sid = re.sub(r"-{2,}", "-", f"{tail}-az{n}").strip("-")
        if not _INSTANCE_ID_RE.match(sid):
            sid = f"reader-az{n}"
        sid = sid[:63].rstrip("-")
        if sid not in taken:
            taken.add(sid)
            return sid
        n += 1


def plan_az_scaleout_impl(
    cache: CacheClient,
    cluster_id: str,
    exclude_az: str = "",
    count: int = 1,
    instance_class: str = "",
    **_ignored,
) -> dict:
    exclude_az = (exclude_az or "").strip()
    instance_class = (instance_class or "").strip()
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    clamped = count > _MAX_COUNT
    count = max(1, min(count, _MAX_COUNT))

    try:
        rds = client_for_cluster(cluster_id, "rds")
        dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        print(f"[plan_az_scaleout] describe failed for {cluster_id}: {e}")
        return {"status": "error", "cluster_id": cluster_id,
                "reason": "클러스터 조회에 실패했습니다 — 대상 클러스터 식별자를 확인하세요."}

    # The cluster's subnet AZs (where a reader can be placed). Round-robin over
    # these minus the excluded one — an AZ that has NO instance yet is a valid
    # spread target (that's the point of a preemptive spread).
    cluster_azs = [a for a in (dbc.get("AvailabilityZones") or []) if a]
    members = dbc.get("DBClusterMembers") or []
    existing_ids = {m.get("DBInstanceIdentifier") for m in members if m.get("DBInstanceIdentifier")}

    if exclude_az and exclude_az not in cluster_azs:
        return {"status": "invalid_az", "cluster_id": cluster_id,
                "available_azs": cluster_azs,
                "reason": f"제외 AZ {exclude_az!r}가 이 클러스터의 AZ 목록에 없습니다."}

    healthy_azs = [a for a in cluster_azs if a != exclude_az]
    if not healthy_azs:
        return {"status": "no_healthy_az", "cluster_id": cluster_id,
                "available_azs": cluster_azs,
                "reason": "제외 후 남는 정상 AZ가 없습니다."}

    # Resolve the writer's concrete class so each approval binds the exact
    # billable class (same as add_reader_instance's preview). If unresolvable,
    # ask the caller to name it rather than plan readers with an empty class
    # (add_reader_instance's execute would refuse those).
    if not instance_class:
        try:
            instance_class = _writer_instance_class(rds, dbc)
        except Exception as e:
            print(f"[plan_az_scaleout] writer-class lookup failed for {cluster_id}: {e}")
            instance_class = ""
        if not instance_class:
            return {"status": "needs_instance_class", "cluster_id": cluster_id,
                    "available_azs": cluster_azs,
                    "reason": "instance_class를 결정할 수 없습니다 — 명시해 주세요 (예: db.serverless)."}

    tail = _tail(cluster_id)
    taken = set(existing_ids)
    planned_readers = []
    for i in range(count):
        planned_readers.append({
            "new_instance_id": _make_reader_id(tail, i + 1, taken),
            "availability_zone": healthy_azs[i % len(healthy_azs)],
            "instance_class": instance_class,
        })

    result = {
        "status": "planned",
        "cluster_id": cluster_id,
        "exclude_az": exclude_az,
        "instance_class": instance_class,
        "available_azs": cluster_azs,
        "healthy_azs": healthy_azs,
        "planned_readers": planned_readers,
    }
    if clamped:
        result["clamped"] = True
        result["note"] = f"count가 최대 {_MAX_COUNT}개로 제한되었습니다."
    return result
