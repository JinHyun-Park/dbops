# R-1 Foundation: RDS Instance Engines (MySQL + SQL Server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register, discover, collect (CloudWatch + meta), and render RDS for MySQL / SQL Server instances as a new `rds_instance` engine family — plus provision the two standing demo instances.

**Architecture:** New `rds_instance` family with early-return dispatch (DocDB/DynamoDB/ElastiCache pattern) across the 4 Python classifier copies + TS mirror. Registration/discovery via `describe_db_instances` (instances with `DBClusterIdentifier` are rejected — they're cluster members). CW collection uses `AWS/RDS` + `DBInstanceIdentifier` dimension writing standard metric_type names with `dimensions='{}'` so triage/alerts/forecast work unmodified. Spec: `docs/superpowers/specs/2026-07-22-rds-instance-engines-design.md`.

**Tech Stack:** Python 3.13 (Lambda), boto3, pytest; Next.js 16 + TS + recharts; CDK Python; AWS CLI (demo instances).

## Global Constraints

- `engine_family.py` is duplicated VERBATIM: `api/clusters/`, `api/dashboard/`, `data-pipeline/etl_collector/collectors/`, `mcp-servers/mcp_servers/shared/` — all 4 edited in lockstep via `cp` from the canonical `mcp-servers` copy; TS mirror `frontend/src/lib/engine.ts`.
- API error responses NEVER include `str(e)` (static reasons only).
- `cluster_meta.engine` is VARCHAR(20) — `sqlserver-ex` (12) fits; never store longer engine strings.
- EC2/RDS resource descriptions sent to AWS: ASCII only (no em-dash).
- Frontend commits: prettier pre-commit reformats → first commit attempt may fail → `git add -A` and re-commit. NO Claude co-author trailer.
- `cdk deploy`: NEVER run two concurrently — one process, multiple stacks: `cdk deploy A B C`.
- Frontend stack ships prebuilt `out/` — `npm run build` BEFORE `cdk deploy dbops-dev-frontend`.
- `cdk/config/settings.py` is the user's real config — never cp/overwrite/rm.
- Cross-account is OUT OF SCOPE for this family in v1 (registration form: same-account only).
- Demo instances are STANDING resources — tag them, do NOT tear down.

---

### Task 1: `rds_instance` family in the classifier + capabilities (4 Python copies)

**Files:**

- Modify: `mcp-servers/mcp_servers/shared/engine_family.py` (canonical)
- Copy to: `api/clusters/engine_family.py`, `api/dashboard/engine_family.py`, `data-pipeline/etl_collector/collectors/engine_family.py`
- Test: `tests/unit/data_pipeline/test_engine_family.py`

**Interfaces:**

- Produces: `RDS_INSTANCE = "rds_instance"` constant; `engine_family("mysql") == "rds_instance"`, `engine_family("sqlserver-*") == "rds_instance"`; `CAPABILITIES["rds_instance"]` dict; new `sql_via` key on `relational` (`"data_api"`) and `rds_instance` (`"direct"`). Tasks 2–5 rely on the family string `"rds_instance"` exactly.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/data_pipeline/test_engine_family.py`:

```python
def test_rds_instance_family_derivation():
    assert ef.engine_family("mysql") == "rds_instance"
    assert ef.engine_family("sqlserver-ex") == "rds_instance"
    assert ef.engine_family("sqlserver-ee") == "rds_instance"
    assert ef.engine_family("sqlserver-se") == "rds_instance"
    assert ef.engine_family("sqlserver-web") == "rds_instance"
    assert ef.engine_family("SQLServer-EX") == "rds_instance"
    # Aurora stays relational — the 'aurora' guard must win over the bare
    # 'mysql' substring.
    assert ef.engine_family("aurora-mysql") == "relational"
    assert ef.engine_family("aurora-postgresql") == "relational"

def test_rds_instance_capabilities():
    caps = ef.CAPABILITIES["rds_instance"]
    assert caps["sql"] is True
    assert caps["sql_via"] == "direct"
    assert ef.CAPABILITIES["relational"]["sql_via"] == "data_api"
    assert caps["rds_meta"] is True
    assert caps["perf_insights"] is True
    assert caps["simulation"] is False
    assert caps["custom_endpoint"] is False
    assert caps["prewarm"] is False
    assert caps["scale_instance"] is False
    assert caps["cw_namespace"] == "AWS/RDS"
    assert caps["findings"] == set()

def test_all_python_copies_are_verbatim_identical():
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "api" / "clusters" / "engine_family.py",
        root / "api" / "dashboard" / "engine_family.py",
        root / "data-pipeline" / "etl_collector" / "collectors" / "engine_family.py",
        root / "mcp-servers" / "mcp_servers" / "shared" / "engine_family.py",
    ]
    contents = [p.read_text() for p in paths]
    assert all(c == contents[0] for c in contents[1:]), "engine_family.py copies diverged"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/data_pipeline/test_engine_family.py -v`
Expected: 3 new tests FAIL (`rds_instance` unknown → classified `relational`; no `CAPABILITIES["rds_instance"]`).

- [ ] **Step 3: Edit the canonical copy** — `mcp-servers/mcp_servers/shared/engine_family.py`:

Add constant after line 17 (`ELASTICACHE = ...`):

```python
RDS_INSTANCE = "rds_instance"
```

Replace `engine_family()` body (keep docstring, add the two new matches BEFORE the fallback):

```python
def engine_family(engine):
    """Map an `engine` string to a family. Unknown → relational (legacy: every
    existing registry row is Aurora; the SQL path is the safe historical default
    and DynamoDB/DocDB are matched explicitly before the fallback)."""
    e = (engine or "").lower()
    if "docdb" in e or "documentdb" in e:
        return DOCUMENTDB
    if "dynamodb" in e:
        return DYNAMODB
    if "redis" in e or "valkey" in e or "memcached" in e or "elasticache" in e:
        return ELASTICACHE
    # RDS instance engines (non-Aurora). Order matters: 'aurora-mysql' contains
    # 'mysql', so the aurora guard keeps Aurora MySQL relational.
    if "sqlserver" in e:
        return RDS_INSTANCE
    if "mysql" in e and "aurora" not in e:
        return RDS_INSTANCE
    return RELATIONAL
