# RDS Instance R-5: Simulation / Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CW-driven instance right-sizing with real-priced cost comparison (compute + storage/IOPS + SQL Server license-aware) for the `rds_instance` engine family (RDS MySQL + RDS SQL Server), as a single positive-gated simulation tool plus its frontend panel.

**Architecture:** One read-only, no-approval MCP tool `simulate_rds_instance_rightsizing` in the simulation server, gated by a new `rds_cost_simulation` capability (positive gate, ElastiCache/DynamoDB precedent). It reads the instance's CloudWatch utilization from `metric_snapshots` (already collected by `cw_collector`), recommends a smaller/larger/hold instance class via a size ladder, and prices current-vs-recommended monthly cost through a new `rds_instance_pricing.py` helper that queries the AWS Price List API (RDS engines, SQL Server edition + license model, gp3 storage + provisioned IOPS). Pricing fails soft (null cost, marked as fallback) — never fabricated. Frontend adds an `rds_instance` branch to the simulator page.

**Tech Stack:** Python 3.12 (MCP Lambda), boto3 `pricing` + `cloudwatch`/cache, RDS Data API cache reads, Next.js 16 / React (simulator page), CDK gateway schema.

## Global Constraints

- **CDK-only infrastructure.** No code change touches AWS resources directly; the tool only reads (Price List API, describe, cache). No new approval action (read-only tool).
- **`engine_family.py` is duplicated VERBATIM in 4 Python copies** — `mcp-servers/mcp_servers/shared/engine_family.py`, `api/clusters/engine_family.py`, `api/dashboard/engine_family.py`, `data-pipeline/etl_collector/collectors/engine_family.py` — plus the TS mirror `frontend/src/lib/engine.ts`. A byte-parity test enforces the 4 Python copies are identical. ANY edit to one MUST be applied byte-identically to all four, and mirrored in TS.
- **Never fabricate a price.** If the Price List API cannot resolve a unit price, that cost field is `null` and `pricing_source` marks it a fallback/estimate. Same discipline as `aurora_pricing.py` / `scaling_simulation.py`.
- **No `str(e)` in tool responses returned to the agent/UI** beyond a truncated `[:200]` reason at most, matching existing tools; never leak raw stack traces.
- **Positive gate.** The tool refuses cleanly (`status: "unsupported_engine"`) for any family whose capability lacks `rds_cost_simulation: True`. A `None` family (missing/error) → refused (only a resolved rds_instance cluster passes). This is the OPPOSITE default from the generic `simulation` guard (which DEFAULT-PERMITs on unknown).
- **Korean user-facing copy** for messages/labels; keep English DBA jargon (IOPS, vCPU, instance class, License Included) per project i18n rules.
- **No `__pycache__` committed** anywhere under `agent/`. Validate agent prompt edits with `ast.parse`, not `python`/`py_compile`.
- **Tests mock the Price List boto client** (`pricing.get_products`) and the cache `execute` — never hit real AWS in unit tests.

---

### Task 1: RDS instance pricing helper (`rds_instance_pricing.py`)

Greenfield Price List API helper for RDS (non-Aurora): instance-hour priced by engine + SQL Server edition + license model + deployment option, and storage/IOPS priced separately. Modeled on `aurora_pricing.py` but RDS engines have no I/O-Optimized variant and DO have a `licenseModel` / edition dimension and separate storage SKUs.

**Files:**

- Create: `mcp-servers/mcp_servers/shared/rds_instance_pricing.py`
- Test: `tests/unit/shared/test_rds_instance_pricing.py`

**Interfaces:**

- Produces:
  - `price_rds_instance_hour(region: str, engine: str, instance_class: str, edition: str | None = None, multi_az: bool = False) -> float | None`
    — `engine` is the registry engine string (`"mysql"`, `"sqlserver-ex"`, `"sqlserver-se"`, `"sqlserver-web"`, `"sqlserver-ee"`). `edition` is derived from `engine` for SQL Server; `None` for MySQL. Returns OnDemand `$/hour` or `None` (soft fail).
  - `price_rds_storage_month(region: str, storage_type: str, gb: float, provisioned_iops: int | None = None) -> dict`
    — returns `{"storage_usd": float|None, "iops_usd": float|None}` (monthly). `storage_type` in `{"gp3","gp2","io1","io2"}`.
  - `RDS_ENGINE_LABEL: dict[str, str]` — maps registry engine → Price List `databaseEngine` value.

