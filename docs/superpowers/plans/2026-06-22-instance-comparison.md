# Instance-vs-Instance Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third Compare mode (`instance`) that compares two instances of one Aurora cluster (writer/reader, reader/reader) across the full per-instance CloudWatch metric set.

**Architecture:** ETL collects per-instance CloudWatch metrics into the existing `metric_snapshots` table tagged with `dimensions={"instance","role"}` (additive, non-breaking — coexists with the cluster-level rows). A cluster's instance list lives on `cluster_meta.instances` (JSONB). The dashboard API gains an `/instances` endpoint and an optional `instance=` filter on `/batch-timeseries`. The Compare page adds an `instance` mode reusing the existing chart grid. Cache-first (≈1-min ETL) gives near-real-time + history in one path.

**Tech Stack:** Python 3.12 Lambdas (data-pipeline ETL, api/dashboard), AWS CDK (Python), RDS Data API → Aurora PG cache, Next.js 16 + Recharts frontend, pytest.

## Global Constraints

- CDK-only infrastructure — never modify AWS resources directly (AGENTS.md).
- Non-breaking: existing cluster-level chart queries MUST be unaffected. `_batch_timeseries` groups by `dimensions`, so per-instance rows are excluded with `NOT jsonb_exists(dimensions, 'instance')` when no `instance` is requested.
- Adding an API route REQUIRES regenerating `frontend/public/openapi.json` via `python3 tools/openapi_gen.py` (the `test_openapi_spec` test gates this).
- Korean translation scope: DB jargon stays English (Replica Lag, IOPS…); descriptions/empty-states are Korean.
- Numbers ≥1000 use `fmtDecimal`/`fmtExact`; the existing Compare chart machinery already handles this — reuse it.
- Commits: conventional subject; NO `Co-Authored-By: Claude` trailer; do NOT reference internal roadmaps/wikis. Frontend commits hit a prettier pre-commit hook — if it reformats, `git add -A` and re-commit (do not chain commit+push).
- `cache_execute(sql, params)` is the cache-write callable passed to collectors; named params only (`:name`). RDS Data API SQL: prefer `jsonb_exists(col, 'key')` over the `?` operator.

---

## File Structure

**Increment 1 — Collection (data stack)**

- Create: `data-pipeline/schema_migrator/sql/schema_v18.sql` — add `cluster_meta.instances JSONB`.
- Modify: `data-pipeline/etl_collector/collectors/meta_collector.py` — build + store the instance list.
- Modify: `data-pipeline/etl_collector/collectors/cw_collector.py` — add `collect_cw_instance_metrics()`.
- Modify: `data-pipeline/etl_collector/handler.py` — call the new per-instance collector.
- Test: `tests/unit/data_pipeline/test_cw_instance_metrics.py`, `tests/unit/data_pipeline/test_meta_instances.py`.

**Increment 2 — API (agent stack)**

- Modify: `api/dashboard/handler.py` — `/instances` branch + `_instances()` + `instance=` param on `_batch_timeseries`.
- Modify: `cdk/stacks/agent_stack.py` — register `GET /api/dashboard/{cluster_id}/instances`.
- Modify: `frontend/public/openapi.json` — regenerated.
- Test: `tests/unit/api/test_dashboard_instances.py`.

**Increment 3 — Frontend (Compare)**

- Modify: `frontend/src/lib/api-client.ts` — `fetchClusterInstances()`, `instance?` on `fetchBatchTimeseries`.
- Modify: `frontend/src/app/compare/page.tsx` — `instance` mode (cluster picker → A/B instance pickers → chart grid).

---

## Increment 1 — Per-instance collection

### Task 1: schema_v18 — `cluster_meta.instances` column

**Files:**

- Create: `data-pipeline/schema_migrator/sql/schema_v18.sql`

**Interfaces:**

- Produces: `cluster_meta.instances` JSONB column holding `[{"id","role","class"}]`.

- [ ] **Step 1: Write the migration**

```sql
-- schema_v18 — per-instance comparison: cluster member list on cluster_meta.
-- Holds [{"id":"<DBInstanceIdentifier>","role":"writer|reader","class":"db.r6g.large"}]
-- so the Compare "instance" mode can populate its A/B pickers without a live
-- RDS describe. Populated each cycle by the meta collector.
ALTER TABLE cluster_meta ADD COLUMN IF NOT EXISTS instances JSONB;
```

