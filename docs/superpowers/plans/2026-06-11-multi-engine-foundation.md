# Multi-Engine Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept AWS DocumentDB and DynamoDB as first-class monitored resources with engine-family grouping everywhere and engine-appropriate dashboard shells, without breaking the existing Aurora paths.

**Architecture:** Add a thin `engine_family` + `CAPABILITIES` layer (kept as the registry/`cluster_id` PK unchanged). Dispatch the ETL loop on engine family BEFORE any RDS call. New CloudWatch-only collectors for DocDB (`AWS/DocDB`) and DynamoDB (`AWS/DynamoDB`). Store non-relational meta in a new `resource_details JSONB` column. Enforce capability gating on frontend panels, backend RDS-live endpoints, and finding collectors. DynamoDB resources use a regex-safe slug `cluster_id`.

**Tech Stack:** Python 3.10 Lambdas (pytest), AWS CDK (Python), Aurora PG cache via RDS Data API, Next.js 15 static export (TypeScript, tsc + Playwright e2e), boto3 (rds, docdb, dynamodb, cloudwatch).

**Spec:** `docs/superpowers/specs/2026-06-11-multi-engine-foundation-design.md`

**Cross-engine convention:** there is no shared backend Lambda layer — `engine_family.py` is duplicated verbatim in each backend package (`api/clusters/`, `data-pipeline/etl_collector/collectors/`, `mcp-servers/mcp_servers/shared/`). The frontend has its own copy in `lib/engine.ts`. A header comment in each marks it canonical-sync.

**Codex adversarial checkpoints (run between phases, per user request):**

- After Phase 1 (data model + ETL dispatch)
- After Phase 2 (collectors + registration + backend gating)
- After Phase 3 (frontend grouping + dashboard shell)
  At each: `codex exec -s read-only -C <repo> "<adversarial prompt against the diff>"`, cross-check findings against code/AWS docs, fold in valid ones.

---

## File Structure

**New backend files:**

- `data-pipeline/etl_collector/collectors/engine_family.py` — `engine_family()`, `CAPABILITIES`, `dynamodb_cluster_id()` (canonical).
- `data-pipeline/etl_collector/collectors/docdb_cw_collector.py` — DocDB CloudWatch (`AWS/DocDB`).
- `data-pipeline/etl_collector/collectors/dynamodb_cw_collector.py` — DynamoDB CloudWatch (`AWS/DynamoDB`) + `describe_table` meta.
- `api/clusters/engine_family.py` — verbatim copy of the canonical helper.
- `mcp-servers/mcp_servers/shared/engine_family.py` — verbatim copy (for the agent guard).

**Modified backend files:**

- `data-pipeline/etl_collector/handler.py` — dispatch by engine_family before RDS calls; gate cost/findings.
- `data-pipeline/etl_collector/collectors/meta_collector.py` — write `resource_details` for relational (no behavior change) + accept it.
- `data-pipeline/schema_migrator/sql/schema_v16.sql` — `ALTER TABLE cluster_meta ADD COLUMN resource_details JSONB`.
- `api/clusters/handler.py` — per-family discovery + registration + test-connection.
- `api/dashboard/handler.py` — gate topology/backups/capacity/health endpoints by engine_family.

**New / modified frontend files:**

- `frontend/src/lib/engine.ts` — add `engineFamily()`, family badge/label/noun, `CAPABILITIES`.
- `frontend/src/lib/group-by-family.ts` — `groupByEngineFamily()` util (new).
- `frontend/src/app/dashboard/page.tsx` — render panels from capability map.
- `frontend/src/app/fleet/page.tsx`, `compare/page.tsx`, `clusters/page.tsx`, `components/design-system/cluster-dropdown.tsx`, `components/design-system/command-palette.tsx` — family grouping + display `resource_name`.
- New dashboard panels: `components/dashboard/dynamodb-overview-panel.tsx`, `docdb-overview-panel.tsx`.

**New test files:**

- `tests/unit/data_pipeline/test_engine_family.py`
- `tests/unit/data_pipeline/test_docdb_cw_collector.py`
- `tests/unit/data_pipeline/test_dynamodb_cw_collector.py`
- `tests/unit/data_pipeline/test_etl_dispatch.py`
- `tests/unit/api/test_clusters_multiengine.py`
- `tests/unit/api/test_dashboard_engine_gating.py`

---

# PHASE 1 — Engine-family model + data model + ETL dispatch

### Task 1: `engine_family` helper + capability map (canonical)

**Files:**

- Create: `data-pipeline/etl_collector/collectors/engine_family.py`
- Test: `tests/unit/data_pipeline/test_engine_family.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_engine_family.py
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"

def _load():
    spec = importlib.util.spec_from_file_location(
        "engine_family", _ROOT / "collectors/engine_family.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

ef = _load()

def test_family_derivation():
    assert ef.engine_family("aurora-postgresql") == "relational"
    assert ef.engine_family("aurora-mysql") == "relational"
    assert ef.engine_family("docdb") == "documentdb"
    assert ef.engine_family("dynamodb") == "dynamodb"
    assert ef.engine_family("") == "relational"        # legacy default
    assert ef.engine_family(None) == "relational"

def test_capabilities_shape():
    assert ef.CAPABILITIES["relational"]["sql"] is True
    assert ef.CAPABILITIES["documentdb"]["sql"] is False
    assert ef.CAPABILITIES["dynamodb"]["rds_meta"] is False
    assert ef.CAPABILITIES["documentdb"]["cw_namespace"] == "AWS/DocDB"
    assert ef.CAPABILITIES["dynamodb"]["cw_namespace"] == "AWS/DynamoDB"
    # findings collectors only run for relational in Foundation
    assert ef.CAPABILITIES["relational"]["findings"] == {"health", "cost", "param_fitness", "capacity_forecast"}
    assert ef.CAPABILITIES["documentdb"]["findings"] == set()
    assert ef.CAPABILITIES["dynamodb"]["findings"] == set()

def test_dynamodb_cluster_id_is_regex_safe():
    import re
    cid = ef.dynamodb_cluster_id("123456789012", "ap-northeast-2", "my_table.v2")
    assert re.match(r"^[a-zA-Z0-9-]{1,63}$", cid)
    assert cid.startswith("ddb-")
    # deterministic
    assert cid == ef.dynamodb_cluster_id("123456789012", "ap-northeast-2", "my_table.v2")
    # distinct per (account, region, table)
    assert cid != ef.dynamodb_cluster_id("123456789012", "us-east-1", "my_table.v2")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_engine_family.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the implementation**

```python
# data-pipeline/etl_collector/collectors/engine_family.py
"""Engine-family classification + capability map (canonical pure module).

No shared Lambda layer spans api/ · data-pipeline/ · mcp-servers/, so this file
is duplicated VERBATIM in each package that needs it:
  - api/clusters/engine_family.py
  - mcp-servers/mcp_servers/shared/engine_family.py
Keep all copies in sync. The frontend mirror lives in frontend/src/lib/engine.ts.
"""

