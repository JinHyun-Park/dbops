"""dynamodb_cost — PURE compute for the DynamoDB capacity-mode cost simulator.

Separated from data access and pricing so BOTH the MCP tool
(mcp_servers/simulation) and the REST handler (api/simulation) call the same
math. Duplicated byte-identical in api/simulation/dynamodb_cost.py because no
shared Lambda layer spans the two function code assets (same convention as
engine_family.py / dynamodb_pricing.py).

INPUTS the caller must gather from the cache (this module does NO I/O):
  - billing_mode: "PROVISIONED" | "PAY_PER_REQUEST" | None
  - region: str (for the response + pricing already done by the caller)
  - window_hours: float — span of the consumed series actually observed (cap 168)
  - consumed: dict with per-side aggregates over the window:
      {"datapoints": int,            # number of 1-min consumed_rcu rows
       "sum_rcu": float,             # Σ consumed_rcu (1-min Sum units) over window
       "sum_wcu": float,             # Σ consumed_wcu
       "p99_rcu_per_min": float,     # p99 of the per-minute consumed_rcu Sum
       "p99_wcu_per_min": float}     # p99 of the per-minute consumed_wcu Sum
  - provisioned: dict or None — latest real provisioned per-second units for a
      PROVISIONED table: {"rcu": float, "wcu": float}; None for on-demand.
  - prices: dict — already resolved (each may be None):
      {"rcu_hr", "wcu_hr", "m_rru", "m_wru"}

THE MATH (every assumption is echoed into response["assumptions"]):
  - Scale the observed window to a 730h month: month_factor = 730/window_hours.
  - On-Demand monthly = (Σrcu/1e6)·$/Mrru·month_factor + (Σwcu/1e6)·$/Mwru·month_factor.
    1 consumed RCU ≈ 1 RRU (≤4KB strongly-consistent read) — stated approximation.
  - Provisioned sizing: capacity is per-second, so convert the per-MINUTE p99 Sum
    to per-second (÷60), divide by headroom (target utilization), ceil:
      rcu_sized = ceil( (p99_rcu_per_min/60) / headroom ).
    p99 (not max) avoids pricing a one-off spike; headroom models auto-scaling.
  - Provisioned monthly = rcu_sized·$/RCU-hr·730 + wcu_sized·$/WCU-hr·730.
  - current_monthly = the table's ACTUAL mode cost (PROVISIONED: from the real
    provisioned units; on-demand: = the On-Demand figure).
  - recommendation: cheaper mode + $ delta + % — ONLY when both prices resolved.

HONESTY CONTRACT (project rule): a price that won't resolve → that side's figure
is None and status becomes "partial"/source "fallback"; a number is NEVER
fabricated. Too few datapoints → status "no_data".
"""

import math

HOURS_PER_MONTH = 730
DEFAULT_HEADROOM = 0.70
MIN_DATAPOINTS = 20


def _round2(v):
    return round(v, 2) if v is not None else None