- [ ] **Step 2: Verify it parses (no apply yet — migrator runs on deploy)**

Run: `python3 -c "import pathlib; print('ok' if 'instances JSONB' in pathlib.Path('data-pipeline/schema_migrator/sql/schema_v18.sql').read_text() else 'missing')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add data-pipeline/schema_migrator/sql/schema_v18.sql
git commit -m "feat(etl): add cluster_meta.instances column (schema_v18)"
```

### Task 2: meta collector stores the instance list

**Files:**

- Modify: `data-pipeline/etl_collector/collectors/meta_collector.py`
- Test: `tests/unit/data_pipeline/test_meta_instances.py`

**Interfaces:**

- Consumes: `cluster["DBClusterMembers"]` (each `{DBInstanceIdentifier, IsClusterWriter}`) from `describe_db_clusters`; `rds_client.describe_db_instances(Filters=[{"Name":"db-cluster-id","Values":[cluster_id]}])` for per-member `DBInstanceClass`.
- Produces: `_build_instance_list(rds_client, cluster_id, members) -> list[dict]` returning `[{"id","role","class"}]`; `collect_cluster_meta` writes it to `cluster_meta.instances`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_meta_instances.py
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "data-pipeline" / "etl_collector" / "collectors" / "meta_collector.py"
_spec = importlib.util.spec_from_file_location("meta_collector", PATH)
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)


def test_build_instance_list_roles_and_class():
    rds = MagicMock()
    rds.describe_db_instances.return_value = {
        "DBInstances": [
            {"DBInstanceIdentifier": "w1", "DBInstanceClass": "db.r6g.large"},
            {"DBInstanceIdentifier": "r1", "DBInstanceClass": "db.r6g.large"},
        ]
    }
    members = [
        {"DBInstanceIdentifier": "w1", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
    ]
    out = mc._build_instance_list(rds, "c1", members)
    assert {"id": "w1", "role": "writer", "class": "db.r6g.large"} in out
    assert {"id": "r1", "role": "reader", "class": "db.r6g.large"} in out


def test_build_instance_list_empty_on_error():
    rds = MagicMock()
    rds.describe_db_instances.side_effect = RuntimeError("denied")
    members = [{"DBInstanceIdentifier": "w1", "IsClusterWriter": True}]
    # falls back to role-only entries (class "") — never raises
    out = mc._build_instance_list(rds, "c1", members)
    assert out == [{"id": "w1", "role": "writer", "class": ""}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/data_pipeline/test_meta_instances.py -q`
Expected: FAIL — `module 'meta_collector' has no attribute '_build_instance_list'`

- [ ] **Step 3: Add `_build_instance_list` to `meta_collector.py`**

Insert after `_writer_instance_class` (before `collect_cluster_meta`):

```python
def _build_instance_list(rds_client, cluster_id: str, members: list) -> list:
    """[{"id","role","class"}] for every cluster member. role from
    DBClusterMembers.IsClusterWriter; class from a single filtered
    describe_db_instances. On describe failure, returns role-only entries
    (class "") so the picker still works."""
    roles = {
        m.get("DBInstanceIdentifier"): ("writer" if m.get("IsClusterWriter") else "reader")
        for m in members
        if m.get("DBInstanceIdentifier")
    }
    classes = {}
    try:
        resp = rds_client.describe_db_instances(
            Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}]
        )
        classes = {
            i["DBInstanceIdentifier"]: i.get("DBInstanceClass", "")
            for i in resp.get("DBInstances", [])
        }
    except Exception as e:
        print(f"[meta] instance list class lookup failed for {cluster_id}: {e}")
    return [
        {"id": iid, "role": role, "class": classes.get(iid, "")}
        for iid, role in roles.items()
    ]
```

- [ ] **Step 4: Wire it into `collect_cluster_meta`**

In `collect_cluster_meta`, after the `instance_class = _writer_instance_class(...)` line, add:

```python
    instances = _build_instance_list(
        rds_client, cluster_id, cluster.get("DBClusterMembers") or []
    )
```

Add `instances` to the INSERT column list (after `instance_class, http_endpoint_enabled,`):

- column list: add `instances,`
- VALUES: add `:instances::jsonb,`
- ON CONFLICT DO UPDATE SET: add `instances = EXCLUDED.instances,`
- params dict: add `"instances": json.dumps(instances),`

Add `import json` at the top of the file if not present.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/data_pipeline/test_meta_instances.py -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add data-pipeline/etl_collector/collectors/meta_collector.py tests/unit/data_pipeline/test_meta_instances.py
git commit -m "feat(etl): collect per-cluster instance list (id/role/class) into cluster_meta"
```