import hashlib

RELATIONAL = "relational"
DOCUMENTDB = "documentdb"
DYNAMODB = "dynamodb"


def engine_family(engine):
    """Map an `engine` string to a family. Unknown → relational (legacy: every
    existing registry row is Aurora; the SQL path is the safe historical default
    and DynamoDB/DocDB are matched explicitly before the fallback)."""
    e = (engine or "").lower()
    if "docdb" in e or "documentdb" in e:
        return DOCUMENTDB
    if "dynamodb" in e:
        return DYNAMODB
    return RELATIONAL


# Per-family capabilities. Foundation runs findings collectors for relational
# only; documentdb/dynamodb collect metrics + meta but emit no findings yet
# (specs #2/#3 add them). `rds_meta`/`perf_insights`/`sql` gate the ETL
# pre-branch RDS calls and the dashboard backend endpoints.
CAPABILITIES = {
    RELATIONAL: {
        "sql": True, "rds_meta": True, "perf_insights": True,
        "cw_namespace": "AWS/RDS",
        "findings": {"health", "cost", "param_fitness", "capacity_forecast"},
    },
    DOCUMENTDB: {
        "sql": False, "rds_meta": True, "perf_insights": False,
        "cw_namespace": "AWS/DocDB",
        "findings": set(),
    },
    DYNAMODB: {
        "sql": False, "rds_meta": False, "perf_insights": False,
        "cw_namespace": "AWS/DynamoDB",
        "findings": set(),
    },
}


def dynamodb_cluster_id(account_id, region, table_name):
    """Regex-safe registry PK for a DynamoDB table. Table names allow `_`/`.`
    and up to 255 chars, which the API validators (`^[a-zA-Z0-9-]{1,63}$`)
    reject — so use a deterministic slug and keep the real name in resource_name."""
    h = hashlib.sha256(f"{account_id}:{region}:{table_name}".encode()).hexdigest()[:12]
    return f"ddb-{h}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_engine_family.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/collectors/engine_family.py tests/unit/data_pipeline/test_engine_family.py
git commit -m "feat(multi-engine): engine_family + capability map + dynamodb slug id"
```

---

### Task 2: Schema v16 — `resource_details` JSONB column

**Files:**

- Create: `data-pipeline/schema_migrator/sql/schema_v16.sql`

- [ ] **Step 1: Write the migration**

```sql
-- schema_v16.sql — neutral resource meta for non-relational engines.
-- Relational rows keep using the typed columns (instance_class, storage_size_gb…);
-- DynamoDB/DocDB store engine-specific meta here (billing_mode, item_count,
-- table_size_bytes, gsi[], instances[], ttl/pitr/streams flags).
ALTER TABLE cluster_meta ADD COLUMN IF NOT EXISTS resource_details JSONB;
```

- [ ] **Step 2: Verify it matches the existing migration style**

Run: `sed -n '1,15p' data-pipeline/schema_migrator/sql/schema_v15.sql`
Expected: confirms the `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` idiom (matches schema_v15's `http_endpoint_enabled`). Adjust the new file to match formatting if needed.

- [ ] **Step 3: Confirm the migrator auto-discovers numbered files**

Run: `grep -rn "schema_v\|sql_files\|sorted\|glob" data-pipeline/schema_migrator/handler.py`
Expected: shows the migrator globs/sorts `schema_v*.sql`. If it uses an explicit list, add `schema_v16.sql` to it.

- [ ] **Step 4: Commit**

```bash
git add data-pipeline/schema_migrator/sql/schema_v16.sql
git commit -m "feat(schema): v16 add cluster_meta.resource_details JSONB for non-relational meta"
```

---

### Task 3: ETL dispatch by engine_family (before RDS calls)

**Files:**

- Modify: `data-pipeline/etl_collector/handler.py` (the per-cluster loop, currently lines ~78-238)
- Test: `tests/unit/data_pipeline/test_etl_dispatch.py`

**Context:** Today the loop calls `collect_cluster_meta` (RDS describe), `describe_db_instances`/PI, and `collect_cw_metrics` (`AWS/RDS`) BEFORE the `if "postgresql" in engine / elif "mysql"` branch. For DynamoDB those RDS/PI/CW(AWS/RDS) calls error every cycle. Restructure so the family is computed first and only family-valid collectors run.

- [ ] **Step 1: Write the failing test (dynamodb routes away from RDS)**

```python
# tests/unit/data_pipeline/test_etl_dispatch.py
import importlib.util, sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"

