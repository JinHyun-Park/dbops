from mcp_servers.shared.cache_client import CacheClient


def generate_upgrade_plan_impl(cache: CacheClient, cluster_id: str, target_version: str, method: str = "blue_green") -> dict:
    meta_sql = "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    cluster = meta.rows[0] if meta.rows else {}

    steps = [
        {"step": 1, "action": "사전 체크", "details": "클러스터 상태 확인, 진행 중인 유지보수 없는지 확인"},
        {"step": 2, "action": "백업 확인", "details": "최신 자동 백업 존재 확인, 필요시 수동 스냅샷 생성"},
        {"step": 3, "action": "파라미터 호환성", "details": f"현재 파라미터 그룹이 {target_version}과 호환되는지 확인"},
        {"step": 4, "action": "애플리케이션 준비", "details": "연결 재시도 로직 확인, 읽기 전용 모드 전환 준비"},
    ]

    if method == "blue_green":
        steps.extend([
            {"step": 5, "action": "Blue/Green 배포 생성", "details": f"aws rds create-blue-green-deployment --source {cluster_id} --target-engine-version {target_version}"},
            {"step": 6, "action": "Green 환경 검증", "details": "Green 환경에서 핵심 쿼리 테스트, 성능 비교"},
            {"step": 7, "action": "전환 (Switchover)", "details": "트래픽을 Green으로 전환 (~30초 다운타임)"},
            {"step": 8, "action": "검증", "details": "애플리케이션 정상 동작 확인, 메트릭 모니터링"},
            {"step": 9, "action": "정리", "details": "Blue 환경 삭제 (롤백 불필요 시)"},
        ])
    else:
        steps.extend([
            {"step": 5, "action": "In-place 업그레이드", "details": f"aws rds modify-db-cluster --db-cluster-identifier {cluster_id} --engine-version {target_version} --apply-immediately"},
            {"step": 6, "action": "대기", "details": "업그레이드 완료 대기 (클러스터 상태 monitoring)"},
            {"step": 7, "action": "검증", "details": "애플리케이션 정상 동작 확인"},
        ])

    rollback = {
        "blue_green": "Blue 환경이 유지되므로 전환 취소로 즉시 롤백 가능",
        "in_place": "스냅샷에서 새 클러스터 복원 필요 (시간 소요)",
        "clone": "원본 클러스터가 유지되므로 DNS 전환으로 롤백",
    }

    return {
        "cluster_id": cluster_id,
        "target_version": target_version,
        "method": method,
        "steps": steps,
        "rollback_plan": rollback.get(method, "수동 복원 필요"),
        "estimated_total_minutes": len(steps) * 5,
    }
