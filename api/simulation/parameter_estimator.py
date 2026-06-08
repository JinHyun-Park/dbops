"""parameter_estimator — shared logic for simulate_parameter_change.

Single source of truth (byte-mirrored into ``api/simulation/``) so the MCP tool
and the REST dashboard mirror derive a parameter-change simulation from the SAME
live metadata instead of the REST side guessing from a static table.

Each caller supplies its own AWS describe glue — the MCP via the cross-account
``rds_client_for_cluster``, the REST via a local ``boto3.client("rds")`` — and
hands the resulting parameter ROW here. The derivation (dynamic vs static from
``ApplyType``, modifiability, allowed-value validation, recommendation) lives
here so it can't drift. ``PARAMETER_INFO`` remains only as a coarse fallback for
when the live group can't be read (AWS-managed ``default.*`` group,
cross-account denied, parameter absent) and as the autocomplete catalog source.
"""

# Coarse static fallback / autocomplete catalog. NOT the source of truth for a
# real simulation — the same parameter can be static on one engine version and
# dynamic on another, which is exactly why the live describe path is preferred.
PARAMETER_INFO = {
    "shared_buffers": {"type": "static", "impact": "memory", "restart": True},
    "work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "maintenance_work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "max_connections": {"type": "static", "impact": "connections", "restart": True},
    "effective_cache_size": {"type": "dynamic", "impact": "planner", "restart": False},
    "random_page_cost": {"type": "dynamic", "impact": "planner", "restart": False},
    "checkpoint_timeout": {"type": "dynamic", "impact": "wal", "restart": False},
    "max_wal_size": {"type": "dynamic", "impact": "wal", "restart": False},
    "autovacuum_vacuum_scale_factor": {"type": "dynamic", "impact": "autovacuum", "restart": False},
    "innodb_buffer_pool_size": {"type": "static", "impact": "memory", "restart": True},
    "innodb_lock_wait_timeout": {"type": "dynamic", "impact": "locking", "restart": False},
    "long_query_time": {"type": "dynamic", "impact": "logging", "restart": False},
    "max_user_connections": {"type": "dynamic", "impact": "connections", "restart": False},
    "tmp_table_size": {"type": "dynamic", "impact": "memory", "restart": False},
}


def _impact_area(parameter_name: str) -> str:
    return PARAMETER_INFO.get(parameter_name, {}).get("impact", "unknown")


def validate_value(new_value: str, allowed_values: str, data_type: str) -> dict:
    """Best-effort check of ``new_value`` against RDS ``AllowedValues``/``DataType``.

    Advisory only (never hard-fails). Returns {"valid": bool, "reason": str}.
    AllowedValues is either a numeric range ("1-65535") or a comma-separated
    enumeration ("on,off"). Only flags a violation when the constraint parses
    cleanly; anything ambiguous passes so we never block a legitimate change on
    incomplete metadata.
    """
    allowed_values = (allowed_values or "").strip()
    if not allowed_values:
        return {"valid": True, "reason": ""}

    # Range form: exactly "<lo>-<hi>" with integer bounds.
    if "," not in allowed_values and allowed_values.count("-") == 1 and not allowed_values.startswith("-"):
        lo_str, hi_str = allowed_values.split("-", 1)
        if lo_str.isdigit() and hi_str.isdigit() and new_value.lstrip("-").isdigit():
            lo, hi, candidate = int(lo_str), int(hi_str), int(new_value)
            if candidate < lo or candidate > hi:
                return {"valid": False, "reason": f"'{new_value}'은(는) 허용 범위 {lo}-{hi}를 벗어납니다."}
            return {"valid": True, "reason": ""}

    # Enumeration form.
    if "," in allowed_values:
        choices = [c.strip() for c in allowed_values.split(",") if c.strip()]
        if choices and new_value not in choices:
            return {"valid": False, "reason": f"'{new_value}'은(는) 허용 값 {choices} 중 하나가 아닙니다."}
        return {"valid": True, "reason": ""}

    # DataType sanity check.
    dtype = (data_type or "").lower()
    if dtype == "integer" and not new_value.lstrip("-").isdigit():
        return {"valid": False, "reason": f"'{new_value}'은(는) 정수 파라미터에 유효하지 않습니다."}
    if dtype == "boolean" and new_value.lower() not in ("on", "off", "true", "false", "0", "1"):
        return {"valid": False, "reason": f"'{new_value}'은(는) boolean 파라미터에 유효하지 않습니다."}

    return {"valid": True, "reason": ""}