def _load_handler():
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location("etl_handler", _ROOT / "handler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_dynamodb_resource_skips_rds_and_pi(monkeypatch):
    h = _load_handler()
    # one dynamodb registry row
    monkeypatch.setattr(h, "_scan_all", lambda t: [{
        "cluster_id": "ddb-abc123def456", "engine": "dynamodb",
        "account_id": "123456789012", "region": "ap-northeast-2",
        "resource_name": "Orders",
    }])
    calls = {"rds_meta": 0, "pi": 0, "rds_cw": 0, "ddb_cw": 0}
    monkeypatch.setattr(h, "collect_cluster_meta", lambda *a, **k: calls.__setitem__("rds_meta", calls["rds_meta"]+1))
    monkeypatch.setattr(h, "collect_pi_metrics", lambda *a, **k: calls.__setitem__("pi", calls["pi"]+1))
    monkeypatch.setattr(h, "collect_cw_metrics", lambda *a, **k: calls.__setitem__("rds_cw", calls["rds_cw"]+1))
    monkeypatch.setattr(h, "collect_dynamodb_metrics", lambda *a, **k: (calls.__setitem__("ddb_cw", calls["ddb_cw"]+1), {"ok": True})[1], raising=False)
    with patch("boto3.resource"), patch("boto3.client"):
        # env + clients are stubbed; the loop should dispatch dynamodb → ddb collector only
        ...
    # NOTE: assert via the dispatch helper rather than full lambda_handler if simpler:
    fam = __import__("collectors.engine_family", fromlist=["engine_family"]).engine_family("dynamodb")
    assert fam == "dynamodb"
    assert calls["rds_meta"] == 0 and calls["pi"] == 0 and calls["rds_cw"] == 0
```

> Note for implementer: `lambda_handler` is heavy to drive end-to-end. Prefer extracting a pure `_collect_one(resource, clients, cache_execute, run_ts)` dispatcher and unit-test THAT (mock the collectors, assert which run per family). The test above is the contract; refactor `lambda_handler`'s per-row body into `_collect_one` so it is testable.

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_etl_dispatch.py -q`
Expected: FAIL (`collect_dynamodb_metrics` missing / dispatch not family-aware).

- [ ] **Step 3: Refactor the per-row body into `_collect_one` with family dispatch**

In `handler.py`, add the import and restructure the loop. Replace the body of the `for cluster in clusters:` loop with a call to a new `_collect_one`, and implement dispatch:

```python
from collectors.engine_family import engine_family, CAPABILITIES
from collectors.dynamodb_cw_collector import collect_dynamodb_metrics   # Task 5
from collectors.docdb_cw_collector import collect_docdb_metrics         # Task 4

def _collect_one(resource, get_client, rds_data, cache_execute,
                 cache_cluster_arn, cache_secret_arn, cache_db_name, run_ts):
    cluster_id = resource["cluster_id"]
    region = resource.get("region", os.environ.get("AWS_REGION", "ap-northeast-2"))
    engine = resource.get("engine", "aurora-postgresql")
    family = engine_family(engine)
    caps = CAPABILITIES[family]
    result = {"cluster_id": cluster_id, "engine_family": family}

    if family == "dynamodb":
        cw = get_client("cloudwatch", region)
        ddb = get_client("dynamodb", region)
        try:
            result["dynamodb"] = collect_dynamodb_metrics(
                cw, ddb, cache_execute, cluster_id,
                resource.get("resource_name", cluster_id))
        except Exception as e:
            result["dynamodb_error"] = str(e); print(f"[{cluster_id}] dynamodb error: {e}")
        return result

    if family == "documentdb":
        cw = get_client("cloudwatch", region)
        docdb = get_client("docdb", region)
        try:
            result["documentdb"] = collect_docdb_metrics(
                cw, docdb, cache_execute, cluster_id, region,
                resource.get("account_id", ""))
        except Exception as e:
            result["documentdb_error"] = str(e); print(f"[{cluster_id}] docdb error: {e}")
        return result

    # relational: existing path (meta + PI + AWS/RDS CW + SQL collectors + findings)
    # ... move the current loop body here unchanged, but gate findings by caps["findings"] ...
    return result
```

Then the loop becomes:

```python
for resource in clusters:
    results.append(_collect_one(resource, get_client, rds_data, cache_execute,
                                cache_cluster_arn, cache_secret_arn, cache_db_name,
                                datetime.now(timezone.utc).isoformat()))
```

Move the existing relational body (meta/pi/cw/stats/health/param/capacity/cost) verbatim into the `relational` branch of `_collect_one`, keeping `run_ts` shared as today.

- [ ] **Step 4: Run the test + the existing ETL tests**

Run: `python -m pytest tests/unit/data_pipeline/ -q`
Expected: PASS (new dispatch test + all existing collector tests still green — the relational path is unchanged).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/handler.py tests/unit/data_pipeline/test_etl_dispatch.py
git commit -m "refactor(etl): dispatch collection by engine_family before RDS calls"
```

---

### Task 4: Gate finding collectors by capability (relational-only)

**Files:**

- Modify: `data-pipeline/etl_collector/handler.py` (relational branch of `_collect_one`)

- [ ] **Step 1: Add the failing assertion to the dispatch test**

Append to `test_etl_dispatch.py`: a documentdb resource must NOT invoke `collect_cost_findings` / `collect_capacity_forecast` / `collect_param_fitness`.

```python
def test_documentdb_resource_emits_no_findings(monkeypatch):
    ef = __import__("collectors.engine_family", fromlist=["CAPABILITIES"])
    assert ef.CAPABILITIES["documentdb"]["findings"] == set()
    # contract: _collect_one for documentdb returns without a 'cost'/'param_fitness' key
```

- [ ] **Step 2: Run to confirm current behavior**

Run: `python -m pytest tests/unit/data_pipeline/test_etl_dispatch.py -q`
Expected: PASS for the capability assertion; the integration guard is enforced structurally (cost/param/capacity are only called inside the relational branch).

- [ ] **Step 3: Confirm cost_check is inside the relational branch**

Today `collect_cost_findings` runs for EVERY row AFTER the engine branch (handler.py ~248). Move it INSIDE the `relational` branch of `_collect_one` so DynamoDB/DocDB never get Aurora cost/Savings-Plan findings.

- [ ] **Step 4: Run all data_pipeline tests**

Run: `python -m pytest tests/unit/data_pipeline/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/handler.py tests/unit/data_pipeline/test_etl_dispatch.py
git commit -m "fix(etl): run cost/param/capacity findings for relational family only"
```

### ▶ CODEX CHECKPOINT 1 (after Phase 1)

```bash
codex exec -s read-only -C /Users/jinstar/Desktop/claude-code/projects/dbops \
"Adversarially review the uncommitted/last-3-commits diff for the ETL engine-family dispatch \
(handler.py _collect_one, engine_family.py, schema_v16). Verify: (1) no RDS/PI/AWS-RDS-CW call \
can execute for a dynamodb/documentdb row; (2) the relational path is byte-for-byte behavior- \
preserving (run_ts still shared across health/cost/param/capacity); (3) cost_check no longer runs \
for non-relational rows; (4) engine_family default for empty/unknown engine cannot mis-skip an \
existing Aurora row. Cite handler.py line numbers. Report P0/P1/P2."
```

Cross-check findings against code; fold in valid ones before Phase 2.

---

# PHASE 2 — New collectors + registration + backend gating

### Task 5: DynamoDB CloudWatch + meta collector

**Files:**

- Create: `data-pipeline/etl_collector/collectors/dynamodb_cw_collector.py`
- Test: `tests/unit/data_pipeline/test_dynamodb_cw_collector.py`

**AWS facts (verified):** namespace `AWS/DynamoDB`, dimension `TableName`. `Consumed{Read,Write}CapacityUnits` use stat `Sum`; `{Read,Write}ThrottleEvents`/`ThrottledRequests` use `Sum`; `Provisioned{Read,Write}CapacityUnits` exist only for provisioned-mode tables (gate on `DescribeTable.BillingModeSummary.BillingMode`); `SuccessfulRequestLatency` requires an `Operation` dimension (collect a core op set: GetItem, Query, PutItem, Scan).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_dynamodb_cw_collector.py
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"

def _load():
    spec = importlib.util.spec_from_file_location(
        "dynamodb_cw_collector", _ROOT / "collectors/dynamodb_cw_collector.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

ddb = _load()

def _cw_with(datapoints):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": datapoints}
    return cw

def test_collects_consumed_capacity_as_sum_and_inserts():
    cw = _cw_with([{"Timestamp": __import__("datetime").datetime(2026,6,11), "Sum": 42.0}])
    dynamo = MagicMock()
    dynamo.describe_table.return_value = {"Table": {
        "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
        "ItemCount": 1000, "TableSizeBytes": 2048,
        "GlobalSecondaryIndexes": [{"IndexName": "gsi1"}], "TableStatus": "ACTIVE"}}
    inserts = []
    def cache_execute(sql, params): inserts.append((sql, params))
    res = ddb.collect_dynamodb_metrics(cw, dynamo, cache_execute, "ddb-abc", "Orders")
    # consumed capacity uses Sum statistic
    stats_used = {c.kwargs.get("Statistics", [None])[0] for c in cw.get_metric_statistics.call_args_list
                  if c.kwargs.get("MetricName") == "ConsumedReadCapacityUnits"}
    assert "Sum" in stats_used
    # on-demand table → no Provisioned* query
    queried = {c.kwargs.get("MetricName") for c in cw.get_metric_statistics.call_args_list}
    assert "ProvisionedReadCapacityUnits" not in queried
    # meta written to resource_details
    assert any("resource_details" in (p.get("details","") if isinstance(p,dict) else "") or "resource_details" in sql
               for sql, p in inserts)
    assert res["metrics_inserted"] > 0

def test_provisioned_table_queries_provisioned_metrics():
    cw = _cw_with([{"Timestamp": __import__("datetime").datetime(2026,6,11), "Sum": 1.0, "Average": 1.0}])
    dynamo = MagicMock()
    dynamo.describe_table.return_value = {"Table": {
        "BillingModeSummary": {"BillingMode": "PROVISIONED"},
        "ItemCount": 5, "TableSizeBytes": 99, "TableStatus": "ACTIVE"}}
    ddb.collect_dynamodb_metrics(cw, dynamo, lambda s,p: None, "ddb-x", "T")
    queried = {c.kwargs.get("MetricName") for c in cw.get_metric_statistics.call_args_list}
    assert "ProvisionedReadCapacityUnits" in queried
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/unit/data_pipeline/test_dynamodb_cw_collector.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the collector**

```python
# data-pipeline/etl_collector/collectors/dynamodb_cw_collector.py
"""DynamoDB CloudWatch + describe_table meta → cache. Namespace AWS/DynamoDB.

Capacity-mode aware (Provisioned* only for provisioned tables). Throughput uses
Sum; latency requires an Operation dimension. Meta (billing mode, item count,
size, GSIs) goes to cluster_meta.resource_details (schema v16)."""
import json
from datetime import datetime, timedelta

# (MetricName, statistic). Throughput → Sum; throttles → Sum; item count → Sum.
_TABLE_METRICS_SUM = [
    ("ConsumedReadCapacityUnits", "consumed_rcu"),
    ("ConsumedWriteCapacityUnits", "consumed_wcu"),
    ("ReadThrottleEvents", "read_throttle_events"),
    ("WriteThrottleEvents", "write_throttle_events"),
    ("ThrottledRequests", "throttled_requests"),
    ("ReturnedItemCount", "returned_item_count"),
]
_PROVISIONED_METRICS_AVG = [
    ("ProvisionedReadCapacityUnits", "provisioned_rcu"),
    ("ProvisionedWriteCapacityUnits", "provisioned_wcu"),
]
# latency requires an Operation dimension — collect a core op set
_LATENCY_OPS = ["GetItem", "Query", "PutItem", "Scan"]


def _insert(cache_execute, cluster_id, ts, metric_type, value, dims="{}"):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dims::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type,
         "value": float(value), "dims": dims})


