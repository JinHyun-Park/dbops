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
import re
import traceback

import boto3

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

_UPGRADE_ESTIMATES = {
    "in_place": {
        "base_minutes": 20,
        "per_100gb": 5,
        "downtime_minutes": 8,
        "risk": "moderate",
    },
    "blue_green": {
        "base_minutes": 30,
        "per_100gb": 8,
        "downtime_seconds": 30,
        "risk": "low",
    },
    "clone": {
        "base_minutes": 15,
        "per_100gb": 3,
        "downtime_minutes": 1,
        "risk": "medium",
    },
}


def _estimate_upgrade_impact(cluster_id: str, target_version: str) -> dict:
    rows = _cache_query(
        "SELECT engine_version, storage_size_gb FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    cluster = rows[0] if rows else {}
    try:
        storage_gb = float(cluster.get("storage_size_gb") or 50)
    except (TypeError, ValueError):
        storage_gb = 50.0

    methods = []
    for method, est in _UPGRADE_ESTIMATES.items():
        total_min = est["base_minutes"] + (storage_gb / 100.0) * est["per_100gb"]
        if "downtime_minutes" in est:
            downtime_text = f"~{est['downtime_minutes']}분"
            downtime_seconds = est["downtime_minutes"] * 60
        else:
            downtime_text = f"~{est['downtime_seconds']}초"
            downtime_seconds = est["downtime_seconds"]
        methods.append(
            {
                "method": method,
                "estimated_minutes": int(round(total_min)),
                "downtime_text": downtime_text,
                "downtime_seconds": downtime_seconds,
                "risk": est["risk"],
            }
        )

    return {
        "cluster_id": cluster_id,
        "current_version": cluster.get("engine_version") or "unknown",
        "target_version": target_version,
        "storage_gb": storage_gb,
        "methods": methods,
        "recommendation": "blue_green",
    }


# ---------------------------------------------------------------------------
# Tool: generate_upgrade_plan
# ---------------------------------------------------------------------------


def _generate_upgrade_plan(
    cluster_id: str, target_version: str, method: str = "blue_green"
) -> dict:
    if method not in ("blue_green", "in_place", "clone"):
        method = "blue_green"

    common_pre = [
        {
            "step": 1,
            "action": "사전 체크",
            "details": "클러스터 상태 확인 · 진행 중인 유지보수 / 백업 윈도우 충돌 여부 확인",
        },
        {
            "step": 2,
            "action": "백업 확인",
            "details": "최신 자동 백업 존재 확인, 필요시 수동 스냅샷 생성",
        },
        {
            "step": 3,
            "action": "파라미터 호환성",
            "details": f"현재 파라미터 그룹이 {target_version}에 호환되는지 확인",
        },
        {
            "step": 4,
            "action": "애플리케이션 준비",
            "details": "커넥션 재시도 로직 / 백오프 / read-only fallback 확인",
        },
    ]

    if method == "blue_green":
        post = [
            {
                "step": 5,
                "action": "Blue/Green 배포 생성",
                "details": (
                    f"aws rds create-blue-green-deployment "
                    f"--source {cluster_id} --target-engine-version {target_version}"
                ),
            },
            {
                "step": 6,
                "action": "Green 환경 검증",
                "details": "Green 환경에서 핵심 read/write 쿼리 실행 · 응답 시간 비교",
            },
            {
                "step": 7,
                "action": "전환 (Switchover)",
                "details": "트래픽을 Green으로 전환 (~30초 다운타임)",
            },
            {
                "step": 8,
                "action": "검증",
                "details": "애플리케이션 정상 동작 확인 · 메트릭 모니터링",
            },
            {
                "step": 9,
                "action": "정리",
                "details": "롤백 불필요 시 Blue 환경 삭제",
            },
        ]
        rollback = "Blue 환경이 유지되므로 전환 취소(switchover-rollback)로 즉시 복귀 가능"
    elif method == "clone":
        post = [
            {
                "step": 5,
                "action": "클러스터 복제 (clone)",
                "details": f"aws rds restore-db-cluster-to-point-in-time --source-db-cluster-identifier {cluster_id} ...",
            },
            {
                "step": 6,
                "action": "복제본 업그레이드",
                "details": f"복제본만 {target_version}로 업그레이드 · 원본은 영향 없음",
            },
            {
                "step": 7,
                "action": "트래픽 DNS 전환",
                "details": "DNS 또는 reader endpoint 갱신으로 트래픽 이전",
            },
            {
                "step": 8,
                "action": "원본 정리",
                "details": "안정화 후 원본 클러스터 삭제",
            },
        ]
        rollback = "원본 클러스터가 유지되므로 DNS 롤백으로 복귀"
    else:
        post = [
            {
                "step": 5,
                "action": "In-place 업그레이드 실행",
                "details": (
                    f"aws rds modify-db-cluster --db-cluster-identifier {cluster_id} "
                    f"--engine-version {target_version} --apply-immediately"
                ),
            },
            {
                "step": 6,
                "action": "대기",
                "details": "업그레이드 완료까지 클러스터 status=upgrading 모니터링 (수분~수십분)",
            },
            {
                "step": 7,
                "action": "검증",
                "details": "버전 확인, 애플리케이션 정상 동작 확인",
            },
        ]
        rollback = "스냅샷 복원으로만 롤백 가능 — 시간 소요. 적용 전 스냅샷 필수."

    steps = common_pre + post
    return {
        "cluster_id": cluster_id,
        "target_version": target_version,
        "method": method,
        "steps": steps,
        "rollback_plan": rollback,
        "estimated_total_minutes": len(steps) * 5,
    }


# ---------------------------------------------------------------------------
# Tool: simulate_parameter_change
# ---------------------------------------------------------------------------

_PARAMETER_INFO = {
    "shared_buffers": {"type": "static", "impact": "memory", "restart": True},
    "work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "maintenance_work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "max_connections": {"type": "static", "impact": "connections", "restart": True},
    "effective_cache_size": {"type": "dynamic", "impact": "planner", "restart": False},
    "random_page_cost": {"type": "dynamic", "impact": "planner", "restart": False},
    "checkpoint_timeout": {"type": "dynamic", "impact": "wal", "restart": False},
    "max_wal_size": {"type": "dynamic", "impact": "wal", "restart": False},
    "autovacuum_vacuum_scale_factor": {
        "type": "dynamic",
        "impact": "autovacuum",
        "restart": False,
    },
    "innodb_buffer_pool_size": {"type": "static", "impact": "memory", "restart": True},
    "innodb_lock_wait_timeout": {
        "type": "dynamic",
        "impact": "locking",
        "restart": False,
    },
    "long_query_time": {"type": "dynamic", "impact": "logging", "restart": False},
    "max_user_connections": {"type": "dynamic", "impact": "connections", "restart": False},
    "tmp_table_size": {"type": "dynamic", "impact": "memory", "restart": False},
}


def _simulate_parameter_change(
    cluster_id: str, parameter_name: str, new_value: str
) -> dict:
    info = _PARAMETER_INFO.get(parameter_name)
    known = info is not None
    if not info:
        info = {"type": "unknown", "impact": "unknown", "restart": False}

    if not known:
        recommendation = (
            "이 파라미터는 시뮬레이터 카탈로그에 없음 — RDS console / DB engine docs로 직접 검증 권장"
        )
    elif info["restart"]:
        recommendation = "재시작 필요 — 점검 윈도우에서 수행 · failover-aware 모드 적용 권장"
    else:
        recommendation = "동적 파라미터 — 적용 즉시 반영, 다운타임 없음"

    return {
        "cluster_id": cluster_id,
        "parameter": parameter_name,
        "new_value": new_value,
        "known": known,
        "is_dynamic": info["type"] == "dynamic",
        "requires_restart": bool(info["restart"]),
        "impact_area": info["impact"],
        "recommendation": recommendation,
    }


def _parameter_catalog() -> list[dict]:
    return [
        {"name": name, **info} for name, info in sorted(_PARAMETER_INFO.items())
    ]


# ---------------------------------------------------------------------------
# Tool: simulate_scaling
# ---------------------------------------------------------------------------

# Aurora Serverless v2 list price (USD per ACU-hour, us-east-1 reference).
# Pricing varies by region — we expose the value so the UI can show the
# assumption and DBAs can cross-check against their billing console.
_ACU_PRICE_PER_HOUR = 0.12
_HOURS_PER_MONTH = 730


def _simulate_scaling(
    cluster_id: str,
    new_min_acu: float | None = None,
    new_max_acu: float | None = None,
) -> dict:
    # Pull live ACU range — describe_db_clusters returns ServerlessV2ScalingConfiguration
    # for Serverless v2 clusters. Provisioned clusters won't have this; we fall
    # back to a typical baseline.
    cur_min, cur_max = 0.5, 4.0
    is_serverless = False
    try:
        rds = boto3.client("rds")
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        if resp.get("DBClusters"):
            cfg = resp["DBClusters"][0].get("ServerlessV2ScalingConfiguration")
            if cfg:
                is_serverless = True
                cur_min = float(cfg.get("MinCapacity", cur_min))
                cur_max = float(cfg.get("MaxCapacity", cur_max))
    except Exception:
        pass

    new_min = float(new_min_acu) if new_min_acu is not None else cur_min
    new_max = float(new_max_acu) if new_max_acu is not None else cur_max

    # Cost assumes mid-point utilisation — gives the right qualitative
    # answer (saving vs increase) without pretending to predict real spend.
    current_avg = (cur_min + cur_max) / 2.0
    proposed_avg = (new_min + new_max) / 2.0
    current_monthly = current_avg * _ACU_PRICE_PER_HOUR * _HOURS_PER_MONTH
    proposed_monthly = proposed_avg * _ACU_PRICE_PER_HOUR * _HOURS_PER_MONTH
    delta = proposed_monthly - current_monthly
    pct = ((proposed_monthly - current_monthly) / current_monthly * 100.0) if current_monthly else 0

    warnings = []
    if new_max < cur_max:
        warnings.append(
            "max_acu를 낮추면 burst 트래픽이 들어왔을 때 쿼리 지연이 발생할 수 있음 — 최근 7일 max usage 확인 권장"
        )
    if new_min > new_max:
        warnings.append("min_acu가 max_acu보다 큼 — RDS API 호출 시 거부됨")
    if new_min < 0.5:
        warnings.append("Aurora Serverless v2 최소 ACU는 0.5 — 더 낮추는 것은 불가")
    if not is_serverless:
        warnings.append(
            "이 클러스터는 Serverless v2가 아님 — ACU 시뮬레이션은 추정치이며 실제 비용은 instance class 단가로 계산됨"
        )

    return {
        "cluster_id": cluster_id,
        "is_serverless_v2": is_serverless,
        "current": {"min_acu": cur_min, "max_acu": cur_max},
        "proposed": {"min_acu": new_min, "max_acu": new_max},
        "cost_assumption": (
            f"mid-point usage · ${_ACU_PRICE_PER_HOUR:.3f}/ACU-hour · {_HOURS_PER_MONTH}h/month"
        ),
        "cost_impact": {
            "current_monthly_usd": round(current_monthly, 2),
            "proposed_monthly_usd": round(proposed_monthly, 2),
            "delta_monthly_usd": round(delta, 2),
            "change_pct": round(pct, 1),
        },
        "warnings": warnings,
        "notes": "ACU 변경은 RDS API 호출 시점에 즉시 적용 · 다운타임 없음",
    }


# ---------------------------------------------------------------------------
# Tool: simulate_ddl_impact
# ---------------------------------------------------------------------------

_TABLE_RX = re.compile(
    r"\b(?:ALTER\s+TABLE|CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+CONCURRENTLY)?\s+\S+\s+ON|"
    r"DROP\s+TABLE|TRUNCATE\s+TABLE|REINDEX\s+TABLE|VACUUM(?:\s+FULL)?|CLUSTER)\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_\.\"]*)",
    re.IGNORECASE,
)


def _simulate_ddl_impact(cluster_id: str, ddl_sql: str) -> dict:
    ddl_upper = ddl_sql.strip().upper()
    table = None
    m = _TABLE_RX.search(ddl_sql)
    if m:
        table = m.group(1).strip().strip('"').split(".")[-1]

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

    size_mb = round(table_bytes / (1024 * 1024), 1) if table_bytes else 0.0

    # Heuristic timing — wall-clock varies enormously by workload, but this
    # gives DBAs a rough order of magnitude for go/no-go.
    estimated_seconds = max(1, int(row_count / 100_000 * 5))

    online_ddl = any(
        kw in ddl_upper
        for kw in (
            "ADD COLUMN",
            "ADD INDEX",
            "CREATE INDEX CONCURRENTLY",
            "CREATE UNIQUE INDEX CONCURRENTLY",
        )
    ) and "DROP" not in ddl_upper

    if "CREATE INDEX CONCURRENTLY" in ddl_upper:
        lock_type = "share update exclusive (concurrent)"
        recommendation = "온라인 인덱스 빌드 — 서비스 영향 거의 없음, 빌드 시간만큼 길어짐"
    elif online_ddl:
        lock_type = "share update exclusive"
        recommendation = "온라인 DDL 가능 — 짧은 AccessShare 충돌만 발생"
    elif any(x in ddl_upper for x in ("ALTER COLUMN TYPE", "ALTER TABLE", "REINDEX")):
        lock_type = "access exclusive"
        recommendation = "테이블 전체 락 — 점검 윈도우에서 수행 · pg_repack 또는 BG 마이그레이션 검토"
    elif "VACUUM FULL" in ddl_upper or "CLUSTER" in ddl_upper:
        lock_type = "access exclusive"
        recommendation = "테이블 전체 락 + 디스크 2배 사용 — pg_repack 강력 권장"
    elif "TRUNCATE" in ddl_upper or "DROP TABLE" in ddl_upper:
        lock_type = "access exclusive"
        recommendation = "비가역 작업 — 백업 확인 후 점검 윈도우에서 수행"
    else:
        lock_type = "unknown"
        recommendation = "구문 해석 실패 — 별도 검증 필요"

    return {
        "cluster_id": cluster_id,
        "ddl": ddl_sql,
        "table": table or "unknown",
        "table_info": {"rows": row_count, "size_mb": size_mb},
        "estimated_seconds": estimated_seconds,
        "online_ddl_possible": online_ddl,
        "lock_type": lock_type,
        "disk_space_needed_mb": round(size_mb * 2.0, 1)
        if ("VACUUM FULL" in ddl_upper or "CLUSTER" in ddl_upper)
        else (round(size_mb * 1.2, 1) if "INDEX" in ddl_upper else 0.0),
        "recommendation": recommendation,
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
            return _response(200, _simulate_scaling(cluster_id, new_min, new_max), origin)

        if raw_path.endswith("/ddl-impact"):
            ddl = body.get("ddl_sql") or ""
            if not ddl.strip():
                return _response(400, {"error": "ddl_sql required"}, origin)
            return _response(200, _simulate_ddl_impact(cluster_id, ddl), origin)

        return _response(404, {"error": f"unknown route: {raw_path}"}, origin)

    except Exception:
        print(f"Simulation handler error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"}, origin)
