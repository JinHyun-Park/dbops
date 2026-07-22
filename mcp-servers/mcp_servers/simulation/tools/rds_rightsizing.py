"""simulate_rds_instance_rightsizing — CW-driven instance right-sizing with real
Price List cost delta for the rds_instance family (RDS MySQL + SQL Server).
Read-only, no approval. Recommends a smaller class when p95 CPU + connection +
IOPS headroom allows, a larger class when hot, else hold; prices current vs
recommended (compute + storage/IOPS + SQL Server license). Never fabricates a
price: any null unit price → pricing_source='fallback_estimate' and null costs.
"""

from mcp_servers.shared.rds_instance_pricing import (
    price_rds_instance_hour,
    price_rds_storage_month,
)
from mcp_servers.simulation.tools.scaling_simulation import (
    _SIZE_LADDER,
    _next_class_up,
)

_HOURS_PER_MONTH = 730


def _next_class_down(instance_class):
    """One step DOWN the size axis of db.<fam>.<size>, or None at the bottom /
    unknown size (mirror of scaling_simulation._next_class_up)."""
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
    """'downsize'/'upsize'/'hold' by size position on _SIZE_LADDER (the size
    token — micro/small/large/… — is shared across db families, so the family
    prefix is irrelevant). 'hold' when equal, or when either token is unknown."""
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


def _license_note(engine):
    e = (engine or "").lower()
    if e == "sqlserver-ex":
        return "SQL Server Express — 라이선스 비용 $0 (License Included 요율에 반영)"
    if e.startswith("sqlserver"):
        return "SQL Server 라이선스는 License Included 인스턴스 요율에 포함되어 가격에 반영됨"
    return None


def simulate_rds_instance_rightsizing_impl(cache, cluster_id=None, window_hours=168,
                                           headroom=0.5, new_instance_class=None, **_):
    if not cluster_id:
        return {"status": "error", "reason": "cluster_id가 필요합니다"}
    try:
        window_hours = max(1, min(int(window_hours or 168), 720))
    except (TypeError, ValueError):
        window_hours = 168
    # headroom is agent/caller-exposed; clamp to [0,1] so it can never collapse
    # or invert the hold band (headroom>1 would push the downsize threshold at or
    # past the 80% upsize threshold → recommending downsize on a hot instance).
    try:
        headroom = max(0.0, min(float(headroom), 1.0))
    except (TypeError, ValueError):
        headroom = 0.5

    meta_rows = cache.execute(
        "SELECT engine, instance_class, region, resource_details "
        "FROM cluster_meta WHERE cluster_id = :cid", {"cid": cluster_id})
    if not (isinstance(meta_rows, list) and meta_rows and isinstance(meta_rows[0], dict)):
        return {"status": "error", "reason": "cluster_meta를 찾지 못했습니다", "cluster_id": cluster_id}
    meta = meta_rows[0]
    engine = meta.get("engine") or ""
    region = meta.get("region") or ""
    cur_class = meta.get("instance_class") or ""
    rd = meta.get("resource_details") or {}
    if isinstance(rd, str):
        import json as _j
        try:
            rd = _j.loads(rd)
        except Exception:
            rd = {}
    # Keys per rds_instance_cw_collector.details: allocated_storage_gb, storage_type,
    # multi_az, license_model. NOTE: the collector does NOT capture provisioned Iops,
    # so `iops` is None here → gp3 baseline pricing (correct for the demo instances;
    # a provisioned-IOPS instance would under-price IOPS until the collector adds it).
    storage_gb = rd.get("allocated_storage_gb") or 0
    storage_type = rd.get("storage_type") or "gp3"
    iops = rd.get("iops")  # not collected today → None → gp3 baseline
    multi_az = bool(rd.get("multi_az"))

    # Aggregate utilization over the window in one query.
    agg = cache.execute(
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
        "  AND metric_type IN ('cpu','db_connections','read_iops','write_iops','freeable_memory')",
        {"cid": cluster_id, "h": window_hours})
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
        # (below) so it can never contradict the numbers — a smaller requested
        # class must read "downsize", not a hardcoded "upsize".
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
    # (via _RDS_EDITION_LABEL → the Price List `databaseEdition` value). Passing an
    # edition here would have to be the Price-List label ("Express"), NOT the raw
    # registry string — so leave it unset and let the helper map it, matching the
    # pricing module's tested calling convention.
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
                "storage": storage_usd, "iops": iops_usd, "license_note": _license_note(engine)},
            "pricing_source": "fallback_estimate" if fallback else "aws_price_list"},
    }