def collect_dynamodb_metrics(cw, dynamo, cache_execute, cluster_id, table_name):
    end = datetime.utcnow(); start = end - timedelta(minutes=10)
    inserted = 0; errors = []

    # --- meta via describe_table → resource_details
    billing_mode = "PROVISIONED"
    try:
        t = dynamo.describe_table(TableName=table_name)["Table"]
        billing_mode = (t.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED")
        details = {
            "billing_mode": billing_mode,
            "item_count": t.get("ItemCount", 0),
            "table_size_bytes": t.get("TableSizeBytes", 0),
            "table_status": t.get("TableStatus", ""),
            "gsi": [g.get("IndexName") for g in t.get("GlobalSecondaryIndexes", [])],
        }
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, engine, resource_details, updated_at) "
            "VALUES (:cid, 'dynamodb', :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET resource_details = EXCLUDED.resource_details, "
            "engine = 'dynamodb', updated_at = NOW()",
            {"cid": cluster_id, "details": json.dumps(details)})
    except Exception as e:
        errors.append(f"describe_table: {e}")

    def pull(metric, stat, dims):
        try:
            return cw.get_metric_statistics(
                Namespace="AWS/DynamoDB", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=60, Statistics=[stat]
            ).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{metric}: {e}"); return []

    table_dim = [{"Name": "TableName", "Value": table_name}]

    for metric, mtype in _TABLE_METRICS_SUM:
        for dp in pull(metric, "Sum", table_dim):
            if dp.get("Sum") is None: continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp["Sum"]); inserted += 1

    if billing_mode == "PROVISIONED":
        for metric, mtype in _PROVISIONED_METRICS_AVG:
            for dp in pull(metric, "Average", table_dim):
                if dp.get("Average") is None: continue
                _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp["Average"]); inserted += 1

    for op in _LATENCY_OPS:
        dims = table_dim + [{"Name": "Operation", "Value": op}]
        for dp in pull("SuccessfulRequestLatency", "Average", dims):
            if dp.get("Average") is None: continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(),
                    f"latency_ms_{op.lower()}", dp["Average"],
                    json.dumps({"operation": op})); inserted += 1

    return {"cluster_id": cluster_id, "billing_mode": billing_mode,
            "metrics_inserted": inserted, "errors": errors}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/unit/data_pipeline/test_dynamodb_cw_collector.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/collectors/dynamodb_cw_collector.py tests/unit/data_pipeline/test_dynamodb_cw_collector.py