**Background the implementer needs:**

- Price List API is queried via `boto3.client("pricing", region_name="us-east-1")`; the cluster's region is a `regionCode` FILTER value, not the client region (identical to `aurora_pricing.py` lines 32-35).
- OnDemand USD extraction: copy `_on_demand_usd(product)` from `aurora_pricing.py:42-52` verbatim (same product-dict shape).
- Process-level cache dict keyed by the full argument tuple, same as `aurora_pricing._CACHE`.
- Every lookup wrapped in try/except → prints a `[rds_instance_pricing]` diagnostic and returns `None`/null fields on failure. NEVER raise.
- **The exact `databaseEngine` label strings for RDS SQL Server editions must be confirmed against the live Price List API** (they are edition-specific, e.g. the API distinguishes Enterprise/Standard/Web/Express and carries a `licenseModel` attribute). Start from the mapping below, then run the probe in Step 0 and correct any label that returns zero SKUs before finalizing.

- [ ] **Step 0: Probe the live Price List API for exact RDS label strings** (implementer has AWS creds)

Run this once to enumerate the real `databaseEngine` + `licenseModel` values so the mapping is correct, not guessed:

```bash
python3 - <<'PY'
import boto3, json
p = boto3.client("pricing", region_name="us-east-1")
seen = {}
tok = None
for _ in range(12):
    kw = {"ServiceCode":"AmazonRDS","MaxResults":100,
          "Filters":[{"Type":"TERM_MATCH","Field":"regionCode","Value":"ap-northeast-2"},
                     {"Type":"TERM_MATCH","Field":"instanceType","Value":"db.t3.small"}]}
    if tok: kw["NextToken"]=tok
    r = p.get_products(**kw)
    for raw in r.get("PriceList", []):
        a = json.loads(raw)["product"]["attributes"]
        key = (a.get("databaseEngine"), a.get("licenseModel"), a.get("deploymentOption"))
        seen[key] = a.get("usagetype")
    tok = r.get("NextToken")
    if not tok: break
for k,v in sorted(seen.items(), key=lambda x: str(x[0])):
    print(k, "->", v)
PY
```

Record the exact strings for MySQL and each SQL Server edition (License included, Single-AZ). Use them in `RDS_ENGINE_LABEL` below.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/shared/test_rds_instance_pricing.py
import json
from unittest.mock import MagicMock, patch

from mcp_servers.shared import rds_instance_pricing as rp


def _product(usagetype, usd, db_engine, license_model="License included", deploy="Single-AZ"):
    return json.dumps({
        "product": {"attributes": {
            "usagetype": usagetype, "databaseEngine": db_engine,
            "licenseModel": license_model, "deploymentOption": deploy}},
        "terms": {"OnDemand": {"x": {"priceDimensions": {"y": {
            "pricePerUnit": {"USD": str(usd)}}}}}},
    })


def test_mysql_instance_hour_resolves():
    fake = MagicMock()
    fake.get_products.return_value = {"PriceList": [
        _product("APN2-InstanceUsage:db.t3.small", 0.045, "MySQL")]}
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        assert rp.price_rds_instance_hour("ap-northeast-2", "mysql", "db.t3.small") == 0.045


def test_sqlserver_express_uses_express_label():
    fake = MagicMock()
    # Only an Express SKU is returned; a wrong label would miss it.
    fake.get_products.return_value = {"PriceList": [
        _product("APN2-InstanceUsage:db.t3.small", 0.052,
                 rp.RDS_ENGINE_LABEL["sqlserver-ex"])]}
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        price = rp.price_rds_instance_hour("ap-northeast-2", "sqlserver-ex", "db.t3.small")
        assert price == 0.052
    # The filter must have carried the Express databaseEngine label.
    sent = fake.get_products.call_args.kwargs["Filters"]
    assert any(f["Field"] == "databaseEngine" and f["Value"] == rp.RDS_ENGINE_LABEL["sqlserver-ex"] for f in sent)


def test_instance_hour_soft_fail_returns_none():
    fake = MagicMock()
    fake.get_products.side_effect = RuntimeError("pricing down")
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        assert rp.price_rds_instance_hour("ap-northeast-2", "mysql", "db.t3.small") is None


