"""Cost-optimization findings — engine-agnostic.

The 17-panel dashboard already shows CPU utilization in the timeseries chart,
but a long-running DBA wants the high-level "right-sized?" question
answered at a glance without scrolling charts. We surface this as a
maintenance finding so it appears in the same ranked list as VACUUM /
bloat / extension issues.

Current rules:
  - cost_oversized                    — avg 7d CPU < 30% AND p95 < 60% on a sized
                                        instance (not Serverless v2 / not burstable t-family)
                                        → recommend one-step downsize
  - cost_serverless_max_too_high      — Serverless v2 cluster whose 7d p95 CPU
                                        suggests the configured max ACU ceiling is
                                        wider than needed (waste under spike pricing)
  - cost_serverless_min_too_low       — Serverless v2 cluster whose min ACU is set
                                        so low it causes per-cycle cold-start latency
                                        (proxy: many idle-to-active transitions)
  - cost_savings_plan_opportunity     — Cost Explorer recommends an SP / RI purchase
                                        that would save >$10/mo on Bedrock or RDS
                                        spend tagged to this DBOps deployment

Storage rightsizing is scaffolded as a no-op for Aurora today (auto-scaled
storage, per-GB-used billing). The `_check_storage_rightsize` entry point
is ready for when the collector grows to cover RDS non-Aurora / DocumentDB
/ DynamoDB — see its docstring for the per-engine plan + the multi-engine
support epic in BACKLOG.md.
"""

import json
from datetime import datetime, timezone

CPU_AVG_THRESHOLD = 30.0
CPU_P95_THRESHOLD = 60.0

# Serverless v2 ACU heuristics, driven by the OBSERVED serverless_acu metric
# (ServerlessDatabaseCapacity) — ACU is exactly what Sv2 bills, so we compare
# real consumed ACU against the configured min/max ceiling, not a CPU proxy.
SV2_MAX_HEADROOM_FACTOR = 0.6  # 7d p95 ACU below (max × this) → ceiling overprovisioned
SV2_SUGGEST_HEADROOM = 1.3     # suggested max = observed p95 ACU × this (burst headroom)
SV2_MIN_RAISE_FACTOR = 0.5     # min ACU < (max × this) gives idle-but-not-too-low feel
SP_SAVINGS_FLOOR_USD = 10.0    # only surface SP recommendations worth >$10/mo

# 이번 실행의 공유 snapshot_time. collect_cost_findings가 handler로부터 받아
# 설정하고 _emit_finding이 읽는다. 같은 ETL 사이클의 health/param_fitness
# finding과 snapshot_time을 맞춰 대시보드 MAX(snapshot_time)에 함께 잡히게
# 한다(collector마다 now()를 따로 찍으면 한 배치만 보이는 버그). Lambda는
# 클러스터를 단일 스레드로 순차 처리하므로 모듈 변수 공유가 안전하다.
_RUN_SNAPSHOT_TS = None