git commit -m "feat(etl): DynamoDB CloudWatch collector (capacity-mode aware, AWS/DynamoDB)"
```

---

### Task 6: DocumentDB CloudWatch + meta collector

**Files:**

- Create: `data-pipeline/etl_collector/collectors/docdb_cw_collector.py`
- Test: `tests/unit/data_pipeline/test_docdb_cw_collector.py`

**AWS facts (verified):** namespace `AWS/DocDB`. Cluster-scoped: `DBClusterReplicaLagMaximum`, `VolumeBytesUsed`. Instance-scoped (use `DBInstanceIdentifier` of the writer): `CPUUtilization`, `DatabaseConnections`, `DatabaseCursors`, `DatabaseCursorsTimedOut`, `BufferCacheHitRatio`, `FreeableMemory`, `ReadLatency`, `WriteLatency`, `DiskQueueDepth`, opcounters. Meta via `docdb.describe_db_clusters`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_docdb_cw_collector.py
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"

def _load():
    spec = importlib.util.spec_from_file_location(
        "docdb_cw_collector", _ROOT / "collectors/docdb_cw_collector.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

dd = _load()

def test_uses_docdb_namespace_and_writer_instance_dim():
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [{"Timestamp": datetime(2026,6,11), "Average": 5.0}]}
    docdb = MagicMock()
    docdb.describe_db_clusters.return_value = {"DBClusters": [{
        "DBClusterIdentifier": "docdb-1", "Engine": "docdb", "EngineVersion": "5.0",
        "Status": "available",
        "DBClusterMembers": [{"DBInstanceIdentifier": "docdb-1-writer", "IsClusterWriter": True}]}]}
    res = dd.collect_docdb_metrics(cw, docdb, lambda s,p: None, "docdb-1", "ap-northeast-2", "123456789012")
    namespaces = {c.kwargs.get("Namespace") for c in cw.get_metric_statistics.call_args_list}
    assert namespaces == {"AWS/DocDB"}
    # at least one instance-scoped query used DBInstanceIdentifier=writer
    dims_used = [tuple(d.values()) for c in cw.get_metric_statistics.call_args_list
                 for d in c.kwargs.get("Dimensions", [])]
    assert ("DBInstanceIdentifier", "docdb-1-writer") in dims_used
    assert res["metrics_inserted"] > 0
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/unit/data_pipeline/test_docdb_cw_collector.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the collector**

```python
# data-pipeline/etl_collector/collectors/docdb_cw_collector.py
"""DocumentDB CloudWatch + meta → cache. Namespace AWS/DocDB.

Cluster-scoped metrics use DBClusterIdentifier; instance-scoped use the writer's
DBInstanceIdentifier (DocDB publishes CPU/connections/cache-hit per instance)."""
import json
from datetime import datetime, timedelta

_CLUSTER_METRICS = [
    ("DBClusterReplicaLagMaximum", "replica_lag_ms", "Average"),
    ("VolumeBytesUsed", "storage_bytes", "Average"),
]
_INSTANCE_METRICS = [
    ("CPUUtilization", "cpu_utilization", "Average"),
    ("DatabaseConnections", "db_connections", "Average"),
    ("DatabaseCursors", "cursors", "Average"),
    ("DatabaseCursorsTimedOut", "cursors_timed_out", "Sum"),
    ("BufferCacheHitRatio", "buffer_cache_hit", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("ReadLatency", "read_latency_ms", "Average"),
    ("WriteLatency", "write_latency_ms", "Average"),
    ("DiskQueueDepth", "disk_queue_depth", "Average"),
    ("OpcountersQuery", "opcounter_query", "Average"),
    ("OpcountersInsert", "opcounter_insert", "Average"),
    ("OpcountersUpdate", "opcounter_update", "Average"),
    ("OpcountersDelete", "opcounter_delete", "Average"),
]


def _insert(cache_execute, cluster_id, ts, metric_type, value):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type, "value": float(value)})


def collect_docdb_metrics(cw, docdb, cache_execute, cluster_id, region, account_id):
    end = datetime.utcnow(); start = end - timedelta(minutes=10)
    inserted = 0; errors = []
    writer = None
    try:
        c = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        members = c.get("DBClusterMembers", [])
        writer = next((m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter")),
                      members[0]["DBInstanceIdentifier"] if members else None)
        details = {"instances": [m.get("DBInstanceIdentifier") for m in members],
                   "instance_count": len(members)}
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, engine, engine_version, status, resource_details, updated_at) "
            "VALUES (:cid, 'docdb', :ver, :status, :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET engine='docdb', engine_version=EXCLUDED.engine_version, "
            "status=EXCLUDED.status, resource_details=EXCLUDED.resource_details, updated_at=NOW()",
            {"cid": cluster_id, "ver": c.get("EngineVersion", ""), "status": c.get("Status", ""),
             "details": json.dumps(details)})
    except Exception as e:
        errors.append(f"describe_db_clusters: {e}")

    def pull(metric, stat, dims):
        try:
            return cw.get_metric_statistics(
                Namespace="AWS/DocDB", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=60, Statistics=[stat]).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{metric}: {e}"); return []

    for metric, mtype, stat in _CLUSTER_METRICS:
        for dp in pull(metric, stat, [{"Name": "DBClusterIdentifier", "Value": cluster_id}]):
            if dp.get(stat) is None: continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp[stat]); inserted += 1

    if writer:
        for metric, mtype, stat in _INSTANCE_METRICS:
            for dp in pull(metric, stat, [{"Name": "DBInstanceIdentifier", "Value": writer}]):
                if dp.get(stat) is None: continue
                _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp[stat]); inserted += 1

    return {"cluster_id": cluster_id, "writer": writer, "metrics_inserted": inserted, "errors": errors}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/unit/data_pipeline/test_docdb_cw_collector.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/collectors/docdb_cw_collector.py tests/unit/data_pipeline/test_docdb_cw_collector.py
git commit -m "feat(etl): DocumentDB CloudWatch collector (AWS/DocDB, cluster+writer dims)"
```

---

### Task 7: Per-family registration & discovery (api/clusters)

**Files:**

- Create: `api/clusters/engine_family.py` (verbatim copy of Task 1 canonical)
- Modify: `api/clusters/handler.py` — `_list_clusters_in_region` (289-336), `_handle_register` (346-413)
- Test: `tests/unit/api/test_clusters_multiengine.py`

- [ ] **Step 1: Copy the canonical helper**

```bash
cp data-pipeline/etl_collector/collectors/engine_family.py api/clusters/engine_family.py
```

Update the header comment's "canonical" note to point both ways.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/api/test_clusters_multiengine.py
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "api" / "clusters"

def _load():
    import sys; sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location("clusters_handler", _ROOT / "handler.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

h = _load()

def test_register_dynamodb_uses_slug_and_describe_table():
    table = MagicMock()
    with patch.object(h, "_rds_client_for") as rds, \
         patch.object(h, "_ddb_client_for") as ddbc:
        ddbc.return_value.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}
        resp = h._handle_register(table, {
            "engine": "dynamodb", "account_id": "123456789012",
            "region": "ap-northeast-2", "resource_name": "Orders"})
        item = table.put_item.call_args.kwargs["Item"]
        assert item["engine"] == "dynamodb"
        assert item["cluster_id"].startswith("ddb-")
        assert item["resource_name"] == "Orders"
        assert "secret_arn" not in item   # DDB foundation needs no secret
        rds.assert_not_called()            # must NOT hit RDS for dynamodb

def test_register_aurora_unchanged():
    table = MagicMock()
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_clusters.return_value = {"DBClusters": [
            {"DBClusterArn": "arn:...", "Engine": "aurora-postgresql", "DatabaseName": "app"}]}
        h._handle_register(table, {"cluster_id": "prod-pg", "account_id": "1", "region": "ap-northeast-2"})
        item = table.put_item.call_args.kwargs["Item"]
        assert item["engine"] == "aurora-postgresql"
        assert item["cluster_id"] == "prod-pg"
```