def describe_all_parameters(rds, pg_name: str) -> list:
    """Every parameter in ``pg_name``, following the RDS ``Marker`` pagination.

    A cluster parameter group has hundreds of parameters past the single page
    size, so the target is frequently on a later page — paginating is what makes
    the lookup reliable. ``rds`` is the caller's client (cross-account or local).
    """
    params: list = []
    marker = None
    while True:
        kwargs = {"DBClusterParameterGroupName": pg_name}
        if marker:
            kwargs["Marker"] = marker
        resp = rds.describe_db_cluster_parameters(**kwargs)
        params.extend(resp.get("Parameters") or [])
        marker = resp.get("Marker")
        if not marker:
            break
    return params


def static_fallback(cluster_id: str, parameter_name: str, new_value: str, reason: str) -> dict:
    """Coarse heuristic result when the live parameter group can't be read.

    ``data_source`` is surfaced so the DBA knows this is a best-guess, not the
    cluster's actual metadata. ``known`` reflects whether the parameter is in the
    fallback catalog at all (drives the UI's "catalog 미등록" warning).
    """
    info = PARAMETER_INFO.get(parameter_name)
    known = info is not None
    info = info or {"type": "unknown", "impact": "unknown", "restart": False}
    return {
        "cluster_id": cluster_id,
        "parameter": parameter_name,
        "new_value": new_value,
        "known": known,
        "is_dynamic": info["type"] == "dynamic",
        "requires_restart": bool(info["restart"]),
        "impact_area": info["impact"],
        "recommendation": "즉시 적용 가능" if not info["restart"] else "재시작 필요 — 점검 윈도우에서 수행 권장",
        "data_source": f"static fallback ({reason})",
    }


def build_live_result(cluster_id: str, parameter_name: str, new_value: str, row: dict, pg_name: str) -> dict:
    """Build a simulation result from a LIVE RDS parameter row.

    ``ApplyType`` decides dynamic (applies immediately) vs static (needs reboot);
    ``ParameterValue`` is the real current value (empty = engine default);
    ``AllowedValues``/``DataType`` drive validation; ``IsModifiable`` says whether
    it can change at all. Carries the legacy ``known``/``impact_area`` keys the
    dashboard reads, plus the richer live fields.
    """
    current_value = row.get("ParameterValue", "")
    apply_type = (row.get("ApplyType") or "").lower()
    is_dynamic = apply_type == "dynamic"
    requires_restart = apply_type == "static"
    is_modifiable = bool(row.get("IsModifiable", True))
    allowed_values = row.get("AllowedValues", "")
    data_type = row.get("DataType", "")
    description = row.get("Description", "")
    source = row.get("Source", "")

    validation = validate_value(new_value, allowed_values, data_type)

    if not is_modifiable:
        recommendation = "이 파라미터는 수정할 수 없습니다 (IsModifiable=false)."
    elif requires_restart:
        recommendation = "재시작 필요 — 점검 윈도우에서 수행 권장"
    else:
        recommendation = "즉시 적용 가능"

    result = {
        "cluster_id": cluster_id,
        "parameter": parameter_name,
        "current_value": current_value if current_value != "" else None,
        "new_value": new_value,
        "known": True,
        "is_dynamic": is_dynamic,
        "requires_restart": requires_restart,
        "is_modifiable": is_modifiable,
        "impact_area": _impact_area(parameter_name),
        "allowed_values": allowed_values or None,
        "data_type": data_type or None,
        "parameter_group": pg_name,
        "impact_note": description,
        "source": source,
        "valid": validation["valid"],
        "recommendation": recommendation,
        "data_source": "live (RDS DescribeDBClusterParameters)",
    }
    if current_value == "":
        result["current_value_note"] = "엔진 기본값 사용 중 (파라미터 그룹에 명시값 없음)"
    if not validation["valid"]:
        result["validation_reason"] = validation["reason"]
    return result