def test_storage_month_gp3_plus_iops():
    fake = MagicMock()

    def products(**kw):
        fields = {f["Field"]: f["Value"] for f in kw["Filters"]}
        vt = fields.get("volumeType", "")
        if "IOPS" in fields.get("productFamily", "") or "PIOPS" in vt:
            return {"PriceList": [_product("APN2-RDS:PIOPS", 0.08, "Any")]}
        return {"PriceList": [_product("APN2-RDS:GP3-Storage", 0.114, "Any")]}

    fake.get_products.side_effect = products
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        out = rp.price_rds_storage_month("ap-northeast-2", "gp3", 100, provisioned_iops=None)
        assert out["storage_usd"] == 100 * 0.114
        assert out["iops_usd"] in (0.0, None)  # gp3 baseline 3000 IOPS free → 0 or null
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/jinstar/Desktop/claude-code/projects/dbops && python3 -m pytest tests/unit/shared/test_rds_instance_pricing.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError` (module not written yet).

- [ ] **Step 3: Implement `rds_instance_pricing.py`**

Use the labels confirmed in Step 0. Skeleton (fill `RDS_ENGINE_LABEL` from the probe; the SQL Server values below are the starting guess — REPLACE with probe output if different):

```python
"""rds_instance_pricing — REAL RDS (non-Aurora) prices from the AWS Price List API.

RDS instances differ from Aurora: no I/O-Optimized variant, but a licenseModel /
edition dimension (SQL Server) and SEPARATE storage + provisioned-IOPS SKUs.
Every lookup fails soft (None / null fields) so a pricing outage degrades a
simulation to an estimate rather than breaking it. Prices are OnDemand,
License-included, Single-AZ unless multi_az=True.
"""

import json

import boto3

# Registry engine -> Price List `databaseEngine`. CONFIRM against Step-0 probe.
RDS_ENGINE_LABEL = {
    "mysql": "MySQL",
    "sqlserver-ex": "SQL Server Express",
    "sqlserver-web": "SQL Server Web",
    "sqlserver-se": "SQL Server Standard",
    "sqlserver-ee": "SQL Server Enterprise",
}

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


def _label(engine: str) -> str | None:
    return RDS_ENGINE_LABEL.get((engine or "").lower())


def price_rds_instance_hour(region, engine, instance_class, edition=None, multi_az=False):
    label = _label(engine)
    if not instance_class or not label:
        return None
    deploy = "Multi-AZ" if multi_az else "Single-AZ"
    key = ("inst", region, label, instance_class, deploy)
    if key in _CACHE:
        return _CACHE[key]
    result = None
    try:
        resp = _client().get_products(
            ServiceCode="AmazonRDS",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": label},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
                {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deploy},
                {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": "License included"},
            ],
            MaxResults=100,
        )
        for raw in resp.get("PriceList", []):
            product = json.loads(raw)
            price = _on_demand_usd(product)
            if price is not None:
                result = price
                break
    except Exception as e:  # pragma: no cover - soft fail
        print(f"[rds_instance_pricing] instance lookup failed ({region}/{engine}/{instance_class}): {e}")
        result = None
    _CACHE[key] = result
    return result