### Task 3: cw_collector — per-instance metrics

**Files:**

- Modify: `data-pipeline/etl_collector/collectors/cw_collector.py`
- Test: `tests/unit/data_pipeline/test_cw_instance_metrics.py`

**Interfaces:**

- Consumes: `cw_client.get_metric_statistics(...)`, `cache_execute(sql, params)`, an `instances` list `[{"id","role",...}]`.
- Produces: `collect_cw_instance_metrics(cw_client, cache_execute, cluster_id, instances) -> dict`. Inserts into `metric_snapshots` with `dimensions = {"instance","role"}`. Module constant `CW_INSTANCE_METRICS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_cw_instance_metrics.py
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "data-pipeline" / "etl_collector" / "collectors" / "cw_collector.py"
_spec = importlib.util.spec_from_file_location("cw_collector", PATH)
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)


def _dp(ts="2026-06-22T00:00:00+00:00", val=42.0):
    import datetime
    return {"Timestamp": datetime.datetime.fromisoformat(ts), "Average": val}


def test_per_instance_uses_instance_dimension_and_tags_rows():
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": [_dp()]}
    writes = []
    def cache_execute(sql, params):
        writes.append((sql, params))
    instances = [{"id": "w1", "role": "writer"}, {"id": "r1", "role": "reader"}]

    out = cw.collect_cw_instance_metrics(client, cache_execute, "c1", instances)

    # CloudWatch queried with DBInstanceIdentifier per instance
    dims = [
        call.kwargs["Dimensions"][0]
        for call in client.get_metric_statistics.call_args_list
    ]
    assert {"Name": "DBInstanceIdentifier", "Value": "w1"} in dims
    assert {"Name": "DBInstanceIdentifier", "Value": "r1"} in dims
    # rows tagged with instance + role in dimensions
    tagged = [json.loads(p["dimensions"]) for _, p in writes]
    assert {"instance": "w1", "role": "writer"} in tagged
    assert {"instance": "r1", "role": "reader"} in tagged
    assert out["metrics_inserted"] == len(writes) > 0


def test_no_instances_is_noop():
    client = MagicMock()
    out = cw.collect_cw_instance_metrics(client, lambda *a: None, "c1", [])
    assert out["metrics_inserted"] == 0
    client.get_metric_statistics.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/data_pipeline/test_cw_instance_metrics.py -q`
Expected: FAIL — `module 'cw_collector' has no attribute 'collect_cw_instance_metrics'`

- [ ] **Step 3: Add the per-instance metric set + collector**

Add after the existing `CW_METRICS` list:

```python
# Instance-dimensioned (DBInstanceIdentifier) metrics for the Compare "instance"
# mode. CPUUtilization is here (not in cluster CW_METRICS) because it's only
# meaningful per instance. AuroraReplicaLag is ~0 on the writer, real on readers.
CW_INSTANCE_METRICS = [
    {"name": "CPUUtilization", "metric_type": "cpu", "stat": "Average"},
    {"name": "AuroraReplicaLag", "metric_type": "replica_lag_ms", "stat": "Average"},
    {"name": "DatabaseConnections", "metric_type": "db_connections", "stat": "Average"},
    {"name": "FreeableMemory", "metric_type": "freeable_memory", "stat": "Average"},
    {"name": "FreeLocalStorage", "metric_type": "free_local_storage", "stat": "Average"},
    {"name": "ReadIOPS", "metric_type": "read_iops", "stat": "Average"},
    {"name": "WriteIOPS", "metric_type": "write_iops", "stat": "Average"},
    {"name": "ReadLatency", "metric_type": "read_latency", "stat": "Average"},
    {"name": "WriteLatency", "metric_type": "write_latency", "stat": "Average"},
    {"name": "NetworkReceiveThroughput", "metric_type": "net_rx", "stat": "Average"},
    {"name": "NetworkTransmitThroughput", "metric_type": "net_tx", "stat": "Average"},
    {"name": "BufferCacheHitRatio", "metric_type": "buffer_cache_hit", "stat": "Average"},
]


def collect_cw_instance_metrics(cw_client, cache_execute, cluster_id, instances):
    """Per-instance CloudWatch metrics tagged dimensions={instance,role}, stored
    alongside the cluster-level rows (which keep dimensions={}). Read by the
    Compare instance mode via the dimensions filter; invisible to cluster-level
    queries (which exclude rows where dimensions has an 'instance' key)."""
    import json
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=10)
    inserted = 0
    errors = []
    for inst in instances or []:
        iid = inst.get("id")
        if not iid:
            continue
        dims_json = json.dumps({"instance": iid, "role": inst.get("role", "")})
        for m in CW_INSTANCE_METRICS:
            try:
                resp = cw_client.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName=m["name"],
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": iid}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=60,
                    Statistics=[m["stat"]],
                )
            except Exception as e:
                errors.append(f"{iid}/{m['metric_type']}: {e}")
                continue
            for dp in resp.get("Datapoints", []):
                value = dp.get(m["stat"])
                if value is None:
                    continue
                cache_execute(
                    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
                    "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dimensions::jsonb) "
                    "ON CONFLICT DO NOTHING",
                    {
                        "cluster_id": cluster_id,
                        "ts": dp["Timestamp"].isoformat(),
                        "metric_type": m["metric_type"],
                        "value": float(value),
                        "dimensions": dims_json,
                    },
                )
                inserted += 1
    return {"cluster_id": cluster_id, "metrics_inserted": inserted, "errors": errors}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/data_pipeline/test_cw_instance_metrics.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/collectors/cw_collector.py tests/unit/data_pipeline/test_cw_instance_metrics.py
git commit -m "feat(etl): collect per-instance CloudWatch metrics tagged dimensions={instance,role}"
```