def _execute(rds_data, cluster_arn, secret_arn, db_name, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=db_name,
        sql=f"/* source=dbops-cost */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        out.append(row)
    return out


def _emit_finding(
    rds_data, cluster_arn, secret_arn, db_name,
    cluster_id, check_type, severity, subject, value_str, threshold_str, recommendation, details,
):
    """Single INSERT into cluster_health_findings — used by every cost check."""
    _execute(
        rds_data, cluster_arn, secret_arn, db_name,
        "INSERT INTO cluster_health_findings "
        "  (cluster_id, snapshot_time, check_type, severity, subject, value_str, threshold_str, recommendation, details) "
        "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, :value_str, :threshold_str, :recommendation, :details::jsonb)",
        {
            "cluster_id": cluster_id,
            "ts": _RUN_SNAPSHOT_TS or datetime.now(timezone.utc).isoformat(),
            "check_type": check_type,
            "severity": severity,
            "subject": subject,
            "value_str": value_str,
            "threshold_str": threshold_str,
            "recommendation": recommendation,
            "details": json.dumps(details),
        },
    )


def _check_oversized(rds_data, cache_arn, cache_secret, cache_db, cluster_id, meta, cpu):
    """Provisioned-instance right-sizing — original P3.3 rule."""
    instance_class = meta.get("instance_class") or ""
    ic_lower = instance_class.lower()
    if "serverless" in ic_lower or ic_lower.startswith("db.t"):
        return 0  # Serverless / burstable handled elsewhere or not applicable.
    avg_cpu, p95_cpu, max_cpu = cpu["avg"], cpu["p95"], cpu["max"]
    if not (avg_cpu < CPU_AVG_THRESHOLD and p95_cpu < CPU_P95_THRESHOLD):
        return 0
    _emit_finding(
        rds_data, cache_arn, cache_secret, cache_db,
        cluster_id, "cost_oversized", "info", instance_class or "instance",
        value_str=f"avg CPU {avg_cpu:.1f}% / p95 {p95_cpu:.1f}% / max {max_cpu:.1f}%",
        threshold_str=f"< {CPU_AVG_THRESHOLD:.0f}% avg & < {CPU_P95_THRESHOLD:.0f}% p95 → consider downsize",
        recommendation=(
            f"{instance_class or '이 인스턴스'}의 7일 평균 CPU가 {avg_cpu:.1f}%입니다 — "
            "한 단계 작은 인스턴스를 검토하세요 (보통 월 30-50% 절감). "
            "축소 후 프로덕션 트래픽 1주를 지켜보고 재평가하세요."
        ),
        details={
            "instance_class": instance_class,
            "avg_cpu": avg_cpu,
            "p95_cpu": p95_cpu,
            "max_cpu": max_cpu,
            "window_days": 7,
        },
    )
    return 1


def _check_serverless_v2_acu(rds_data, cache_arn, cache_secret, cache_db, cluster_id, meta, acu):
    """Serverless v2 ACU rightsizing from the OBSERVED serverless_acu metric
    (ServerlessDatabaseCapacity). ACU is exactly what Sv2 bills, so we compare
    real consumed ACU against the configured min/max ceiling — no CPU proxy.
    Skips (returns 0) when there is no ACU history rather than guessing."""
    sv2_min = meta.get("serverlessv2_min_acu")
    sv2_max = meta.get("serverlessv2_max_acu")
    if sv2_max is None:
        return 0  # Not a Serverless v2 cluster.
    sv2_min = float(sv2_min or 0)
    sv2_max = float(sv2_max)
    if sv2_max <= 0 or acu is None:
        return 0

    emitted = 0
    p95_acu, max_acu = acu["p95"], acu["max"]

    # Max ceiling over-provisioned: observed p95 ACU stays well below the configured max.
    if p95_acu < sv2_max * SV2_MAX_HEADROOM_FACTOR:
        suggested_max = max(sv2_min + 2, round(p95_acu * SV2_SUGGEST_HEADROOM, 1))
        suggested_max = min(suggested_max, sv2_max)  # never recommend raising the ceiling
        if suggested_max < sv2_max:
            _emit_finding(
                rds_data, cache_arn, cache_secret, cache_db,
                cluster_id, "cost_serverless_max_too_high", "info",
                f"Serverless v2 (max {sv2_max:.1f} ACU)",
                value_str=f"7d p95 ACU {p95_acu:.1f} / max {max_acu:.1f} (ceiling {sv2_max:.1f})",
                threshold_str=f"p95 ACU < max × {SV2_MAX_HEADROOM_FACTOR:.0%} → ceiling overprovisioned",
                recommendation=(
                    f"max ACU 한도는 {sv2_max:.1f}인데 관측된 7일 p95 소비는 {p95_acu:.1f} ACU입니다. "
                    f"max ACU를 ~{suggested_max:.1f}로 낮춰도 관측 피크를 커버하며, 폭주 시 비용 상한만 줄어듭니다. "
                    "(Serverless v2는 소비 ACU-시간으로 과금됩니다.)"
                ),
                details={
                    "current_min_acu": sv2_min,
                    "current_max_acu": sv2_max,
                    "suggested_max_acu": suggested_max,
                    "p95_acu": p95_acu,
                    "max_acu": max_acu,
                },
            )
            emitted += 1

    # Min floor too low: cluster regularly runs near its ceiling (p95 ACU close to
    # max) but min is set very low → scale-up latency on every cold spike.
    if sv2_min > 0 and sv2_min < sv2_max * SV2_MIN_RAISE_FACTOR and p95_acu > sv2_max * 0.7:
        suggested_min = round(sv2_max * 0.5, 1)
        _emit_finding(
            rds_data, cache_arn, cache_secret, cache_db,
            cluster_id, "cost_serverless_min_too_low", "info",
            f"Serverless v2 (min {sv2_min:.1f} ACU)",
            value_str=f"min={sv2_min:.1f} ACU, 7d p95={p95_acu:.1f} ACU (ceiling {sv2_max:.1f})",
            threshold_str=f"min < max × {SV2_MIN_RAISE_FACTOR:.1f} AND p95 ACU > max × 0.7 → cold-start risk",
            recommendation=(
                f"min ACU {sv2_min:.1f}로는 부하 대비 여유가 없습니다 (7일 p95 {p95_acu:.1f} ACU, 한도 {sv2_max:.1f}). "
                f"min ACU를 ~{suggested_min:.1f}로 올리면 트래픽 스파이크 시 스케일업 지연이 줄어듭니다. "
                "대신 평시 비용 하한이 올라가는 트레이드오프입니다 — cold-start 지연이 아프다면 수용하세요."
            ),
            details={
                "current_min_acu": sv2_min,
                "current_max_acu": sv2_max,
                "suggested_min_acu": suggested_min,
                "p95_acu": p95_acu,
            },
        )
        emitted += 1

    return emitted


# RDS's floor for gp2/gp3 general-purpose storage. Below this there is nowhere to
# go, so an over-allocated 20 GB instance gets NO finding: measured on the standing
# fixtures, dbops-demo-mysql uses 1.8 of 20 GB and dbops-demo-mssql 0.35 of 20 GB,
# and both are already at the floor. A ratio check alone would have produced two
# findings nobody can act on, which is the fastest way to teach a DBA to ignore the
# Cost tab.
_RDS_MIN_STORAGE_GB = 20
# Waste has to be BOTH proportionally large and absolutely large. A 30 GB instance
# using 9 GB is 30% but only 21 GB wasted, which is not worth a migration.
_STORAGE_WASTE_RATIO = 0.35
_STORAGE_MIN_WASTED_GB = 50


def _storage_usage(rds_data, cache_arn, cache_secret, cache_db, cluster_id, allocated_gb):
    """(used_gb, free_gb, samples) from the collected FreeStorageSpace series.

    Uses the MAX free over the window, i.e. the LOW-WATER mark of usage, so a brief
    spike does not make the instance look busy. The cluster-level dimension filter
    is the strict one every other metric_snapshots aggregate uses: without it this
    would mix per-instance rows into the total.
    """
    rows = _execute(
        rds_data, cache_arn, cache_secret, cache_db,
        "SELECT MAX(value) AS max_free, COUNT(*) AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'free_storage_bytes' "
        "  AND ts > NOW() - INTERVAL '7 days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id},
    )
    row = rows[0] if rows else {}
    try:
        samples = int(row.get("samples") or 0)
        free_gb = float(row.get("max_free") or 0) / (1024 ** 3)
    except (TypeError, ValueError):
        return None, None, 0
    if samples < 100 or free_gb <= 0:
        # Not enough of a series to characterise steady-state usage. Say nothing:
        # a storage migration is expensive advice to give on thin data.
        return None, None, samples
    return max(allocated_gb - free_gb, 0.0), free_gb, samples


def _check_storage_rightsize(rds_data, cache_arn, cache_secret, cache_db, cluster_id, meta):
    """Over-allocated PROVISIONED storage on a standalone RDS instance.

    Aurora and DocumentDB are no-ops by design: storage auto-scales and bills per
    GB USED, so there is no allocation to shrink. DynamoDB has no storage sizing at
    all (its analogue is provisioned RCU/WCU, which is a different check and a
    different check_type). So this only ever fires for the rds_instance family,
    where AllocatedStorage is fixed and billed per allocated GB.

    THE RECOMMENDATION CANNOT SAY "SHRINK IT", and the scaffold this replaced was
    wrong about that: RDS allocated storage can only ever be INCREASED. There is no
    ModifyDBInstance that makes it smaller. The only remedies are a migration to a
    new, smaller instance (dump/restore or a read replica promoted after a smaller
    initial allocation) or accepting the cost. Advice a DBA cannot execute is worse
    than no advice, so the finding names the real remedy.

    Storage AUTOSCALING is called out when it is on, because it is how an instance
    silently gets here: allocation grows under load and never comes back down.
    """
    engine = (meta.get("engine") or "").lower()
    if not engine:
        return 0
    # Positive gate: only the provisioned-storage family. Everything else is a
    # deliberate no-op with the reason in the docstring above.
    if ("aurora" in engine or "docdb" in engine or "dynamodb" in engine
            or engine in ("redis", "valkey", "memcached")):
        return 0

    details_json = meta.get("resource_details") or {}
    if isinstance(details_json, str):
        try:
            details_json = json.loads(details_json)
        except (ValueError, TypeError):
            details_json = {}
    try:
        allocated_gb = float(details_json.get("allocated_storage_gb") or 0)
    except (TypeError, ValueError):
        allocated_gb = 0.0
    if allocated_gb <= _RDS_MIN_STORAGE_GB:
        # At or below the floor there is no smaller instance to migrate to.
        return 0

    used_gb, free_gb, samples = _storage_usage(
        rds_data, cache_arn, cache_secret, cache_db, cluster_id, allocated_gb
    )
    if used_gb is None:
        return 0

    ratio = used_gb / allocated_gb if allocated_gb else 1.0
    wasted_gb = allocated_gb - used_gb
    if ratio >= _STORAGE_WASTE_RATIO or wasted_gb < _STORAGE_MIN_WASTED_GB:
        return 0

    # The smallest allocation that still leaves real headroom, floored at the RDS
    # minimum. Not a promise, a starting point for sizing the migration target.
    suggested_gb = max(_RDS_MIN_STORAGE_GB, int(used_gb * 2) + 1)
    storage_type = str(details_json.get("storage_type") or "").lower()
    autoscale_max = details_json.get("max_allocated_storage_gb")

    notes = [
        f"프로비저닝된 스토리지 {allocated_gb:.0f}GB 중 약 {used_gb:.1f}GB만 "
        f"사용 중입니다 (사용률 {ratio * 100:.0f}%, 미사용 {wasted_gb:.0f}GB). "
        f"RDS는 할당 스토리지를 **줄일 수 없습니다**(증가만 가능). 따라서 "
        f"ModifyDBInstance로는 해결되지 않고, 더 작게 할당한 새 인스턴스로 "
        f"마이그레이션(덤프/복원 또는 작은 할당의 read replica 승격)해야 합니다. "
        f"목표 할당량은 현재 사용량의 2배 + 여유인 약 {suggested_gb}GB부터 "
        f"검토하세요."
    ]
    if autoscale_max:
        notes.append(
            f"스토리지 자동 확장이 켜져 있습니다(최대 {autoscale_max}GB). 부하 시 "
            "할당량이 자동으로 늘어나며 이후 자동으로 줄지는 않으므로, 지금 값이 "
            "과거 피크의 결과일 수 있습니다."
        )
    if storage_type == "gp2":
        notes.append(
            "storage_type이 gp2입니다. gp3로 전환하면 마이그레이션 없이 "
            "ModifyDBInstance로 즉시 적용되고, 동일 용량에서 더 저렴하며 baseline "
            "3000 IOPS를 확보합니다. 마이그레이션보다 먼저 검토할 항목입니다."
        )

    _emit_finding(
        rds_data, cache_arn, cache_secret, cache_db, cluster_id,
        "cost_storage_oversized", "info",
        f"스토리지 과다 할당: {allocated_gb:.0f}GB 중 {used_gb:.1f}GB 사용",
        f"사용 {used_gb:.1f}GB / 할당 {allocated_gb:.0f}GB ({ratio * 100:.0f}%)",
        f"사용률 {_STORAGE_WASTE_RATIO * 100:.0f}% 미만 그리고 미사용 "
        f"{_STORAGE_MIN_WASTED_GB}GB 이상",
        " ".join(notes),
        {
            "engine": engine,
            "allocated_storage_gb": allocated_gb,
            "used_storage_gb": round(used_gb, 2),
            "free_storage_gb": round(free_gb, 2),
            "usage_ratio": round(ratio, 4),
            "wasted_gb": round(wasted_gb, 2),
            "suggested_allocation_gb": suggested_gb,
            "storage_type": storage_type or None,
            "storage_autoscaling_max_gb": autoscale_max,
            "samples": samples,
            # Stated in the payload too: a consumer that renders only the numbers
            # must not imply a resize is available.
            "shrink_supported_by_aws": False,
        },
    )
    return 1


def _check_savings_plan_opportunity(rds_data, cache_arn, cache_secret, cache_db, cluster_id):
    """Pull Cost Explorer's Savings Plans recommendation for the account.
    Cached daily in cost_recommendations_cache so we don't repay the
    $0.01-per-request CE fee on every 5-min ETL cycle.

    Note: SP recommendations are account-wide, not cluster-scoped — but we
    record under each registered DBOps cluster so the finding appears on
    every dashboard. The recommendation itself references the workload as
    'DBOps-tagged spend' so users know it's the same opportunity."""
    # Reuse cached recommendation if it's fresh (< 23h old — under 24h so
    # daily CE refresh happens predictably).
    cached = _execute(
        rds_data, cache_arn, cache_secret, cache_db,
        "SELECT estimated_monthly_savings_usd, recommended_action, details, snapshot_time "
        "FROM cost_recommendations_cache "
        "WHERE cluster_id = :cid AND recommendation_type = 'savings_plan' "
        "  AND snapshot_time > NOW() - INTERVAL '23 hours' "
        "LIMIT 1",
        {"cid": cluster_id},
    )
    rec = cached[0] if cached else None
    if not rec:
        # No cached row — fetch from Cost Explorer. Lazy boto3 import so unit
        # tests that don't touch this path don't pay the import cost.
        try:
            import boto3  # type: ignore
            ce = boto3.client("ce", region_name="us-east-1")
            resp = ce.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears="ONE_YEAR",
                PaymentOption="NO_UPFRONT",
                LookbackPeriodInDays="THIRTY_DAYS",
            )
        except Exception as e:
            print(f"[cost] CE SP recommendation failed for {cluster_id}: {e}")
            return 0

        summary = ((resp.get("SavingsPlansPurchaseRecommendation") or {}).get("SavingsPlansPurchaseRecommendationSummary")) or {}
        try:
            monthly = float(summary.get("EstimatedMonthlySavingsAmount") or 0)
        except (TypeError, ValueError):
            monthly = 0.0
        action = ""
        details = {}
        if monthly > 0:
            try:
                hourly_commit = float(summary.get("HourlyCommitmentToPurchase") or 0)
            except (TypeError, ValueError):
                hourly_commit = 0.0
            action = (
                f"시간당 ${hourly_commit:.2f} Compute Savings Plan(1년, 선결제 없음) 커밋 시 "
                f"월 ~${monthly:.2f} 절감이 예상됩니다."
            )
            details = {
                "estimated_monthly_savings_usd": monthly,
                "hourly_commitment_usd": hourly_commit,
                "term_years": 1,
                "payment_option": "NO_UPFRONT",
                "lookback_days": 30,
            }
        _execute(
            rds_data, cache_arn, cache_secret, cache_db,
            "INSERT INTO cost_recommendations_cache "
            "  (cluster_id, recommendation_type, snapshot_time, estimated_monthly_savings_usd, recommended_action, details) "
            "VALUES (:cid, 'savings_plan', NOW(), :savings, :action, :details::jsonb) "
            "ON CONFLICT (cluster_id, recommendation_type) DO UPDATE "
            "  SET snapshot_time = NOW(), "
            "      estimated_monthly_savings_usd = EXCLUDED.estimated_monthly_savings_usd, "
            "      recommended_action = EXCLUDED.recommended_action, "
            "      details = EXCLUDED.details",
            {"cid": cluster_id, "savings": monthly, "action": action, "details": json.dumps(details)},
        )
        rec = {
            "estimated_monthly_savings_usd": monthly,
            "recommended_action": action,
            "details": json.dumps(details),
        }

    savings = float(rec.get("estimated_monthly_savings_usd") or 0)
    if savings < SP_SAVINGS_FLOOR_USD:
        return 0

    details_obj = rec.get("details")
    if isinstance(details_obj, str):
        try:
            details_obj = json.loads(details_obj)
        except Exception:
            details_obj = {}
    _emit_finding(
        rds_data, cache_arn, cache_secret, cache_db,
        cluster_id, "cost_savings_plan_opportunity", "info",
        "Account-level Compute Savings Plan",
        value_str=f"~${savings:.2f}/mo savings projected",
        threshold_str=f"> ${SP_SAVINGS_FLOOR_USD:.0f}/mo savings → worth committing",
        recommendation=(
            (rec.get("recommended_action") or "")
            + " 정확한 시간당 커밋 금액은 Cost Explorer → Savings Plans → Recommendations에서 확인하세요."
        ),
        details=details_obj or {},
    )
    return 1