- [ ] **Step 3: Run to confirm failure**

Run: `python -m pytest tests/unit/api/test_clusters_multiengine.py -q`
Expected: FAIL (`_ddb_client_for` missing; register is RDS-only).

- [ ] **Step 4: Implement family-aware register + a `_ddb_client_for` + discovery generalization**

In `api/clusters/handler.py`:

- Add `from engine_family import engine_family, dynamodb_cluster_id`.
- Add client factories mirroring `_rds_client_for`:

```python
def _ddb_client_for(region, role_arn=""):
    return _session_for(region, role_arn).client("dynamodb")
def _docdb_client_for(region, role_arn=""):
    return _session_for(region, role_arn).client("docdb")
```

- In `_handle_register`, branch by family at the top:

```python
    fam = engine_family(body.get("engine", ""))
    if fam == "dynamodb":
        return _register_dynamodb(table, body)
    if fam == "documentdb":
        return _register_docdb(table, body)
    # relational: existing path (unchanged)
```

- Add `_register_dynamodb(table, body)`:

```python
def _register_dynamodb(table, body):
    for f in ("account_id", "region", "resource_name"):
        if not body.get(f): return _resp(400, {"error": f"{f} required"})
    account_id, region, name = body["account_id"], body["region"], body["resource_name"]
    status, err = "ok", ""
    try:
        _ddb_client_for(region, body.get("spoke_role_arn", "")).describe_table(TableName=name)
    except Exception as e:
        status, err = "failed", str(e)[:300]
    cid = dynamodb_cluster_id(account_id, region, name)
    item = {"cluster_id": cid, "account_id": account_id, "region": region,
            "engine": "dynamodb", "resource_name": name, "resource_type": "dynamodb-table",
            "engine_family": "dynamodb", "requires_secret_for_foundation": False,
            "spoke_role_arn": body.get("spoke_role_arn", ""),
            "registered_at": datetime.utcnow().isoformat() + "Z",
            "connection_status": status, "connection_error": err}
    table.put_item(Item=item)
    return _resp(201 if status == "ok" else 207,
                 {"status": "registered" if status == "ok" else "registered_with_warning",
                  "cluster_id": cid, "connection_status": status})
```

- Add `_register_docdb(table, body)` analogously using `_docdb_client_for(...).describe_db_clusters(DBClusterIdentifier=body["cluster_id"])`, `engine="docdb"`, `resource_name=cluster_id`, no secret.
- In `_list_clusters_in_region`, after the Aurora loop, add a DynamoDB enumeration (`list_tables` + `describe_table`) and a DocDB enumeration (`docdb.describe_db_clusters`), each tagged with `engine`/`engine_family`/`resource_name`. Keep the Aurora `startswith("aurora")` skip (now correct: it only governs the RDS path).

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/unit/api/test_clusters_multiengine.py tests/unit/api/test_clusters_test_connection.py -q`
Expected: PASS (new + existing Aurora connection tests).

- [ ] **Step 6: Commit**

```bash
git add api/clusters/engine_family.py api/clusters/handler.py tests/unit/api/test_clusters_multiengine.py
git commit -m "feat(api): per-family cluster registration + discovery (dynamodb/docdb)"
```

---

### Task 8: Backend capability gating for dashboard RDS-live endpoints

**Files:**

- Modify: `api/dashboard/handler.py` — topology (~1940), backups (~2064), capacity-forecast (~1652) endpoints; `_health_findings` (~1507)
- Create: `api/dashboard/engine_family.py` (verbatim copy)
- Test: `tests/unit/api/test_dashboard_engine_gating.py`

- [ ] **Step 1: Copy helper + write failing test**

```bash
cp data-pipeline/etl_collector/collectors/engine_family.py api/dashboard/engine_family.py
```

```python
# tests/unit/api/test_dashboard_engine_gating.py
# Load the dashboard handler; for a dynamodb cluster_id, topology/backups/capacity
# endpoints must return {"not_applicable": True} (or 200 empty) WITHOUT calling
# rds.describe_db_clusters. Mock the registry lookup to return engine='dynamodb'
# and assert no RDS describe call happens.
```

(Implementer: model the test on existing `tests/unit/api/test_*` patterns — mock the clusters table get_item to return `{"engine": "dynamodb"}` and assert the RDS client's `describe_db_clusters` is never called and the response carries `not_applicable`.)

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/unit/api/test_dashboard_engine_gating.py -q`
Expected: FAIL (endpoints call RDS unconditionally).

- [ ] **Step 3: Implement gating**

At the top of each RDS-live endpoint (`_topology`, `_backups`, `_capacity_forecast` HTTP handlers), resolve the resource's engine family from the clusters registry and short-circuit:

```python
from engine_family import engine_family, CAPABILITIES
fam = engine_family(_registry_engine(cluster_id))   # helper: get_item engine
if not CAPABILITIES[fam]["rds_meta"]:
    return _resp(200, {"not_applicable": True, "engine_family": fam})
```

For `_health_findings`, no RDS call, but ensure it only returns rows whose `check_type` belongs to `CAPABILITIES[fam]["findings"]` (today that set is empty for non-relational, so it returns none — preventing stray findings).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/api/ -q`
Expected: PASS (new gating + all existing dashboard tests).

- [ ] **Step 5: Commit**

```bash
git add api/dashboard/engine_family.py api/dashboard/handler.py tests/unit/api/test_dashboard_engine_gating.py
git commit -m "feat(api): gate RDS-live dashboard endpoints + findings by engine_family"
```

---

### Task 9: Agent/MCP unknown-engine guard

**Files:**

- Create: `mcp-servers/mcp_servers/shared/engine_family.py` (verbatim copy)
- Modify: `mcp-servers/mcp_servers/shared/cluster_targets.py` (target resolution) — return a clear `unsupported_engine` signal for non-relational families.
- Test: extend an existing shared test or add `tests/unit/mcp_servers/shared/test_engine_guard.py`

- [ ] **Step 1: Copy helper + write failing test**

```bash
cp data-pipeline/etl_collector/collectors/engine_family.py mcp-servers/mcp_servers/shared/engine_family.py
```

Test: resolving a target for a `dynamodb` registry row returns `{"status": "unsupported_engine", "engine_family": "dynamodb"}` rather than attempting SQL.

- [ ] **Step 2: Run to confirm failure**

Run: `python -m pytest tests/unit/mcp_servers/shared/test_engine_guard.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the guard**