### Task 4: wire per-instance collection into the ETL handler

**Files:**

- Modify: `data-pipeline/etl_collector/handler.py`

**Interfaces:**

- Consumes: `collect_cluster_meta` return (extend to surface members) OR re-derive members. Simplest: capture the instance list from the meta step's describe and pass to `collect_cw_instance_metrics`.

- [ ] **Step 1: Make `collect_cluster_meta` return the instance list**

In `meta_collector.collect_cluster_meta`, change the final return to include the list:

```python
    cache_execute(sql, params)
    return {"cluster_id": cluster_id, "status": cluster["Status"], "instances": instances}
```

- [ ] **Step 2: Call the per-instance collector in `handler.py`**

In `handler.py`, the `result["meta"] = collect_cluster_meta(...)` block already runs first. Import the new collector at the top (alongside the existing `collect_cw_metrics` import):

```python
from collectors.cw_collector import collect_cw_metrics, collect_cw_instance_metrics
```

(match the existing import style in handler.py — if it imports `from collectors.cw_collector import collect_cw_metrics`, extend that line.)

After the existing `result["cw"] = collect_cw_metrics(cw_client, cache_execute, cluster_id)` block, add:

```python
    try:
        meta_instances = (result.get("meta") or {}).get("instances") or []
        result["cw_instance"] = collect_cw_instance_metrics(
            cw_client, cache_execute, cluster_id, meta_instances
        )
    except Exception as e:
        result["cw_instance_error"] = str(e)
        print(f"[{cluster_id}] cw_instance error: {e}")
```

- [ ] **Step 3: Syntax-check the changed files (no **pycache** concern — data-pipeline)**

