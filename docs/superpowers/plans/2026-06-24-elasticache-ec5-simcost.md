# ElastiCache EC-5 Simulation + Cost / Right-Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ElastiCache node-resize cost simulation (AWS Price List API) + an `elasticache_cost_oversized` right-sizing finding, mirroring the DynamoDB/Aurora cost model. Pure compute, fully unit-testable.

**Architecture:** New `elasticache_pricing.py` (Price List API, mirror `aurora_pricing.py`) + `elasticache_cost.py` (pure cost math) → `simulate_elasticache_node_resize` MCP tool (positive-gated on a new `elasticache_cost_simulation` capability) + REST mirror + frontend simulator branch. Right-sizing via a 7th rule in `elasticache_findings.py`. Capability corrected: `simulation:False` + `elasticache_cost_simulation:True` (mirror DynamoDB).

**Tech Stack:** Python 3.12 (simulation MCP + ETL collector + api/simulation), boto3 `pricing`/`elasticache`, Next.js 16.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** (user rule).
- **No hardcoded prices** — AWS Price List API (`boto3.client("pricing", region_name="us-east-1")`, target region as `regionCode` FILTER), process-level soft-fail cache, `None` on miss. NEVER fabricate a dollar figure → a missing price makes the result `status="partial"`.
- **Capability correction (5-copy — the 4 `engine_family.py`; the frontend `engine.ts` has NO `simulation` field so it is NOT changed):** `CAPABILITIES["elasticache"]`: set `"simulation": False`, add `"elasticache_cost_simulation": True`.
- **Positive gate:** `simulate_elasticache_node_resize` is gated on `elasticache_cost_simulation` (mirror `simulate_dynamodb_capacity_cost`'s `ddb_cost_simulation` gate) — non-ElastiCache → `unsupported_engine`.
- **Right-sizing finding `elasticache_cost_oversized`:** severity `info`; shares the handler `snapshot_ts`; skips burstable `cache.t*`.
- **`cdk/tool_definitions.py` parity** for the new MCP tool. Read-only (no approval, no write IAM).
- **Korean** user-facing copy; AWS tokens (node types) verbatim.

---

### Task 1: Capability correction + `elasticache_pricing.py` + `elasticache_cost.py`

**Files:**

- Modify: the 4 `engine_family.py` copies (`api/clusters/`, `api/dashboard/`, `data-pipeline/etl_collector/collectors/`, `mcp-servers/mcp_servers/shared/`)
- Create: `mcp-servers/mcp_servers/shared/elasticache_pricing.py`
- Create: `mcp-servers/mcp_servers/shared/elasticache_cost.py`
- Test: `tests/unit/mcp_servers/shared/test_elasticache_cost.py` (create); extend `tests/unit/test_engine_family.py`

**Interfaces:**

- Produces: `price_per_node_hour(region, engine, node_type) -> float|None`; `compute_node_resize_cost(engine, region, current_node_type, current_node_count, new_node_type, new_node_count) -> dict`.

- [ ] **Step 1: Write the failing tests.** Extend `tests/unit/test_engine_family.py` (the EC-1 test) — in `test_all_copies_classify_elasticache`, add:

```python
        assert caps["simulation"] is False
        assert caps["elasticache_cost_simulation"] is True
```

Create `tests/unit/mcp_servers/shared/test_elasticache_cost.py`:

```python
import importlib.util
from pathlib import Path
from unittest.mock import patch

_C = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/shared/elasticache_cost.py"
_spec = importlib.util.spec_from_file_location("ec_cost", _C)
cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost)


def test_resize_cost_both_prices():
    with patch.object(cost, "price_per_node_hour", side_effect=lambda r, e, nt: {"cache.t4g.micro": 0.02, "cache.r7g.large": 0.30}[nt]):
        r = cost.compute_node_resize_cost("redis", "ap-northeast-2", "cache.t4g.micro", 1, "cache.r7g.large", 2)
    assert r["status"] == "ok"
    assert round(r["current_monthly"], 2) == round(0.02 * 1 * 730, 2)
    assert round(r["proposed_monthly"], 2) == round(0.30 * 2 * 730, 2)
    assert r["delta_monthly"] > 0


def test_resize_cost_partial_when_price_missing():
    with patch.object(cost, "price_per_node_hour", side_effect=lambda r, e, nt: 0.02 if nt == "cache.t4g.micro" else None):
        r = cost.compute_node_resize_cost("redis", "ap-northeast-2", "cache.t4g.micro", 1, "cache.x.unknown", 1)
    assert r["status"] == "partial"
    assert r["proposed_monthly"] is None
    assert r["current_monthly"] is not None
```

- [ ] **Step 2: Run them to verify they fail.**

Run: `python -m pytest tests/unit/test_engine_family.py tests/unit/mcp_servers/shared/test_elasticache_cost.py -q` → FAIL.

- [ ] **Step 3: Capability change.** In ALL FOUR `engine_family.py` copies, edit `CAPABILITIES["elasticache"]` — change `"simulation": True,` to `"simulation": False,` and add `"elasticache_cost_simulation": True,`. Keep all other keys identical. (Apply byte-identically to all 4.)

- [ ] **Step 4: Create `mcp-servers/mcp_servers/shared/elasticache_pricing.py`** (mirror aurora_pricing's `price_per_instance_hour`):

```python
"""elasticache_pricing — REAL ElastiCache node prices from the AWS Price List
API. No hardcoded prices (regional staleness). Target region is the `regionCode`
FILTER, not the client region. Process-cached, soft-fail (None on miss)."""

import json

import boto3

# Price List `cacheEngine` attribute values. Valkey is priced as Redis today.
_ENGINE_LABEL = {"redis": "Redis", "valkey": "Redis", "memcached": "Memcached"}

_CACHE: dict = {}


def _client():
    return boto3.client("pricing", region_name="us-east-1")


def _on_demand_usd(product: dict):
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is not None:
                try:
                    return float(usd)
                except (TypeError, ValueError):
                    return None
    return None


def price_per_node_hour(region: str, engine: str, node_type: str):
    """$/hour for an ElastiCache node in `region`, or None if unavailable."""
    if not node_type:
        return None
    eng = (engine or "redis").lower()
    label = _ENGINE_LABEL.get(eng, "Redis")
    key = ("node", region, label, node_type)
    if key in _CACHE:
        return _CACHE[key]
    result = None
    try:
        resp = _client().get_products(
            ServiceCode="AmazonElastiCache",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": node_type},
                {"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": label},
            ],
            MaxResults=100,
        )
    except Exception as e:  # pragma: no cover
        print(f"[elasticache_pricing] lookup failed ({region}/{node_type}): {e}")
        _CACHE[key] = None
        return None
    for raw in resp.get("PriceList", []):
        product = json.loads(raw)
        price = _on_demand_usd(product)
        if price is not None:
            result = price
            break
    _CACHE[key] = result
    return result
```

- [ ] **Step 5: Create `mcp-servers/mcp_servers/shared/elasticache_cost.py`:**

```python
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
    cur_price = price_per_node_hour(region, engine, current_node_type)
    new_price = price_per_node_hour(region, engine, new_node_type) if new_node_type else cur_price
    new_count = new_node_count if new_node_count else current_node_count
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
```

- [ ] **Step 6: Run tests + verify the 4 copies are byte-identical for the change.**

Run: `python -m pytest tests/unit/test_engine_family.py tests/unit/mcp_servers/shared/test_elasticache_cost.py -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 7: Commit.**

```bash
git add api/clusters/engine_family.py api/dashboard/engine_family.py data-pipeline/etl_collector/collectors/engine_family.py mcp-servers/mcp_servers/shared/engine_family.py mcp-servers/mcp_servers/shared/elasticache_pricing.py mcp-servers/mcp_servers/shared/elasticache_cost.py tests/unit/test_engine_family.py tests/unit/mcp_servers/shared/test_elasticache_cost.py
git commit -m "feat(elasticache): node pricing (Price List API) + resize cost math + capability correction (simulation->positive gate)"
```

---

### Task 2: `simulate_elasticache_node_resize` MCP tool + positive gate

**Files:**

- Create: `mcp-servers/mcp_servers/simulation/tools/elasticache_scaling_simulation.py`
- Modify: `mcp-servers/mcp_servers/simulation/handler.py` (import + TOOLS entry + positive gate arm)
- Modify: `cdk/tool_definitions.py` (parity entry)
- Test: `tests/unit/mcp_servers/simulation/test_elasticache_scaling.py` (create)

**Interfaces:**

- Consumes: `compute_node_resize_cost` (Task 1), `client_for_cluster`/`lookup_cluster` (shared). Produces: `simulate_elasticache_node_resize_impl`.

- [ ] **Step 1: Read the templates.** Read `mcp-servers/mcp_servers/simulation/tools/scaling_simulation.py` (the Aurora describe→cost shape), `mcp-servers/mcp_servers/simulation/handler.py` (the `simulate_dynamodb_capacity_cost` POSITIVE-gate arm ~line 157-172, the TOOLS dict, import style), and `simulate_dynamodb_capacity_cost`'s tool for the result shape. Confirm `client_for_cluster`/`lookup_cluster` in `cluster_targets.py`.

- [ ] **Step 2: Write the failing test.** Create `tests/unit/mcp_servers/simulation/test_elasticache_scaling.py`:

```python
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_T = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/simulation/tools/elasticache_scaling_simulation.py"
_spec = importlib.util.spec_from_file_location("ec_scale", _T)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _wire():
    mod.lookup_cluster = lambda cid: {"resource_name": "my-redis", "region": "ap-northeast-2",
                                      "spoke_role_arn": "", "resource_details": {"engine": "redis"}}
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis", "CacheNodeType": "cache.t4g.micro",
         "MemberClusters": ["my-redis-001"], "NodeGroups": [{"NodeGroupId": "0001",
            "NodeGroupMembers": [{"CacheClusterId": "my-redis-001"}]}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.compute_node_resize_cost = lambda **k: {"status": "ok", "current_monthly": 14.6,
        "proposed_monthly": 219.0, "delta_monthly": 204.4, "note": "n"}
    return ec


def test_resize_returns_cost():
    _wire()
    r = mod.simulate_elasticache_node_resize_impl(None, cluster_id="my-redis",
        new_node_type="cache.r7g.large", new_node_count=1)
    assert r["status"] == "ok"
    assert r["proposed_monthly"] == 219.0


def test_resize_missing_cluster_id():
    assert mod.simulate_elasticache_node_resize_impl(None)["status"] in ("error", "invalid")
```

- [ ] **Step 3: Run it to verify it fails.** `python -m pytest tests/unit/mcp_servers/simulation/test_elasticache_scaling.py -q` → FAIL.

- [ ] **Step 4: Create `elasticache_scaling_simulation.py`:**

```python
"""simulate_elasticache_node_resize — estimate the monthly cost of resizing an
ElastiCache cluster's node type / count. Read-only (describe + Price List API),
no approval. Cross-account via client_for_cluster."""

from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster
from mcp_servers.shared.elasticache_cost import compute_node_resize_cost


def _current(client, name):
    rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    if not rg:
        return None, None
    g = rg[0]
    node_type = g.get("CacheNodeType", "")
    count = len(g.get("MemberClusters") or []) or 1
    return node_type, count


def simulate_elasticache_node_resize_impl(cache, cluster_id=None, new_node_type=None,
                                          new_node_count=None, **_):
    if not cluster_id:
        return {"status": "error", "reason": "cluster_id가 필요합니다"}
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    region = row.get("region", "")
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        cur_type, cur_count = _current(client, name)
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not cur_type:
        return {"status": "error", "reason": "replication group을 찾지 못했습니다", "cluster_id": cluster_id}
    result = compute_node_resize_cost(
        engine, region, cur_type, cur_count, new_node_type, new_node_count)
    result["cluster_id"] = cluster_id
    return result
```

- [ ] **Step 5: Register + positive-gate** in `mcp-servers/mcp_servers/simulation/handler.py`:
  - Import the impl (match existing style).
  - Add the `TOOLS` entry (description "ElastiCache only: estimate node resize monthly cost"; input_schema `cluster_id` required + `new_node_type`/`new_node_count` optional).
  - In the dispatch, add a positive-gate arm mirroring `simulate_dynamodb_capacity_cost` — BEFORE the generic `else` negative gate:

```python
        if tool_name == "simulate_elasticache_node_resize":
            if not CAPABILITIES.get(fam, {}).get("elasticache_cost_simulation", False):
                return {"content": [{"type": "text", "text": json.dumps({
                    "status": "unsupported_engine", "engine_family": fam, "cluster_id": cluster_id,
                    "message": "노드 리사이즈 비용 시뮬레이션은 ElastiCache 전용입니다.",
                })}]}
```

(Place it as an `elif`/`if` alongside the existing `if tool_name == "simulate_dynamodb_capacity_cost":` positive gate so both special tools are positively gated and the rest fall to the `else` negative gate. Read the existing structure and integrate cleanly — likely turn the existing `if/else` into `if dynamodb / elif elasticache / else`.)

- [ ] **Step 6: Parity entry** in `cdk/tool_definitions.py` (match the existing simulate\_\* entries' `_tool(...)` format).

- [ ] **Step 7: Run tests.**

Run: `python -m pytest tests/unit/mcp_servers/simulation/ -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression (parity test passes).

- [ ] **Step 8: Commit.**

```bash
git add mcp-servers/mcp_servers/simulation/tools/elasticache_scaling_simulation.py mcp-servers/mcp_servers/simulation/handler.py cdk/tool_definitions.py tests/unit/mcp_servers/simulation/test_elasticache_scaling.py
git commit -m "feat(elasticache): node-resize cost simulation MCP tool (positive-gated)"
```

---

### Task 3: `elasticache_cost_oversized` right-sizing finding

**Files:**

- Modify: `data-pipeline/etl_collector/collectors/elasticache_findings.py` (add a 7th rule)
- Test: extend `tests/unit/data_pipeline/test_elasticache_findings.py`

**Interfaces:**

- Extends `collect_elasticache_findings` — adds an `elasticache_cost_oversized` finding from 7-day CPU.

- [ ] **Step 1: Read** `data-pipeline/etl_collector/collectors/cost_check.py` `_check_oversized` (the avg<30/p95<60 + skip-burstable thresholds) and the existing `elasticache_findings.py` (the aggregation query + `add(...)` + the engine/node_type from cluster_meta). The CPU rule needs `node_type` (to skip `cache.t*`) — read it from cluster_meta resource_details.

- [ ] **Step 2: Write the failing test.** Add to `tests/unit/data_pipeline/test_elasticache_findings.py` a test where the aggregation returns `avg_cpu`/`p95_cpu`/`cpu_samples` indicating oversize (e.g. avg 12, p95 25, samples 30) on a non-burstable node (`cache.r7g.large`) → asserts an `elasticache_cost_oversized` finding (severity `info`) is emitted; and a high-CPU case → none; and a `cache.t4g.micro` burstable node → none. (Use the existing `_fake_rds` harness; the meta query returns `node_type` too — extend the meta mock to return node_type, and the agg to return cpu avg/p95/samples. If the existing collector's single aggregation query doesn't yet select 7-day CPU avg/p95, the rule adds its own read — model the test on however the collector reads; keep it consistent with the existing test harness.)

- [ ] **Step 3: Add the rule.** In `elasticache_findings.py`, after the existing 6 rules, add a 7-day CPU right-sizing read + rule:

```python
    # Rule 7: cost right-sizing (oversized) — 7-day CPU, skip burstable nodes
    node_type = (rd.get("node_type") or "") if isinstance(rd, dict) else ""
    if node_type and not node_type.startswith("cache.t"):
        cpu_rows = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT AVG(value) AS avg_cpu, "
            "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_cpu, "
            "  COUNT(*) AS n "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid AND metric_type IN ('engine_cpu','cache_cpu') "
            "  AND ts > NOW() - INTERVAL '7 days' "
            "  AND (dimensions IS NULL OR dimensions::text = '{}')",
            {"cid": cluster_id},
        )
        if cpu_rows:
            cr = cpu_rows[0]
            avg_cpu = cr.get("avg_cpu")
            p95_cpu = cr.get("p95_cpu")
            n = int(cr.get("n") or 0)
            if avg_cpu is not None and p95_cpu is not None and n >= 20 \
               and float(avg_cpu) < 30.0 and float(p95_cpu) < 60.0:
                add("elasticache_cost_oversized", "info", "ElastiCache Oversized (cost)",
                    f"7일 CPU 평균 {float(avg_cpu):.1f}% / p95 {float(p95_cpu):.1f}%",
                    "avg < 30% & p95 < 60% → 다운사이즈 검토",
                    f"{node_type}의 7일 CPU 평균이 {float(avg_cpu):.1f}%입니다 — 한 단계 작은 노드 타입을 검토하세요(보통 월 30-50% 절감). 축소 후 1주 관찰 권장.",
                    {"node_type": node_type, "avg_cpu": float(avg_cpu), "p95_cpu": float(p95_cpu), "window_days": 7})
```

(`rd` is the resource_details dict the collector already loads for the engine branch; reuse it. The `node_type` was stored by the EC-1/EC-3 collector's cluster_meta upsert. If `rd` isn't already in scope at that point, load `node_type` from the same `cluster_meta` query the collector uses for `engine`.)

- [ ] **Step 4: Run tests.**

Run: `python -m pytest tests/unit/data_pipeline/test_elasticache_findings.py -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 5: Commit.**

```bash
git add data-pipeline/etl_collector/collectors/elasticache_findings.py tests/unit/data_pipeline/test_elasticache_findings.py
git commit -m "feat(elasticache): cost-oversized right-sizing finding (7-day CPU)"
```

---

### Task 4: REST mirror + frontend simulator branch + IAM

**Files:**

- Modify: `api/simulation/handler.py` (a `_simulate_elasticache_node_resize` arm)
- Modify: `frontend/src/app/simulator/page.tsx` (elasticache branch) + any api-client fn
- Modify: `cdk/stacks/agent_stack.py` (simulation MCP + api/simulation Lambda IAM: `pricing:GetProducts` if absent + `elasticache:DescribeReplicationGroups`/`DescribeCacheClusters`)
- Test: extend `tests/unit/api/test_simulation*.py` if present; CDK synth

- [ ] **Step 1: Read** `api/simulation/handler.py` (`_simulate_scaling` + how sub-actions route), `frontend/src/app/simulator/page.tsx` (the `engineFamily` branch — the dynamodb branch keyed to `ddb_cost_simulation` is the template), the api-client simulation fn, and the simulation MCP + api/simulation Lambda IAM in `cdk/stacks/agent_stack.py` (does the role already have `pricing:GetProducts` for aurora/dynamodb pricing? add `elasticache:Describe*`).

- [ ] **Step 2: REST arm.** Add `_simulate_elasticache_node_resize(cluster_id, new_node_type=None, new_node_count=None)` to `api/simulation/handler.py` calling the SAME `compute_node_resize_cost` (import the shared module path used by api/ — confirm api/ can import `mcp_servers.shared` or mirror the cost module; if api/ can't import mcp-servers, the cost math is small enough to duplicate, but PREFER a shared import if the codebase already shares modules between api/ and mcp-servers/). Route it on the engine/action the frontend calls.

- [ ] **Step 3: Frontend branch.** In `simulator/page.tsx`, add an `elasticache` arm to the `engineFamily(...)` switch: a node-type + count input → calls the resize endpoint → renders current/proposed monthly + delta (reuse the dynamodb cost-result card). `partial` status shows the note. Korean labels; `fmtDecimal` for ≥1000.

- [ ] **Step 4: IAM.** In `cdk/stacks/agent_stack.py`, grant the simulation MCP Lambda (and api/simulation Lambda) `elasticache:DescribeReplicationGroups`/`DescribeCacheClusters` (read) + confirm `pricing:GetProducts` exists (aurora/dynamodb sim already use Price List — likely present; add if absent).

- [ ] **Step 5: Run.**

Run: `python -m pytest tests/cdk/test_synth.py -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression.
Run: `cd frontend && npm run build` → PASS.

- [ ] **Step 6: Commit.**

```bash
git add api/simulation/handler.py frontend/src/app/simulator/page.tsx frontend/src/lib/api-client.ts cdk/stacks/agent_stack.py
git commit -m "feat(elasticache): REST + simulator UI for node-resize cost + sim/pricing IAM"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) — focus: NO hardcoded prices (Price List API + soft-fail `partial`, never a fabricated figure); the capability flip (`simulation:False` + `elasticache_cost_simulation:True`) is byte-identical across the 4 copies AND the positive gate refuses non-ElastiCache while the 6 Aurora tools now also refuse ElastiCache (negative gate); right-sizing finding shares `snapshot_ts` + skips burstable; read-only (no write IAM); parity for the new MCP tool; frontend `partial` handling.
- Deploy dev: `cdk deploy dbops-dev-agent` (simulation MCP + api/simulation + IAM). Frontend build → sync → invalidate `E1234567890ABC`.
- Live smoke: invoke `simulate_elasticache_node_resize` (direct Lambda invoke) against a non-ElastiCache cluster → `unsupported_engine` (positive gate live). The priced happy-path needs a real cluster + Price List access — `partial`/`ok` is unit-covered; a registered ElastiCache cluster (if one exists) would return a real estimate. Right-sizing finding is unit-covered.
- Then `superpowers:finishing-a-development-branch`.
