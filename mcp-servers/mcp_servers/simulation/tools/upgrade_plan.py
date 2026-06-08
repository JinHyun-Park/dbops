from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster
from mcp_servers.simulation.tools.upgrade_impact import _classify_upgrade

# Time-estimate constants (minutes). The formula is:
#   base + storage_term + reader_term [+ major_uplift]
# where storage_term scales with the data volume that must be copied/upgraded,
# reader_term covers re-creating/upgrading each replica, and major_uplift
# accounts for the extra compatibility/parameter-group work a major needs.
_BASE_MINUTES = 15
_MINUTES_PER_100GB = 6
_MINUTES_PER_READER = 6
_MAJOR_UPLIFT_MINUTES = 30


def _resolve_reader_count(cluster_id: str) -> tuple[int, str]:
    """Resolve the live reader count via cross-account-aware RDS describe.

    Counts non-writer members from ``DBClusterMembers``. Wrapped in try/except:
    a missing reader count must not block plan generation, so on any failure we
    degrade to 0 readers and surface a note. Mirrors the helper in
    upgrade_impact so both tools see the same topology signal.
    """
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        members = resp["DBClusters"][0].get("DBClusterMembers", [])
        readers = sum(1 for m in members if not m.get("IsClusterWriter", False))
        return readers, ""
    except Exception as e:  # pragma: no cover - defensive, exercised via mocks
        return 0, f"리더 수 확인 불가 (0으로 가정): {e}"


def generate_upgrade_plan_impl(cache: CacheClient, cluster_id: str, target_version: str, method: str = "blue_green") -> dict:
    """Generate an upgrade runbook whose steps and time reflect the REAL upgrade.

    Steps are no longer fixed: a MAJOR upgrade adds parameter-group-family
    migration, extension/feature compatibility checks, and (PG) a pg_upgrade
    pre-check that a minor upgrade does not need. Readers add a per-reader
    verification step. Total time is computed from storage + readers + upgrade
    type instead of ``len(steps) * 5``.
    """
    meta_sql = "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    cluster = meta.rows[0] if meta.rows else {}
    current_version = cluster.get("engine_version", "unknown")
    storage_gb = float(cluster.get("storage_size_gb", 50))

    upgrade_type = _classify_upgrade(current_version, target_version)
    readers, reader_note = _resolve_reader_count(cluster_id)
    is_major = upgrade_type == "major"
    # Engine comes from the cluster_meta `engine` column (e.g. "aurora-postgresql"
    # / "aurora-mysql") — authoritative, unlike inferring it from version text
    # (a MySQL "8.0" target would otherwise be misread as a PG major and get a
    # spurious pg_upgrade step).
    is_postgres = "postgres" in (cluster.get("engine") or "").lower()

    steps: list[dict] = []

    def add(action: str, details: str) -> None:
        steps.append({"step": len(steps) + 1, "action": action, "details": details})

    # --- Common pre-flight ---
    add("사전 체크", "클러스터 상태 확인, 진행 중인 유지보수 없는지 확인")
    add("백업 확인", "최신 자동 백업 존재 확인, 필요시 수동 스냅샷 생성")
    add("파라미터 호환성", f"현재 파라미터 그룹이 {target_version}과 호환되는지 확인")

    # --- Major-only preparation (needed in BOTH method branches) ---
    # A major bumps the parameter-group family and may break extensions/SQL, so
    # these checks must run before either blue/green or in-place proceeds.
    if is_major:
        add(
            "파라미터 그룹 패밀리 마이그레이션",
            f"신규 메이저({target_version})용 파라미터/클러스터 파라미터 그룹 패밀리 생성 및 값 이관",
        )
        add(
            "확장(extension)/비호환 기능 호환성 점검",
            "설치된 extension·deprecated 기능·예약어/타입 변경 등 메이저 비호환 항목 점검",
        )
        if is_postgres:
            add("pg_upgrade 사전 점검", "pg_upgrade --check로 사전 호환성 검증, 비호환 객체 식별")

    add("애플리케이션 준비", "연결 재시도 로직 확인, 읽기 전용 모드 전환 준비")

    # --- Method-specific execution ---
    if method == "blue_green":
        add(
            "Blue/Green 배포 생성",
            f"aws rds create-blue-green-deployment --source {cluster_id} --target-engine-version {target_version}",
        )
        add("Green 환경 검증", "Green 환경에서 핵심 쿼리 테스트, 성능 비교")
        if readers > 0:
            add(
                "리더 복제 검증",
                f"Green의 리더 {readers}개가 재생성/업그레이드된 뒤 replica lag·복제 상태 점검",
            )
        add("전환 (Switchover)", "트래픽을 Green으로 전환 (~30초 다운타임)")
        add("검증", "애플리케이션 정상 동작 확인, 메트릭 모니터링")
        add("정리", "Blue 환경 삭제 (롤백 불필요 시)")
    elif method == "clone":
        add(
            "클러스터 클론 생성",
            f"{cluster_id}의 fast clone 생성 (원본 데이터/트래픽에 영향 없음)",
        )
        add("클론 업그레이드", f"클론 클러스터를 {target_version}으로 업그레이드 (원본 무영향)")
        add("클론 검증", "클론에서 핵심 쿼리·성능 검증, 비호환 여부 확인")
        if readers > 0:
            add("리더 검증", f"클론의 리더 {readers}개 replica lag·복제 상태 점검")
        add("엔드포인트 전환", "애플리케이션을 클론 클러스터 엔드포인트로 전환 (DNS/설정)")
        add("검증", "애플리케이션 정상 동작 확인, 메트릭 모니터링")
    else:  # in_place
        add(
            "In-place 업그레이드",
            f"aws rds modify-db-cluster --db-cluster-identifier {cluster_id} --engine-version {target_version} --apply-immediately",
        )
        add("대기", "업그레이드 완료 대기 (클러스터 상태 monitoring)")
        if readers > 0:
            add(
                "리더 업그레이드 검증",
                f"리더 {readers}개가 함께 업그레이드된 뒤 replica lag·복제 상태 점검",
            )
        add("검증", "애플리케이션 정상 동작 확인")

    rollback = {
        "blue_green": "Blue 환경이 유지되므로 전환 취소로 즉시 롤백 가능",
        "in_place": "스냅샷에서 새 클러스터 복원 필요 (시간 소요)",
        "clone": "원본 클러스터가 유지되므로 DNS 전환으로 롤백",
    }

    # Time estimate (minutes): base + storage term + reader term + major uplift.
    # Replaces the old len(steps)*5 heuristic so the number tracks the actual
    # data volume, replica topology, and upgrade class.
    estimated_total_minutes = round(
        _BASE_MINUTES
        + (storage_gb / 100) * _MINUTES_PER_100GB
        + readers * _MINUTES_PER_READER
        + (_MAJOR_UPLIFT_MINUTES if is_major else 0)
    )

    result = {
        "cluster_id": cluster_id,
        "current_version": current_version,
        "target_version": target_version,
        "upgrade_type": upgrade_type,
        "readers": readers,
        "method": method,
        "steps": steps,
        "rollback_plan": rollback.get(method, "수동 복원 필요"),
        "estimated_total_minutes": estimated_total_minutes,
    }
    if reader_note:
        result["reader_note"] = reader_note
    return result
