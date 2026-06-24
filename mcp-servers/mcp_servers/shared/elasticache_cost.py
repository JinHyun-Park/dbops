"""elasticache_cost — pure node-resize cost math (no I/O), reused by the MCP tool
and the REST handler. Prices via elasticache_pricing (Price List API); a missing
price yields status=partial (never a fabricated figure). 730h/month."""

from mcp_servers.shared.elasticache_pricing import price_per_node_hour

_HOURS_PER_MONTH = 730


def _monthly(price, count):
    if price is None:
        return None
    return price * max(1, int(count)) * _HOURS_PER_MONTH


def compute_node_resize_cost(engine, region, current_node_type, current_node_count,
                             new_node_type, new_node_count):
    # Clamp counts once at the top so displayed values match cost math
    current_node_count = max(1, int(current_node_count or 1))
    new_count = new_node_count if new_node_count else current_node_count
    new_count = max(1, int(new_count))

    cur_price = price_per_node_hour(region, engine, current_node_type)
    new_price = price_per_node_hour(region, engine, new_node_type) if new_node_type else cur_price
    current_monthly = _monthly(cur_price, current_node_count)
    proposed_monthly = _monthly(new_price, new_count)
    status = "ok" if (current_monthly is not None and proposed_monthly is not None) else "partial"
    delta = None
    if current_monthly is not None and proposed_monthly is not None:
        delta = proposed_monthly - current_monthly
    note = ("일부 노드 단가를 AWS Price List API에서 확인하지 못해 부분 추정입니다."
            if status == "partial" else
            "노드-시간 비용만 계산했습니다(데이터 전송·스냅샷 스토리지·예약 노드 제외, 730h/월).")
    return {
        "status": status, "engine": engine, "region": region,
        "current": {"node_type": current_node_type, "node_count": current_node_count, "price_per_hour": cur_price},
        "proposed": {"node_type": new_node_type or current_node_type, "node_count": new_count, "price_per_hour": new_price},
        "current_monthly": current_monthly, "proposed_monthly": proposed_monthly,
        "delta_monthly": delta,
        "delta_pct": (round(delta / current_monthly * 100, 1) if (delta is not None and current_monthly) else None),
        "note": note,
    }