```

In `CAPABILITIES`: add `"sql_via": "data_api",` to the RELATIONAL entry (right after `"sql": True,`), then add a new entry after the ELASTICACHE block:

```python
    RDS_INSTANCE: {
        # SQL-capable but NOT via RDS Data API (Aurora-only) — R-3 wires the
        # direct-TCP path; until then execute_sql's Data API call must not be
        # reached for this family (sql_via is the dispatch key).
        "sql": True, "sql_via": "direct",
        "rds_meta": True, "perf_insights": True, "simulation": False,
        # Cluster/reader-topology concepts — never applicable to a standalone
        # DB instance.
        "custom_endpoint": False, "prewarm": False, "scale_instance": False,
        # Shared namespace with Aurora but instance-dimensioned
        # (DBInstanceIdentifier; the DBClusterIdentifier dimension does not
        # exist for these engines).
        "cw_namespace": "AWS/RDS",
        # R-2 adds MySQL findings; empty set = no family finding collectors yet.
        "findings": set(),
    },
```

- [ ] **Step 4: Sync the 3 other copies verbatim**

```bash
cd <repo>
cp mcp-servers/mcp_servers/shared/engine_family.py api/clusters/engine_family.py
cp mcp-servers/mcp_servers/shared/engine_family.py api/dashboard/engine_family.py
cp mcp-servers/mcp_servers/shared/engine_family.py data-pipeline/etl_collector/collectors/engine_family.py
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/data_pipeline/test_engine_family.py -v`
Expected: ALL PASS (old + 3 new).

- [ ] **Step 6: Run the full unit suite (regression)**

Run: `python3 -m pytest tests/unit -q 2>&1 | tail -5`
Expected: all pass; no existing test asserts "mysql"→relational (verified: only aurora-mysql is asserted).

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/mcp_servers/shared/engine_family.py api/clusters/engine_family.py api/dashboard/engine_family.py data-pipeline/etl_collector/collectors/engine_family.py tests/unit/data_pipeline/test_engine_family.py
git commit -m "feat(engine): rds_instance family + sql_via capability (R-1)"
```

---

### Task 2: Registration + discovery for RDS instances (api/clusters)

**Files:**

- Modify: `api/clusters/handler.py` (register: after `_register_elasticache` ~line 588; dispatch in `_handle_register` ~line 591; discovery block in `_list_clusters_in_region` after the ElastiCache block ~line 448)
- Modify: `cdk/stacks/agent_stack.py` (clusters Lambda IAM — verify/add `rds:DescribeDBInstances`)
- Test: `tests/unit/api/test_register_rds_instance.py` (new)

**Interfaces:**

- Consumes: `engine_family()` returning `"rds_instance"` (Task 1 — the api/clusters copy).
- Produces: registry items with `engine_family="rds_instance"`, `engine` = real AWS engine string (`mysql` / `sqlserver-ex` …), `resource_type=f"rds-{engine}"`, `endpoint`, `port`, optional `db_secret_arn`/`db_write_secret_arn` (empty in R-1, consumed by R-2/R-3). Register API accepts `{engine: "mysql"|"sqlserver", cluster_id, account_id, region}` — the stored `engine` comes from the AWS describe response, not the request body.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/api/test_register_rds_instance.py`. Copy the handler module-loading pattern from an existing file in `tests/unit/api/` (e.g. the importlib/sys.modules stubbing used by the clusters tests there; if none loads `api/clusters/handler.py` yet, mirror `tests/unit/data_pipeline/test_engine_family.py`'s `importlib.util.spec_from_file_location` approach, stubbing `boto3` and the `tenancy` import in `sys.modules` before exec):

```python
def _mk_instance(engine="mysql", cluster_member=None):
    inst = {
        "DBInstanceIdentifier": "dbops-demo-mysql",
        "Engine": engine, "EngineVersion": "8.0.42",
        "DBInstanceStatus": "available",
        "Endpoint": {"Address": "demo.x.ap-northeast-2.rds.amazonaws.com", "Port": 3306},
    }
    if cluster_member:
        inst["DBClusterIdentifier"] = cluster_member
    return inst

