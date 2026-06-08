from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

# Static heuristic kept ONLY as a graceful fallback for when we cannot read the
# cluster's live parameter group (cross-account/unregistered/unreachable
# cluster, an AWS-managed `default.*` group whose effective values we don't own,
# or the parameter simply isn't present in the group). The live RDS describe
# path below is the source of truth; this table is intentionally coarse and may
# be wrong for a given engine version, which is exactly why it's a last resort.
PARAMETER_INFO = {
    "shared_buffers": {"type": "static", "impact": "memory", "restart": True},
    "work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "maintenance_work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "max_connections": {"type": "static", "impact": "connections", "restart": True},
    "effective_cache_size": {"type": "dynamic", "impact": "planner", "restart": False},
    "innodb_buffer_pool_size": {"type": "static", "impact": "memory", "restart": True},
    "innodb_lock_wait_timeout": {"type": "dynamic", "impact": "locking", "restart": False},
    "long_query_time": {"type": "dynamic", "impact": "logging", "restart": False},
}


def _static_fallback(cluster_id: str, parameter_name: str, new_value: str, reason: str) -> dict:
    """Build a simulation result from the coarse PARAMETER_INFO heuristic.

    Used whenever the live RDS describe path is unavailable. We surface
    `data_source` so the caller (and the DBA) knows this is a best-guess and not
    grounded in the cluster's actual parameter group — important because the
    same parameter can be static on one engine version and dynamic on another.
    """
    info = PARAMETER_INFO.get(parameter_name, {"type": "unknown", "impact": "unknown", "restart": False})
    return {
        "cluster_id": cluster_id,
        "parameter": parameter_name,
        "new_value": new_value,
        "is_dynamic": info["type"] == "dynamic",
        "requires_restart": info["restart"],
        "impact_area": info["impact"],
        "recommendation": "즉시 적용 가능" if not info["restart"] else "재시작 필요 — 점검 윈도우에서 수행 권장",
        "data_source": f"static fallback ({reason})",
    }


def _validate_value(new_value: str, allowed_values: str, data_type: str) -> dict:
    """Best-effort check of `new_value` against RDS `AllowedValues`/`DataType`.

    This is advisory only — a simulation, not an enforcement point — so it never
    hard-fails. We return {"valid": bool, "reason": str}. AllowedValues from RDS
    comes in two shapes: a numeric range ("1-65535") or a comma-separated
    enumeration ("on,off"). We only flag a violation when we can confidently
    parse the constraint; anything ambiguous (e.g. a range like "1-65535,-1"
    with sentinels, or an unparseable value) is treated as valid so we don't
    block a legitimate change on incomplete metadata.
    """
    allowed_values = (allowed_values or "").strip()
    if not allowed_values:
        return {"valid": True, "reason": ""}

    # Range form: exactly "<lo>-<hi>" with integer bounds. Validate only when the
    # candidate is itself an integer; non-integer candidates against an integer
    # range are flagged via the DataType check below instead.
    if "," not in allowed_values and allowed_values.count("-") == 1 and not allowed_values.startswith("-"):
        lo_str, hi_str = allowed_values.split("-", 1)
        if lo_str.isdigit() and hi_str.isdigit() and new_value.lstrip("-").isdigit():
            lo, hi, candidate = int(lo_str), int(hi_str), int(new_value)
            if candidate < lo or candidate > hi:
                return {
                    "valid": False,
                    "reason": f"'{new_value}'은(는) 허용 범위 {lo}-{hi}를 벗어납니다.",
                }
            return {"valid": True, "reason": ""}

    # Enumeration form: a comma-separated list of literal allowed tokens.
    if "," in allowed_values:
        choices = [c.strip() for c in allowed_values.split(",") if c.strip()]
        if choices and new_value not in choices:
            return {
                "valid": False,
                "reason": f"'{new_value}'은(는) 허용 값 {choices} 중 하나가 아닙니다.",
            }
        return {"valid": True, "reason": ""}

    # DataType sanity: an integer/boolean parameter given a clearly non-numeric
    # value. Keep this conservative — booleans accept on/off/0/1, integers accept
    # signed digits; everything else (string/list/float) passes.
    dtype = (data_type or "").lower()
    if dtype == "integer" and not new_value.lstrip("-").isdigit():
        return {"valid": False, "reason": f"'{new_value}'은(는) 정수 파라미터에 유효하지 않습니다."}
    if dtype == "boolean" and new_value.lower() not in ("on", "off", "true", "false", "0", "1"):
        return {"valid": False, "reason": f"'{new_value}'은(는) boolean 파라미터에 유효하지 않습니다."}

    return {"valid": True, "reason": ""}