Run: `python3 -c "import ast; [ast.parse(open(f).read()) for f in ['data-pipeline/etl_collector/handler.py','data-pipeline/etl_collector/collectors/meta_collector.py']]; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Run the data_pipeline unit suite (regression)**

Run: `python3 -m pytest tests/unit/data_pipeline -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/handler.py data-pipeline/etl_collector/collectors/meta_collector.py
git commit -m "feat(etl): run per-instance metric collection each cycle"
```

### Task 5: deploy + verify collection (manual checkpoint)

- [ ] **Step 1: Deploy the data stack**

Run: `cd cdk && cdk deploy dbops-dev-data --require-approval never` (background; wait for "Total time").

- [ ] **Step 2: Verify per-instance rows + instance list (after ≥1 ETL cycle, ~2 min)**

Query the cache (use the cache cluster ARN/secret) for `dbops-dev-sample-samplepg789869c8-caf4ladtqz0i`:

- `SELECT count(*) FROM metric_snapshots WHERE dimensions ? 'instance' AND ts > NOW() - INTERVAL '10 min'` → > 0
- `SELECT instances FROM cluster_meta WHERE cluster_id = '<id>'` → JSON array with id/role/class

Expected: per-instance rows present; `cluster_meta.instances` populated. (Single-instance clusters: 1 entry, writer.)

---

## Increment 2 — API

### Task 6: `/instances` endpoint + `instance=` filter on batch-timeseries

**Files:**

- Modify: `api/dashboard/handler.py`
- Test: `tests/unit/api/test_dashboard_instances.py`

**Interfaces:**

- Consumes: `query(sql, params)` cache reader, `_response(status, body, max_age=)`, `path_params["cluster_id"]`, `qs`.
- Produces: `_instances(query, cluster_id) -> {"instances":[...]}`; `_batch_timeseries(..., instance=None)`; routes `/instances` and `instance=` on `/batch-timeseries`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_dashboard_instances.py
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "api" / "dashboard" / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler", PATH)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


def test_instances_reads_cluster_meta():
    rows = [{"instances": json.dumps([{"id": "w1", "role": "writer", "class": "db.r6g.large"}])}]
    out = h._instances(lambda sql, params=None: rows, "c1")
    assert out["instances"][0]["id"] == "w1"
    assert out["instances"][0]["role"] == "writer"


def test_instances_empty_when_absent():
    out = h._instances(lambda sql, params=None: [], "c1")
    assert out == {"instances": []}


def test_batch_timeseries_instance_filter_in_sql():
    captured = {}
    def query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params or {}
        return []
    h._batch_timeseries(query, "c1", ["cpu"], 1, instance="r1")
    assert "jsonb_exists" in captured["sql"]
    assert captured["params"].get("inst") == "r1"


def test_batch_timeseries_excludes_instance_rows_by_default():
    captured = {}
    def query(sql, params=None):
        captured["sql"] = sql
        return []
    h._batch_timeseries(query, "c1", ["cpu"], 1)  # no instance
    assert "NOT jsonb_exists(dimensions, 'instance')" in captured["sql"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/api/test_dashboard_instances.py -q`
Expected: FAIL — `_instances` not defined / instance filter missing.

- [ ] **Step 3: Add `_instances()` near `_resource_details`**

```python
def _instances(query, cluster_id):
    """Cluster member list for the Compare instance picker, from
    cluster_meta.instances (populated by the meta collector)."""
    rows = query(
        "SELECT instances::text AS instances FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    if not rows or not rows[0].get("instances"):
        return {"instances": []}
    try:
        import json
        return {"instances": json.loads(rows[0]["instances"]) or []}
    except (ValueError, TypeError):
        return {"instances": []}
```

- [ ] **Step 4: Add `instance` param to `_batch_timeseries`**

Change the signature:

```python
def _batch_timeseries(
    query,
    cluster_id,
    metric_names,
    hours,
    offset_hours=0,
    from_iso=None,
    to_iso=None,
    instance=None,
):
```

In the WHERE clause of `select_head`, after `metric_type IN ({placeholders})`, add the dimensions filter. Replace the existing `select_head` WHERE tail so it reads:

```python
    inst_clause = (
        " AND dimensions->>'instance' = :inst"
        if instance
        else " AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))"
    )
    select_head = (
        f"SELECT {_BUCKET_TS_EXPR} AS ts, metric_type, "
        f"AVG(value)::double precision AS value, dimensions::text AS dimensions "
        f"FROM metric_snapshots "
        f"WHERE cluster_id = :cid AND metric_type IN ({placeholders})"
        f"{inst_clause} "
    )
    if instance:
        params["inst"] = instance
```

(Place the `params["inst"]` assignment after `params` is initialized and before the query runs. Add `"instance": instance` to `base_meta` so the response echoes it.)

- [ ] **Step 5: Route `/instances` + pass `instance=` to batch-timeseries**

In `lambda_handler`, add before the final `return _response(200, _overview(...))`:

```python
        if raw_path.endswith("/instances"):
            return _response(200, _instances(query, cluster_id), max_age=30)
```

In the existing `/batch-timeseries` branch, read the param and pass it:

```python
            instance = (qs.get("instance") or "").strip() or None
```