def price_rds_storage_month(region, storage_type, gb, provisioned_iops=None):
    """gp3/gp2/io1/io2 storage + optional provisioned-IOPS monthly cost.
    Returns {"storage_usd": float|None, "iops_usd": float|None}. gp3 includes a
    3000-IOPS baseline: only IOPS above baseline is charged (0 below it)."""
    st = (storage_type or "gp3").lower()
    vol_map = {"gp3": "General Purpose", "gp2": "General Purpose",
               "io1": "Provisioned IOPS", "io2": "Provisioned IOPS"}
    key = ("stor", region, st, round(float(gb or 0), 2), provisioned_iops)
    if key in _CACHE:
        return _CACHE[key]
    storage_usd = iops_usd = None
    try:
        cli = _client()
        # Storage GB-month
        resp = cli.get_products(
            ServiceCode="AmazonRDS",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                {"Type": "TERM_MATCH", "Field": "volumeType", "Value": vol_map.get(st, "General Purpose")},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
            ],
            MaxResults=100,
        )
        for raw in resp.get("PriceList", []):
            unit = _on_demand_usd(json.loads(raw))
            if unit is not None:
                storage_usd = round(unit * float(gb or 0), 2)
                break
        # Provisioned IOPS (io1/io2, or gp3 above 3000 baseline)
        billable_iops = 0
        if st in ("io1", "io2") and provisioned_iops:
            billable_iops = provisioned_iops
        elif st == "gp3" and provisioned_iops and provisioned_iops > 3000:
            billable_iops = provisioned_iops - 3000
        if billable_iops:
            resp = cli.get_products(
                ServiceCode="AmazonRDS",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Provisioned IOPS"},
                ],
                MaxResults=100,
            )
            for raw in resp.get("PriceList", []):
                unit = _on_demand_usd(json.loads(raw))
                if unit is not None:
                    iops_usd = round(unit * billable_iops, 2)
                    break
        else:
            iops_usd = 0.0
    except Exception as e:  # pragma: no cover - soft fail
        print(f"[rds_instance_pricing] storage lookup failed ({region}/{st}): {e}")
    out = {"storage_usd": storage_usd, "iops_usd": iops_usd}
    _CACHE[key] = out
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/jinstar/Desktop/claude-code/projects/dbops && python3 -m pytest tests/unit/shared/test_rds_instance_pricing.py -v`
Expected: PASS (4 tests). If `test_storage_month_gp3_plus_iops` filter fields differ from your implementation, align the test's `products()` stub with the actual filter set you send — do NOT weaken the assertion that storage_usd = unit × gb.

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/mcp_servers/shared/rds_instance_pricing.py tests/unit/shared/test_rds_instance_pricing.py
git commit -m "feat(shared): RDS instance Price List helper (engine/edition/license + storage/IOPS) — R-5"
```

---

### Task 2: Right-sizing tool + capability + handler wiring

The tool: read CW utilization from `metric_snapshots`, recommend a class (down if underutilized, up if hot, else hold), price current-vs-recommended, return a cost-delta breakdown. Register in the simulation handler's `TOOLS` dict and add its positive gate.

**Files:**

- Create: `mcp-servers/mcp_servers/simulation/tools/rds_rightsizing.py`
- Modify: `mcp-servers/mcp_servers/simulation/handler.py` (add import, `TOOLS` entry, positive-gate branch)
- Modify (all 4 byte-identical copies): `mcp-servers/mcp_servers/shared/engine_family.py`, `api/clusters/engine_family.py`, `api/dashboard/engine_family.py`, `data-pipeline/etl_collector/collectors/engine_family.py` — add `"rds_cost_simulation": True` to `CAPABILITIES[RDS_INSTANCE]`
- Test: `tests/unit/simulation/test_rds_rightsizing.py`

**Interfaces:**

- Consumes: `price_rds_instance_hour`, `price_rds_storage_month`, `RDS_ENGINE_LABEL` (Task 1); the cache `execute` from the handler's `CacheClient`; the size-ladder helpers from `scaling_simulation` (`_SIZE_LADDER`, `_next_class_up`) — import and add a `_next_class_down`.
- Produces: `simulate_rds_instance_rightsizing_impl(cache, cluster_id=None, window_hours=168, headroom=0.5, new_instance_class=None, **_) -> dict`.

**Return shape (contract for the frontend — Task 5 depends on it):**

