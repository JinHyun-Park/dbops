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
import tenancy
from aurora_pricing import price_per_acu_hour, price_per_instance_hour
from ddl_estimator import estimate_ddl, resolve_table
from dynamodb_cost import compute_capacity_cost
from dynamodb_pricing import (
    price_per_million_rru,
    price_per_million_wru,
    price_per_rcu_hour,
    price_per_wcu_hour,
)
from elasticache_pricing import price_per_node_hour
from parameter_estimator import (
    PARAMETER_INFO,
    build_live_result,
    describe_all_parameters,
    static_fallback,
)
from rds_instance_pricing import price_rds_instance_hour, price_rds_storage_month
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
        # REQUIRED: without this the Data API omits columnMetadata, so the
        # column-name → value mapping below produces empty dict rows and every
        # name-based .get() returns None. (Latent bug — all REST simulation
        # cache reads silently returned empty until the DynamoDB cost tool, which
        # has no live-describe fallback, surfaced it.)
        includeResultMetadata=True,
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
# Cluster registry lookup — for tenancy gate
# ---------------------------------------------------------------------------


def _cluster_item(cluster_id: str) -> dict:
    """Fetch {cluster_id, team_id} from the clusters registry for a single
    cluster. Returns {} on miss or infra error (caller's cluster_visible treats
    missing team_id as default-open)."""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not cluster_id or not table_name:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[simulation] cluster lookup failed for {cluster_id}: {e}")
        return {}


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

    # engine is read above and passed in: on standalone RDS MySQL the major
    # boundary is the second component (8.0 -> 8.4 is a major per AWS), so
    # classifying without it calls that a minor. This route has no
    # engine-family gate, so a registered rds_instance cluster reaches here.
    upgrade_type = classify_upgrade(current_version, target_version, engine)
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
    coarse heuristic only when the live describe is unavailable.

    The fallback `reason` becomes the response's ``data_source`` label, so it
    stays a STATIC string: an RDS describe error carries the hub account id, the
    platform role name and the parameter-group ARN, and goes to CloudWatch only."""
    try:
        rds = boto3.client("rds")
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception as e:
        print(f"[simulation] describe_db_clusters failed for {cluster_id}: {e}")
        return static_fallback(cluster_id, parameter_name, new_value, "live describe unavailable")

    pg_name = cluster.get("DBClusterParameterGroup") or ""
    if not pg_name:
        return static_fallback(cluster_id, parameter_name, new_value, "no parameter group on cluster")
    if pg_name.startswith("default."):
        return static_fallback(cluster_id, parameter_name, new_value, "AWS-default parameter group")

    try:
        params = describe_all_parameters(rds, pg_name)
    except Exception as e:
        print(f"[simulation] describe_all_parameters failed for {cluster_id} (group={pg_name}): {e}")
        return static_fallback(cluster_id, parameter_name, new_value, "live describe unavailable")

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
        # Keep the raised-vs-empty distinction (the caller acts on it differently)
        # but as a STATIC flag: neither the message nor the exception class goes
        # into the response. Full detail to CloudWatch.
        print(f"[simulation] scaling describe_db_clusters failed for {cluster_id}: {e}")
        _describe_failed = True
    else:
        _describe_failed = False
    if cluster is None:
        mode = "provisioned" if new_instance_class else "serverless"
        cur = {"instance_class": None} if mode == "provisioned" else {"min_acu": None, "max_acu": None}
        prop = (
            {"instance_class": new_instance_class}
            if mode == "provisioned"
            else {"min_acu": new_min_acu, "max_acu": new_max_acu}
        )
        why = (
            "라이브 클러스터 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요)"
            if _describe_failed
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


def _observed_avg_acu(cluster_id):
    """Average ServerlessDatabaseCapacity over a 14-day lookback (local account),
    or (None, reason). The real serverless bill tracks the observed ACU draw,
    not the min/max midpoint. Fails soft → midpoint fallback."""
    from datetime import datetime, timedelta, timezone
    try:
        cw = boto3.client("cloudwatch")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=14)
        resp = cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="ServerlessDatabaseCapacity",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Average"],
        )
        pts = [d["Average"] for d in resp.get("Datapoints", []) if d.get("Average") is not None]
        if not pts:
            return None, "CloudWatch ServerlessDatabaseCapacity 데이터포인트 없음"
        return sum(pts) / len(pts), "CloudWatch 14일 평균 ACU"
    except Exception as e:
        print(f"[simulation] observed ACU lookup failed for {cluster_id}: {e}")
        return None, "관측 ACU 조회 실패 (자세한 원인은 서버 로그를 확인하세요)"


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
    # Observed average ACU is the real billing driver; midpoint is the fallback.
    observed_acu, acu_basis_note = _observed_avg_acu(cluster_id)

    def _effective(min_acu, max_acu):
        if observed_acu is not None:
            return max(min_acu, min(observed_acu, max_acu))  # same workload, new bounds
        return (min_acu + max_acu) / 2.0  # midpoint fallback

    if price is None:
        cost_impact = _cost_impact(None, None)
        data_source = "estimate (pricing unavailable)"
        source = "fallback"
    else:
        current_monthly = _effective(cur_min, cur_max) * price * _HOURS_PER_MONTH * member_count
        proposed_monthly = _effective(new_min, new_max) * price * _HOURS_PER_MONTH * member_count
        cost_impact = _cost_impact(current_monthly, proposed_monthly)
        data_source = "live (describe_db_clusters) + aws_pricing_api"
        source = "aws_pricing_api"

    acu_basis = "observed" if observed_acu is not None else "midpoint"
    confidence = "high" if (price is not None and observed_acu is not None) else "low"
    basis_phrase = (
        f"관측 평균 {round(observed_acu, 2)} ACU 기준({acu_basis_note})"
        if observed_acu is not None
        else f"중간값 ACU 기준 추정({acu_basis_note} — 관측 ACU 없음)"
    )

    return {
        "cluster_id": cluster_id,
        "mode": "serverless",
        "current": {"min_acu": cur_min, "max_acu": cur_max},
        "proposed": {"min_acu": new_min, "max_acu": new_max},
        "writers": writers,
        "readers": readers,
        "observed_avg_acu": round(observed_acu, 2) if observed_acu is not None else None,
        "acu_basis": acu_basis,
        "confidence": confidence,
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
            f"{basis_phrase}, {_HOURS_PER_MONTH}h/month × {member_count}개 인스턴스. "
            "리더는 라이터와 동일한 ACU로 근사했으며, 단가는 리전/IO-Optimized 여부를 반영한 "
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
    # `engine` decides the online-DDL semantics and which rewrite tooling the
    # recommendation may name; the MCP tool reads the same two columns, so the
    # dashboard and the agent cannot disagree about the same statement.
    meta = _cache_query(
        "SELECT instance_class, engine FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    row = meta[0] if meta else {}
    instance_class = row.get("instance_class")

    est = estimate_ddl(
        ddl_sql=ddl_sql,
        table=table,
        row_count=row_count,
        size_mb=size_mb,
        instance_class=instance_class,
        io_optimized=False,
        engine=row.get("engine"),
    )
    return {"cluster_id": cluster_id, **est}


# ---------------------------------------------------------------------------
# Tool: simulate_dynamodb_capacity_cost (DynamoDB only)
# ---------------------------------------------------------------------------

# 7-day cap on the consumed-capacity lookback (matches the MCP tool).
_DDB_WINDOW_CAP_HOURS = 168


def _ddb_meta(cluster_id: str):
    """(billing_mode, region, table_class, is_global_table) for a DynamoDB table
    from cluster_meta. billing_mode/table_class are in resource_details JSONB;
    region is a top-level column. Absent table_class defaults to STANDARD (pre-fix
    ETL rows won't have it until next collection cycle)."""
    rows = _cache_query(
        "SELECT region, "
        "resource_details->>'billing_mode' AS billing_mode, "
        "resource_details->>'table_class' AS table_class, "
        "resource_details->'global_table_replicas' AS global_table_replicas "
        "FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    if not rows:
        return None, "", "STANDARD", False
    row = rows[0]
    table_class = row.get("table_class") or "STANDARD"
    raw_replicas = row.get("global_table_replicas")
    try:
        replicas = json.loads(raw_replicas) if isinstance(raw_replicas, str) else (raw_replicas or [])
    except (TypeError, ValueError):
        replicas = []
    is_global_table = bool(replicas)
    return row.get("billing_mode"), (row.get("region") or ""), table_class, is_global_table


def _ddb_consumed(cluster_id: str, window_hours: float) -> dict:
    """Per-side consumed aggregates over the window (table-level rows only; the
    GSI rows carry dimensions={"gsi":..} and are excluded). p99 is of the
    per-minute Sum series; datapoints counts the consumed_rcu rows."""
    rows = _cache_query(
        "SELECT "
        "  COUNT(*) FILTER (WHERE metric_type = 'consumed_rcu') AS datapoints, "
        "  COALESCE(SUM(value) FILTER (WHERE metric_type = 'consumed_rcu'), 0) AS sum_rcu, "
        "  COALESCE(SUM(value) FILTER (WHERE metric_type = 'consumed_wcu'), 0) AS sum_wcu, "
        "  COALESCE(percentile_cont(0.99) WITHIN GROUP ("
        "    ORDER BY value) FILTER (WHERE metric_type = 'consumed_rcu'), 0) AS p99_rcu, "
        "  COALESCE(percentile_cont(0.99) WITHIN GROUP ("
        "    ORDER BY value) FILTER (WHERE metric_type = 'consumed_wcu'), 0) AS p99_wcu "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cluster_id "
        "  AND metric_type IN ('consumed_rcu', 'consumed_wcu') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cluster_id": cluster_id, "hours": str(window_hours)},
    )
    row = rows[0] if rows else {}

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    return {
        "datapoints": int(row.get("datapoints") or 0),
        "sum_rcu": _f(row.get("sum_rcu")),
        "sum_wcu": _f(row.get("sum_wcu")),
        "p99_rcu_per_min": _f(row.get("p99_rcu")),
        "p99_wcu_per_min": _f(row.get("p99_wcu")),
    }


def _ddb_provisioned(cluster_id: str):
    """Latest per-second provisioned_rcu/wcu for a PROVISIONED table, or None."""
    rows = _cache_query(
        "SELECT metric_type, value FROM metric_snapshots m "
        "WHERE cluster_id = :cluster_id AND metric_type IN ('provisioned_rcu', 'provisioned_wcu') "
        "  AND (dimensions IS NULL OR dimensions::text = '{}') "
        "  AND ts = (SELECT MAX(ts) FROM metric_snapshots "
        "            WHERE cluster_id = m.cluster_id AND metric_type = m.metric_type "
        "              AND (dimensions IS NULL OR dimensions::text = '{}'))",
        {"cluster_id": cluster_id},
    )
    if not rows:
        return None
    out = {}
    for r in rows:
        try:
            val = float(r.get("value")) if r.get("value") is not None else 0.0
        except (TypeError, ValueError):
            val = 0.0
        if r.get("metric_type") == "provisioned_rcu":
            out["rcu"] = val
        elif r.get("metric_type") == "provisioned_wcu":
            out["wcu"] = val
    return out or None


def _simulate_dynamodb_capacity_cost(
    cluster_id: str, headroom: float = 0.70, window_hours: float = 168
) -> dict:
    """REST mirror of the MCP tool — gathers the consumed series + billing
    mode/region from the cache, resolves both modes' prices via the Price List
    API, and delegates the math to the shared compute. Same JSON shape as the
    MCP tool; never fabricates a dollar number (partial/fallback on a miss)."""
    try:
        window = float(window_hours)
    except (TypeError, ValueError):
        window = _DDB_WINDOW_CAP_HOURS
    window = max(1.0, min(window, _DDB_WINDOW_CAP_HOURS))

    billing_mode, region, table_class, is_global_table = _ddb_meta(cluster_id)
    consumed = _ddb_consumed(cluster_id, window)
    provisioned = _ddb_provisioned(cluster_id) if billing_mode == "PROVISIONED" else None

    prices = {
        "rcu_hr": price_per_rcu_hour(region) if region else None,
        "wcu_hr": price_per_wcu_hour(region) if region else None,
        "m_rru": price_per_million_rru(region) if region else None,
        "m_wru": price_per_million_wru(region) if region else None,
    }

    return compute_capacity_cost(
        cluster_id=cluster_id,
        billing_mode=billing_mode,
        region=region,
        window_hours=window,
        consumed=consumed,
        provisioned=provisioned,
        prices=prices,
        headroom=headroom,
        table_class=table_class,
        is_global_table=is_global_table,
    )


# ---------------------------------------------------------------------------
# Tool: simulate_elasticache_node_resize
# ---------------------------------------------------------------------------

# Hours billed per month (same convention as _HOURS_PER_MONTH for Aurora).
_EC_HOURS_PER_MONTH = 730


def _simulate_elasticache_node_resize(
    cluster_id: str,
    new_node_type: str | None = None,
    new_node_count: int | None = None,
) -> dict:
    """Estimate the monthly cost impact of resizing an ElastiCache node type or
    count using REAL AWS pricing (Price List API — same source as the MCP tool).

    The cluster's current node type + count are resolved live from
    DescribeReplicationGroups in the local account (the ElastiCache cluster
    must be registered in cluster_meta with `engine=elasticache`). A pricing
    miss or describe failure degrades to status=partial rather than crashing.

    The REST arm works identically to the MCP tool but resolves the cluster
    metadata from cluster_meta (cache DB) instead of the registry DDB table,
    because this Lambda has Data API access but no CLUSTERS_TABLE env.
    """
    region = os.environ.get("AWS_REGION", "")

    # Resolve engine + region from cluster_meta.
    rows = _cache_query(
        "SELECT engine, region, resource_name, resource_details "
        "FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    row = rows[0] if rows else {}

    # engine field in cluster_meta for ElastiCache is stored as e.g.
    # "elasticache" (top-level) or the cache engine in resource_details.
    rd_raw = row.get("resource_details")
    rd: dict = {}
    if isinstance(rd_raw, str):
        try:
            import json as _json
            rd = _json.loads(rd_raw)
        except Exception:
            pass
    elif isinstance(rd_raw, dict):
        rd = rd_raw

    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    # Strip "elasticache" wrapper — the underlying engine is redis/valkey/memcached.
    if engine == "elasticache":
        engine = "redis"
    cluster_region = row.get("region") or region
    resource_name = row.get("resource_name") or cluster_id

    # Resolve current node type + count live from DescribeReplicationGroups.
    cur_type: str | None = None
    cur_count: int = 1
    describe_err: str | None = None
    try:
        # Region-scope the client to the cluster's registered region — pricing
        # already uses cluster_region, so describing in the Lambda's default
        # region would mis-resolve a cross-region cluster (not found → a
        # misleading "partial" while pricing was computed for the real region).
        ec = boto3.client("elasticache", region_name=cluster_region)
        rg_resp = ec.describe_replication_groups(ReplicationGroupId=resource_name)
        rg_list = rg_resp.get("ReplicationGroups") or []
        if rg_list:
            rg = rg_list[0]
            cur_type = rg.get("CacheNodeType") or None
            cur_count = len(rg.get("MemberClusters") or []) or 1
    except Exception as e:
        # STATIC reason only: the exception class name is a diagnostic, not
        # something the caller can act on, and the message carries the hub
        # account id / role name. Full detail to CloudWatch.
        print(f"[simulation] elasticache describe failed for {resource_name}: {e}")
        describe_err = "라이브 클러스터 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요)"

    if cur_type is None and describe_err is None:
        # describe succeeded but group not found — a genuinely different, and
        # more actionable, verdict than a failed describe.
        describe_err = "해당 이름의 replication group을 찾을 수 없습니다"

    if cur_type is None:
        return {
            "status": "partial",
            "cluster_id": cluster_id,
            "current": {"node_type": None, "node_count": None, "price_per_hour": None},
            "proposed": {
                "node_type": new_node_type,
                "node_count": new_node_count,
                "price_per_hour": None,
            },
            "current_monthly": None,
            "proposed_monthly": None,
            "delta_monthly": None,
            "delta_pct": None,
            "note": f"{describe_err}. 비용 비교를 생략합니다.",
        }

    eff_node_type = new_node_type or cur_type
    eff_count = int(new_node_count) if new_node_count is not None else cur_count

    cur_price = price_per_node_hour(cluster_region, engine, cur_type)
    new_price = (
        price_per_node_hour(cluster_region, engine, eff_node_type)
        if new_node_type
        else cur_price
    )

    def _monthly(price, count):
        if price is None:
            return None
        return price * max(1, int(count)) * _EC_HOURS_PER_MONTH

    current_monthly = _monthly(cur_price, cur_count)
    proposed_monthly = _monthly(new_price, eff_count)
    status = (
        "ok"
        if current_monthly is not None and proposed_monthly is not None
        else "partial"
    )
    delta = (
        round(proposed_monthly - current_monthly, 2)
        if current_monthly is not None and proposed_monthly is not None
        else None
    )
    delta_pct = (
        round(delta / current_monthly * 100, 1)
        if delta is not None and current_monthly
        else None
    )
    note = (
        "일부 노드 단가를 AWS Price List API에서 확인하지 못해 부분 추정입니다."
        if status == "partial"
        else (
            "노드-시간 비용만 계산했습니다"
            f"(데이터 전송·스냅샷 스토리지·예약 노드 제외, {_EC_HOURS_PER_MONTH}h/월)."
        )
    )

    return {
        "status": status,
        "cluster_id": cluster_id,
        "engine": engine,
        "region": cluster_region,
        "current": {
            "node_type": cur_type,
            "node_count": cur_count,
            "price_per_hour": cur_price,
        },
        "proposed": {
            "node_type": eff_node_type,
            "node_count": eff_count,
            "price_per_hour": new_price,
        },
        "current_monthly": round(current_monthly, 2) if current_monthly is not None else None,
        "proposed_monthly": round(proposed_monthly, 2) if proposed_monthly is not None else None,
        "delta_monthly": delta,
        "delta_pct": delta_pct,
        "pricing_source": "aws_pricing_api" if cur_price is not None else "fallback",
        "note": note,
    }


# ---------------------------------------------------------------------------
# Tool: simulate_rds_instance_rightsizing (REST mirror)
#
# Faithful replica of the reviewed MCP tool
# (mcp_servers/simulation/tools/rds_rightsizing.py). This Lambda can't import
# mcp_servers, so the tiny size-ladder helpers below are copied verbatim from
# scaling_simulation._SIZE_LADDER/_next_class_up + the tool's own
# _next_class_down/_ladder_direction/_license_note. Keep the two in sync.
# ---------------------------------------------------------------------------

# T-family size tokens ("micro"/"small") prepended for rds_instance (RDS
# MySQL/SQL Server) — no Aurora class uses those, so this is additive.
_SIZE_LADDER = [
    "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge", "8xlarge",
    "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge",
]


def _next_class_up(instance_class):
    """The next larger instance class in the same family, or None if it can't
    be resolved (mirror of scaling_simulation._next_class_up)."""
    if not instance_class:
        return None
    prefix, _, size = instance_class.rpartition(".")
    if not prefix:
        return None
    try:
        i = _SIZE_LADDER.index(size)
    except ValueError:
        return None
    if i + 1 >= len(_SIZE_LADDER):
        return None
    return f"{prefix}.{_SIZE_LADDER[i + 1]}"


def _next_class_down(instance_class):
    """One step DOWN the size axis of db.<fam>.<size>, or None at the bottom /
    unknown size (mirror of the MCP tool's _next_class_down)."""
    if not instance_class or not instance_class.startswith("db."):
        return None
    parts = instance_class.split(".")
    if len(parts) != 3:
        return None
    prefix, size = f"{parts[0]}.{parts[1]}", parts[2]
    try:
        i = _SIZE_LADDER.index(size)
    except ValueError:
        return None
    if i == 0:
        return None
    return f"{prefix}.{_SIZE_LADDER[i - 1]}"


def _ladder_direction(cur_class, target):
    """'downsize'/'upsize'/'hold' by size position on _SIZE_LADDER."""
    if not target or target == cur_class:
        return "hold"

    def _idx(ic):
        try:
            return _SIZE_LADDER.index(ic.split(".")[2])
        except (AttributeError, IndexError, ValueError):
            return None

    ci, ti = _idx(cur_class), _idx(target)
    if ci is None or ti is None:
        return "hold"
    return "downsize" if ti < ci else "upsize" if ti > ci else "hold"


def _rds_license_note(engine):
    e = (engine or "").lower()
    if e == "sqlserver-ex":
        return "SQL Server Express — 라이선스 비용 $0 (License Included 요율에 반영)"
    if e.startswith("sqlserver"):
        return "SQL Server 라이선스는 License Included 인스턴스 요율에 포함되어 가격에 반영됨"
    return None


def _simulate_rds_instance_rightsizing(
    cluster_id: str,
    window_hours: int = 168,
    headroom: float = 0.5,
    new_instance_class: str | None = None,
) -> dict:
    """CW-driven RDS-instance (MySQL/SQL Server) right-sizing with real Price
    List cost delta. Read-only. Faithful REST mirror of the MCP tool
    simulate_rds_instance_rightsizing_impl — reads cluster_meta + metric_snapshots
    via the cache (Data API) instead of the mcp_servers cache client.
    Never fabricates a price: any null unit price → pricing_source
    'fallback_estimate' and null costs."""
    if not cluster_id:
        return {"status": "error", "reason": "cluster_id가 필요합니다"}
    try:
        window_hours = max(1, min(int(window_hours or 168), 720))
    except (TypeError, ValueError):
        window_hours = 168
    # headroom is caller-exposed; clamp to [0,1] so it can never collapse or
    # invert the hold band (headroom>1 would push the downsize threshold at or
    # past the 80% upsize threshold → recommending downsize on a hot instance).
    try:
        headroom = max(0.0, min(float(headroom), 1.0))
    except (TypeError, ValueError):
        headroom = 0.5

    meta_rows = _cache_query(
        "SELECT engine, instance_class, region, resource_details "
        "FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    if not (isinstance(meta_rows, list) and meta_rows and isinstance(meta_rows[0], dict)):
        return {"status": "error", "reason": "cluster_meta를 찾지 못했습니다", "cluster_id": cluster_id}
    meta = meta_rows[0]
    engine = (meta.get("engine") or "").lower()
    region = meta.get("region") or ""
    cur_class = meta.get("instance_class") or ""

    # Positive engine guard: only the rds_instance family (RDS MySQL / SQL
    # Server). Anything else (Aurora, DocumentDB, DynamoDB, ElastiCache) is
    # refused cleanly rather than mis-priced.
    if not (engine == "mysql" or engine.startswith("sqlserver")):
        return {
            "status": "unsupported_engine",
            "cluster_id": cluster_id,
            "engine": engine,
            "message": "인스턴스 라이트사이징은 RDS MySQL·SQL Server 인스턴스에서만 지원됩니다.",
        }

    rd = meta.get("resource_details") or {}
    if isinstance(rd, str):
        try:
            rd = json.loads(rd)
        except Exception:
            rd = {}
    if not isinstance(rd, dict):
        rd = {}
    # Keys per rds_instance_cw_collector.details: allocated_storage_gb,
    # storage_type, multi_az. iops is NOT collected → None → gp3 baseline.
    storage_gb = rd.get("allocated_storage_gb") or 0
    storage_type = rd.get("storage_type") or "gp3"
    iops = rd.get("iops")  # not collected today → None → gp3 baseline
    multi_az = bool(rd.get("multi_az"))

    agg = _cache_query(
        "SELECT "
        " PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) FILTER (WHERE metric_type='cpu') AS cpu_p95, "
        " AVG(value) FILTER (WHERE metric_type='cpu') AS cpu_avg, "
        " MAX(value) FILTER (WHERE metric_type='db_connections') AS conn_peak, "
        " PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) FILTER (WHERE metric_type='read_iops') AS read_iops_p95, "
        " PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) FILTER (WHERE metric_type='write_iops') AS write_iops_p95, "
        " MIN(value) FILTER (WHERE metric_type='freeable_memory') AS freeable_mem_min, "
        " COUNT(*) FILTER (WHERE metric_type='cpu') AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND ts >= NOW() - (:h || ' hours')::interval "
        "  AND metric_type IN ('cpu','db_connections','read_iops','write_iops','freeable_memory') "
        # Cluster/instance-level rows only (same strict filter as the MCP twin
        # simulation/tools/rds_rightsizing.py and _cache_query sites above).
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "h": window_hours},
    )
    row = agg[0] if isinstance(agg, list) and agg and isinstance(agg[0], dict) else {}
    cpu_p95 = row.get("cpu_p95")
    samples = row.get("samples") or 0
    if cpu_p95 is None or not samples:
        return {"status": "insufficient_data", "cluster_id": cluster_id,
                "message": "우측 사이징에 필요한 CloudWatch 지표가 아직 충분히 수집되지 않았습니다."}

    conn_peak = row.get("conn_peak") or 0
    mem_min = row.get("freeable_mem_min")
    mem_min_mb = round(mem_min / (1024 * 1024), 1) if isinstance(mem_min, (int, float)) else None
    util = {"cpu_p95": round(cpu_p95, 1), "cpu_avg": round(row.get("cpu_avg") or 0, 1),
            "conn_peak": int(conn_peak), "read_iops_p95": round(row.get("read_iops_p95") or 0, 1),
            "write_iops_p95": round(row.get("write_iops_p95") or 0, 1),
            "freeable_mem_min_mb": mem_min_mb, "window_hours": window_hours, "samples": int(samples)}

    # Recommendation: explicit override wins; else CPU-p95-driven with a hold band.
    if new_instance_class:
        # Explicit override: the action label is resolved AFTER the cost delta
        # (below) so it can never contradict the numbers.
        target, action = new_instance_class, None
        reason = "요청한 인스턴스 클래스로 비용 비교"
    elif cpu_p95 >= 80:
        target = _next_class_up(cur_class) or cur_class
        action = "upsize" if target != cur_class else "hold"
        reason = f"CPU p95 {util['cpu_p95']}% — 한 단계 확대 권장"
    elif cpu_p95 <= min(40 * headroom / 0.5, 75) and conn_peak < 50:
        down = _next_class_down(cur_class)
        target, action = (down, "downsize") if down else (cur_class, "hold")
        reason = (f"CPU p95 {util['cpu_p95']}% · 커넥션 최대 {util['conn_peak']} — 한 단계 축소 여력"
                  if down else "이미 최소 클래스 — 축소 불가")
    else:
        target, action, reason = cur_class, "hold", f"CPU p95 {util['cpu_p95']}% — 현행 유지 적정"

    # edition is resolved INSIDE price_rds_instance_hour from the registry engine
    # (via _RDS_EDITION_LABEL → the Price List `databaseEdition`). Passing the raw
    # engine string ("sqlserver-ex") as edition would match zero SKUs — leave it
    # unset and let the helper map it (matching the pricing module's convention).
    cur_hr = price_rds_instance_hour(region, engine, cur_class, multi_az=multi_az)
    tgt_hr = price_rds_instance_hour(region, engine, target, multi_az=multi_az)
    stor = price_rds_storage_month(region, storage_type, storage_gb, iops)
    storage_usd, iops_usd = stor.get("storage_usd"), stor.get("iops_usd")

    fallback = any(v is None for v in (cur_hr, tgt_hr, storage_usd, iops_usd))

    def _monthly(hr):
        if hr is None or storage_usd is None or iops_usd is None:
            return None
        return round(hr * _HOURS_PER_MONTH + storage_usd + iops_usd, 2)

    cur_monthly, tgt_monthly = _monthly(cur_hr), _monthly(tgt_hr)
    delta = round(tgt_monthly - cur_monthly, 2) if (cur_monthly is not None and tgt_monthly is not None) else None
    pct = round(delta / cur_monthly * 100, 1) if (delta is not None and cur_monthly) else None

    # Explicit-override action (deferred above): label from the real cost
    # direction; fall back to ladder position when pricing degraded (delta None).
    if action is None:
        if delta is not None and delta != 0:
            action = "downsize" if delta < 0 else "upsize"
        else:
            action = _ladder_direction(cur_class, target)

    return {
        "status": "ok", "cluster_id": cluster_id, "engine": engine, "region": region,
        "current": {"instance_class": cur_class, "storage_gb": storage_gb,
                    "storage_type": storage_type, "iops": iops},
        "utilization": util,
        "recommendation": {"action": action, "instance_class": target, "reason": reason},
        "cost_impact": {
            "current_monthly_usd": cur_monthly, "proposed_monthly_usd": tgt_monthly,
            "delta_monthly_usd": delta, "change_pct": pct,
            "breakdown": {
                "compute_current": round(cur_hr * _HOURS_PER_MONTH, 2) if cur_hr is not None else None,
                "compute_proposed": round(tgt_hr * _HOURS_PER_MONTH, 2) if tgt_hr is not None else None,
                "storage": storage_usd, "iops": iops_usd, "license_note": _rds_license_note(engine)},
            "pricing_source": "fallback_estimate" if fallback else "aws_price_list"},
    }


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

        # Tenancy gate: 403 if the caller cannot see this cluster.
        if not tenancy.cluster_visible(event, _cluster_item(cluster_id)):
            return _response(403, {"error": "이 클러스터에 대한 접근 권한이 없습니다."}, origin)

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

        if raw_path.endswith("/dynamodb-capacity-cost"):
            headroom = body.get("headroom")
            window_hours = body.get("window_hours")
            kwargs = {}
            if headroom is not None:
                kwargs["headroom"] = float(headroom)
            if window_hours is not None:
                kwargs["window_hours"] = float(window_hours)
            return _response(
                200,
                _simulate_dynamodb_capacity_cost(cluster_id, **kwargs),
                origin,
            )

        if raw_path.endswith("/elasticache-node-resize"):
            new_node_type = body.get("new_node_type") or None
            new_node_count = body.get("new_node_count")
            if new_node_count is not None:
                try:
                    new_node_count = int(new_node_count)
                except (TypeError, ValueError):
                    new_node_count = None
            return _response(
                200,
                _simulate_elasticache_node_resize(cluster_id, new_node_type, new_node_count),
                origin,
            )

        if raw_path.endswith("/rds-instance-rightsizing"):
            kwargs = {}
            window_hours = body.get("window_hours")
            if window_hours is not None:
                try:
                    kwargs["window_hours"] = int(window_hours)
                except (TypeError, ValueError):
                    pass
            headroom = body.get("headroom")
            if headroom is not None:
                try:
                    kwargs["headroom"] = float(headroom)
                except (TypeError, ValueError):
                    pass
            new_instance_class = body.get("new_instance_class")
            if new_instance_class:
                kwargs["new_instance_class"] = new_instance_class
            return _response(
                200,
                _simulate_rds_instance_rightsizing(cluster_id, **kwargs),
                origin,
            )

        return _response(404, {"error": f"unknown route: {raw_path}"}, origin)

    except Exception:
        print(f"Simulation handler error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"}, origin)
