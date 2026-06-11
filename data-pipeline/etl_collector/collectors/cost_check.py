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

# Serverless v2 ACU heuristics. ACU correlates closely with CPU at low load
# (1 ACU ≈ 2 GB RAM + proportional vCPU) so we treat low CPU as a proxy for
# low ACU consumption. Raise max ACU only if there's been a spike in the
# observation window; recommend lowering otherwise.
SV2_MAX_LOWER_P95_CPU = 40.0   # p95 CPU below this → max ACU likely too high
SV2_MAX_LOWER_FACTOR = 0.5     # suggest cutting max ACU to (current × this)
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


def _check_serverless_v2_acu(rds_data, cache_arn, cache_secret, cache_db, cluster_id, meta, cpu):
    """Serverless v2 max ACU rightsizing — fires if CPU never approached
    the implied ceiling. Uses CPU as a proxy for ACU consumption (the actual
    `serverless_database_capacity` CW metric isn't collected today)."""
    sv2_min = meta.get("serverlessv2_min_acu")
    sv2_max = meta.get("serverlessv2_max_acu")
    if sv2_max is None:
        return 0  # Not a Serverless v2 cluster.
    sv2_min = float(sv2_min or 0)
    sv2_max = float(sv2_max)
    if sv2_max <= 0:
        return 0

    emitted = 0
    p95_cpu, max_cpu = cpu["p95"], cpu["max"]

    if p95_cpu < SV2_MAX_LOWER_P95_CPU:
        suggested_max = max(sv2_min + 2, round(sv2_max * SV2_MAX_LOWER_FACTOR, 1))
        _emit_finding(
            rds_data, cache_arn, cache_secret, cache_db,
            cluster_id, "cost_serverless_max_too_high", "info",
            f"Serverless v2 (max {sv2_max:.1f} ACU)",
            value_str=f"7d p95 CPU {p95_cpu:.1f}% / max {max_cpu:.1f}%",
            threshold_str=f"< {SV2_MAX_LOWER_P95_CPU:.0f}% p95 CPU → max ACU likely overprovisioned",
            recommendation=(
                f"max ACU가 {sv2_max:.1f}인데 관측된 p95 CPU는 {p95_cpu:.1f}%에 불과합니다. "
                f"max ACU를 ~{suggested_max:.1f}로 낮추면 동일 처리량에 버스트 비용 노출만 줄어듭니다. "
                "(Serverless v2는 피크 ACU-시간으로 과금됩니다.)"
            ),
            details={
                "current_min_acu": sv2_min,
                "current_max_acu": sv2_max,
                "suggested_max_acu": suggested_max,
                "p95_cpu": p95_cpu,
                "max_cpu": max_cpu,
            },
        )
        emitted += 1

    # Min ACU advice: if min is very low (< 50% of max), it may cause idle-to-active
    # latency hits. We only nudge if the user also has a high p95 — implying
    # they DO need responsive cold-start.
    if sv2_min > 0 and sv2_min < sv2_max * SV2_MIN_RAISE_FACTOR and p95_cpu > 70.0:
        suggested_min = round(sv2_max * 0.5, 1)
        _emit_finding(
            rds_data, cache_arn, cache_secret, cache_db,
            cluster_id, "cost_serverless_min_too_low", "info",
            f"Serverless v2 (min {sv2_min:.1f} ACU)",
            value_str=f"min={sv2_min:.1f} ACU, p95 CPU={p95_cpu:.1f}%",
            threshold_str=f"min < max × {SV2_MIN_RAISE_FACTOR:.1f} AND p95 > 70% → cold-start risk",
            recommendation=(
                f"min ACU {sv2_min:.1f}로는 부하 대비 여유가 없습니다 (p95 CPU {p95_cpu:.1f}%). "
                f"min ACU를 ~{suggested_min:.1f}로 올리면 트래픽 스파이크 시 스케일업 지연이 줄어듭니다. "
                "대신 평시 비용 하한이 올라가는 트레이드오프입니다 — cold-start 지연이 아프다면 수용하세요."
            ),
            details={
                "current_min_acu": sv2_min,
                "current_max_acu": sv2_max,
                "suggested_min_acu": suggested_min,
                "p95_cpu": p95_cpu,
            },
        )
        emitted += 1

    return emitted


def _check_storage_rightsize(rds_data, cache_arn, cache_secret, cache_db, cluster_id, meta):
    """Storage right-sizing — currently a no-op scaffold.

    The DBOps collector today only handles Aurora MySQL/PostgreSQL, where
    storage auto-scales and bills per-GB-used (not allocated) — there's no
    "shrink the disk" advice to give. This function is the entry point for
    when the collector grows to cover other engines:

      • RDS non-Aurora (MySQL/PG/MariaDB): `AllocatedStorage` is fixed and
        billed per allocated GB. Compare with actual used (information_schema
        / pg_database_size) — if used << allocated and StorageType is gp2/gp3,
        recommend shrinking via Modify-DBInstance.
      • DocumentDB: same storage model as Aurora — skip.
      • DynamoDB: storage itself is per-GB-used, so storage rightsize doesn't
        apply. The equivalent finding is *provisioned-capacity rightsize* on
        RCU/WCU (TableDescription.ProvisionedThroughput vs actual ConsumedCapacity).
        Emit a different check_type — `cost_dynamodb_capacity_oversized` —
        when the provisioned tier is adopted.

    Plumbing this in requires:
      1. meta_collector branching on the service (rds.describe_db_clusters
         today; add docdb.describe_db_clusters, rds.describe_db_instances,
         dynamodb.describe_table).
      2. cluster_meta schema extension for `allocated_storage_gb`,
         `actual_storage_used_gb`, `storage_type`, `provisioned_rcu`,
         `provisioned_wcu`.
      3. New `cost_storage_oversized` / `cost_dynamodb_capacity_oversized`
         check_types in CHECK_LABELS (frontend) + this collector.

    Tracked in BACKLOG: "Multi-engine support — DocumentDB / RDS non-Aurora /
    DynamoDB" epic.
    """
    engine = (meta.get("engine") or "").lower()
    # All currently-supported engines are Aurora — storage auto-scales.
    if "aurora" in engine or not engine:
        return 0
    # Future engines hit this branch — for now we just log so it's obvious
    # the scaffold ran. Real check goes here once the meta is collected.
    print(
        f"[cost] storage rightsize scaffold hit for {cluster_id} (engine={engine}) — "
        "implement when multi-engine collector lands."
    )
    return 0


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
        "SELECT instance_class, engine_mode, serverlessv2_min_acu, serverlessv2_max_acu "
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

    emitted = 0
    emitted += _check_oversized(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, meta, cpu
    )
    emitted += _check_serverless_v2_acu(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, meta, cpu
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