and add `instance=instance,` to the `_batch_timeseries(...)` call args.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/api/test_dashboard_instances.py -q`
Expected: PASS (4 passed)

- [ ] **Step 7: Regression — existing dashboard tests unaffected**

Run: `python3 -m pytest tests/unit/api -q`
Expected: PASS (all)

- [ ] **Step 8: Commit**

```bash
git add api/dashboard/handler.py tests/unit/api/test_dashboard_instances.py
git commit -m "feat(dashboard): /instances endpoint + instance filter on batch-timeseries"
```

### Task 7: register the `/instances` route (CDK) + openapi

**Files:**

- Modify: `cdk/stacks/agent_stack.py`
- Modify: `frontend/public/openapi.json` (regenerated)

**Interfaces:**

- Consumes: existing `dashboard_alias` integration + `self.api.add_routes` pattern.

- [ ] **Step 1: Add the route**

Find an existing dashboard GET route registration (e.g. `path="/api/dashboard/{cluster_id}/resource-details"`) and add, right after it:

```python
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/instances",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration(
                "DashboardInstancesIntegration", dashboard_alias
            ),
        )
```

(Use the SAME integration object/alias the neighboring dashboard routes use — copy the exact variable name from the adjacent route.)

- [ ] **Step 2: Synthesize to validate the route**

Run: `cd cdk && cdk synth dbops-dev-agent --quiet 2>&1 | tail -3`
Expected: no error (exit 0).

- [ ] **Step 3: Regenerate the OpenAPI spec**

Run: `python3 tools/openapi_gen.py`
Then verify: `python3 -c "import json; d=json.load(open('frontend/public/openapi.json')); print('/api/dashboard/{cluster_id}/instances' in d['paths'])"`
Expected: `True`

- [ ] **Step 4: Run the openapi parity test**

Run: `python3 -m pytest tests/unit/test_openapi_spec.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cdk/stacks/agent_stack.py frontend/public/openapi.json
git commit -m "feat(dashboard): register /instances route"
```

### Task 8: deploy + verify API (manual checkpoint)

- [ ] **Step 1: Deploy the agent stack**

Run: `cd cdk && cdk deploy dbops-dev-agent --require-approval never` (background; wait for completion).

- [ ] **Step 2: Verify endpoints (browser devtools or authed curl)**

- `GET /api/dashboard/<samplepg-id>/instances` → `{"instances":[{id,role,class},...]}`
- `GET /api/dashboard/<samplepg-id>/batch-timeseries?metrics=cpu&hours=1&instance=<writer-id>` → series for that instance only.
- `GET .../batch-timeseries?metrics=cpu&hours=1` (no instance) → cluster-level only (no instance series — regression check).

---

## Increment 3 — Frontend Compare instance mode

### Task 9: api-client — instances fetch + instance param

**Files:**

- Modify: `frontend/src/lib/api-client.ts`

**Interfaces:**

- Produces: `fetchClusterInstances(clusterId) -> Promise<{instances: {id:string; role:string; class:string}[]}>`; `fetchBatchTimeseries(clusterId, metrics, rangeOrHours, offsetHours, instance?)`.

- [ ] **Step 1: Add `fetchClusterInstances`**

Near `fetchClusters` (use the existing `authedFetch` + `api()` + `enc()` helpers):

```typescript
export interface ClusterInstance {
  id: string;
  role: string; // writer | reader
  class: string;
}

export async function fetchClusterInstances(
  clusterId: string,
): Promise<{ instances: ClusterInstance[] }> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/instances`),
  );
  if (!res.ok) throw new Error(`인스턴스 목록 조회 실패 (상태 ${res.status})`);
  return res.json();
}
```

- [ ] **Step 2: Add optional `instance` to `fetchBatchTimeseries`**