def compute_capacity_cost(
    *,
    cluster_id: str,
    billing_mode,
    region: str,
    window_hours: float,
    consumed: dict,
    provisioned,
    prices: dict,
    headroom: float = DEFAULT_HEADROOM,
    table_class: str = "STANDARD",
    is_global_table: bool = False,
) -> dict:
    """Pure compute. See module docstring for the input contract. Never raises
    on a missing price/provisioned value — it degrades to partial/fallback.

    Scope: STANDARD, non-global tables only. STANDARD_INFREQUENT_ACCESS and
    global tables use different capacity pricing SKUs; this simulator returns
    status "unsupported" for them rather than computing wrong numbers."""
    datapoints = int(consumed.get("datapoints") or 0)
    norm_mode = billing_mode if billing_mode in ("PROVISIONED", "PAY_PER_REQUEST") else None

    # --- no-data floor: silent-when-uncertain ---
    if datapoints < MIN_DATAPOINTS:
        return {
            "status": "no_data",
            "cluster_id": cluster_id,
            "billing_mode": norm_mode,
            "region": region,
            "window_hours": round(window_hours, 2),
            "datapoints": datapoints,
            "no_data_reason": (
                f"소비 용량 데이터포인트가 {datapoints}개로 최소 {MIN_DATAPOINTS}개 미만입니다. "
                "신뢰할 수 있는 비용 비교를 위해 더 많은 관측이 필요합니다."
            ),
            "pricing_source": "fallback",
            "assumptions": [],
        }

    # --- unsupported table class / global table guard ---
    # Must come AFTER the no-data floor so caller always gets datapoints echoed.
    if table_class != "STANDARD" or is_global_table:
        if table_class != "STANDARD":
            unsupported_reason = (
                f"{table_class} 테이블 클래스는 용량 단가 모델이 달라 비용 비교를 지원하지 않습니다."
            )
        else:
            unsupported_reason = (
                "글로벌 테이블(멀티 리전 복제)은 용량 단가 모델이 달라 비용 비교를 지원하지 않습니다."
            )
        assumptions = [
            "이 시뮬레이터는 STANDARD 테이블 클래스이며 글로벌 테이블(멀티 리전 복제)이 아닌 "
            "DynamoDB 테이블만 지원합니다.",
        ]
        return {
            "status": "unsupported",
            "cluster_id": cluster_id,
            "billing_mode": norm_mode,
            "region": region,
            "window_hours": round(window_hours, 2),
            "datapoints": datapoints,
            "unsupported_reason": unsupported_reason,
            "on_demand_monthly_usd": None,
            "provisioned_monthly_usd": None,
            "current_monthly_usd": None,
            "recommended_mode": None,
            "monthly_savings_usd": None,
            "savings_pct": None,
            "pricing_source": "fallback",
            "assumptions": assumptions,
        }

    if window_hours <= 0:
        window_hours = 1.0
    headroom = headroom if (isinstance(headroom, (int, float)) and 0 < headroom <= 1) else DEFAULT_HEADROOM
    month_factor = HOURS_PER_MONTH / window_hours

    p_rcu_hr = prices.get("rcu_hr")
    p_wcu_hr = prices.get("wcu_hr")
    p_m_rru = prices.get("m_rru")
    p_m_wru = prices.get("m_wru")

    sum_rcu = float(consumed.get("sum_rcu") or 0.0)
    sum_wcu = float(consumed.get("sum_wcu") or 0.0)
    p99_rcu_min = float(consumed.get("p99_rcu_per_min") or 0.0)
    p99_wcu_min = float(consumed.get("p99_wcu_per_min") or 0.0)

    # --- On-Demand monthly (needs both request-unit prices) ---
    on_demand_monthly = None
    if p_m_rru is not None and p_m_wru is not None:
        rru_month = (sum_rcu * month_factor) / 1_000_000.0
        wru_month = (sum_wcu * month_factor) / 1_000_000.0
        on_demand_monthly = rru_month * p_m_rru + wru_month * p_m_wru

    # --- Provisioned sizing (p99 per-second / headroom, ceil) ---
    rcu_sized = math.ceil((p99_rcu_min / 60.0) / headroom) if p99_rcu_min > 0 else 0
    wcu_sized = math.ceil((p99_wcu_min / 60.0) / headroom) if p99_wcu_min > 0 else 0
    provisioned_monthly = None
    if p_rcu_hr is not None and p_wcu_hr is not None:
        provisioned_monthly = (
            rcu_sized * p_rcu_hr * HOURS_PER_MONTH
            + wcu_sized * p_wcu_hr * HOURS_PER_MONTH
        )

    # --- current_monthly = the table's ACTUAL mode cost ---
    current_monthly = None
    if norm_mode == "PROVISIONED" and provisioned and p_rcu_hr is not None and p_wcu_hr is not None:
        cur_rcu = float(provisioned.get("rcu") or 0.0)
        cur_wcu = float(provisioned.get("wcu") or 0.0)
        current_monthly = (
            cur_rcu * p_rcu_hr * HOURS_PER_MONTH + cur_wcu * p_wcu_hr * HOURS_PER_MONTH
        )
    elif norm_mode == "PAY_PER_REQUEST":
        current_monthly = on_demand_monthly

    # --- recommendation: only when BOTH mode prices resolved ---
    recommended_mode = None
    monthly_savings_usd = None
    savings_pct = None
    both_resolved = on_demand_monthly is not None and provisioned_monthly is not None
    if both_resolved:
        if provisioned_monthly < on_demand_monthly:
            recommended_mode = "PROVISIONED"
            cheaper, dearer = provisioned_monthly, on_demand_monthly
        else:
            recommended_mode = "PAY_PER_REQUEST"
            cheaper, dearer = on_demand_monthly, provisioned_monthly
        monthly_savings_usd = dearer - cheaper
        savings_pct = (monthly_savings_usd / dearer * 100.0) if dearer > 0 else None

    pricing_resolved = (
        p_rcu_hr is not None and p_wcu_hr is not None
        and p_m_rru is not None and p_m_wru is not None
    )
    pricing_source = "aws_pricing_api" if pricing_resolved else "fallback"
    status = "ok" if pricing_resolved else "partial"

    assumptions = [
        f"관측 윈도우 {round(window_hours, 1)}h를 730h/월로 환산(×{round(month_factor, 2)})했습니다.",
        "On-Demand: 1 consumed RCU ≈ 1 RRU(≤4KB strongly-consistent read), 1 consumed WCU ≈ 1 WRU로 근사합니다.",
        f"Provisioned sizing: p99(consumed/분 ÷ 60) ÷ headroom {headroom:.0%}를 올림(ceil)한 per-second 용량 "
        f"(RCU {rcu_sized} / WCU {wcu_sized}). 1분 CloudWatch 집계 기준의 평활화된 하한값입니다. "
        "1분 미만 burst는 관측되지 않으므로 실제 필요 Provisioned 용량(과 비용)은 이 추정보다 높을 수 있습니다.",
        "RCU/WCU(capacity)만 비교합니다 — storage·backup·stream·global-table replication·free-tier는 제외합니다.",
    ]
    if not pricing_resolved:
        assumptions.append(
            "일부 단가를 AWS Price List API에서 확인하지 못해 해당 비용은 생략했습니다(fallback)."
        )

    return {
        "status": status,
        "cluster_id": cluster_id,
        "billing_mode": norm_mode,
        "region": region,
        "window_hours": round(window_hours, 2),
        "datapoints": datapoints,
        "on_demand_monthly_usd": _round2(on_demand_monthly),
        "provisioned_monthly_usd": _round2(provisioned_monthly),
        "current_monthly_usd": _round2(current_monthly),
        "recommended_mode": recommended_mode,
        "monthly_savings_usd": _round2(monthly_savings_usd),
        "savings_pct": round(savings_pct, 1) if savings_pct is not None else None,
        "sizing": {
            "rcu_per_sec": rcu_sized,
            "wcu_per_sec": wcu_sized,
            "basis": "p99",
            "headroom": headroom,
        },
        "pricing_source": pricing_source,
        "assumptions": assumptions,
    }