```python
{
  "status": "ok",                      # or "insufficient_data" / "error"
  "cluster_id": "dbops-demo-mssql",
  "engine": "sqlserver-ex",
  "region": "ap-northeast-2",
  "current": {"instance_class": "db.t3.small", "storage_gb": 20, "storage_type": "gp3", "iops": None},
  "utilization": {"cpu_p95": 6.1, "cpu_avg": 3.0, "conn_peak": 2,
                  "freeable_mem_min_mb": 812.0, "read_iops_p95": 0.0,
                  "write_iops_p95": 1.0, "window_hours": 168, "samples": 2016},
  "recommendation": {"action": "downsize|upsize|hold",
                     "instance_class": "db.t3.micro",
                     "reason": "CPU p95 6% · 커넥션 최대 2 — 한 단계 축소 여력"},
  "cost_impact": {
     "current_monthly_usd": 42.10, "proposed_monthly_usd": 24.30,
     "delta_monthly_usd": -17.80, "change_pct": -42.3,
     "breakdown": {"compute_current": 30.0, "compute_proposed": 12.2,
                   "storage": 2.28, "iops": 0.0, "license_note": "SQL Server Express — 라이선스 비용 $0"},
     "pricing_source": "aws_price_list"   # or "fallback_estimate" if any unit price was null
  }
}
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/simulation/test_rds_rightsizing.py
from unittest.mock import MagicMock, patch

from mcp_servers.simulation.tools import rds_rightsizing as rr


class _Cache:
    def __init__(self, meta_row, metric_rows):
        self._meta = meta_row
        self._metrics = metric_rows

    def execute(self, sql, params=None):
        if "cluster_meta" in sql:
            return [self._meta]
        return self._metrics


def _meta(engine="sqlserver-ex"):
    # resource_details keys are exactly what rds_instance_cw_collector writes.
    return {"engine": engine, "instance_class": "db.t3.small", "region": "ap-northeast-2",
            "resource_details": {"storage_type": "gp3", "allocated_storage_gb": 20,
                                 "multi_az": False, "license_model": "license-included"}}


def _metrics(cpu_p95=6.0, conn_peak=2, read_p95=0.0, write_p95=1.0, mem_min=800.0, samples=2016):
    # tool aggregates in SQL; the stub returns the already-aggregated single row
    return [{"cpu_p95": cpu_p95, "cpu_avg": cpu_p95 / 2, "conn_peak": conn_peak,
             "read_iops_p95": read_p95, "write_iops_p95": write_p95,
             "freeable_mem_min_mb": mem_min, "samples": samples}]


def test_underutilized_recommends_downsize_and_cheaper():
    cache = _Cache(_meta(), _metrics(cpu_p95=6.0))
    with patch.object(rr, "price_rds_instance_hour", side_effect=[0.052, 0.026]), \
         patch.object(rr, "price_rds_storage_month", return_value={"storage_usd": 2.28, "iops_usd": 0.0}):
        out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mssql")
    assert out["status"] == "ok"
    assert out["recommendation"]["action"] == "downsize"
    assert out["recommendation"]["instance_class"] == "db.t3.micro"
    assert out["cost_impact"]["delta_monthly_usd"] < 0
    assert out["cost_impact"]["pricing_source"] == "aws_price_list"


def test_hot_recommends_upsize():
    cache = _Cache(_meta("mysql"), _metrics(cpu_p95=88.0, conn_peak=90))
    with patch.object(rr, "price_rds_instance_hour", side_effect=[0.045, 0.09]), \
         patch.object(rr, "price_rds_storage_month", return_value={"storage_usd": 2.28, "iops_usd": 0.0}):
        out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mysql")
    assert out["recommendation"]["action"] == "upsize"
    assert out["cost_impact"]["delta_monthly_usd"] > 0


def test_null_price_marks_fallback_never_fabricates():
    cache = _Cache(_meta(), _metrics(cpu_p95=6.0))
    with patch.object(rr, "price_rds_instance_hour", return_value=None), \
         patch.object(rr, "price_rds_storage_month", return_value={"storage_usd": None, "iops_usd": None}):
        out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mssql")
    assert out["cost_impact"]["pricing_source"] == "fallback_estimate"
    assert out["cost_impact"]["current_monthly_usd"] is None


def test_insufficient_data_when_no_metrics():
    cache = _Cache(_meta(), [{"cpu_p95": None, "samples": 0}])
    out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mssql")
    assert out["status"] == "insufficient_data"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/jinstar/Desktop/claude-code/projects/dbops && python3 -m pytest tests/unit/simulation/test_rds_rightsizing.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `rds_rightsizing.py`**

```python
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