def collect_cost_findings(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None):
    """Top-level entry — runs every cost check and tallies findings."""
    global _RUN_SNAPSHOT_TS
    _RUN_SNAPSHOT_TS = snapshot_ts
    meta_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        # `engine` and `resource_details` are read for the storage check: it gates
        # on the engine family and takes allocated_storage_gb / storage_type /
        # max_allocated_storage_gb out of the JSONB. Without them that check saw
        # None for both and silently returned 0 for every cluster.
        "SELECT instance_class, engine_mode, serverlessv2_min_acu, serverlessv2_max_acu, "
        "       engine, resource_details "
        "FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    meta = meta_rows[0] if meta_rows else {}

    cpu_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  AVG(value) AS avg_cpu, "
        "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_cpu, "
        "  MAX(value) AS max_cpu, "
        "  COUNT(*) AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type = 'cpu' "
        "  AND ts > NOW() - INTERVAL '7 days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id},
    )
    if not cpu_rows or cpu_rows[0]["samples"] is None or int(cpu_rows[0]["samples"] or 0) < 20:
        # Still run the SP check — it doesn't depend on CPU history.
        sp_emitted = _check_savings_plan_opportunity(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id
        )
        return {
            "cluster_id": cluster_id,
            "skipped": "insufficient_cpu_history",
            "findings_emitted": sp_emitted,
        }

    cpu = {
        "avg": float(cpu_rows[0]["avg_cpu"] or 0),
        "p95": float(cpu_rows[0]["p95_cpu"] or 0),
        "max": float(cpu_rows[0]["max_cpu"] or 0),
    }

    # Observed Serverless v2 ACU (ServerlessDatabaseCapacity) for ACU rightsizing.
    # None when there is no/insufficient history (non-Sv2 or just-registered) — the
    # Sv2 check then skips rather than guessing from CPU.
    acu_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  AVG(value) AS avg_acu, "
        "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_acu, "
        "  MAX(value) AS max_acu, "
        "  COUNT(*) AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type = 'serverless_acu' "
        "  AND ts > NOW() - INTERVAL '7 days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id},
    )
    acu = None
    if acu_rows and acu_rows[0]["samples"] is not None and int(acu_rows[0]["samples"] or 0) >= 20:
        acu = {
            "avg": float(acu_rows[0]["avg_acu"] or 0),
            "p95": float(acu_rows[0]["p95_acu"] or 0),
            "max": float(acu_rows[0]["max_acu"] or 0),
        }

    emitted = 0
    emitted += _check_oversized(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, meta, cpu
    )
    emitted += _check_serverless_v2_acu(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, meta, acu
    )
    emitted += _check_storage_rightsize(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, meta
    )
    emitted += _check_savings_plan_opportunity(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id
    )

    return {
        "cluster_id": cluster_id,
        "instance_class": meta.get("instance_class"),
        "engine_mode": meta.get("engine_mode"),
        "serverlessv2_max_acu": meta.get("serverlessv2_max_acu"),
        "avg_cpu": cpu["avg"],
        "p95_cpu": cpu["p95"],
        "findings_emitted": emitted,
    }