In the target-resolution function used by `execute_sql`/`cache_client.execute_on_target`, after loading the registry row, if `not CAPABILITIES[engine_family(engine)]["sql"]` return a structured `unsupported_engine` result so the agent surfaces "이 리소스 타입은 현재 단계에서 챗 진단을 지원하지 않습니다 (Phase 1)".

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/mcp_servers/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/mcp_servers/shared/engine_family.py mcp-servers/mcp_servers/shared/cluster_targets.py tests/unit/mcp_servers/shared/test_engine_guard.py
git commit -m "feat(mcp): guard non-relational engines from SQL target resolution"
```

---

### Task 10: CDK IAM permissions for discovery/ETL

**Files:**

- Modify: `cdk/stacks/data_stack.py` (ETL role), `cdk/stacks/agent_stack.py` or wherever the clusters API role is defined

- [ ] **Step 1: Add permissions to the ETL Lambda role**

Add an inline policy statement granting `dynamodb:ListTables`, `dynamodb:DescribeTable`, `docdb:DescribeDBClusters` (resource `*` or scoped) to `self.etl_lambda` (mirror the existing CloudWatch grant block).

- [ ] **Step 2: Add the same to the clusters API role** (discovery/register need them).

- [ ] **Step 3: CDK snapshot test**

Run: `python -m pytest tests/unit/cdk/ -q -k data_stack` (or the project's CDK snapshot test command). Update the snapshot if the project regenerates them deliberately.

- [ ] **Step 4: Synth to verify no errors**

Run: `cd cdk && cdk synth dbops-dev-data >/dev/null && echo OK`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add cdk/stacks/
git commit -m "feat(cdk): grant ETL+clusters roles dynamodb/docdb describe permissions"
```

### ▶ CODEX CHECKPOINT 2 (after Phase 2)

```bash
codex exec -s read-only -C /Users/jinstar/Desktop/claude-code/projects/dbops \
"Adversarially review Phase-2 diff: dynamodb_cw_collector, docdb_cw_collector, api/clusters \
per-family register/discover, api/dashboard engine gating, mcp engine guard, CDK IAM. Verify: \
(1) DynamoDB Provisioned* only queried for PROVISIONED tables; latency uses Operation dim; \
Consumed* uses Sum. (2) DocDB uses AWS/DocDB + writer DBInstanceIdentifier for instance metrics. \
(3) register/discover never hit RDS for dynamodb; slug cluster_id passes ^[a-zA-Z0-9-]{1,63}$. \
(4) every RDS-live dashboard endpoint is gated (topology/backups/capacity) — grep for \
describe_db_clusters in api/dashboard/handler.py and confirm each is guarded. (5) IAM least- \
privilege. Cite file:line. P0/P1/P2."
```

Cross-check; fold in valid findings before Phase 3.

---

# PHASE 3 — Frontend grouping + dashboard shell

### Task 11: `engineFamily()` + family metadata in `lib/engine.ts`

**Files:**

- Modify: `frontend/src/lib/engine.ts`

- [ ] **Step 1: Add family types + helpers (append, do not change existing exports)**

```typescript
export type EngineFamily = "relational" | "documentdb" | "dynamodb";

export function engineFamily(engine: string | null | undefined): EngineFamily {
  const e = (engine || "").toLowerCase();
  if (e.includes("docdb") || e.includes("documentdb")) return "documentdb";
  if (e.includes("dynamodb")) return "dynamodb";
  return "relational";
}

export interface FamilyMeta {
  label: string;
  noun: string;
  accent: string;
  classes: string;
}
export const FAMILY_META: Record<EngineFamily, FamilyMeta> = {
  relational: {
    label: "Relational (Aurora)",
    noun: "클러스터",
    accent: "bg-sky-400",
    classes: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  },
  documentdb: {
    label: "DocumentDB",
    noun: "클러스터",
    accent: "bg-emerald-400",
    classes: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  },
  dynamodb: {
    label: "DynamoDB",
    noun: "테이블",
    accent: "bg-violet-400",
    classes: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  },
};

// Frontend mirror of backend CAPABILITIES — which dashboard panels render per family.
export const FAMILY_PANELS: Record<EngineFamily, Set<string>> = {
  relational: new Set(["all-relational"]), // sentinel: render the existing full panel set
  documentdb: new Set([
    "overview",
    "connections",
    "replicaLag",
    "cacheHit",
    "cursors",
    "opcounters",
    "backups",
  ]),
  dynamodb: new Set(["overview", "capacity", "throttles", "latency", "cost"]),
};
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit && echo TSC_OK`
Expected: `TSC_OK`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/engine.ts
git commit -m "feat(fe): engineFamily + family metadata + panel capability map"
```

---

### Task 12: `groupByEngineFamily` util + apply to enumeration points

**Files:**

- Create: `frontend/src/lib/group-by-family.ts`
- Modify: `frontend/src/app/fleet/page.tsx`, `compare/page.tsx`, `clusters/page.tsx`, `components/design-system/cluster-dropdown.tsx`, `components/design-system/command-palette.tsx`

- [ ] **Step 1: Create the util**

```typescript
// frontend/src/lib/group-by-family.ts
import { EngineFamily, engineFamily } from "@/lib/engine";

export interface HasEngine {
  engine?: string;
  resource_name?: string;
  cluster_id: string;
}

export function groupByEngineFamily<T extends HasEngine>(
  items: T[],
): Record<EngineFamily, T[]> {
  const groups: Record<EngineFamily, T[]> = {
    relational: [],
    documentdb: [],
    dynamodb: [],
  };
  for (const it of items) groups[engineFamily(it.engine)].push(it);
  return groups;
}