def _edition(engine):
    e = (engine or "").lower()
    return e if e.startswith("sqlserver") else None


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
        " COUNT(*) AS samples "
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
        target, action = new_instance_class, ("upsize" if new_instance_class != cur_class else "hold")
        reason = "요청한 인스턴스 클래스로 비용 비교"
    elif cpu_p95 >= 80:
        target = _next_class_up(cur_class) or cur_class
        action = "upsize" if target != cur_class else "hold"
        reason = f"CPU p95 {util['cpu_p95']}% — 한 단계 확대 권장"
    elif cpu_p95 <= 40 * headroom / 0.5 and conn_peak < 50:
        down = _next_class_down(cur_class)
        target, action = (down, "downsize") if down else (cur_class, "hold")
        reason = (f"CPU p95 {util['cpu_p95']}% · 커넥션 최대 {util['conn_peak']} — 한 단계 축소 여력"
                  if down else "이미 최소 클래스 — 축소 불가")
    else:
        target, action, reason = cur_class, "hold", f"CPU p95 {util['cpu_p95']}% — 현행 유지 적정"

    edition = _edition(engine)
    cur_hr = price_rds_instance_hour(region, engine, cur_class, edition, multi_az)
    tgt_hr = price_rds_instance_hour(region, engine, target, edition, multi_az)
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
```

**Note on the `headroom` band:** the `cpu_p95 <= 40 * headroom / 0.5` expression makes the downsize threshold scale with `headroom` (default 0.5 → 40% p95). Keep this simple form; do not over-parameterize.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/jinstar/Desktop/claude-code/projects/dbops && python3 -m pytest tests/unit/simulation/test_rds_rightsizing.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire into the simulation handler**

In `mcp-servers/mcp_servers/simulation/handler.py`:

Add the import after line 17:

```python
from mcp_servers.simulation.tools.rds_rightsizing import simulate_rds_instance_rightsizing_impl
```

Add to the `TOOLS` dict (after the `simulate_elasticache_node_resize` entry, before the closing `}`):

```python
    "simulate_rds_instance_rightsizing": {
        "impl": simulate_rds_instance_rightsizing_impl,
        "description": "RDS instance (MySQL/SQL Server) only: CW-driven right-sizing with real Price List cost delta (compute + storage/IOPS + SQL Server license). Read-only, no approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target RDS instance cluster ID"},
                "window_hours": {"type": "number", "description": "Utilization lookback window in hours (default 168, cap 720)"},
                "new_instance_class": {"type": "string", "description": "Optional: price a specific target class instead of the auto recommendation"},
            },
            "required": ["cluster_id"],
        },
    },
```

Add the positive-gate branch in `lambda_handler` — insert a new `elif` BEFORE the final `else:` at line 198:

```python
        elif tool_name == "simulate_rds_instance_rightsizing":
            # POSITIVE gate: rds_instance-only right-sizing/cost. A None family →
            # .get(None,{}) → False → refused; only a resolved rds_instance passes.
            if not CAPABILITIES.get(fam, {}).get("rds_cost_simulation", False):
                return {"content": [{"type": "text", "text": json.dumps({
                    "status": "unsupported_engine",
                    "engine_family": fam,
                    "cluster_id": cluster_id,
                    "message": "인스턴스 우측 사이징/비용 시뮬레이션은 RDS MySQL·SQL Server 전용입니다.",
                })}]}
```

- [ ] **Step 6: Add `rds_cost_simulation` to the capability (ALL 4 copies + verify byte-parity)**

In each of the 4 `engine_family.py` copies, add `"rds_cost_simulation": True,` inside `CAPABILITIES[RDS_INSTANCE]` (next to `"simulation": False,`). The edit MUST be byte-identical across all four. Then verify:

```bash
cd /Users/jinstar/Desktop/claude-code/projects/dbops
for f in mcp-servers/mcp_servers/shared api/clusters api/dashboard data-pipeline/etl_collector/collectors; do
  md5 -q "$f/engine_family.py"
done | sort -u | wc -l   # expect: 1
python3 -m pytest tests/ -k "engine_family and parity" -v
```

Expected: the `md5` count is `1` (all identical) and the parity test passes.

- [ ] **Step 7: Run the simulation handler + capability tests**

Run: `cd /Users/jinstar/Desktop/claude-code/projects/dbops && python3 -m pytest tests/unit/simulation/ tests/ -k "engine_family or simulation_handler or rightsizing" -v`
Expected: PASS, including any existing handler parity/gate tests.

- [ ] **Step 8: Commit**

```bash
git add mcp-servers/mcp_servers/simulation/tools/rds_rightsizing.py \
        mcp-servers/mcp_servers/simulation/handler.py \
        mcp-servers/mcp_servers/shared/engine_family.py \
        api/clusters/engine_family.py api/dashboard/engine_family.py \
        data-pipeline/etl_collector/collectors/engine_family.py \
        tests/unit/simulation/test_rds_rightsizing.py