def test_register_rds_instance_happy_path(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_instances.return_value = {"DBInstances": [_mk_instance()]}
        resp = h._register_rds_instance(mock_table, {
            "cluster_id": "dbops-demo-mysql", "account_id": "123", "region": "ap-northeast-2"})
    assert resp["statusCode"] == 201
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["engine_family"] == "rds_instance"
    assert item["engine"] == "mysql"
    assert item["resource_type"] == "rds-mysql"
    assert item["port"] == 3306

def test_register_rejects_cluster_member(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_instances.return_value = {
            "DBInstances": [_mk_instance(engine="aurora-mysql", cluster_member="my-aurora")]}
        resp = h._register_rds_instance(mock_table, {
            "cluster_id": "my-aurora-instance-1", "account_id": "123", "region": "ap-northeast-2"})
    assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()

def test_register_hard_fails_on_describe_error(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_instances.side_effect = Exception("AccessDenied secret-sauce")
        resp = h._register_rds_instance(mock_table, {
            "cluster_id": "nope", "account_id": "123", "region": "ap-northeast-2"})
    assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()
    # Static reason — the raw exception text must NOT leak into the response.
    assert "secret-sauce" not in resp["body"]

def test_handle_register_dispatches_rds_instance(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_register_rds_instance") as reg:
        reg.return_value = {"statusCode": 201, "body": "{}"}
        h._handle_register(mock_table, {"engine": "sqlserver", "cluster_id": "x",
                                        "account_id": "1", "region": "ap-northeast-2"})
        reg.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/api/test_register_rds_instance.py -v`
Expected: FAIL — `_register_rds_instance` does not exist.

- [ ] **Step 3: Implement registration** — in `api/clusters/handler.py`, add after `_register_elasticache` (~line 588):

```python
# RDS instance engines (non-Aurora). Engine strings from the RDS API.
_RDS_INSTANCE_ENGINES = ("mysql", "sqlserver-ee", "sqlserver-se", "sqlserver-ex", "sqlserver-web")


def _register_rds_instance(table, body):
    """Standalone RDS DB instance (RDS for MySQL / SQL Server). Unlike the
    Aurora path this HARD-FAILS (400, no registry row) on describe errors or
    cluster members — a half-registered instance row is useless downstream
    (no cluster_arn to fall back on)."""
    for f in ("cluster_id", "account_id", "region"):
        if not body.get(f):
            return _resp(400, {"error": f"{f} required"})
    cluster_id, account_id, region = body["cluster_id"], body["account_id"], body["region"]
    spoke_role_arn = body.get("spoke_role_arn", "")
    try:
        resp = _rds_client_for(region, spoke_role_arn).describe_db_instances(
            DBInstanceIdentifier=cluster_id)
        insts = resp.get("DBInstances") or []
    except Exception as e:
        print(f"[register] describe_db_instances failed for {cluster_id}: {e}")
        return _resp(400, {"error": "describe_db_instances failed - check identifier/region/permissions"})
    if not insts:
        return _resp(400, {"error": "db instance not found"})
    inst = insts[0]
    if inst.get("DBClusterIdentifier"):
        return _resp(400, {"error": "instance belongs to a DB cluster - register the cluster instead"})
    engine = inst.get("Engine", "")
    if engine not in _RDS_INSTANCE_ENGINES:
        return _resp(400, {"error": "unsupported instance engine"})
    endpoint = inst.get("Endpoint") or {}
    item = {
        "cluster_id": cluster_id, "account_id": account_id, "region": region,
        "engine": engine, "engine_family": "rds_instance",
        "engine_version": inst.get("EngineVersion", ""),
        "resource_name": cluster_id, "resource_type": f"rds-{engine}",
        "endpoint": endpoint.get("Address", ""),
        "port": int(endpoint.get("Port") or 0),
        "requires_secret_for_foundation": False,
        "spoke_role_arn": spoke_role_arn,
        # R-2 (monitoring reads) / R-3 (writes) fill these; empty is valid now.
        "db_secret_arn": body.get("db_secret_arn", ""),
        "db_write_secret_arn": body.get("db_write_secret_arn", ""),
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "connection_status": "ok", "connection_error": "",
    }
    table.put_item(Item=item)
    return _resp(201, {"status": "registered", "cluster_id": cluster_id,
                       "connection_status": "ok"})
```

In `_handle_register` (~line 591), add the 4th family branch:

```python
    if fam == "elasticache":
        return _register_elasticache(table, body)
    if fam == "rds_instance":
        return _register_rds_instance(table, body)
```

- [ ] **Step 4: Implement discovery** — in `_list_clusters_in_region`, append after the ElastiCache try/except block (~line 448), before `return out`:

```python
    # RDS instance engines (non-Aurora MySQL / SQL Server) — best-effort.
    try:
        inst_paginator = rds.get_paginator("describe_db_instances")
        for ipage in inst_paginator.paginate():
            for i in ipage.get("DBInstances", []):
                if i.get("DBClusterIdentifier"):
                    # Aurora/DocDB cluster members are registered via their cluster.
                    continue
                iengine = i.get("Engine", "")
                if iengine not in _RDS_INSTANCE_ENGINES:
                    continue
                iid = i.get("DBInstanceIdentifier", "")
                out.append({
                    "cluster_id": iid,
                    "engine": iengine,
                    "engine_family": "rds_instance",
                    "engine_version": i.get("EngineVersion", ""),
                    "resource_name": iid,
                    "resource_type": f"rds-{iengine}",
                    "status": i.get("DBInstanceStatus", ""),
                    "region": region,
                    "secret_source": "n/a",
                })
    except Exception as e:
        print(f"[discover] rds instances failed in {region}: {e}")
```

- [ ] **Step 5: IAM check** — grep the clusters Lambda's policy in `cdk/stacks/agent_stack.py`:

Run: `grep -n "DescribeDBInstances\|DescribeDBClusters" cdk/stacks/agent_stack.py`
If the clusters API Lambda's statement lists `rds:DescribeDBClusters` but NOT `rds:DescribeDBInstances`, add `"rds:DescribeDBInstances"` to that same statement's actions list. (The ETL collector in data_stack already has it — relational PI lookup uses it today.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/api/test_register_rds_instance.py tests/unit/api -q 2>&1 | tail -5`
Expected: new tests PASS, no api regressions.

- [ ] **Step 7: Commit**

```bash
git add api/clusters/handler.py cdk/stacks/agent_stack.py tests/unit/api/test_register_rds_instance.py
git commit -m "feat(clusters): register/discover standalone RDS instances (R-1)"
```

---

### Task 3: ETL collection — CW metrics + cluster_meta for rds_instance

**Files:**

- Create: `data-pipeline/etl_collector/collectors/rds_instance_cw_collector.py`
- Modify: `data-pipeline/etl_collector/handler.py` (`_collect_one` — new branch after the elasticache block ~line 163, before the relational path; plus the import next to `collect_docdb_metrics`'s import)
- Test: `tests/unit/data_pipeline/test_rds_instance_collector.py` (new)

**Interfaces:**

- Consumes: family string `"rds_instance"` (Task 1), registry rows from Task 2 (`cluster_id` = instance identifier).
- Produces: `collect_rds_instance_metrics(cw, rds_client, cache_execute, cluster_id, region, account_id) -> dict` returning `{"cluster_id", "metrics_inserted", "errors", "resource_id", "pi_enabled"}`; metric_snapshots rows with metric_type in `cpu, db_connections, freeable_memory, free_storage_bytes, read_iops, write_iops, read_latency, write_latency, net_rx, net_tx, swap_usage` and `dimensions='{}'`; a `cluster_meta` row with `resource_details` JSONB (keys: `instance_class, multi_az, storage_type, allocated_storage_gb, license_model, publicly_accessible, pi_enabled, endpoint, port`) — Task 5's panel interface MUST match these key names exactly (3-tier parity).

- [ ] **Step 1: Write the failing tests** — `tests/unit/data_pipeline/test_rds_instance_collector.py` (load the module with the same `importlib.util.spec_from_file_location` pattern as `test_engine_family.py`):

```python
def _mk_clients():
    rds = MagicMock()
    rds.describe_db_instances.return_value = {"DBInstances": [{
        "DBInstanceIdentifier": "dbops-demo-mysql", "Engine": "mysql",
        "EngineVersion": "8.0.42", "DBInstanceClass": "db.t4g.micro",
        "DBInstanceStatus": "available", "MultiAZ": False,
        "StorageType": "gp3", "AllocatedStorage": 20,
        "LicenseModel": "general-public-license", "PubliclyAccessible": False,
        "PerformanceInsightsEnabled": True, "DbiResourceId": "db-ABC",
        "Endpoint": {"Address": "x.rds.amazonaws.com", "Port": 3306},
    }]}
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": datetime(2026, 7, 22, 5, 0), "Average": 12.5}]}
    return cw, rds

def test_collect_uses_instance_dimension_and_writes_meta():
    cw, rds = _mk_clients()
    calls = []
    def cache_execute(sql, params=None):
        calls.append((sql, params))
    r = mod.collect_rds_instance_metrics(cw, rds, cache_execute,
                                         "dbops-demo-mysql", "ap-northeast-2", "123")
    assert r["resource_id"] == "db-ABC" and r["pi_enabled"] is True
    assert r["metrics_inserted"] > 0
    # Every CW call must be instance-dimensioned — DBClusterIdentifier does not
    # exist for standalone instances.
    for c in cw.get_metric_statistics.call_args_list:
        assert c.kwargs["Namespace"] == "AWS/RDS"
        assert c.kwargs["Dimensions"] == [
            {"Name": "DBInstanceIdentifier", "Value": "dbops-demo-mysql"}]
    meta_calls = [p for (s, p) in calls if "cluster_meta" in s]
    assert meta_calls, "cluster_meta upsert missing"
    details = json.loads(meta_calls[0]["details"])
    assert details["instance_class"] == "db.t4g.micro"
    assert details["pi_enabled"] is True
    metric_calls = [p for (s, p) in calls if "metric_snapshots" in s]
    assert {p["metric_type"] for p in metric_calls} >= {"cpu"}

def test_describe_failure_is_nonfatal():
    cw, rds = _mk_clients()
    rds.describe_db_instances.side_effect = Exception("boom")
    r = mod.collect_rds_instance_metrics(cw, rds, lambda *a, **k: None,
                                         "x", "ap-northeast-2", "123")
    assert r["resource_id"] is None
    assert any("describe_db_instances" in e for e in r["errors"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/data_pipeline/test_rds_instance_collector.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the collector** — create `data-pipeline/etl_collector/collectors/rds_instance_cw_collector.py`:

```python
"""RDS instance (non-Aurora MySQL / SQL Server) CloudWatch + meta -> cache.

Namespace AWS/RDS with the DBInstanceIdentifier dimension — standalone DB
instances never expose DBClusterIdentifier. Rows land with dimensions='{}'
(cluster-scoped) because the instance IS the monitored resource, so triage /
alerts / capacity forecast read them unmodified."""
import json
from datetime import datetime, timedelta

_METRICS = [
    ("CPUUtilization", "cpu", "Average"),
    ("DatabaseConnections", "db_connections", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("FreeStorageSpace", "free_storage_bytes", "Average"),
    ("ReadIOPS", "read_iops", "Average"),
    ("WriteIOPS", "write_iops", "Average"),
    ("ReadLatency", "read_latency", "Average"),
    ("WriteLatency", "write_latency", "Average"),
    ("NetworkReceiveThroughput", "net_rx", "Average"),
    ("NetworkTransmitThroughput", "net_tx", "Average"),
    ("SwapUsage", "swap_usage", "Average"),
]


def collect_rds_instance_metrics(cw, rds_client, cache_execute, cluster_id, region, account_id):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted, errors = 0, []
    resource_id, pi_enabled = None, False

    try:
        inst = rds_client.describe_db_instances(
            DBInstanceIdentifier=cluster_id)["DBInstances"][0]
        resource_id = inst.get("DbiResourceId")
        pi_enabled = bool(inst.get("PerformanceInsightsEnabled"))
        endpoint = inst.get("Endpoint") or {}
        details = {
            "instance_class": inst.get("DBInstanceClass"),
            "multi_az": bool(inst.get("MultiAZ")),
            "storage_type": inst.get("StorageType"),
            "allocated_storage_gb": inst.get("AllocatedStorage"),
            "license_model": inst.get("LicenseModel"),
            "publicly_accessible": bool(inst.get("PubliclyAccessible")),
            "pi_enabled": pi_enabled,
            "endpoint": endpoint.get("Address"),
            "port": endpoint.get("Port"),
        }
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, account_id, region, engine, engine_version, instance_class, status, resource_details, updated_at) "
            "VALUES (:cid, :account_id, :region, :engine, :ver, :cls, :status, :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET engine=EXCLUDED.engine, "
            "engine_version=EXCLUDED.engine_version, instance_class=EXCLUDED.instance_class, "
            "status=EXCLUDED.status, resource_details=EXCLUDED.resource_details, updated_at=NOW()",
            {"cid": cluster_id, "account_id": account_id, "region": region,
             "engine": inst.get("Engine", ""), "ver": inst.get("EngineVersion", ""),
             "cls": inst.get("DBInstanceClass", ""),
             "status": inst.get("DBInstanceStatus", ""),
             "details": json.dumps(details)})
    except Exception as e:
        errors.append(f"describe_db_instances: {e}")

    for metric, mtype, stat in _METRICS:
        try:
            dps = cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName=metric,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": cluster_id}],
                StartTime=start, EndTime=end, Period=60, Statistics=[stat],
            ).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{mtype}: {e}")
            continue
        for dp in dps:
            value = dp.get(stat)
            if value is None:
                continue
            cache_execute(
                "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
                "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
                "ON CONFLICT DO NOTHING",
                {"cluster_id": cluster_id, "ts": dp["Timestamp"].isoformat(),
                 "metric_type": mtype, "value": float(value)})
            inserted += 1

    return {"cluster_id": cluster_id, "metrics_inserted": inserted,
            "errors": errors, "resource_id": resource_id, "pi_enabled": pi_enabled}
```

- [ ] **Step 4: Wire the dispatch branch** — in `data-pipeline/etl_collector/handler.py`, add the import next to the existing collector imports (match their exact style — check the top of the file, they import like `from collectors.docdb_cw_collector import collect_docdb_metrics`):

```python
from collectors.rds_instance_cw_collector import collect_rds_instance_metrics
```

Then in `_collect_one`, after the elasticache block's `return result` (~line 163) and BEFORE the relational path comment, insert:

```python
    # ------------------------------------------------------------------
    # RDS instance path (non-Aurora MySQL / SQL Server) — instance-dimensioned
    # CW + meta + PI; no Aurora-cluster/Data-API calls
    # ------------------------------------------------------------------
    if family == "rds_instance":
        cw = get_client("cloudwatch", region)
        rds_client = get_client("rds", region)
        try:
            r = collect_rds_instance_metrics(
                cw, rds_client, cache_execute, cluster_id, region, account_id)
            result["rds_instance"] = r
        except Exception as e:
            result["rds_instance_error"] = str(e)
            print(f"[{cluster_id}] rds_instance error: {e}")
            return result
        if r.get("pi_enabled") and r.get("resource_id"):
            try:
                pi_client = get_client("pi", region)
                result["pi"] = collect_pi_metrics(
                    pi_client, cache_execute, r["resource_id"], cluster_id)
            except Exception as e:
                result["pi_error"] = str(e)
                print(f"[{cluster_id}] pi error: {e}")
        return result
```

- [ ] **Step 5: Add a dispatch test** — append to `tests/unit/data_pipeline/test_etl_dispatch.py` (it already has `_load_handler()`, `_fake_get_client`, `_COMMON_KWARGS` at the top — reuse them):

```python
# ---------------------------------------------------------------------------
# Test 4: RDS instance (non-Aurora) routes ONLY to collect_rds_instance_metrics
# ---------------------------------------------------------------------------

def test_rds_instance_routes_to_instance_collector_only():
    handler = _load_handler()

    resource = {
        "cluster_id": "dbops-demo-mysql",
        "engine": "mysql",
        "engine_family": "rds_instance",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }

    mock_inst_collector = MagicMock(return_value={
        "metrics_inserted": 7, "errors": [],
        "resource_id": None, "pi_enabled": False})
    mock_meta = MagicMock()
    mock_pi = MagicMock()
    mock_cw = MagicMock()
    mock_cost = MagicMock()

    with (
        patch.object(handler, "collect_rds_instance_metrics", mock_inst_collector),
        patch.object(handler, "collect_cluster_meta", mock_meta),
        patch.object(handler, "collect_pi_metrics", mock_pi),
        patch.object(handler, "collect_cw_metrics", mock_cw),
        patch.object(handler, "collect_cost_findings", mock_cost),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    mock_inst_collector.assert_called_once()
    # Aurora-cluster meta / cluster-dimension CW / cost must NOT run;
    # PI must not run either (pi_enabled=False in the collector result).
    mock_meta.assert_not_called()
    mock_cw.assert_not_called()
    mock_cost.assert_not_called()
    mock_pi.assert_not_called()
    assert result["cluster_id"] == "dbops-demo-mysql"


def test_rds_instance_runs_pi_when_enabled():
    handler = _load_handler()

    resource = {
        "cluster_id": "dbops-demo-mysql",
        "engine": "mysql",
        "engine_family": "rds_instance",
        "region": "ap-northeast-2",
        "account_id": "111122223333",
    }

    mock_inst_collector = MagicMock(return_value={
        "metrics_inserted": 7, "errors": [],
        "resource_id": "db-ABC", "pi_enabled": True})
    mock_pi = MagicMock(return_value={"rows": 1})

    with (
        patch.object(handler, "collect_rds_instance_metrics", mock_inst_collector),
        patch.object(handler, "collect_pi_metrics", mock_pi),
    ):
        result = handler._collect_one(resource, **_COMMON_KWARGS)

    mock_pi.assert_called_once()
    # resource_id from the collector (NOT a db-cluster-id filtered lookup)
    assert mock_pi.call_args.args[2] == "db-ABC"
    assert "pi" in result
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/unit/data_pipeline -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add data-pipeline/etl_collector/collectors/rds_instance_cw_collector.py data-pipeline/etl_collector/handler.py tests/unit/data_pipeline/test_rds_instance_collector.py
git commit -m "feat(etl): rds_instance CW/meta/PI collection branch (R-1)"
```

---

### Task 4: Frontend classification mirror + registration form

**Files:**

- Modify: `frontend/src/lib/engine.ts` (EngineKind ~L7-29, engineBadge ~L46-107, EngineGroup ~L264-319, EngineFamily ~L327-406)
- Modify: `frontend/src/lib/group-by-family.ts` (both Record literals)
- Modify: `frontend/src/app/dashboard/page.tsx` (TABS_BY_FAMILY ~L181)
- Modify: `frontend/src/app/clusters/page.tsx` (engine options ~L854, validation ~L331, payload ~L361, field conditions ~L868/L925)

**Interfaces:**

- Consumes: backend family string `"rds_instance"`, engine strings `mysql` / `sqlserver-*`.
- Produces: `EngineKind "sqlserver"`, `EngineFamily "rds_instance"`, `EngineGroup "rds-mysql" | "rds-sqlserver"` — Task 5 gates panels on `fam === "rds_instance"`.

- [ ] **Step 1: engine.ts — kind + badge.** In `EngineKind` add `| "sqlserver"` after `"mysql"`. In `engineKind()` insert BEFORE the mysql check:

```ts
if (e.includes("sqlserver")) return "sqlserver";
```

In `engineBadge()` add a case before `docdb`:

```ts
    case "sqlserver":
      return {
        label: "SQL Server",
        short: "MSSQL",
        classes: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
        accent: "bg-indigo-400",
      };
```

- [ ] **Step 2: engine.ts — family + group.** `EngineFamily`: add `| "rds_instance"`. In `engineFamily()` insert before `return "relational";`:

```ts
// RDS instance engines (non-Aurora). 'aurora-mysql' contains 'mysql' — the
// aurora guard keeps Aurora MySQL relational. Mirrors engine_family.py.
if (e.includes("sqlserver")) return "rds_instance";
if (e.includes("mysql") && !e.includes("aurora")) return "rds_instance";
```

`EngineGroup`: add `| "rds-mysql" | "rds-sqlserver"`. In `engineGroup()` insert after the elasticache line:

```ts
if (fam === "rds_instance")
  return engineKind(engine) === "sqlserver" ? "rds-sqlserver" : "rds-mysql";
```

`ENGINE_GROUP_ORDER`: insert `"rds-mysql", "rds-sqlserver",` after `"aurora-mysql",`.
`ENGINE_GROUP_META`: add entries:

```ts
  "rds-mysql": {
    label: "RDS MySQL",
    accent: "bg-amber-400",
    classes: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  },
  "rds-sqlserver": {
    label: "RDS SQL Server",
    accent: "bg-indigo-400",
    classes: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  },
```

`FAMILY_META`: add:

```ts
  rds_instance: {
    label: "RDS Instance",
    noun: "인스턴스",
    accent: "bg-indigo-400",
    classes: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  },
```

`FAMILY_PANELS`: add `rds_instance: new Set(["overview"]),` (the map is currently dead code but the Record type forces the key).

- [ ] **Step 3: group-by-family.ts + dashboard tabs.** Add `rds_instance: []`-style keys to BOTH exhaustive Record literals in `group-by-family.ts` (match the shape of the sibling keys exactly). In `dashboard/page.tsx` `TABS_BY_FAMILY` add:

```ts
  rds_instance: ["overview", "audit"],
```

(The audit tab already renders `EventsPanel` for `fam !== "relational"` — no gating change needed; RDS instance events flow through the engine-agnostic event_processor.)

- [ ] **Step 4: Registration form** — `frontend/src/app/clusters/page.tsx`:

Options (~L854):

```tsx
                <option value="aurora-postgresql">Aurora PostgreSQL</option>
                <option value="aurora-mysql">Aurora MySQL</option>
                <option value="mysql">MySQL (RDS)</option>
                <option value="sqlserver">SQL Server (RDS)</option>
                <option value="dynamodb">DynamoDB</option>
                <option value="docdb">DocumentDB</option>
```

(The form sends the generic `sqlserver`; the backend stores the real edition engine string from `describe_db_instances`.)

Validation (~L331): extend the docdb branch condition to cover the new engines (identifier + account + region required):

```ts
    } else if (["docdb", "mysql", "sqlserver"].includes(form.engine)) {
```

Payload (~L368): extend the docdb payload branch the same way:

```ts
      } else if (["docdb", "mysql", "sqlserver"].includes(form.engine)) {
        payload = {
          engine: form.engine,
          cluster_id: form.cluster_id,
          account_id: form.account_id,
          region: form.region,
        };
      }
```

Field visibility (~L925): the `cluster_id` input's condition currently lists the two aurora literals — add the new engines so the identifier field shows:

```ts
              {(form.engine === "aurora-postgresql" ||
                form.engine === "aurora-mysql" ||
                form.engine === "mysql" ||
                form.engine === "sqlserver") && (
```

Do NOT add the new engines to the cross-account mode toggle condition (~L868) — v1 is same-account only.

- [ ] **Step 5: Compile check — the exhaustive Records are the safety net**

Run: `cd frontend && npx tsc --noEmit 2>&1 | head -20`
Expected: zero errors. If any `Record<EngineFamily|EngineGroup, …>` site was missed, tsc names it — fix each by adding the new key(s) following the sibling entries.

- [ ] **Step 6: Commit** (prettier may reformat on first attempt — re-add and re-commit)

```bash
git add -A && git commit -m "feat(ui): rds_instance family — classification, groups, registration form (R-1)" || (git add -A && git commit -m "feat(ui): rds_instance family — classification, groups, registration form (R-1)")
```

---

### Task 5: RDS instance overview panel (dashboard)

**Files:**

- Create: `frontend/src/components/dashboard/rds-instance-overview-panel.tsx`
- Modify: `frontend/src/app/dashboard/page.tsx` (import ~L59-61; render block in the overview tab next to the sibling family panels ~L904-947)

**Interfaces:**

- Consumes: `fetchResourceDetails(clusterId)` → `{engine, engine_family, resource_details}` where `resource_details` keys are EXACTLY Task 3's: `instance_class, multi_az, storage_type, allocated_storage_gb, license_model, publicly_accessible, pi_enabled, endpoint, port`. Timeseries via the same batch-timeseries client call the DynamoDB panel uses, with metric types from Task 3: `cpu, db_connections, freeable_memory, free_storage_bytes`.
- Produces: `<RdsInstanceOverviewPanel clusterId={...} range={...} />` (accept the SAME props the sibling panels receive at dashboard/page.tsx:910-947 — read those lines and match).

- [ ] **Step 1: Read the template.** Read `frontend/src/components/dashboard/dynamodb-overview-panel.tsx` fully. Reuse verbatim: its imports (recharts, `fetchResourceDetails`, `fetchBatchTimeseries`, `TimeRange`, `Expandable`, `fmtDecimal`/`fmtBytes`, `useChartColors`), its fetch/refresh `useEffect` scaffolding, and its chart-card layout. Verify `fetchBatchTimeseries`'s exact signature in `frontend/src/lib/api-client.ts` before calling it.

- [ ] **Step 2: Write the panel.** Structure (mirror the template's JSX patterns; Korean explanatory text, English jargon per project convention):

```tsx
// RDS instance resource_details — MUST match the collector's JSON keys
// (rds_instance_cw_collector.py builds this dict; 3-tier parity).
interface RdsInstanceDetails {
  instance_class?: string;
  multi_az?: boolean;
  storage_type?: string;
  allocated_storage_gb?: number;
  license_model?: string;
  publicly_accessible?: boolean;
  pi_enabled?: boolean;
  endpoint?: string;
  port?: number;
}

const METRICS = [
  "cpu",
  "db_connections",
  "freeable_memory",
  "free_storage_bytes",
];
```

Panel body: (a) a resource card grid showing instance_class / engine_version(from the outer detail response) / Multi-AZ / storage (`{storage_type} · {allocated_storage_gb} GiB`) / license_model / PI enabled — dashes for missing values (honest empty states, no fabrication); (b) four chart cards (CPU %, Connections, Freeable Memory via fmtBytes, Free Storage via fmtBytes) using the template's Area/Line chart card markup with `useChartColors`.

- [ ] **Step 3: Render it in the dashboard.** In `dashboard/page.tsx`: add the import next to the sibling panels (~L60), then in the overview tab where `DynamodbOverviewPanel`/`DocdbOverviewPanel` render (~L904-947), add an adjacent block with IDENTICAL props to the siblings:

```tsx
{
  fam === "rds_instance" && (
    <RdsInstanceOverviewPanel clusterId={selectedCluster} range={range} />
  );
}
```

(If the siblings receive different/extra props, copy their exact prop list.)

- [ ] **Step 4: Compile + build**

Run: `cd frontend && npx tsc --noEmit && npm run build 2>&1 | tail -5`
Expected: build succeeds.

- [ ] **Step 5: Commit** (prettier re-add pattern)

```bash
git add -A && git commit -m "feat(dashboard): RDS instance overview panel (R-1)" || (git add -A && git commit -m "feat(dashboard): RDS instance overview panel (R-1)")
```

---

### Task 6: Provision the standing demo instances (live AWS)

**Files:** none (one-off AWS CLI; standing demo resources — this is the project-sanctioned exception to CDK-only, with identifying tags)

**Interfaces:**

- Produces: available instances `dbops-demo-mysql` (MySQL 8.x, db.t4g.micro) and `dbops-demo-mssql` (SQL Server Express, db.t3.small) in the data VPC, master passwords in Secrets Manager (`--manage-master-user-password`), SG `dbops-demo-rds-sg` allowing 3306/1433 from the VPC CIDR. Task 7 registers them.

- [ ] **Step 1: Resolve the data VPC + subnet group from an existing sample cluster**

```bash
SUBNET_GROUP=$(aws rds describe-db-clusters --db-cluster-identifier $(aws rds describe-db-clusters --query "DBClusters[?starts_with(DBClusterIdentifier,'dbops-dev-sample')].DBClusterIdentifier | [0]" --output text) --query "DBClusters[0].DBSubnetGroup" --output text)
VPC_ID=$(aws rds describe-db-subnet-groups --db-subnet-group-name "$SUBNET_GROUP" --query "DBSubnetGroups[0].VpcId" --output text)
VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --query "Vpcs[0].CidrBlock" --output text)
echo "$SUBNET_GROUP / $VPC_ID / $VPC_CIDR"
```

Expected: a subnet group name, vpc-…, and a CIDR. (If the first query returns None, list clusters and pick the sample PG cluster's subnet group manually.)

- [ ] **Step 2: Create the demo SG** (ASCII-only description!)

```bash
SG_ID=$(aws ec2 create-security-group --group-name dbops-demo-rds-sg \
  --description "dbops demo RDS instances - mysql 3306 mssql 1433 from vpc" \
  --vpc-id "$VPC_ID" --query GroupId --output text \
  --tag-specifications 'ResourceType=security-group,Tags=[{Key=dbops-demo,Value=true},{Key=Application,Value=DBOps}]')
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 3306 --cidr "$VPC_CIDR"
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 1433 --cidr "$VPC_CIDR"
```

- [ ] **Step 3: Create both instances** (run in background; ~10 min to available)

```bash
aws rds create-db-instance --db-instance-identifier dbops-demo-mysql \
  --engine mysql --db-instance-class db.t4g.micro \
  --allocated-storage 20 --storage-type gp3 \
  --master-username dbopsadmin --manage-master-user-password \
  --db-subnet-group-name "$SUBNET_GROUP" --vpc-security-group-ids "$SG_ID" \
  --no-publicly-accessible --enable-performance-insights \
  --backup-retention-period 1 --no-multi-az \
  --tags Key=dbops-demo,Value=true Key=Application,Value=DBOps

aws rds create-db-instance --db-instance-identifier dbops-demo-mssql \
  --engine sqlserver-ex --db-instance-class db.t3.small \
  --allocated-storage 20 --storage-type gp3 \
  --master-username dbopsadmin --manage-master-user-password \
  --db-subnet-group-name "$SUBNET_GROUP" --vpc-security-group-ids "$SG_ID" \
  --no-publicly-accessible --enable-performance-insights \
  --backup-retention-period 1 --license-model license-included \
  --tags Key=dbops-demo,Value=true Key=Application,Value=DBOps
```

Notes: SQL Server Express requires `--license-model license-included`. If `--enable-performance-insights` errors on either engine/class combo, retry that create WITHOUT the flag and note it (PI can be enabled later; the collector treats pi_enabled=false gracefully).

- [ ] **Step 4: Wait for available** (background; poll, don't block)

```bash
aws rds wait db-instance-available --db-instance-identifier dbops-demo-mysql
aws rds wait db-instance-available --db-instance-identifier dbops-demo-mssql
aws rds describe-db-instances --db-instance-identifier dbops-demo-mysql --query "DBInstances[0].[DBInstanceStatus,Engine,EngineVersion,Endpoint.Address]" --output text
aws rds describe-db-instances --db-instance-identifier dbops-demo-mssql --query "DBInstances[0].[DBInstanceStatus,Engine,EngineVersion,Endpoint.Address]" --output text
```

Expected: both `available` with endpoints. START THIS TASK FIRST (before Task 1) if executing sequentially — creation overlaps with the code tasks.

---

### Task 7: Deploy + live end-to-end verification

**Files:** none new (deploy + verification)

**Interfaces:**

- Consumes: everything above; demo instances available (Task 6).

- [ ] **Step 1: Full unit suite**

Run: `python3 -m pytest tests/unit -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 2: Frontend build, then ONE sequential deploy process**

```bash
cd frontend && npm run build && cd ../cdk && source .venv/bin/activate && \
  cdk deploy dbops-dev-data dbops-dev-agent dbops-dev-frontend --require-approval never
```

Expected: three ✅ stacks. Verify the frontend chunk actually changed in S3 and the CloudFront invalidation completed (feedback_no_concurrent_cdk_deploy / feedback_frontend_build_before_deploy).

- [ ] **Step 3: Register both demo instances via the live API** (or the UI form)

```bash
# Obtain a token the same way prior live verifications did (browser localStorage
# dbops_id_token via Chrome MCP, or the e2e user). Then:
curl -s -X POST "$API_BASE/api/clusters" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"engine":"mysql","cluster_id":"dbops-demo-mysql","account_id":"<acct>","region":"ap-northeast-2"}'
curl -s -X POST "$API_BASE/api/clusters" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"engine":"sqlserver","cluster_id":"dbops-demo-mssql","account_id":"<acct>","region":"ap-northeast-2"}'
```

Expected: both 201 `{"status":"registered","connection_status":"ok"}`; the mssql row's stored engine is the REAL edition (`sqlserver-ex`).

- [ ] **Step 4: Verify collection after one ETL interval (~5 min)** — query the cache via RDS Data API (remember `includeResultMetadata=True`):

```
SELECT metric_type, count(*), max(ts) FROM metric_snapshots
WHERE cluster_id IN ('dbops-demo-mysql','dbops-demo-mssql')
GROUP BY metric_type ORDER BY 1;
SELECT cluster_id, engine, instance_class, resource_details->>'pi_enabled'
FROM cluster_meta WHERE cluster_id LIKE 'dbops-demo-%';
```

Expected: cpu/db_connections/freeable_memory/free_storage_bytes (+iops/latency) rows for BOTH ids; cluster_meta rows with engine `mysql` / `sqlserver-ex` and instance_class populated.

- [ ] **Step 5: Browser verification (Chrome MCP — never AppleScript)**

  - Fleet/⌘K/ClusterDropdown: both demo instances appear under new groups "RDS MySQL" / "RDS SQL Server" with correct badges.
  - Dashboard for each: overview tab shows the resource card (instance class, storage, license) + 4 charts with real datapoints; audit tab shows events (or an honest empty state); NO relational-only tabs (성능·쿼리 etc.) visible.
  - Registration form: the two new options render, cluster_id field shows, cross-account toggle absent for them.

- [ ] **Step 6: Update memory/backlog + report** — record R-1 completion (and any deviations) in the session report to the user. Commit any leftover fixes.

---

## Execution notes (orchestrator)

- Model routing per the user's standing instruction: Tasks 1–3 (backend) → Opus 4.8 subagents; Tasks 4–5 (frontend) → Sonnet 5 subagents; Task 6–7 (CLI/deploy/verify) → orchestrator (Fable) directly, since they touch live AWS + browser.
- Task 6 first (background) — instance creation overlaps the code tasks.
- Tasks 1→2→3 sequential (2 and 3 consume 1's family string; 3 is independent of 2 but shares the classifier). Task 4 can run parallel to 2–3 after Task 1 lands. Task 5 after 3+4 (needs the resource_details contract + engine.ts).
- Every subagent's "done" claim is verified by the orchestrator: run their tests, `git status`/diff inspection (feedback_verify_subagent_writes).
