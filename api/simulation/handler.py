"""Simulation REST API — exposes the Simulation MCP tool surface to the
dashboard UI.

The agent path (AgentCore Gateway → MCP) still serves these tools to the
chat agent. This handler is a thin REST mirror so the static frontend can
render "what-if" panels without going through the agent. The two surfaces
implement the same logic independently (vs sharing code) because the
Lambda code asset is sandboxed per-function; if the impls drift we'll
catch it via parallel unit tests in tests/unit/api/simulation/."""

import json
import os
import traceback

import boto3
from aurora_pricing import price_per_acu_hour, price_per_instance_hour
from ddl_estimator import estimate_ddl, resolve_table
from parameter_estimator import (
    PARAMETER_INFO,
    build_live_result,
    describe_all_parameters,
    static_fallback,
)
from upgrade_estimator import classify_upgrade, estimate_upgrade

# ---------------------------------------------------------------------------
# Cache (PG) helper — minimal Data API wrapper. Inlined rather than imported
# from mcp-servers to keep this Lambda's code asset self-contained.
# ---------------------------------------------------------------------------


def _rds_data():
    return boto3.client("rds-data")


def _cache_query(sql: str, params: dict | None = None) -> list[dict]:
    rds_data = _rds_data()
    sql_params = []
    for k, v in (params or {}).items():
        if v is None:
            sql_params.append({"name": k, "value": {"isNull": True}})
        elif isinstance(v, bool):
            sql_params.append({"name": k, "value": {"booleanValue": v}})
        elif isinstance(v, int):
            sql_params.append({"name": k, "value": {"longValue": v}})
        elif isinstance(v, float):
            sql_params.append({"name": k, "value": {"doubleValue": v}})
        else:
            sql_params.append({"name": k, "value": {"stringValue": str(v)}})

    resp = rds_data.execute_statement(
        resourceArn=os.environ["CACHE_DB_CLUSTER_ARN"],
        secretArn=os.environ["CACHE_DB_SECRET_ARN"],
        database=os.environ.get("CACHE_DB_NAME", "dbops"),
        sql=sql,
        parameters=sql_params,
    )
    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for record in resp.get("records", []):
        row = {}
        for col, field in zip(cols, record, strict=False):
            if field.get("isNull"):
                row[col] = None
            else:
                # Data API returns one populated typed key per cell.
                row[col] = next(
                    (v for k, v in field.items() if k != "isNull"), None
                )
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Tool: check_upgrade_compatibility
# ---------------------------------------------------------------------------