git commit -m "feat(simulation): RDS instance right-sizing + cost tool, positive-gated rds_cost_simulation — R-5"
```

---

### Task 3: Gateway schema + agent prompt

Register the tool in the Gateway schema so the agent can call it, and teach the agent when to use it.

**Files:**

- Modify: `cdk/tool_definitions.py` (`simulation_schema()` — add one `_tool(...)`)
- Modify: `agent/prompts/system_prompt.py` (mention the tool for rds_instance)
- Modify: `agent/prompts/cheatsheet.py` (one-line usage)

**Interfaces:**

- Consumes: the tool name + input schema defined in Task 2 (`simulate_rds_instance_rightsizing`, params `cluster_id`, `window_hours`, `new_instance_class`).

- [ ] **Step 1: Add the gateway tool entry**

In `cdk/tool_definitions.py`, inside `simulation_schema()`, add after the `simulate_elasticache_node_resize` entry:

```python
        _tool("simulate_rds_instance_rightsizing",
              "RDS instance (MySQL/SQL Server) only: CW-driven right-sizing with real Price List cost delta (compute + storage/IOPS + SQL Server license); read-only, no approval",
              {"cluster_id": "string", "window_hours": "number", "new_instance_class": "string"},
              ["cluster_id"]),
```

- [ ] **Step 2: Verify the schema is syntactically valid and includes the tool**

Run:

```bash
cd /Users/jinstar/Desktop/claude-code/projects/dbops/cdk
python3 -c "import tool_definitions as t; names=[x['name'] for x in t.simulation_schema()]; print(names); assert 'simulate_rds_instance_rightsizing' in names"
```

Expected: prints the list including `simulate_rds_instance_rightsizing`; no AssertionError.

- [ ] **Step 3: Add the agent prompt guidance**

In `agent/prompts/system_prompt.py`, find the simulation-tools guidance section and add a line for rds_instance (Korean, matching surrounding style), e.g.:

> RDS MySQL·SQL Server 인스턴스의 비용 최적화·우측 사이징 질문에는 `simulate_rds_instance_rightsizing`를 사용한다(읽기 전용, 승인 불필요). Aurora 전용 `simulate_scaling`은 rds_instance에 쓰지 않는다.

In `agent/prompts/cheatsheet.py`, add a one-line entry next to the other simulation tools mapping the intent ("인스턴스가 너무 크다/작다, 비용 절감") to `simulate_rds_instance_rightsizing`.

- [ ] **Step 4: Validate prompt files parse (NO py_compile — leaves **pycache**)**

Run:

```bash
cd /Users/jinstar/Desktop/claude-code/projects/dbops
python3 -c "import ast; ast.parse(open('agent/prompts/system_prompt.py').read()); ast.parse(open('agent/prompts/cheatsheet.py').read()); print('ok')"
```

Expected: `ok`. Then confirm no new `__pycache__` under `agent/`:

```bash
find agent -name __pycache__ -newer AGENTS.md; echo "clean"
```

- [ ] **Step 5: Commit**

```bash
git add cdk/tool_definitions.py agent/prompts/system_prompt.py agent/prompts/cheatsheet.py
git commit -m "feat(agent): register simulate_rds_instance_rightsizing in gateway schema + prompts — R-5"
```

---

### Task 4: Frontend simulator panel for rds_instance

Add an `rds_instance` branch to the simulator page that renders a right-sizing + cost panel calling the new tool via the SSE agent path (same mechanism the ElastiCache node-resize simulator uses).

**Files:**

- Modify: `frontend/src/app/simulator/page.tsx` (add `fam === "rds_instance"` branch + the panel component, reusing the shared monthly-cost line at ~:1346/:1429)
- Test: none new (frontend has no unit harness for these panels); verified via the live E2E in Task 5's closeout. Follow the existing ElastiCache panel (page.tsx ~:1044) as the structural template.

**Interfaces:**

- Consumes: the Task 2 return shape (`status`, `utilization`, `recommendation`, `cost_impact.{current_monthly_usd,proposed_monthly_usd,delta_monthly_usd,change_pct,breakdown,pricing_source}`), and `engineFamily(current?.engine)` from `@/lib/engine`.

- [ ] **Step 1: Read the ElastiCache panel + shared cost-line as the template**

Read `frontend/src/app/simulator/page.tsx` around the ElastiCache node-resize simulator (search `ElastiCache node-resize cost simulator`, ~line 1044) and the shared `monthly` cost-line component (~1346, ~1429). Match its data-fetch pattern (agent SSE throwaway session), card layout, number formatting (`fmtDecimal`/`fmtExact` for values ≥1000 per project rule), and null/`n/a` handling when a cost is null.

- [ ] **Step 2: Add the branch**

In the `fam === ...` ternary chain (page.tsx ~:67-82), add an `rds_instance` arm BEFORE the final Aurora `else`:

```tsx
      ) : fam === "rds_instance" ? (
        <RdsRightsizingSimulator clusterId={selectedCluster} engine={current?.engine} />
```

- [ ] **Step 3: Implement `RdsRightsizingSimulator`**

Add the component inline in page.tsx (matching the ElastiCache one's structure). It:

- Shows current instance class / storage / engine.
- On mount (or button press) calls `simulate_rds_instance_rightsizing` for `clusterId` via the same agent-SSE helper the ElastiCache panel uses.
- Renders: utilization summary (CPU p95, peak connections, IOPS p95), the recommendation (action badge downsize/upsize/hold + reason), and current-vs-proposed monthly cost using the shared cost-line, plus the `license_note` and a provenance line when `pricing_source === "fallback_estimate"` ("실시간 가격을 가져오지 못해 추정치입니다").
- Handles `status: "insufficient_data"` with the empty-state copy from the return `message`.
- Never renders a fabricated number: when a cost field is null, show `n/a` (reuse the shared line's null handling).

- [ ] **Step 4: Build the frontend (MANDATORY before any deploy)**

Run:

```bash
cd /Users/jinstar/Desktop/claude-code/projects/dbops/frontend && npm run build
```

Expected: build succeeds, `out/` regenerated. Confirm the new component compiled (no TS errors). Do NOT deploy from a stale `out/`.

- [ ] **Step 5: Commit**

```bash
cd /Users/jinstar/Desktop/claude-code/projects/dbops
git add frontend/src/app/simulator/page.tsx
git commit -m "feat(ui): RDS instance right-sizing/cost simulator panel — R-5"
```

---

## Post-implementation: deploy + live verification (controller runs after final review)

Not a task subagent step — the controller does this after the whole-branch review is clean, per the program's live-verify discipline:

1. Deploy sequentially (never concurrent): `cd cdk && cdk deploy dbops-dev-agent dbops-dev-frontend --require-approval never`. Frontend deploy must S3-sync `out/` **excluding `config.json`**.
2. Live: on `/simulator` with `dbops-demo-mssql` selected → the RDS panel renders; recommendation + a real (non-null) monthly cost from the Price List API appears; SQL Server Express shows the $0-license note. Repeat for `dbops-demo-mysql`.
3. Live gate proof: the tool refuses for an Aurora cluster (`unsupported_engine`) — confirms the positive gate.
4. Chat proof: "dbops-demo-mssql 인스턴스 비용 최적화 가능해?" → agent calls `simulate_rds_instance_rightsizing` and returns the recommendation + cost.

## Self-Review (completed by plan author)

- **Spec coverage:** instance right-sizing (CW-driven) ✓ Task 2; storage/IOPS cost ✓ Task 1 `price_rds_storage_month` + Task 2 breakdown; SQL Server license-aware cost ✓ Task 1 edition/license label + Task 2 `license_note`; positive-gate pattern ✓ Task 2 handler + capability. Frontend surface ✓ Task 4. Agent access ✓ Task 3.
- **Placeholder scan:** none — every code step carries full code.
- **Type consistency:** tool name `simulate_rds_instance_rightsizing` and capability key `rds_cost_simulation` used identically in Tasks 2/3/4; return-shape keys (`cost_impact.delta_monthly_usd`, `recommendation.action`) match between the Task 2 contract and the Task 4 consumer.
- **Schema facts CONFIRMED by the plan author against source (do not re-verify):**
  - `metric_snapshots` columns are `(cluster_id, ts, metric_type, value, dimensions)` — timestamp column is `ts` (timestamptz). rds_instance collects `metric_type` in exactly `cpu, db_connections, freeable_memory, free_storage_bytes, read_iops, write_iops, read_latency, write_latency, net_rx, net_tx, swap_usage` (`rds_instance_cw_collector._METRICS`). The tool's query set is a valid subset.
  - `cluster_meta` typed columns include `region, engine, engine_version, instance_class, status, resource_details, updated_at`. `resource_details` (JSONB) keys for rds_instance are exactly: `instance_class, multi_az, storage_type, allocated_storage_gb, license_model, publicly_accessible, pi_enabled, endpoint, port` (`rds_instance_cw_collector`). Use `allocated_storage_gb` and `multi_az`; there is NO `iops` key (provisioned IOPS not collected → gp3-baseline pricing).
- **The ONE remaining live-verification point:** exact Price List `databaseEngine`/`licenseModel` label strings for RDS SQL Server editions (Task 1 Step 0 probe) — everything else is confirmed.