Locate the `fetchBatchTimeseries` signature + its URL build (`?metrics=${csv}&${rangeQs(...)}${offsetQs}`). Add a trailing optional `instance?: string` parameter and append `&instance=<enc>` to the query string when set. Keep all existing callers working (param is optional, omitted = current behavior).

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx --no-install tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api-client.ts
git commit -m "feat(compare): api-client — fetchClusterInstances + instance param"
```

### Task 10: Compare `instance` mode

**Files:**

- Modify: `frontend/src/app/compare/page.tsx`

**Interfaces:**

- Consumes: `fetchClusterInstances`, `fetchBatchTimeseries(..., instance)`, existing `ClusterPicker`, chart grid, `seriesA`/`seriesB`.

- [ ] **Step 1: Extend the Mode type + state**

- `type Mode = "cluster" | "period" | "instance";`
- Add state: `instanceCluster` (string), `instanceA` (string), `instanceB` (string), `clusterInstances` (`ClusterInstance[]`).
- On `instanceCluster` change (useEffect), call `fetchClusterInstances(instanceCluster)` → `setClusterInstances`, and default `instanceA`/`instanceB` to the first two members (writer + first reader if present).

- [ ] **Step 2: Add the mode toggle button**

Next to the existing `cluster`/`period` buttons, add an `instance` button mirroring their markup (active style when `mode === "instance"`), label `"인스턴스"`.

- [ ] **Step 3: Add the picker block for instance mode**

In the picker section, add an `instance` branch: a `ClusterPicker` bound to `instanceCluster`, then two instance `<select>`s (A and B) populated from `clusterInstances`, each option labeled `"<id> (<role>)"`. Reuse the `ClusterPicker` styling; for instances use a sibling `InstancePicker` native `<select>` (mirror `ClusterPicker`'s markup, options = clusterInstances).

- [ ] **Step 4: Add the instance load effect**

Mirror the `cluster` load effect (the one calling `fetchBatchTimeseries(clusterA, ...)` and `fetchBatchTimeseries(clusterB, ...)`). Guard on `mode === "instance" && instanceCluster && instanceA && instanceB && instanceA !== instanceB`. Fetch:

```typescript
fetchBatchTimeseries(instanceCluster, metricIds, hours, 0, instanceA),
fetchBatchTimeseries(instanceCluster, metricIds, hours, 0, instanceB),
```

Set `seriesA`/`seriesB` from the two results (same shape as cluster mode → the existing chart grid renders unchanged).

- [ ] **Step 5: Labels**

`labelA`/`labelB` in instance mode = `instanceA` / `instanceB` (the instance ids). The chart grid + the existing legend reuse these.

- [ ] **Step 6: Engine gating**

Instance mode is same-cluster, so both instances share the cluster's engine. Set the `showPgOnly` logic from `instanceCluster`'s engine (reuse the existing `clusters.find(...).engine` pattern used for `clusterA`).

- [ ] **Step 7: Build (typecheck + static export)**

Run: `cd frontend && npm run build`
Expected: exit 0; `/compare` in the route list.

- [ ] **Step 8: Commit (mind the prettier hook)**

```bash
git add frontend/src/app/compare/page.tsx
git commit -m "feat(compare): instance-vs-instance mode (writer/reader, reader/reader)"
```

If the prettier hook reformats and aborts: `git add -A` then re-run the same commit.

### Task 11: deploy frontend + end-to-end verify (manual checkpoint)

- [ ] **Step 1: Deploy frontend**

Run: `aws s3 sync frontend/out/ s3://dbops-dev-frontend-123456789012/ --delete --exclude config.json --region ap-northeast-2` then `aws cloudfront create-invalidation --distribution-id E1234567890ABC --paths "/*"` and wait for `Completed`.

- [ ] **Step 2: Browser E2E**

On `/compare`: switch to **인스턴스** mode → pick a cluster (e.g. samplepg/samplemysql) → the A/B instance pickers populate (with role) → select two instances → the 2×3 grid renders per-instance series. For a single-instance cluster, only one instance shows (expected). Switch back to `cluster`/`period` and confirm those still work (regression).

---

## Self-Review

**Spec coverage:**

- Spec §2.1 (additive metric_snapshots, dimensions filter) → Task 3 (write) + Task 6 Step 4 (read filter, non-breaking). ✓
- Spec §2.2 (per-instance metric set) → Task 3 `CW_INSTANCE_METRICS`. ✓
- Spec §2.3 (instances on cluster_meta) → Task 1 (column) + Task 2 (populate). ✓
- Spec §3.2 (API: /instances + instance filter) → Task 6, Task 7. ✓
- Spec §3.3 (frontend instance mode) → Tasks 9–10. ✓
- Spec §6 (tests) → Tasks 2,3,6 unit; Tasks 5,8,11 e2e checkpoints. ✓

**Placeholder scan:** none — every code/test step has literal content; mirror-this-pattern steps (Task 9 Step 2, Task 10) point at named existing functions with exact param additions.

**Type consistency:** instance row shape `{instance, role}` (Task 3) == filter `dimensions->>'instance'` / `jsonb_exists(dimensions,'instance')` (Task 6) == `_instances` array `{id,role,class}` (Task 2/6) == `ClusterInstance {id,role,class}` (Task 9) == picker options (Task 10). `collect_cw_instance_metrics(cw_client, cache_execute, cluster_id, instances)` signature consistent across Tasks 3–4. ✓
