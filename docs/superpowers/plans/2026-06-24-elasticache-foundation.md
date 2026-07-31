# ElastiCache EC-1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ElastiCache (Redis/Valkey/Memcached) a registered, metric-collected, dashboard-visible DBOps engine family — read-only, CloudWatch-only — reusing the existing multi-engine abstractions.

**Architecture:** Add an `elasticache` engine family to the canonical `engine_family()`/`CAPABILITIES` model (4 synced Python copies + the frontend mirror), then fill the existing engine branch points: registration/discovery in `api/clusters`, a CloudWatch ETL collector + dispatch branch, and a dashboard panel. No new infra construct.

**Tech Stack:** Python 3.12 Lambdas (boto3 `elasticache` + `cloudwatch`), AWS CDK (Python), Next.js 16 (TypeScript).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **5-copy sync — VERBATIM:** `engine_family()` + `CAPABILITIES` are duplicated in `api/clusters/engine_family.py`, `api/dashboard/engine_family.py`, `data-pipeline/etl_collector/collectors/engine_family.py`, `mcp-servers/mcp_servers/shared/engine_family.py`. All four MUST get the identical edit. The frontend mirror is `frontend/src/lib/engine.ts`.
- **Engines in scope:** Redis OSS, Valkey, Memcached (node clusters + Redis/Valkey replication groups incl. cluster-mode). Serverless ElastiCache OUT of scope.
- **EC-1 is read-only:** only `elasticache:Describe*` + CloudWatch reads. No mutation, no secret, no protocol connection (those are EC-3/EC-4).
- **Registry PK = the real ElastiCache name** (matches the `^[a-zA-Z0-9-]{1,63}$` validator; no slug, unlike DynamoDB).
- **metric_snapshots row shape:** `INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions)` — `ts` is `timestamptz`, `dimensions` is `jsonb`, `ON CONFLICT DO NOTHING`. Cluster-level rows carry `dimensions = '{}'`.
- **Capability flags declared now, behavior later:** `CAPABILITIES[elasticache]` declares `findings`/`simulation`/`elasticache_write`/`live_read` but EC-1 acts only on metrics; later specs flip behavior without re-touching the map.
- If any new API route is added, regenerate `frontend/public/openapi.json` via `python tools/openapi_gen.py` (route-table parity test). EC-1 adds NO new route (registration reuses `/api/clusters`).

---

### Task 1: Engine-family model — add `elasticache` to all 5 copies

**Files:**

- Modify: `api/clusters/engine_family.py`, `api/dashboard/engine_family.py`, `data-pipeline/etl_collector/collectors/engine_family.py`, `mcp-servers/mcp_servers/shared/engine_family.py` (identical edit to each)
- Modify: `frontend/src/lib/engine.ts`
- Test: `tests/unit/test_engine_family.py` (create if absent; otherwise extend)

**Interfaces:**

- Produces: `engine_family("redis"|"valkey"|"memcached"|"elasticache-redis") == "elasticache"`; `CAPABILITIES["elasticache"]` dict; frontend `engineFamily()` returns `"elasticache"`, `FAMILY_META.elasticache`, `FAMILY_PANELS.elasticache`.

- [ ] **Step 1: Write the failing test.** Create/extend `tests/unit/test_engine_family.py`. Import each of the 4 copies and assert identical behavior:

```python
"""All four engine_family.py copies must classify ElastiCache identically."""
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_COPIES = [
    _ROOT / "api/clusters/engine_family.py",
    _ROOT / "api/dashboard/engine_family.py",
    _ROOT / "data-pipeline/etl_collector/collectors/engine_family.py",
    _ROOT / "mcp-servers/mcp_servers/shared/engine_family.py",
]


def _load(p):
    spec = importlib.util.spec_from_file_location(f"ef_{abs(hash(str(p)))}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_all_copies_classify_elasticache():
    for p in _COPIES:
        m = _load(p)
        assert m.engine_family("redis") == "elasticache"
        assert m.engine_family("valkey") == "elasticache"
        assert m.engine_family("memcached") == "elasticache"
        assert m.engine_family("elasticache-redis") == "elasticache"
        # unchanged families
        assert m.engine_family("aurora-postgresql") == "relational"
        assert m.engine_family("docdb") == "documentdb"
        assert m.engine_family("dynamodb") == "dynamodb"
        caps = m.CAPABILITIES["elasticache"]
        assert caps["sql"] is False
        assert caps["cw_namespace"] == "AWS/ElastiCache"
        assert caps["elasticache_write"] is True
        assert caps["live_read"] is True
        assert caps["findings"] == {"elasticache"}
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `python -m pytest tests/unit/test_engine_family.py -q`
Expected: FAIL (`KeyError: 'elasticache'` / `engine_family("redis") == "relational"`).

- [ ] **Step 3: Apply the identical edit to all 4 Python copies.** In each `engine_family.py`, add the constant after the existing ones:

```python
ELASTICACHE = "elasticache"
```

In `engine_family()`, add the detection BEFORE the `return RELATIONAL` fallback (after the dynamodb branch):

```python
    if "redis" in e or "valkey" in e or "memcached" in e or "elasticache" in e:
        return ELASTICACHE