def _check_upgrade_compatibility(cluster_id: str, target_version: str) -> dict:
    rows = _cache_query(
        "SELECT engine, engine_version FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    current = rows[0] if rows else {}
    engine = current.get("engine") or "aurora-postgresql"
    current_version = current.get("engine_version") or ""

    rds = boto3.client("rds")
    upgradable: list[str] = []
    target_desc = ""
    target_release_date = ""

    if current_version:
        try:
            cur_resp = rds.describe_db_engine_versions(
                Engine=engine, EngineVersion=current_version
            )
            for v in cur_resp.get("DBEngineVersions", []):
                for t in v.get("ValidUpgradeTarget", []):
                    upgradable.append(t["EngineVersion"])
        except Exception:
            pass

    try:
        tgt_resp = rds.describe_db_engine_versions(
            Engine=engine, EngineVersion=target_version
        )
        if tgt_resp.get("DBEngineVersions"):
            ev = tgt_resp["DBEngineVersions"][0]
            target_desc = ev.get("DBEngineVersionDescription", "")
            rd = ev.get("CreateTime")
            if rd:
                target_release_date = rd.isoformat()
    except Exception:
        target_desc = ""

    return {
        "cluster_id": cluster_id,
        "engine": engine,
        "current_version": current_version or "unknown",
        "target_version": target_version,
        "is_compatible": target_version in upgradable,
        "valid_upgrade_targets": upgradable[:12],
        "target_description": target_desc,
        "target_release_date": target_release_date,
    }


# ---------------------------------------------------------------------------
# Tool: estimate_upgrade_impact
# ---------------------------------------------------------------------------

def _resolve_upgrade_readers(cluster_id: str) -> int:
    """Live reader count from describe_db_clusters (local account), or 0.

    Mirrors the MCP tool's topology signal. Degrades to 0 on any failure
    (unregistered/unreachable cluster, perms) so the estimate never breaks.
    """
    try:
        resp = boto3.client("rds").describe_db_clusters(DBClusterIdentifier=cluster_id)
        members = resp.get("DBClusters", [{}])[0].get("DBClusterMembers", [])
        return sum(1 for m in members if not m.get("IsClusterWriter"))
    except Exception:
        return 0


def _resolve_table_count(cluster_id: str):
    """Object-count proxy: distinct tables in the latest table_stats snapshot.

    Object count — not raw storage — dominates MAJOR upgrade duration, so we
    read it from the ETL's ``table_stats`` cache. Returns ``None`` when
    unavailable so the estimator flags low confidence rather than assuming 0.
    """
    try:
        rows = _cache_query(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT DISTINCT schema_name, table_name FROM table_stats"
            "  WHERE cluster_id = :cluster_id"
            "    AND snapshot_time = ("
            "      SELECT MAX(snapshot_time) FROM table_stats WHERE cluster_id = :cluster_id"
            "    )"
            ") t",
            {"cluster_id": cluster_id},
        )
        if not rows:
            return None
        n = rows[0].get("n")
        if n is None:
            return None
        n = int(n)
        return n if n > 0 else None
    except Exception:
        return None


def _estimate_upgrade_impact(cluster_id: str, target_version: str) -> dict:
    """Per-method upgrade impact via the shared object-count-driven model.

    MINOR upgrades cost ~a writer reboot (size-independent); MAJOR upgrades
    scale with the live OBJECT COUNT (table_stats) + major-version jump +
    readers. Method changes downtime (blue/green = sub-minute switchover;
    in-place = the upgrade window). Each method carries a range, and the
    response carries confidence + the factors used + a methodology note.
    """
    rows = _cache_query(
        "SELECT engine, engine_version, storage_size_gb FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    cluster = rows[0] if rows else {}
    try:
        storage_gb = float(cluster.get("storage_size_gb") or 50)
    except (TypeError, ValueError):
        storage_gb = 50.0

    readers = _resolve_upgrade_readers(cluster_id)
    table_count = _resolve_table_count(cluster_id)

    est = estimate_upgrade(
        engine=cluster.get("engine") or "aurora-postgresql",
        current_version=cluster.get("engine_version") or "unknown",
        target_version=target_version,
        storage_gb=storage_gb,
        readers=readers,
        table_count=table_count,
    )

    return {
        "cluster_id": cluster_id,
        "current_version": cluster.get("engine_version") or "unknown",
        "target_version": target_version,
        "engine": est["engine"],
        "upgrade_type": est["upgrade_type"],
        "major_jump": est["major_jump"],
        "storage_gb": storage_gb,
        "readers": readers,
        "table_count": table_count,
        "object_count_basis": est["object_count_basis"],
        "confidence": est["confidence"],
        "methods": est["methods"],
        "recommendation": est["recommendation"],
        "recommendation_reason": est["recommendation_reason"],
        "methodology_note": est["methodology_note"],
    }


# ---------------------------------------------------------------------------
# Tool: generate_upgrade_plan
# ---------------------------------------------------------------------------


def _generate_upgrade_plan(
    cluster_id: str, target_version: str, method: str = "blue_green"
) -> dict:
    if method not in ("blue_green", "in_place", "clone"):
        method = "blue_green"

    # Gather real signals up front: engine/version/storage, live reader count,
    # and the object count (table_stats) that drives a major's duration. Steps
    # then mirror the MCP plan tool so the agent and the dashboard never drift.
    meta = _cache_query(
        "SELECT engine, engine_version, storage_size_gb FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    cluster = meta[0] if meta else {}
    current_version = cluster.get("engine_version") or "unknown"
    engine = cluster.get("engine") or "aurora-postgresql"
    try:
        storage_gb = float(cluster.get("storage_size_gb") or 50)
    except (TypeError, ValueError):
        storage_gb = 50.0
    readers = _resolve_upgrade_readers(cluster_id)
    table_count = _resolve_table_count(cluster_id)

    upgrade_type = classify_upgrade(current_version, target_version)
    is_major = upgrade_type == "major"
    # Engine from cluster_meta (authoritative) — a MySQL major must NOT get a
    # PG-only pg_upgrade step.
    is_postgres = "postgres" in engine.lower()

    steps: list[dict] = []

    def add(action: str, details: str) -> None:
        steps.append({"step": len(steps) + 1, "action": action, "details": details})

    # --- Common pre-flight ---
    add("사전 체크", "클러스터 상태 확인 · 진행 중인 유지보수 / 백업 윈도우 충돌 여부 확인")
    add("백업 확인", "최신 자동 백업 존재 확인, 필요시 수동 스냅샷 생성")
    add("파라미터 호환성", f"현재 파라미터 그룹이 {target_version}에 호환되는지 확인")

    # --- Major-only preparation (needed in BOTH method branches) ---
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

    add("애플리케이션 준비", "커넥션 재시도 로직 / 백오프 / read-only fallback 확인")

    # --- Method-specific execution ---
    if method == "blue_green":
        add(
            "Blue/Green 배포 생성",
            f"aws rds create-blue-green-deployment --source {cluster_id} --target-engine-version {target_version}",
        )
        add("Green 환경 검증", "Green 환경에서 핵심 read/write 쿼리 실행 · 응답 시간 비교")
        if readers > 0:
            add(
                "리더 복제 검증",
                f"Green의 리더 {readers}개가 재생성/업그레이드된 뒤 replica lag·복제 상태 점검",
            )
        add("전환 (Switchover)", "트래픽을 Green으로 전환 (~30초 다운타임)")
        add("검증", "애플리케이션 정상 동작 확인 · 메트릭 모니터링")
        add("정리", "롤백 불필요 시 Blue 환경 삭제")
        rollback = "Blue 환경이 유지되므로 전환 취소(switchover-rollback)로 즉시 복귀 가능"
    elif method == "clone":
        add("클러스터 클론 생성", f"{cluster_id}의 fast clone 생성 (원본 데이터/트래픽에 영향 없음)")
        add("클론 업그레이드", f"클론 클러스터를 {target_version}으로 업그레이드 (원본 무영향)")
        add("클론 검증", "클론에서 핵심 쿼리·성능 검증, 비호환 여부 확인")
        if readers > 0:
            add("리더 검증", f"클론의 리더 {readers}개 replica lag·복제 상태 점검")
        add("엔드포인트 전환", "애플리케이션을 클론 클러스터 엔드포인트로 전환 (DNS/설정)")
        add("검증", "애플리케이션 정상 동작 확인 · 메트릭 모니터링")
        rollback = "원본 클러스터가 유지되므로 DNS 전환으로 롤백"
    else:  # in_place
        add(
            "In-place 업그레이드 실행",
            f"aws rds modify-db-cluster --db-cluster-identifier {cluster_id} --engine-version {target_version} --apply-immediately",
        )
        add("대기", "업그레이드 완료까지 클러스터 status=upgrading 모니터링")
        if readers > 0:
            add(
                "리더 업그레이드 검증",
                f"리더 {readers}개가 함께 업그레이드된 뒤 replica lag·복제 상태 점검",
            )
        add("검증", "버전 확인, 애플리케이션 정상 동작 확인")
        rollback = "스냅샷 복원으로만 롤백 가능 — 시간 소요. 적용 전 스냅샷 필수."

    # Time from the shared object-count-driven model (not len(steps)*5).
    est = estimate_upgrade(
        engine=engine,
        current_version=current_version,
        target_version=target_version,
        storage_gb=storage_gb,
        readers=readers,
        table_count=table_count,
    )
    chosen = next((m for m in est["methods"] if m["method"] == method), est["methods"][0])

    return {
        "cluster_id": cluster_id,
        "current_version": current_version,
        "target_version": target_version,
        "engine": est["engine"],
        "upgrade_type": est["upgrade_type"],
        "readers": readers,
        "table_count": table_count,
        "method": method,
        "steps": steps,
        "rollback_plan": rollback,
        "estimated_total_minutes": chosen["estimated_minutes"],
        "estimated_range_minutes": [chosen["range_low_minutes"], chosen["range_high_minutes"]],
        "downtime_text": chosen["downtime_text"],
        "confidence": est["confidence"],
        "object_count_basis": est["object_count_basis"],
        "methodology_note": est["methodology_note"],
    }


# ---------------------------------------------------------------------------
# Tool: simulate_parameter_change
# ---------------------------------------------------------------------------

def _simulate_parameter_change(
    cluster_id: str, parameter_name: str, new_value: str
) -> dict:
    """REST mirror — reads the cluster's LIVE parameter group (same shared
    derivation as the MCP tool) instead of a static catalog, so the dashboard
    reports the real ApplyType/IsModifiable/AllowedValues. Degrades to the
    coarse heuristic only when the live describe is unavailable."""
    try:
        rds = boto3.client("rds")
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception as e:
        return static_fallback(cluster_id, parameter_name, new_value, f"live describe unavailable: {e}")

    pg_name = cluster.get("DBClusterParameterGroup") or ""
    if not pg_name:
        return static_fallback(cluster_id, parameter_name, new_value, "no parameter group on cluster")
    if pg_name.startswith("default."):
        return static_fallback(cluster_id, parameter_name, new_value, "AWS-default parameter group")

    try:
        params = describe_all_parameters(rds, pg_name)
    except Exception as e:
        return static_fallback(cluster_id, parameter_name, new_value, f"live describe unavailable: {e}")

    row = next((p for p in params if p.get("ParameterName") == parameter_name), None)
    if row is None:
        return static_fallback(cluster_id, parameter_name, new_value, "parameter not found in group")

    return build_live_result(cluster_id, parameter_name, new_value, row, pg_name)


def _parameter_catalog() -> list[dict]:
    return [
        {"name": name, **info} for name, info in sorted(PARAMETER_INFO.items())
    ]


# ---------------------------------------------------------------------------
# Tool: simulate_scaling
# ---------------------------------------------------------------------------

# Hours billed per month for a continuously-running instance. 730 =
# 365 * 24 / 12, the AWS convention for monthly estimates.
_HOURS_PER_MONTH = 730


def _cost_impact(current_monthly, proposed_monthly) -> dict:
    """Build the cost_impact block. Any None input means the price was
    unavailable, so the delta/pct are also None rather than fabricated."""
    if current_monthly is None or proposed_monthly is None:
        return {
            "current_monthly_usd": (
                round(current_monthly, 2) if current_monthly is not None else None
            ),
            "proposed_monthly_usd": (
                round(proposed_monthly, 2) if proposed_monthly is not None else None
            ),
            "delta_monthly_usd": None,
            "change_pct": None,
        }
    delta = proposed_monthly - current_monthly
    pct = (delta / current_monthly * 100.0) if current_monthly else None
    return {
        "current_monthly_usd": round(current_monthly, 2),
        "proposed_monthly_usd": round(proposed_monthly, 2),
        "delta_monthly_usd": round(delta, 2),
        "change_pct": round(pct, 1) if pct is not None else None,
    }


def _simulate_scaling(
    cluster_id: str,
    new_min_acu: float | None = None,
    new_max_acu: float | None = None,
    new_instance_class: str | None = None,
) -> dict:
    """Estimate the monthly cost impact of a scaling change using REAL AWS
    pricing. Serverless v2 clusters scale by ACU range; provisioned clusters
    scale by instance class. The cluster's mode is detected live from
    describe_db_clusters; pricing comes from the Price List API for the
    cluster's region/engine/edition. Any pricing miss degrades to a cost-free
    estimate (cost None, source "fallback") rather than crashing."""
    region = os.environ.get("AWS_REGION", "")
    # Never crash on a describe failure (unregistered/unreachable cluster, perms):
    # degrade to a cost-free estimate that still matches the response contract.
    try:
        rds = boto3.client("rds")
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = resp.get("DBClusters", [])
        cluster = clusters[0] if clusters else None
    except Exception as e:
        cluster = None
        _describe_err = type(e).__name__
    else:
        _describe_err = None
    if cluster is None:
        mode = "provisioned" if new_instance_class else "serverless"
        cur = {"instance_class": None} if mode == "provisioned" else {"min_acu": None, "max_acu": None}
        prop = (
            {"instance_class": new_instance_class}
            if mode == "provisioned"
            else {"min_acu": new_min_acu, "max_acu": new_max_acu}
        )
        why = (
            f"라이브 클러스터 조회 실패({_describe_err})"
            if _describe_err
            else "describe_db_clusters가 해당 cluster_id를 반환하지 않았습니다"
        )
        return {
            "cluster_id": cluster_id,
            "mode": mode,
            "current": cur,
            "proposed": prop,
            "writers": 0,
            "readers": 0,
            "cost_impact": {
                "current_monthly_usd": None,
                "proposed_monthly_usd": None,
                "delta_monthly_usd": None,
                "change_pct": None,
            },
            "unit_pricing": {
                "kind": "instance" if mode == "provisioned" else "acu",
                "price_per_hour": None,
                "region": region,
                "io_optimized": False,
                "source": "fallback",
            },
            "data_source": "estimate (live describe unavailable)",
            "note": f"{why}로 비용 비교를 생략합니다.",
        }

    engine = cluster.get("Engine")
    io_optimized = cluster.get("StorageType") == "aurora-iopt1"
    members = cluster.get("DBClusterMembers", [])
    writers = sum(1 for m in members if m.get("IsClusterWriter"))
    readers = sum(1 for m in members if not m.get("IsClusterWriter"))
    # Always bill at least one instance even if the API omitted member roles.
    member_count = max(1, writers + readers)

    scaling = cluster.get("ServerlessV2ScalingConfiguration")
    if scaling:
        return _scaling_serverless(
            cluster_id,
            region,
            engine,
            io_optimized,
            scaling,
            writers,
            readers,
            member_count,
            new_min_acu,
            new_max_acu,
        )
    return _scaling_provisioned(
        cluster_id,
        region,
        engine,
        io_optimized,
        cluster_id,
        members,
        writers,
        readers,
        member_count,
        new_instance_class,
    )


def _scaling_serverless(
    cluster_id,
    region,
    engine,
    io_optimized,
    scaling,
    writers,
    readers,
    member_count,
    new_min_acu,
    new_max_acu,
) -> dict:
    cur_min = float(scaling.get("MinCapacity"))
    cur_max = float(scaling.get("MaxCapacity"))
    new_min = float(new_min_acu) if new_min_acu is not None else cur_min
    new_max = float(new_max_acu) if new_max_acu is not None else cur_max

    price = price_per_acu_hour(region, engine, io_optimized)
    if price is None:
        cost_impact = _cost_impact(None, None)
        data_source = "estimate (pricing unavailable)"
        source = "fallback"
    else:
        # Mid-point usage: Serverless v2 scales continuously between min and
        # max, so the long-run average is best approximated by the midpoint.
        # Each member bills its own ACU-hours, so multiply by member_count.
        current_monthly = ((cur_min + cur_max) / 2.0) * price * _HOURS_PER_MONTH * member_count
        proposed_monthly = ((new_min + new_max) / 2.0) * price * _HOURS_PER_MONTH * member_count
        cost_impact = _cost_impact(current_monthly, proposed_monthly)
        data_source = "live (describe_db_clusters) + aws_pricing_api"
        source = "aws_pricing_api"

    return {
        "cluster_id": cluster_id,
        "mode": "serverless",
        "current": {"min_acu": cur_min, "max_acu": cur_max},
        "proposed": {"min_acu": new_min, "max_acu": new_max},
        "writers": writers,
        "readers": readers,
        "cost_impact": cost_impact,
        "unit_pricing": {
            "kind": "acu",
            "price_per_hour": price,
            "region": region,
            "io_optimized": io_optimized,
            "source": source,
        },
        "data_source": data_source,
        "note": (
            f"중간값 ACU 기준 추정치({_HOURS_PER_MONTH}h/month, {member_count}개 인스턴스). "
            "리더는 라이터와 동일한 ACU 범위로 근사했으며, 단가는 리전/IO-Optimized 여부를 반영한 "
            "실제 AWS 가격입니다. ACU 변경은 즉시 적용되며 다운타임이 없습니다."
        ),
    }


def _scaling_provisioned(
    cluster_id,
    region,
    engine,
    io_optimized,
    db_cluster_id,
    members,
    writers,
    readers,
    member_count,
    new_instance_class,
) -> dict:
    # describe_db_clusters members carry DBInstanceClass on some API versions;
    # describe_db_instances is the authoritative source for the class of each
    # member, so resolve current classes from there.
    rds = boto3.client("rds")
    current_classes = []
    try:
        inst_resp = rds.describe_db_instances(
            Filters=[{"Name": "db-cluster-id", "Values": [db_cluster_id]}]
        )
        current_classes = [
            i.get("DBInstanceClass")
            for i in inst_resp.get("DBInstances", [])
            if i.get("DBInstanceClass")
        ]
    except Exception:
        current_classes = [
            m.get("DBInstanceClass") for m in members if m.get("DBInstanceClass")
        ]

    current_class = current_classes[0] if current_classes else None
    proposed_class = new_instance_class or current_class

    # Current monthly = sum of each member's real per-hour price * hours.
    current_monthly = None
    current_prices = []
    for cls in current_classes:
        p = price_per_instance_hour(region, engine, cls, io_optimized)
        if p is None:
            current_prices = None
            break
        current_prices.append(p)
    if current_prices is not None and current_prices:
        current_monthly = sum(current_prices) * _HOURS_PER_MONTH

    # Proposed: resize every member to new_instance_class (if given), else keep
    # current. member_count * price * hours.
    proposed_price = (
        price_per_instance_hour(region, engine, proposed_class, io_optimized)
        if proposed_class
        else None
    )
    if new_instance_class:
        proposed_monthly = (
            member_count * proposed_price * _HOURS_PER_MONTH
            if proposed_price is not None
            else None
        )
    else:
        # No resize requested -> proposed cost equals current cost.
        proposed_monthly = current_monthly

    cost_impact = _cost_impact(current_monthly, proposed_monthly)
    if cost_impact["current_monthly_usd"] is None and cost_impact["proposed_monthly_usd"] is None:
        data_source = "estimate (pricing unavailable)"
        source = "fallback"
        unit_price = None
    else:
        data_source = "live (describe_db_clusters) + aws_pricing_api"
        source = "aws_pricing_api"
        unit_price = proposed_price if proposed_price is not None else (
            current_prices[0] if current_prices else None
        )

    return {
        "cluster_id": cluster_id,
        "mode": "provisioned",
        "current": {"instance_class": current_class},
        "proposed": {"instance_class": proposed_class},
        "writers": writers,
        "readers": readers,
        "cost_impact": cost_impact,
        "unit_pricing": {
            "kind": "instance",
            "price_per_hour": unit_price,
            "region": region,
            "io_optimized": io_optimized,
            "source": source,
        },
        "data_source": data_source,
        "note": (
            f"프로비저닝 인스턴스 {member_count}개 기준 추정치({_HOURS_PER_MONTH}h/month). "
            "단가는 리전/IO-Optimized 여부를 반영한 실제 AWS 가격입니다. 인스턴스 클래스 변경은 "
            "재시작(또는 failover)을 동반할 수 있습니다."
        ),
    }


# ---------------------------------------------------------------------------
# Tool: simulate_ddl_impact
# ---------------------------------------------------------------------------

def _simulate_ddl_impact(cluster_id: str, ddl_sql: str) -> dict:
    """REST mirror of the DDL impact tool — shares the object/size + instance-
    derived-throughput model with the MCP tool (no more row_count/100k*5)."""
    table = resolve_table(ddl_sql)

    row_count = 0
    table_bytes = 0
    if table:
        rows = _cache_query(
            "SELECT n_live_tup, total_bytes FROM table_stats "
            "WHERE cluster_id = :cluster_id AND lower(table_name) = :table_name "
            "ORDER BY snapshot_time DESC LIMIT 1",
            {"cluster_id": cluster_id, "table_name": table.lower()},
        )
        if rows:
            row_count = int(rows[0].get("n_live_tup") or 0)
            table_bytes = int(rows[0].get("total_bytes") or 0)

    size_mb = table_bytes / (1024 * 1024) if table_bytes else 0.0

    # Instance class grounds the throughput estimate (vs the old flat 40 MB/s).
    meta = _cache_query(
        "SELECT instance_class FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    instance_class = meta[0].get("instance_class") if meta else None

    est = estimate_ddl(
        ddl_sql=ddl_sql,
        table=table,
        row_count=row_count,
        size_mb=size_mb,
        instance_class=instance_class,
        io_optimized=False,
    )
    return {"cluster_id": cluster_id, **est}


# ---------------------------------------------------------------------------
# Lambda dispatcher
# ---------------------------------------------------------------------------


def _set_origin(event):
    headers = event.get("headers") or {}
    return headers.get("origin") or headers.get("Origin") or "*"


def _response(status: int, body: dict, origin: str = "*") -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _parse_body(event):
    body = event.get("body")
    if not body:
        return {}
    try:
        return json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        return {}


def lambda_handler(event, context):
    origin = _set_origin(event)
    method = (event.get("requestContext") or {}).get("http", {}).get("method") or event.get(
        "httpMethod", "GET"
    )
    raw_path = event.get("rawPath") or event.get("path") or ""

    if method == "OPTIONS":
        return _response(200, {}, origin)

    try:
        # GET routes
        if raw_path.endswith("/parameter-catalog"):
            return _response(200, {"parameters": _parameter_catalog()}, origin)

        # POST routes — all simulation tools accept a JSON body so the UI
        # can send rich payloads (long DDL strings, decimals for ACU) without
        # URL encoding.
        body = _parse_body(event)
        cluster_id = body.get("cluster_id")
        if not cluster_id:
            return _response(400, {"error": "cluster_id required"}, origin)

        if raw_path.endswith("/upgrade-compatibility"):
            target = body.get("target_version") or ""
            if not target:
                return _response(400, {"error": "target_version required"}, origin)
            return _response(
                200, _check_upgrade_compatibility(cluster_id, target), origin
            )

        if raw_path.endswith("/upgrade-impact"):
            target = body.get("target_version") or ""
            if not target:
                return _response(400, {"error": "target_version required"}, origin)
            return _response(
                200, _estimate_upgrade_impact(cluster_id, target), origin
            )

        if raw_path.endswith("/upgrade-plan"):
            target = body.get("target_version") or ""
            method_name = body.get("method") or "blue_green"
            if not target:
                return _response(400, {"error": "target_version required"}, origin)
            return _response(
                200, _generate_upgrade_plan(cluster_id, target, method_name), origin
            )

        if raw_path.endswith("/parameter-change"):
            pname = body.get("parameter_name") or ""
            pval = body.get("new_value")
            if not pname:
                return _response(400, {"error": "parameter_name required"}, origin)
            if pval is None:
                return _response(400, {"error": "new_value required"}, origin)
            return _response(
                200, _simulate_parameter_change(cluster_id, pname, str(pval)), origin
            )

        if raw_path.endswith("/scaling"):
            new_min = body.get("new_min_acu")
            new_max = body.get("new_max_acu")
            new_instance_class = body.get("new_instance_class")
            return _response(
                200,
                _simulate_scaling(cluster_id, new_min, new_max, new_instance_class),
                origin,
            )

        if raw_path.endswith("/ddl-impact"):
            ddl = body.get("ddl_sql") or ""
            if not ddl.strip():
                return _response(400, {"error": "ddl_sql required"}, origin)
            return _response(200, _simulate_ddl_impact(cluster_id, ddl), origin)

        return _response(404, {"error": f"unknown route: {raw_path}"}, origin)

    except Exception:
        print(f"Simulation handler error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"}, origin)