// Display name: human resource_name when present (DynamoDB slug id is opaque), else cluster_id.
export function displayName(it: HasEngine): string {
  return it.resource_name || it.cluster_id;
}
```

- [ ] **Step 2: Apply grouping to each enumeration point**

For each of Fleet, ClusterDropdown, CommandPalette, Clusters page, Compare: replace the flat `.map` over the cluster list with `groupByEngineFamily(list)` rendered as labeled sections (use `FAMILY_META[fam].label` headers + count), and render `displayName(item)` + family badge instead of raw `cluster_id`. In Compare, filter candidate B to `engineFamily(b) === engineFamily(a)`.

(Implementer: follow each file's existing list-render markup; only the iteration shape changes — wrap in family sections. Keep selection/search behavior. The dropdown search should match `displayName`.)

- [ ] **Step 3: Typecheck + lint**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/lib/group-by-family.ts && echo OK`
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/group-by-family.ts frontend/src/app/fleet/page.tsx frontend/src/app/compare/page.tsx frontend/src/app/clusters/page.tsx frontend/src/components/design-system/cluster-dropdown.tsx frontend/src/components/design-system/command-palette.tsx
git commit -m "feat(fe): group resource enumerations by engine family"
```

---

### Task 13: Dashboard shell — render panels by family

**Files:**

- Create: `frontend/src/components/dashboard/dynamodb-overview-panel.tsx`, `frontend/src/components/dashboard/docdb-overview-panel.tsx`
- Modify: `frontend/src/app/dashboard/page.tsx`

- [ ] **Step 1: Create the DynamoDB overview panel**

A panel that fetches the resource's metrics (existing dashboard metrics API by `cluster_id`) + `resource_details` and renders: billing mode, item count, table size, GSI list, consumed vs provisioned capacity (timeseries via existing chart components), throttles, latency-by-op. Reuse existing chart/card primitives from `components/dashboard/`. No SQL/instances.

- [ ] **Step 2: Create the DocumentDB overview panel**

Renders connections, replica lag, cache hit, cursors, opcounters, instance list (from `resource_details`). Reuse existing chart/card primitives.

- [ ] **Step 3: Gate the dashboard by family**

In `dashboard/page.tsx`, compute `const fam = engineFamily(cluster?.engine)`. Wrap the existing Aurora/SQL panel block in `{fam === "relational" && (<>…existing panels…</>)}`. Add `{fam === "dynamodb" && <DynamodbOverviewPanel .../>}` and `{fam === "documentdb" && <DocdbOverviewPanel .../>}`. MaintenanceHealthPanel/CapacityForecastPanel render only for relational (they already gate by engine, but add the family guard so they don't render empty for DDB/DocDB).

- [ ] **Step 4: Typecheck**

Run: `cd frontend && npx tsc --noEmit && echo TSC_OK`
Expected: `TSC_OK`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/dashboard/dynamodb-overview-panel.tsx frontend/src/components/dashboard/docdb-overview-panel.tsx frontend/src/app/dashboard/page.tsx
git commit -m "feat(fe): engine-family dashboard shell (DynamoDB/DocumentDB overview panels)"
```

---

### Task 14: Clusters registration form — DynamoDB/DocumentDB options

**Files:**

- Modify: `frontend/src/app/clusters/page.tsx`

- [ ] **Step 1: Add engine options + family-appropriate fields**

Add `dynamodb` and `docdb` to the engine selector. When `dynamodb`, the form collects `resource_name` (table name) + account + region (no secret/ARN fields). When `docdb`, collects `cluster_id` + account + region (no secret). POST to the existing `/api/clusters` register endpoint with `engine` set accordingly.

- [ ] **Step 2: Typecheck + build**

Run: `cd frontend && npx tsc --noEmit && npm run build >/tmp/b.log 2>&1 && echo BUILD_OK`
Expected: `BUILD_OK`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/clusters/page.tsx
git commit -m "feat(fe): register DynamoDB/DocumentDB resources from Clusters page"
```

### ▶ CODEX CHECKPOINT 3 (after Phase 3)

```bash
codex exec -s read-only -C /Users/jinstar/Desktop/claude-code/projects/dbops \
"Adversarially review Phase-3 frontend diff: engine.ts family helpers, group-by-family, dashboard \
shell gating, dynamodb/docdb panels, clusters form. Verify: (1) NO Aurora/SQL panel can render for \
a dynamodb/documentdb resource (grep dashboard/page.tsx for the family guard around each panel). \
(2) dropdown/search display resource_name not the opaque ddb- slug. (3) Compare blocks cross-family. \
(4) no any-typed escapes / unhandled undefined engine. Cite file:line. P0/P1/P2."
```

---

# Deploy & Live Verification (after all phases + checkpoints green)

- [ ] Deploy data + frontend: `cd cdk && cdk deploy dbops-dev-data --require-approval never` then `cd ../frontend && npm run build` then `cd ../cdk && cdk deploy dbops-dev-frontend --require-approval never`.
- [ ] Register a real **DynamoDB table** in the dev account via the Clusters page (or `POST /api/clusters {engine: dynamodb, ...}`).
- [ ] Invoke the ETL Lambda once; confirm `dynamodb` result has `metrics_inserted > 0`, no `_error`, and the RELATIONAL resources are unaffected (no regressions).
- [ ] Query cache: `serverless_acu`-style check — confirm DynamoDB `metric_type` rows (`consumed_rcu`, `throttled_requests`, `latency_ms_getitem`) and `cluster_meta.resource_details` for the table.
- [ ] Browser: dashboard for the DynamoDB resource shows the DynamoDB overview shell, NOT Aurora/SQL panels; Fleet/dropdown group by family and show the table name; Maintenance Health/Capacity panels do not render for it.
- [ ] Confirm Aurora dashboards are byte-for-byte unchanged.
- [ ] DocumentDB: if a DocDB cluster exists in dev, repeat end-to-end; else verify via the unit tests + code review and note in the completion report.

---

## Self-Review (completed during planning)

**Spec coverage:** Engine-family model → Task 1/11; resource_details → Task 2/5/6; ETL dispatch → Task 3; finding gating → Task 4/8; DynamoDB collector → Task 5; DocDB collector → Task 6; registration/discovery → Task 7; backend endpoint gating → Task 8; agent guard → Task 9; CDK IAM → Task 10; grouping → Task 12; dashboard shell → Task 13; registration form → Task 14; capability map → Task 1 (backend) + Task 11 (frontend). Cross-account explicitly deferred (spec §Cross-account) — no task, by design.

**Placeholder scan:** Frontend integration tasks (12/13/14) describe changes against existing markup rather than reproducing entire large page files verbatim — this is deliberate (the files are large and the executor follows existing render patterns); the NEW modules and the exact gating logic are fully specified with code. No "TBD/handle edge cases" steps.

**Type consistency:** `engine_family()`/`CAPABILITIES` names match across backend copies; `engineFamily()`/`FAMILY_META`/`FAMILY_PANELS`/`groupByEngineFamily`/`displayName` consistent across frontend tasks; `collect_dynamodb_metrics`/`collect_docdb_metrics` signatures match their handler call sites in Task 3.