def _describe_all_parameters(rds, pg_name: str) -> list[dict]:
    """Return every parameter in `pg_name`, following the RDS `Marker` pagination.

    A cluster parameter group has hundreds of parameters, well past the single
    describe page size, so the target parameter is frequently on a later page.
    Paginating here (rather than reading only page one) is what makes the live
    lookup actually reliable for arbitrary parameters.
    """
    params: list[dict] = []
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


def simulate_parameter_change_impl(cache: CacheClient, cluster_id: str, parameter_name: str, new_value: str) -> dict:
    """Simulate a parameter change using the cluster's REAL parameter metadata.

    Rather than guessing static/dynamic from a hardcoded table, we read the
    cluster's actual parameter group and the target parameter's live row:
    `ApplyType` decides whether the change applies immediately ("dynamic") or
    needs a reboot ("static"); `ParameterValue` is the real current value (empty
    means the engine default); `AllowedValues`/`DataType` drive best-effort
    validation of the proposed value; `IsModifiable` tells us if it can change
    at all. If any of that is unavailable we degrade gracefully to the static
    heuristic instead of crashing — this is advisory simulation, not enforcement.
    """
    # Resolve the cluster's parameter group. Any failure here (cross-account
    # assume-role denied, cluster not registered, RDS unreachable) drops us into
    # the static fallback rather than surfacing an exception to the agent.
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception as e:
        return _static_fallback(cluster_id, parameter_name, new_value, f"live describe unavailable: {e}")

    pg_name = cluster.get("DBClusterParameterGroup") or ""
    if not pg_name:
        return _static_fallback(cluster_id, parameter_name, new_value, "no parameter group on cluster")

    # AWS-managed `default.*` groups: their effective values aren't ours to read
    # meaningfully (and can't be modified anyway), so treat them like the live
    # path is unavailable and fall back to the heuristic.
    if pg_name.startswith("default."):
        return _static_fallback(cluster_id, parameter_name, new_value, "AWS-default parameter group")

    try:
        params = _describe_all_parameters(rds, pg_name)
    except Exception as e:
        return _static_fallback(cluster_id, parameter_name, new_value, f"live describe unavailable: {e}")

    row = next((p for p in params if p.get("ParameterName") == parameter_name), None)
    if row is None:
        return _static_fallback(cluster_id, parameter_name, new_value, "parameter not found in group")

    # Derive the real metadata from the live row.
    current_value = row.get("ParameterValue", "")
    apply_type = (row.get("ApplyType") or "").lower()
    is_dynamic = apply_type == "dynamic"
    requires_restart = apply_type == "static"
    is_modifiable = bool(row.get("IsModifiable", True))
    allowed_values = row.get("AllowedValues", "")
    data_type = row.get("DataType", "")
    description = row.get("Description", "")
    source = row.get("Source", "")

    validation = _validate_value(new_value, allowed_values, data_type)

    # Recommendation: a non-modifiable parameter overrides everything; otherwise
    # dynamic applies immediately and static needs a maintenance-window reboot.
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
        "is_dynamic": is_dynamic,
        "requires_restart": requires_restart,
        "is_modifiable": is_modifiable,
        "allowed_values": allowed_values or None,
        "data_type": data_type or None,
        "parameter_group": pg_name,
        "impact_note": description,
        "source": source,
        "valid": validation["valid"],
        "recommendation": recommendation,
        "data_source": "live (RDS DescribeDBClusterParameters)",
    }
    # Empty current value means the engine default is in effect — call it out so
    # the DBA isn't surprised that the "current" value reads as unset.
    if current_value == "":
        result["current_value_note"] = "엔진 기본값 사용 중 (파라미터 그룹에 명시값 없음)"
    if not validation["valid"]:
        result["validation_reason"] = validation["reason"]
    return result
