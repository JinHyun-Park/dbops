"""Aurora 인스턴스 클래스 → 메모리(GB)·vCPU 매핑.

Parameter Fitness 진단의 전제: work_mem/shared_buffers/effective_cache_size
적정성은 "인스턴스 메모리 대비"로만 판단할 수 있는데, RDS describe는 메모리
GB를 직접 주지 않는다(instance_class 문자열만). cluster_meta에 instance_class만
수집되므로 여기서 사양을 역매핑한다.

매핑에 없는 클래스(미래 세대 등)는 (None, None)을 돌려주고, 호출부는 메모리
의존 진단을 건너뛴다 — 틀린 메모리로 잘못된 권고를 내느니 침묵한다.

Serverless v2(db.serverless)는 메모리가 ACU에 비례(1 ACU ≈ 2 GB)해 고정값이
없다 — cluster_meta.serverlessv2_max_acu로 별도 계산하므로 여기선 None.
"""

# 크기 토큰 → (메모리 GB, vCPU). r/m/x 계열(메모리·범용 최적화)의 표준 비율.
# Aurora 권장은 r-family(메모리 최적화)라 이 비율이 대부분의 운영 클러스터를
# 커버한다. t-family(버스터블)는 메모리 비율이 달라 별도 표로 잡는다.
_SIZE_RX = {
    "large": (16, 2),
    "xlarge": (32, 4),
    "2xlarge": (64, 8),
    "4xlarge": (128, 16),
    "8xlarge": (256, 32),
    "12xlarge": (384, 48),
    "16xlarge": (512, 64),
    "24xlarge": (768, 96),
}

# 버스터블 t-family: 메모리가 r-family보다 훨씬 작다(잘못 적용하면 work_mem
# 위험 진단이 과소평가됨).
_T_FAMILY = {
    "db.t3.medium": (4, 2),
    "db.t3.large": (8, 2),
    "db.t4g.medium": (4, 2),
    "db.t4g.large": (8, 2),
}


def instance_memory_gb(instance_class: str):
    """(memory_gb, vcpu) — 매핑 불가 시 (None, None).

    db.serverless는 (None, None) — ACU 기반이라 호출부가 max_acu로 계산해야 한다.
    """
    ic = (instance_class or "").strip().lower()
    if not ic or ic == "db.serverless":
        return (None, None)
    if ic in _T_FAMILY:
        return _T_FAMILY[ic]
    # db.r6g.2xlarge → 크기 토큰 "2xlarge"
    parts = ic.split(".")
    if len(parts) < 3:
        return (None, None)
    size = parts[-1]
    return _SIZE_RX.get(size, (None, None))