```

Add to the `CAPABILITIES` dict:

```python
    ELASTICACHE: {
        "sql": False, "rds_meta": False, "perf_insights": False,
        "simulation": True,
        "elasticache_write": True,
        "live_read": True,
        "cw_namespace": "AWS/ElastiCache",
        "findings": {"elasticache"},
    },
```

- [ ] **Step 4: Edit the frontend mirror `frontend/src/lib/engine.ts`.**

  - Extend the union: `export type EngineFamily = "relational" | "documentdb" | "dynamodb" | "elasticache";`
  - In `engineFamily()`, add before `return "relational";`:
    ```typescript
    if (
      e.includes("redis") ||
      e.includes("valkey") ||
      e.includes("memcached") ||
      e.includes("elasticache")
    )
      return "elasticache";
    ```
  - Add to `FAMILY_META`:
    ```typescript
      elasticache: {
        label: "ElastiCache",
        noun: "클러스터",
        accent: "bg-rose-400",
        classes: "bg-rose-500/15 text-rose-300 border-rose-500/40",
      },
    ```
  - Add to `FAMILY_PANELS`:
    ```typescript
      elasticache: new Set([
        "overview",
        "memory",
        "hitRate",
        "connections",
        "evictions",
        "throughput",
        "replicationLag",
      ]),
    ```

- [ ] **Step 5: Run tests + typecheck.**

Run: `python -m pytest tests/unit/test_engine_family.py -q` → PASS.
Run: `cd frontend && npx tsc --noEmit` → no errors (the `Record<EngineFamily, ...>` maps now require the `elasticache` key, which Step 4 added).

- [ ] **Step 6: Commit.**

```bash
git add api/clusters/engine_family.py api/dashboard/engine_family.py data-pipeline/etl_collector/collectors/engine_family.py mcp-servers/mcp_servers/shared/engine_family.py frontend/src/lib/engine.ts tests/unit/test_engine_family.py
git commit -m "feat(elasticache): add elasticache engine family to canonical model + frontend mirror"
```

---

### Task 2: Registration + discovery + IAM

**Files:**

- Modify: `api/clusters/handler.py` (add `_elasticache_client_for`, `_register_elasticache`, `_handle_register` dispatch, discover enumeration)
- Modify: `cdk/stacks/agent_stack.py` (clusters Lambda IAM: elasticache describe)
- Test: `tests/unit/api/test_clusters_elasticache.py` (create)

**Interfaces:**

- Consumes: `engine_family()` (Task 1), `_session_for(region, role_arn)` (existing), `_resp()` (existing).
- Produces: `_register_elasticache(table, body)` → registry row with `engine_family="elasticache"`, `resource_type="elasticache-{engine}"`, `resource_details` JSONB.

- [ ] **Step 1: Read the templates.** Read `api/clusters/handler.py`: `_session_for` (~243), `_ddb_client_for`/`_docdb_client_for` (~269-274), `_register_dynamodb`/`_register_docdb` (~410-462), `_handle_register` (~465), and the discover enumeration (`_handle_discover` ~541 + the rds/dynamodb/docdb enumeration ~313-398). Mirror these exactly.

- [ ] **Step 2: Write the failing test.** Create `tests/unit/api/test_clusters_elasticache.py`:

```python
"""ElastiCache registration via api/clusters handler."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_H = Path(__file__).resolve().parents[3] / "api" / "clusters" / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler", _H)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _table():
    t = MagicMock()
    t.put_item.return_value = {}
    return t


def _body(name="my-redis", engine="redis"):
    return {"account_id": "111122223333", "region": "ap-northeast-2",
            "resource_name": name, "engine": engine}


def test_register_redis_replication_group():
    fake = MagicMock()
    fake.describe_replication_groups.return_value = {
        "ReplicationGroups": [{
            "ReplicationGroupId": "my-redis", "Status": "available",
            "ClusterEnabled": True, "AuthTokenEnabled": True,
            "TransitEncryptionEnabled": True,
            "NodeGroups": [{"NodeGroupId": "0001"}, {"NodeGroupId": "0002"}],
            "MemberClusters": ["my-redis-0001-001", "my-redis-0001-002"],
            "CacheNodeType": "cache.r7g.large",
        }]
    }
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, _body())
    assert r["statusCode"] in (201, 207)
    item = table.put_item.call_args.kwargs["Item"]
    assert item["engine_family"] == "elasticache"
    assert item["resource_type"] == "elasticache-redis"
    assert item["cluster_id"] == "my-redis"   # real name, no slug
    rd = item["resource_details"]
    assert rd["cluster_mode"] is True and rd["num_node_groups"] == 2


def test_register_memcached_cache_cluster_fallback():
    fake = MagicMock()
    # not a replication group → fall back to cache cluster
    fake.describe_replication_groups.side_effect = Exception("ReplicationGroupNotFoundFault")
    fake.describe_cache_clusters.return_value = {
        "CacheClusters": [{
            "CacheClusterId": "my-memcached", "Engine": "memcached",
            "EngineVersion": "1.6.22", "CacheClusterStatus": "available",
            "CacheNodeType": "cache.t4g.small", "NumCacheNodes": 3,
        }]
    }
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, _body(name="my-memcached", engine="memcached"))
    assert r["statusCode"] in (201, 207)
    item = table.put_item.call_args.kwargs["Item"]
    assert item["resource_type"] == "elasticache-memcached"
    assert item["resource_details"]["num_cache_nodes"] == 3


def test_register_not_found_warns():
    fake = MagicMock()
    fake.describe_replication_groups.side_effect = Exception("not found")
    fake.describe_cache_clusters.side_effect = Exception("CacheClusterNotFound")
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, _body())
    assert r["statusCode"] == 207  # registered_with_warning
    assert table.put_item.call_args.kwargs["Item"]["connection_status"] == "failed"


def test_handle_register_dispatches_elasticache():
    fake = MagicMock()
    fake.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "r", "Status": "available", "ClusterEnabled": False,
         "MemberClusters": ["r-001"], "CacheNodeType": "cache.t4g.micro"}]}
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._handle_register(table, _body(name="r", engine="redis"))
    assert r["statusCode"] in (201, 207)
    assert table.put_item.call_args.kwargs["Item"]["engine_family"] == "elasticache"


def test_register_missing_fields_400():
    r = handler._register_elasticache(_table(), {"engine": "redis"})
    assert r["statusCode"] == 400
```

- [ ] **Step 3: Run it to verify it fails.**

Run: `python -m pytest tests/unit/api/test_clusters_elasticache.py -q`
Expected: FAIL (`AttributeError: _register_elasticache` / `_elasticache_client_for`).

- [ ] **Step 4: Add the client helper.** In `api/clusters/handler.py`, next to `_ddb_client_for`/`_docdb_client_for`:

```python
def _elasticache_client_for(region: str, role_arn: str = ""):
    return _session_for(region, role_arn).client("elasticache")
```

- [ ] **Step 5: Add `_register_elasticache`.** Place it next to `_register_docdb`:

```python
def _register_elasticache(table, body):
    for f in ("account_id", "region", "resource_name"):
        if not body.get(f):
            return _resp(400, {"error": f"{f} required"})
    account_id, region, name = body["account_id"], body["region"], body["resource_name"]
    role_arn = body.get("spoke_role_arn", "")
    cli = _elasticache_client_for(region, role_arn)
    status, err = "ok", ""
    engine = (body.get("engine") or "redis").lower()
    details = {}
    # Try a Redis/Valkey replication group first; fall back to a standalone /
    # Memcached cache cluster (a name can be either).
    try:
        rg = (cli.describe_replication_groups(ReplicationGroupId=name)
              .get("ReplicationGroups") or [])
        if rg:
            g = rg[0]
            node_groups = g.get("NodeGroups") or []
            members = g.get("MemberClusters") or []
            details = {
                "engine": engine, "status": g.get("Status", ""),
                "cluster_mode": bool(g.get("ClusterEnabled", False)),
                "num_node_groups": len(node_groups),
                "replicas_per_node_group": max(0, (len(members) // max(1, len(node_groups))) - 1),
                "node_type": g.get("CacheNodeType", ""),
                "auth_enabled": bool(g.get("AuthTokenEnabled", False)),
                "tls_enabled": bool(g.get("TransitEncryptionEnabled", False)),
            }
        else:
            raise Exception("no replication group")
    except Exception:
        try:
            cc = (cli.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)
                  .get("CacheClusters") or [])
            if cc:
                c = cc[0]
                engine = (c.get("Engine") or engine).lower()
                details = {
                    "engine": engine, "status": c.get("CacheClusterStatus", ""),
                    "engine_version": c.get("EngineVersion", ""),
                    "node_type": c.get("CacheNodeType", ""),
                    "num_cache_nodes": c.get("NumCacheNodes", 0),
                    "cluster_mode": False,
                    "auth_enabled": bool(c.get("AuthTokenEnabled", False)),
                    "tls_enabled": bool(c.get("TransitEncryptionEnabled", False)),
                }
            else:
                status, err = "failed", "not found"
        except Exception as e:
            status, err = "failed", str(e)[:300]
    item = {
        "cluster_id": name, "account_id": account_id, "region": region,
        "engine": engine, "engine_family": "elasticache",
        "resource_name": name, "resource_type": f"elasticache-{engine}",
        "resource_details": details,
        "requires_secret_for_foundation": False,
        "spoke_role_arn": role_arn,
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": status, "connection_error": err,
    }
    table.put_item(Item=item)
    return _resp(201 if status == "ok" else 207,
                 {"status": "registered" if status == "ok" else "registered_with_warning",
                  "cluster_id": name, "connection_status": status})
```

- [ ] **Step 6: Add the dispatch branch** to `_handle_register` (after the documentdb branch, before the relational body):

```python
    if fam == "elasticache":
        return _register_elasticache(table, body)
```

- [ ] **Step 7: Add discovery enumeration.** In the discover path (mirror the dynamodb `list_tables` / docdb `describe_db_clusters` blocks ~356-398), add an ElastiCache enumeration that appends candidates to the discover results. Use the existing per-region try/except + print-on-failure pattern:

```python
    try:
        ec = _session_for(region, role_arn).client("elasticache")
        for rg in (ec.get_paginator("describe_replication_groups")
                   .paginate()):
            for g in rg.get("ReplicationGroups", []):
                discovered.append({
                    "cluster_id": g["ReplicationGroupId"],
                    "engine": "redis", "engine_family": "elasticache",
                    "resource_type": "elasticache-redis",
                    "account_id": account_id, "region": region,
                })
        for cc in (ec.get_paginator("describe_cache_clusters")
                   .paginate(ShowCacheNodeInfo=False)):
            for c in cc.get("CacheClusters", []):
                # replication-group members are already covered above; skip them
                if c.get("ReplicationGroupId"):
                    continue
                eng = (c.get("Engine") or "redis").lower()
                discovered.append({
                    "cluster_id": c["CacheClusterId"],
                    "engine": eng, "engine_family": "elasticache",
                    "resource_type": f"elasticache-{eng}",
                    "account_id": account_id, "region": region,
                })
    except Exception as e:
        print(f"[discover] elasticache failed in {region}: {e}")
```

(Adapt the variable names — `discovered`, `account_id`, `role_arn` — to whatever the existing discover function uses; read the function first and match it. If the discover entries use a different dict shape, match that shape.)

- [ ] **Step 8: Add IAM.** In `cdk/stacks/agent_stack.py`, the clusters Lambda IAM block (~line 747, where `dynamodb:ListTables`/`DescribeTable` is granted), add a statement (or extend) for ElastiCache describe:

```python
        clusters_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
            ],
            resources=["*"],
        ))
```

- [ ] **Step 9: Run tests + synth.**

Run: `python -m pytest tests/unit/api/test_clusters_elasticache.py -q` → PASS.
Run: `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 10: Commit.**

```bash
git add api/clusters/handler.py cdk/stacks/agent_stack.py tests/unit/api/test_clusters_elasticache.py
git commit -m "feat(elasticache): register + discover ElastiCache clusters (replication-group + cache-cluster) + IAM"
```

---

### Task 3: ETL CloudWatch collector + dispatch + IAM

**Files:**

- Create: `data-pipeline/etl_collector/collectors/elasticache_cw_collector.py`
- Modify: `data-pipeline/etl_collector/handler.py` (`_collect_one` dispatch branch + import)
- Modify: `cdk/stacks/agent_stack.py` (ETL Lambda IAM: elasticache describe — only if the ETL role lacks it; cloudwatch read it already has)
- Test: `tests/unit/data_pipeline/test_elasticache_collector.py` (create)

**Interfaces:**

- Consumes: `cache_execute` (runs the metric_snapshots INSERT), a CloudWatch client, an ElastiCache client. Mirrors `collect_dynamodb_metrics(cw, dynamo, cache_execute, cluster_id, table_name, account_id, region)`.
- Produces: `collect_elasticache_metrics(cw, ec, cache_execute, cluster_id, resource_name, engine, region, account_id) -> dict`.

- [ ] **Step 1: Read the template.** Read `data-pipeline/etl_collector/collectors/dynamodb_cw_collector.py` in full (the `_insert` helper, `get_metric_statistics`-based `pull`, the per-metric loops, the cluster_meta upsert) and `data-pipeline/etl_collector/handler.py` `_collect_one` (the dynamodb + documentdb branches). The new collector mirrors these.

- [ ] **Step 2: Write the failing test.** Create `tests/unit/data_pipeline/test_elasticache_collector.py`:

```python
"""ElastiCache CloudWatch collector → metric_snapshots."""
import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/elasticache_cw_collector.py"
_spec = importlib.util.spec_from_file_location("ec_collector", _C)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _cw_with(value=42.0):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": datetime(2026, 6, 24, 0, 0, 0), "Average": value, "Sum": value}]
    }
    return cw


def test_redis_metrics_inserted_with_types():
    cw = _cw_with()
    ec = MagicMock()
    rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            rows.append(params["metric_type"])
    res = mod.collect_elasticache_metrics(cw, ec, cache_execute, "my-redis", "my-redis", "redis", "ap-northeast-2", "111122223333")
    assert res["metrics_inserted"] > 0
    # representative Redis metric types present
    assert "memory_usage_pct" in rows
    assert "evictions" in rows
    assert "curr_connections" in rows


def test_memcached_branch_subset():
    cw = _cw_with()
    ec = MagicMock()
    rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            rows.append(params["metric_type"])
    mod.collect_elasticache_metrics(cw, ec, cache_execute, "mc", "mc", "memcached", "ap-northeast-2", "111122223333")
    # memcached has no replication lag / DatabaseMemoryUsagePercentage
    assert "replication_lag" not in rows
    assert "get_hits" in rows or "curr_items" in rows


def test_empty_series_tolerated():
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    ec = MagicMock()
    res = mod.collect_elasticache_metrics(cw, ec, lambda *a, **k: None, "r", "r", "redis", "ap-northeast-2", "1")
    assert res["metrics_inserted"] == 0  # no rows, no raise
```

- [ ] **Step 3: Run it to verify it fails.**

Run: `python -m pytest tests/unit/data_pipeline/test_elasticache_collector.py -q`
Expected: FAIL (collector module does not exist).

- [ ] **Step 4: Create `data-pipeline/etl_collector/collectors/elasticache_cw_collector.py`:**

```python
# data-pipeline/etl_collector/collectors/elasticache_cw_collector.py
"""ElastiCache CloudWatch → cache. Namespace AWS/ElastiCache, dimension
CacheClusterId. Redis/Valkey use the full metric set; Memcached uses a subset
(no replication/persistence). Cluster-level rows only (dimensions='{}')."""
from datetime import datetime, timedelta

# (metric_name, metric_type, statistic)
_REDIS_METRICS = [
    ("CPUUtilization", "cache_cpu", "Average"),
    ("EngineCPUUtilization", "engine_cpu", "Average"),
    ("DatabaseMemoryUsagePercentage", "memory_usage_pct", "Average"),
    ("BytesUsedForCache", "bytes_used", "Average"),
    ("CacheHits", "cache_hits", "Sum"),
    ("CacheMisses", "cache_misses", "Sum"),
    ("CurrConnections", "curr_connections", "Average"),
    ("NewConnections", "new_connections", "Sum"),
    ("Evictions", "evictions", "Sum"),
    ("Reclaimed", "reclaimed", "Sum"),
    ("ReplicationLag", "replication_lag", "Average"),
    ("SwapUsage", "swap_usage", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("CurrItems", "curr_items", "Average"),
    ("NetworkBytesIn", "net_in", "Sum"),
    ("NetworkBytesOut", "net_out", "Sum"),
]
_MEMCACHED_METRICS = [
    ("CPUUtilization", "cache_cpu", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("SwapUsage", "swap_usage", "Average"),
    ("CurrConnections", "curr_connections", "Average"),
    ("NewConnections", "new_connections", "Sum"),
    ("Evictions", "evictions", "Sum"),
    ("Reclaimed", "reclaimed", "Sum"),
    ("CurrItems", "curr_items", "Average"),
    ("BytesUsedForCacheItems", "bytes_used", "Average"),
    ("GetHits", "get_hits", "Sum"),
    ("GetMisses", "get_misses", "Sum"),
    ("NetworkBytesIn", "net_in", "Sum"),
    ("NetworkBytesOut", "net_out", "Sum"),
]


def _insert(cache_execute, cluster_id, ts, metric_type, value):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dims::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type,
         "value": float(value), "dims": "{}"})


def collect_elasticache_metrics(cw, ec, cache_execute, cluster_id, resource_name, engine, region, account_id):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted = 0
    errors = []
    eng = (engine or "redis").lower()
    metrics = _MEMCACHED_METRICS if eng == "memcached" else _REDIS_METRICS
    dims = [{"Name": "CacheClusterId", "Value": resource_name}]

    def pull(metric, stat):
        try:
            return cw.get_metric_statistics(
                Namespace="AWS/ElastiCache", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=60, Statistics=[stat]
            ).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{metric}: {e}")
            return []

    for metric, mtype, stat in metrics:
        for dp in pull(metric, stat):
            v = dp.get(stat)
            if v is None:
                continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, v)
            inserted += 1

    return {"cluster_id": cluster_id, "engine": eng,
            "metrics_inserted": inserted, "errors": errors}
```

- [ ] **Step 5: Add the dispatch branch** in `data-pipeline/etl_collector/handler.py` `_collect_one`, AFTER the documentdb branch and BEFORE the relational body. First add the import at the top with the other collector imports:

```python
from collectors.elasticache_cw_collector import collect_elasticache_metrics
```

(match the existing import style — the dynamodb/docdb collectors are imported the same way; check the top of the file.)

Then the branch:

```python
    if family == "elasticache":
        cw = get_client("cloudwatch", region)
        ec = get_client("elasticache", region)
        resource_name = resource.get("resource_name", cluster_id)
        engine = resource.get("engine", "redis")
        try:
            result["elasticache"] = collect_elasticache_metrics(
                cw, ec, cache_execute, cluster_id, resource_name, engine, region, account_id,
            )
        except Exception as e:
            result["elasticache_error"] = str(e)
            print(f"[{cluster_id}] elasticache error: {e}")
        return result
```

(No findings collector in EC-1.)

- [ ] **Step 6: Add ETL Lambda IAM if missing.** In `cdk/stacks/agent_stack.py`, find the ETL collector Lambda's IAM. It already has `cloudwatch:GetMetricStatistics`/`GetMetricData` (used for RDS). Add elasticache describe ONLY if the collector calls describe (this collector does not call describe in EC-1 — it uses `resource_name` from the registry — so `ec` client is passed but unused for metrics; you may still grant `elasticache:DescribeReplicationGroups`/`DescribeCacheClusters` for forward-compat). If the ETL role already covers `cloudwatch:GetMetricStatistics` broadly, no metric-IAM change is needed. Verify and add the elasticache describe statement to the ETL Lambda role if absent.

- [ ] **Step 7: Run tests + synth.**

Run: `python -m pytest tests/unit/data_pipeline/test_elasticache_collector.py -q` → PASS.
Run: `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 8: Commit.**

```bash
git add data-pipeline/etl_collector/collectors/elasticache_cw_collector.py data-pipeline/etl_collector/handler.py cdk/stacks/agent_stack.py tests/unit/data_pipeline/test_elasticache_collector.py
git commit -m "feat(elasticache): CloudWatch ETL collector + dispatch branch (Redis/Valkey/Memcached)"
```

---

### Task 4: Dashboard panel

**Files:**

- Create: `frontend/src/components/dashboard/elasticache-overview-panel.tsx`
- Modify: `frontend/src/app/dashboard/page.tsx` (add the `fam === "elasticache"` branch + import)
- (FAMILY_PANELS / FAMILY_META already added in Task 1.)

**Interfaces:**

- Consumes: the same metrics-fetch hook/props the `dynamodb-overview-panel.tsx` uses (read it first to match the data-loading pattern + props); `metric_type` series `memory_usage_pct`, `cache_hits`, `cache_misses`, `evictions`, `curr_connections`, `cache_cpu`, `engine_cpu`, `replication_lag`, `net_in`, `net_out`.

- [ ] **Step 1: Read the template.** Read `frontend/src/components/dashboard/dynamodb-overview-panel.tsx` fully — its props (cluster id, time range), how it fetches metric series (which api-client function / batch-timeseries call), and how it renders cards/charts. Read the `fam === "dynamodb"` branch in `frontend/src/app/dashboard/page.tsx` to see exactly how the panel is mounted (props passed).

- [ ] **Step 2: Create `frontend/src/components/dashboard/elasticache-overview-panel.tsx`.** Mirror `dynamodb-overview-panel.tsx`'s structure and data-loading exactly; render ElastiCache cards: **Memory usage %** (`memory_usage_pct`), **Hit rate** (derived: `cache_hits / (cache_hits + cache_misses)`, or `get_hits/(get_hits+get_misses)` for Memcached), **Evictions** (`evictions`), **Connections** (`curr_connections`), **CPU / Engine CPU** (`cache_cpu`, `engine_cpu`), **Replication lag** (`replication_lag`, hidden when the series is empty — Memcached / single-node), **Network throughput** (`net_in`/`net_out`). Use the existing chart components the dynamodb/docdb panels use (Recharts/Tremor per the codebase). Numbers ≥1000 use the existing `fmtDecimal`/`fmtExact` helpers (project rule); percentages/durations use raw `.toFixed`. Korean labels for descriptions/empty-states; metric jargon (hit rate, eviction, replication lag) stays as-is.

- [ ] **Step 3: Mount the panel** in `frontend/src/app/dashboard/page.tsx`. Import it, and add the branch next to the existing `{fam === "dynamodb" && (...)}` / `{fam === "documentdb" && (...)}` blocks:

```tsx
{
  fam === "elasticache" && (
    <ElasticacheOverviewPanel clusterId={clusterId} range={range} />
  );
}
```

(Match the EXACT prop names the sibling panels receive — read the dynamodb branch and copy its prop shape.)

- [ ] **Step 4: Build.**

Run: `cd frontend && npm run build`
Expected: PASS, no type errors; `/dashboard` still prerenders.

- [ ] **Step 5: Commit.**

```bash
git add frontend/src/components/dashboard/elasticache-overview-panel.tsx frontend/src/app/dashboard/page.tsx
git commit -m "feat(elasticache): dashboard overview panel (memory/hit-rate/evictions/connections/lag/throughput)"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD` — focus: all 4 engine_family.py copies + engine.ts mirror are byte-consistent for the new family; registration stores the correct `resource_type`/`resource_details`; the collector's metric_type strings match what the dashboard panel reads; read-only (no mutation/secret/protocol); no regression to relational/docdb/dynamodb branches.
- Deploy dev: `cdk deploy dbops-dev-data dbops-dev-agent` (ETL collector lives in the **data** stack; clusters Lambda + IAM in the **agent** stack — confirm which stack each changed Lambda is in and deploy those). Frontend build → `aws s3 sync frontend/out/ s3://dbops-dev-frontend-123456789012 --delete --exclude config.json --region ap-northeast-2` → CloudFront invalidation `E1234567890ABC`.
- Live smoke: register a real dev ElastiCache cluster (or, if none exists, create a small `cache.t4g.micro` Redis via a one-off — per the CDK-only-scope memory, a temporary test resource via AWS CLI is acceptable with an identifying tag + teardown). Confirm: register → `connection_status=ok` + correct `resource_details`; after one ETL cycle, `metric_snapshots` has elasticache rows; the dashboard renders the ElastiCache panel. If no cluster is available, verify the family/register/collector via unit + a registration dry-run against a non-existent name (→ 207 warning) and note the metrics path is unit-covered.
- Then `superpowers:finishing-a-development-branch`.
