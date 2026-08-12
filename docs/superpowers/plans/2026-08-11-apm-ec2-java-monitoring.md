# APM for EC2 Java/Spring Boot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bottom-menu "APM" feature that monitors Java/Spring Boot apps on EC2 — on-demand log search (level-filtered), cache-first APM/host metrics — reading CloudWatch read-only.

**Architecture:** New DynamoDB `apm_targets` registry (separate from the clusters registry, so `engine_family` is untouched). An ETL collector pulls host/APM metrics + per-level log counts into the Aurora PG cache. A new `api/apm` Lambda serves cache-first reads plus one on-demand CloudWatch Logs search route. A new `/apm` Next.js page renders a metric summary + a level-filtered log viewer.

**Tech Stack:** Python 3.12 Lambdas (boto3, RDS Data API), AWS CDK (Python), DynamoDB, Aurora PostgreSQL cache, Next.js 16 static export (React, `useSmartPoll`), pytest.

## Global Constraints

- **Read-only against CloudWatch.** dbops never installs/configures EC2 instrumentation. Only these spoke actions: `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, `logs:StartQuery`, `logs:GetQueryResults`, `logs:FilterLogEvents`, `logs:DescribeLogGroups`, `ec2:DescribeInstances`. All read-only.
- **Metrics = cache-first; log search = on-demand.** Never call AWS in real time for metric/summary rendering. Raw log lines are NEVER stored in the cache — only per-level counts.
- **Default log-search level filter is `["ERROR","WARN"]`** when the caller omits `levels`, to avoid unbounded scans.
- **CDK-only infrastructure.** All AWS resources via CDK stacks. Env-specific values only in `cdk/config/settings.py`.
- **All cache SQL from Lambdas must carry a `/* source=dbops-* */` comment** (the `_execute` wrapper adds it).
- **Cross-account spoke assume must be scoped** to `arn:aws:iam::*:role/dbops-spoke-role` (the `data_stack.py` pattern), not `resources=["*"]`.
- **Migrator reads only `data-pipeline/schema_migrator/sql/`.** The top-level `data-pipeline/sql/` copy is vestigial — do not add migrations there.
- **`mcp_servers/` underscore is the import root** — not relevant here; APM adds no MCP tool, only REST + collector + frontend.
- **Vendored `tenancy.py` copies must stay byte-identical** (`tests/unit/api/test_tenancy_parity.py`). Copy it verbatim; use only its generic helpers.

---

### Task 1: Cache schema migration (`schema_v28.sql`)

**Files:**
- Create: `data-pipeline/schema_migrator/sql/schema_v28.sql`
- Test: `tests/unit/test_apm_schema_migration.py`

**Interfaces:**
- Produces: three cache tables — `apm_target_meta(target_id PK, instance_id, region, service_name, log_groups jsonb, team, last_seen_at)`, `apm_metric_snapshots(target_id, ts, metric_type, value, dimensions jsonb)` with index `idx_apm_metric_lookup(target_id, metric_type, ts)`, `apm_log_level_counts(target_id, ts, log_group, level, count)` with `UNIQUE(target_id, ts, log_group, level)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_apm_schema_migration.py
"""The APM migration must be idempotent DDL that the schema_migrator picks up."""
import re
from pathlib import Path

MIG = Path(__file__).resolve().parents[2] / "data-pipeline/schema_migrator/sql/schema_v28.sql"


def test_migration_file_exists_and_is_v28():
    assert MIG.exists(), "schema_v28.sql must exist"


def test_creates_three_apm_tables_idempotently():
    sql = MIG.read_text()
    for table in ("apm_target_meta", "apm_metric_snapshots", "apm_log_level_counts"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", sql), f"{table} missing/ not idempotent"


def test_metric_lookup_index_and_log_unique_present():
    sql = MIG.read_text()
    assert "CREATE INDEX IF NOT EXISTS idx_apm_metric_lookup" in sql
    assert "UNIQUE (target_id, ts, log_group, level)" in sql


def test_no_raw_log_text_column():
    # Contract: raw log lines are never stored; only per-level counts.
    sql = MIG.read_text().lower()
    assert "message" not in sql and "raw_log" not in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_apm_schema_migration.py -q`
Expected: FAIL (file does not exist).

- [ ] **Step 3: Write the migration**

```sql
-- data-pipeline/schema_migrator/sql/schema_v28.sql
-- v28: APM feature for EC2 Java/Spring Boot apps. Cache-first host/APM metrics
-- plus per-level log COUNTS. Raw log lines are deliberately NOT stored here —
-- they are fetched on demand from CloudWatch at search time (cost/capacity/
-- security). apm_target_meta is a convenience mirror; the source of truth for
-- targets is the DynamoDB apm_targets registry.

CREATE TABLE IF NOT EXISTS apm_target_meta (
  target_id     VARCHAR(255) PRIMARY KEY,
  instance_id   VARCHAR(64),
  region        VARCHAR(32),
  service_name  VARCHAR(255),
  log_groups    JSONB,
  team          VARCHAR(255),
  last_seen_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS apm_metric_snapshots (
  target_id    VARCHAR(255),
  ts           TIMESTAMPTZ,
  metric_type  VARCHAR(64),
  value        DOUBLE PRECISION,
  dimensions   JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_apm_metric_lookup
  ON apm_metric_snapshots (target_id, metric_type, ts);

CREATE TABLE IF NOT EXISTS apm_log_level_counts (
  target_id  VARCHAR(255),
  ts         TIMESTAMPTZ,
  log_group  VARCHAR(512),
  level      VARCHAR(16),
  count      BIGINT,
  UNIQUE (target_id, ts, log_group, level)
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_apm_schema_migration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/schema_migrator/sql/schema_v28.sql tests/unit/test_apm_schema_migration.py
git commit -m "feat(apm): cache schema for APM targets, metrics, log-level counts"
```

---

### Task 2: `apm_targets` DynamoDB registry (foundation stack)

**Files:**
- Modify: `cdk/stacks/foundation_stack.py` (add table next to `clusters_table`, ~line 124-131)
- Test: `tests/cdk/test_apm_foundation.py`

**Interfaces:**
- Produces: `foundation.apm_targets_table` (attribute exposed on the stack), table name `dbops-{ENV}-apm-targets`, PK `target_id` (STRING), PAY_PER_REQUEST, PITR on.

- [ ] **Step 1: Write the failing test**

```python
# tests/cdk/test_apm_foundation.py
import aws_cdk as cdk
from aws_cdk.assertions import Template
from cdk.stacks.foundation_stack import FoundationStack


def _template():
    app = cdk.App()
    stack = FoundationStack(app, "TestFoundation")
    return Template.from_stack(stack)


def test_apm_targets_table_exists():
    _template().has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": "target_id", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/cdk/test_apm_foundation.py -q`
Expected: FAIL (no table with `target_id` HASH key).

- [ ] **Step 3: Add the table**

In `cdk/stacks/foundation_stack.py`, immediately after the `self.clusters_table = dynamodb.Table(...)` block, add:

```python
        # APM targets — EC2 Java/Spring Boot apps monitored via CloudWatch.
        # Separate from the clusters registry so engine_family stays untouched.
        self.apm_targets_table = dynamodb.Table(
            self, "ApmTargetsTable",
            table_name=f"dbops-{Settings.ENV}-apm-targets",
            partition_key=dynamodb.Attribute(name="target_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/cdk/test_apm_foundation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cdk/stacks/foundation_stack.py tests/cdk/test_apm_foundation.py
git commit -m "feat(apm): apm_targets DynamoDB registry in foundation stack"
```

---

### Task 3: APM collector (pure function)

**Files:**
- Create: `data-pipeline/etl_collector/collectors/apm_collector.py`
- Test: `tests/unit/test_apm_collector.py`

**Interfaces:**
- Consumes: a `cloudwatch` client (`get_metric_statistics`), a `logs` client (`start_query`/`get_query_results`), and a `cache_execute(sql, params)` closure (same shape as `data-pipeline/etl_collector/handler.py`).
- Produces: `collect_apm(cw, logs_client, cache_execute, target)` where `target` is a dict `{target_id, instance_id, region, service_name, log_groups: [...], team}`. Returns `{"target_id": ..., "metrics_inserted": int, "log_buckets_inserted": int, "errors": [str]}`. Writes `apm_metric_snapshots`, `apm_log_level_counts`, and upserts `apm_target_meta`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_apm_collector.py
import importlib.util
from datetime import datetime
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_collector",
    Path(__file__).resolve().parents[2] / "data-pipeline/etl_collector/collectors/apm_collector.py",
)
apm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(apm)


class FakeCW:
    def get_metric_statistics(self, **kw):
        return {"Datapoints": [{"Timestamp": datetime(2026, 8, 11, 0, 0), "Average": 42.0}]}


class FakeLogs:
    def start_query(self, **kw):
        return {"queryId": "q1"}

    def get_query_results(self, **kw):
        return {"status": "Complete", "results": [
            [{"field": "level", "value": "ERROR"}, {"field": "cnt", "value": "5"}],
            [{"field": "level", "value": "WARN"}, {"field": "cnt", "value": "3"}],
        ]}


def test_collect_apm_writes_metrics_and_log_counts():
    rows = []
    def cache_execute(sql, params):
        rows.append((sql, params))
    target = {"target_id": "svc-a", "instance_id": "i-1", "region": "ap-northeast-2",
              "service_name": "orders", "log_groups": ["/app/orders"], "team": ""}

    result = apm.collect_apm(FakeCW(), FakeLogs(), cache_execute, target)

    assert result["target_id"] == "svc-a"
    assert result["metrics_inserted"] > 0
    assert result["log_buckets_inserted"] == 2  # ERROR + WARN buckets
    joined = " ".join(sql for sql, _ in rows)
    assert "apm_metric_snapshots" in joined
    assert "apm_log_level_counts" in joined
    assert "apm_target_meta" in joined


def test_collect_apm_records_errors_but_does_not_raise():
    class BoomCW:
        def get_metric_statistics(self, **kw):
            raise RuntimeError("throttled")
    target = {"target_id": "svc-b", "instance_id": "i-2", "region": "ap-northeast-2",
              "service_name": "x", "log_groups": [], "team": ""}
    result = apm.collect_apm(BoomCW(), FakeLogs(), lambda s, p: None, target)
    assert result["errors"]  # non-empty
    assert result["target_id"] == "svc-b"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_apm_collector.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the collector**

```python
# data-pipeline/etl_collector/collectors/apm_collector.py
"""APM collector for EC2 Java/Spring Boot targets.

Pulls host + APM metrics (CloudWatch GetMetricStatistics) and per-level log
COUNTS (Logs Insights `stats count() by level`) into the Aurora PG cache. Raw
log lines are never stored — those are fetched on demand by api/apm at search
time. Read-only against CloudWatch.
"""
import json
import time
from datetime import datetime, timedelta

# (metric_name, namespace, dimension_name, metric_type, statistic)
_METRICS = [
    ("CPUUtilization", "AWS/EC2", "InstanceId", "cpu", "Average"),
    ("mem_used_percent", "CWAgent", "InstanceId", "mem", "Average"),
    ("disk_used_percent", "CWAgent", "InstanceId", "disk", "Average"),
    ("Latency", "ApplicationSignals", "Service", "latency_p99", "Average"),
    ("Error", "ApplicationSignals", "Service", "error_rate", "Average"),
    ("Fault", "ApplicationSignals", "Service", "fault_rate", "Average"),
]

_LEVEL_QUERY = "stats count(*) as cnt by level | sort cnt desc | limit 20"


def _metric_dim(dim_name, target):
    if dim_name == "InstanceId":
        return target.get("instance_id", "")
    if dim_name == "Service":
        return target.get("service_name", "")
    return ""


def collect_apm(cw, logs_client, cache_execute, target):
    target_id = target["target_id"]
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted, log_buckets, errors = 0, 0, []

    # 1) host + APM metrics
    for metric, namespace, dim_name, mtype, stat in _METRICS:
        dim_value = _metric_dim(dim_name, target)
        if not dim_value:
            continue
        try:
            dps = cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric,
                Dimensions=[{"Name": dim_name, "Value": dim_value}],
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
                "INSERT INTO apm_metric_snapshots (target_id, ts, metric_type, value, dimensions) "
                "VALUES (:target_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
                "ON CONFLICT DO NOTHING",
                {"target_id": target_id, "ts": dp["Timestamp"].isoformat(),
                 "metric_type": mtype, "value": float(value)})
            inserted += 1

    # 2) per-level log COUNTS (no raw lines)
    bucket_ts = end.replace(second=0, microsecond=0).isoformat()
    for log_group in target.get("log_groups") or []:
        try:
            qid = logs_client.start_query(
                logGroupName=log_group,
                startTime=int((end - timedelta(minutes=5)).timestamp() * 1000),
                endTime=int(end.timestamp() * 1000),
                queryString=f"fields @message | {_LEVEL_QUERY}",
            )["queryId"]
            rows = None
            for _ in range(25):
                r = logs_client.get_query_results(queryId=qid)
                if r.get("status") == "Complete":
                    rows = r.get("results", []) or []
                    break
                if r.get("status") in ("Failed", "Cancelled"):
                    errors.append(f"{log_group}: query {r.get('status')}")
                    break
                time.sleep(1)
            for row in rows or []:
                fields = {f["field"]: f["value"] for f in row}
                level = (fields.get("level") or "").upper()[:16]
                cnt = int(float(fields.get("cnt", "0")))
                if not level:
                    continue
                cache_execute(
                    "INSERT INTO apm_log_level_counts (target_id, ts, log_group, level, count) "
                    "VALUES (:target_id, :ts::timestamptz, :log_group, :level, :count) "
                    "ON CONFLICT (target_id, ts, log_group, level) DO UPDATE SET count=EXCLUDED.count",
                    {"target_id": target_id, "ts": bucket_ts, "log_group": log_group,
                     "level": level, "count": cnt})
                log_buckets += 1
        except Exception as e:
            errors.append(f"{log_group}: {e}")

    # 3) meta mirror
    try:
        cache_execute(
            "INSERT INTO apm_target_meta (target_id, instance_id, region, service_name, log_groups, team, last_seen_at) "
            "VALUES (:target_id, :instance_id, :region, :service_name, :log_groups::jsonb, :team, NOW()) "
            "ON CONFLICT (target_id) DO UPDATE SET instance_id=EXCLUDED.instance_id, region=EXCLUDED.region, "
            "service_name=EXCLUDED.service_name, log_groups=EXCLUDED.log_groups, team=EXCLUDED.team, last_seen_at=NOW()",
            {"target_id": target_id, "instance_id": target.get("instance_id", ""),
             "region": target.get("region", ""), "service_name": target.get("service_name", ""),
             "log_groups": json.dumps(target.get("log_groups") or []), "team": target.get("team", "")})
    except Exception as e:
        errors.append(f"meta: {e}")

    return {"target_id": target_id, "metrics_inserted": inserted,
            "log_buckets_inserted": log_buckets, "errors": errors}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_apm_collector.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/etl_collector/collectors/apm_collector.py tests/unit/test_apm_collector.py
git commit -m "feat(apm): collector for host/APM metrics + per-level log counts"
```

---

### Task 4: Wire the APM collection pass into the ETL handler

**Files:**
- Modify: `data-pipeline/etl_collector/handler.py` (import at top ~line 6-40; add APM pass in `lambda_handler` after the clusters loop ~line 660)
- Test: `tests/unit/test_apm_etl_pass.py`

**Interfaces:**
- Consumes: `collect_apm` from Task 3; the existing `make_get_client`, `cache_execute`, `_scan_all`, `_session_for` in the handler.
- Produces: a `_collect_apm_targets(apm_table, make_get_client, cache_execute)` helper that scans `APM_TARGETS_TABLE`, builds a spoke-bound client per target, and calls `collect_apm`. Reads env var `APM_TARGETS_TABLE`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_apm_etl_pass.py
import importlib.util
from pathlib import Path

_H = Path(__file__).resolve().parents[2] / "data-pipeline/etl_collector/handler.py"


def test_handler_imports_collect_apm_and_defines_pass():
    src = _H.read_text()
    assert "from collectors.apm_collector import collect_apm" in src
    assert "_collect_apm_targets" in src
    assert "APM_TARGETS_TABLE" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_apm_etl_pass.py -q`
Expected: FAIL.

- [ ] **Step 3: Wire it in**

Add to the imports block (top of `handler.py`, alongside the other `from collectors...` lines):

```python
from collectors.apm_collector import collect_apm
```

Add this helper near the other module-level helpers (e.g. after `_scan_all`):

```python
def _collect_apm_targets(apm_table, make_get_client, cache_execute):
    """Separate registry pass: APM targets are EC2 apps, not clusters."""
    results = []
    for t in _scan_all(apm_table):
        region = t.get("region", "")
        get_client = make_get_client(t.get("spoke_role_arn", ""))
        try:
            cw = get_client("cloudwatch", region)
            logs_client = get_client("logs", region)
            results.append(collect_apm(cw, logs_client, cache_execute, {
                "target_id": t.get("target_id", ""),
                "instance_id": t.get("instance_id", ""),
                "region": region,
                "service_name": t.get("service_name", ""),
                "log_groups": t.get("log_groups") or [],
                "team": t.get("team", ""),
            }))
        except Exception as e:
            results.append({"target_id": t.get("target_id", ""), "errors": [str(e)]})
            print(f"[apm] {t.get('target_id')} error: {e}")
    return results
```

In `lambda_handler`, after the `for resource in clusters:` loop completes (and before the return / retention purge), add:

```python
    apm_table_name = os.environ.get("APM_TARGETS_TABLE", "")
    apm_results = []
    if apm_table_name:
        apm_table = dynamodb.Table(apm_table_name)
        apm_results = _collect_apm_targets(apm_table, make_get_client, cache_execute)
```

Then include `apm_results` in whatever summary dict `lambda_handler` returns (add key `"apm": apm_results`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_apm_etl_pass.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full collector suite to confirm no import breakage**

Run: `python3 -m pytest tests/unit/test_apm_collector.py tests/unit/test_apm_etl_pass.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add data-pipeline/etl_collector/handler.py tests/unit/test_apm_etl_pass.py
git commit -m "feat(apm): APM target collection pass in ETL handler"
```

---

### Task 5: `api/apm` handler — targets CRUD + tenancy

**Files:**
- Create: `api/apm/__init__.py` (empty), `api/apm/handler.py`, `api/apm/tenancy.py` (byte-identical copy)
- Test: `tests/unit/api/test_apm_handler.py`

**Interfaces:**
- Consumes: env `APM_TARGETS_TABLE`, `CACHE_DB_CLUSTER_ARN`, `CACHE_DB_SECRET_ARN`, `CACHE_DB_NAME`, `TEAM_MEMBERS_TABLE`, `TEAM_MEMBERS_BY_USER_INDEX`.
- Produces: `lambda_handler(event, context)` dispatching: `GET /api/apm/targets` (list), `POST /api/apm/targets` (create), `GET|PUT|DELETE /api/apm/targets/{id}`, plus routes stubbed for Tasks 6-7. Helpers `_resp`, `_target_visible(event, item)`. Target item shape: `{target_id, instance_id, region, account_id, spoke_role_arn, log_groups[], service_name, team}`.

- [ ] **Step 1: Copy the vendored tenancy module verbatim**

```bash
cp api/saved_queries/tenancy.py api/apm/tenancy.py
```
(Do not edit it — `tests/unit/api/test_tenancy_parity.py` requires byte-identical copies.)

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/api/test_apm_handler.py
import importlib.util
import json
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


def _event(method, path_id=None, body=None, admin=True):
    # admin token: cognito:groups=["dbops-admin"]; base64url payload
    import base64
    claims = {"cognito:groups": ["dbops-admin"] if admin else ["team-x"],
              "cognito:username": "hailey"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    tok = f"h.{payload}.s"
    return {
        "requestContext": {"http": {"method": method}},
        "headers": {"Authorization": f"Bearer {tok}"},
        "pathParameters": {"id": path_id} if path_id else {},
        "queryStringParameters": {},
        "body": json.dumps(body) if body else None,
    }


def test_list_targets_returns_200(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_scan_targets", lambda: [
        {"target_id": "svc-a", "service_name": "orders", "team": ""}])
    resp = mod.lambda_handler(_event("GET"), None)
    assert resp["statusCode"] == 200
    assert "svc-a" in resp["body"]


def test_unknown_route_405():
    mod = _load()
    resp = mod.lambda_handler(_event("PATCH"), None)
    assert resp["statusCode"] == 405
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/api/test_apm_handler.py -q`
Expected: FAIL (module not found).

- [ ] **Step 4: Write the handler (CRUD portion)**

```python
# api/apm/handler.py
"""APM REST API — targets registry CRUD, cache-first reads, on-demand log search.

Metrics/summaries are read from the Aurora PG cache. Log SEARCH is the one
on-demand path: it assumes the target's spoke role and queries CloudWatch Logs
at request time (mirrors api/dashboard _log_insights). Read-only against AWS.
"""
import json
import os
import time

import boto3

import tenancy

_TABLE = os.environ.get("APM_TARGETS_TABLE", "")


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _table():
    return boto3.resource("dynamodb").Table(_TABLE)


def _scan_targets():
    resp = _table().scan()
    items = resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = _table().scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _get_target(target_id):
    return _table().get_item(Key={"target_id": target_id}).get("Item")


def _target_visible(event, item):
    """Reuse generic tenancy primitives; APM targets carry an optional `team`."""
    if tenancy.is_admin(event):
        return True
    team = (item or {}).get("team")
    if not team:
        return True
    return team in tenancy.my_team_ids(tenancy.caller_username(event))


def _execute(sql, params=None):
    rds = boto3.client("rds-data")
    sql_params = []
    for k, v in (params or {}).items():
        if v is None:
            sql_params.append({"name": k, "value": {"isNull": True}})
        elif isinstance(v, bool):
            sql_params.append({"name": k, "value": {"booleanValue": v}})
        elif isinstance(v, int):
            sql_params.append({"name": k, "value": {"longValue": v}})
        elif isinstance(v, float):
            sql_params.append({"name": k, "value": {"doubleValue": v}})
        else:
            sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds.execute_statement(
        resourceArn=os.environ["CACHE_DB_CLUSTER_ARN"],
        secretArn=os.environ["CACHE_DB_SECRET_ARN"],
        database=os.environ.get("CACHE_DB_NAME", "dbops"),
        sql=f"/* source=dbops-apm */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    meta = resp.get("columnMetadata", [])
    cols = [c.get("name") or c.get("label") or "" for c in meta]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        out.append(row)
    return out


def _list(event):
    items = [t for t in _scan_targets() if _target_visible(event, t)]
    return _resp(200, {"targets": items})


def _create(event):
    if not tenancy.is_admin(event):
        return _resp(403, {"error": "admin only"})
    body = json.loads(event.get("body") or "{}")
    tid = body.get("target_id")
    if not tid:
        return _resp(400, {"error": "target_id required"})
    item = {
        "target_id": tid,
        "instance_id": body.get("instance_id", ""),
        "region": body.get("region", ""),
        "account_id": body.get("account_id", ""),
        "spoke_role_arn": body.get("spoke_role_arn", ""),
        "log_groups": body.get("log_groups") or [],
        "service_name": body.get("service_name", ""),
        "team": body.get("team", ""),
    }
    _table().put_item(Item=item)
    return _resp(201, item)


def _get_one(event, target_id):
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    return _resp(200, item)


def _update(event, target_id):
    if not tenancy.is_admin(event):
        return _resp(403, {"error": "admin only"})
    existing = _get_target(target_id)
    if not existing:
        return _resp(404, {"error": "not found"})
    body = json.loads(event.get("body") or "{}")
    existing.update({k: v for k, v in body.items() if k != "target_id"})
    _table().put_item(Item=existing)
    return _resp(200, existing)


def _delete(event, target_id):
    if not tenancy.is_admin(event):
        return _resp(403, {"error": "admin only"})
    _table().delete_item(Key={"target_id": target_id})
    return _resp(200, {"deleted": target_id})


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod") or "GET"
    )
    pp = event.get("pathParameters") or {}
    raw_path = event.get("rawPath") or (event.get("requestContext", {}).get("http", {}).get("path") or "")
    target_id = pp.get("id")

    # /api/apm/{id}/overview | /metrics | /logs/search  → Tasks 6-7
    if target_id and raw_path.endswith("/overview") and method == "GET":
        return _overview(event, target_id)
    if target_id and raw_path.endswith("/metrics") and method == "GET":
        return _metrics(event, target_id)
    if target_id and raw_path.endswith("/logs/search") and method == "POST":
        return _logs_search(event, target_id)

    # /api/apm/targets  and  /api/apm/targets/{id}
    if method == "GET" and not target_id:
        return _list(event)
    if method == "POST" and not target_id:
        return _create(event)
    if method == "GET" and target_id:
        return _get_one(event, target_id)
    if method == "PUT" and target_id:
        return _update(event, target_id)
    if method == "DELETE" and target_id:
        return _delete(event, target_id)
    return _resp(405, {"error": f"method {method} not allowed"})
```

Also create an empty `api/apm/__init__.py`. **Note:** `_overview`, `_metrics`, `_logs_search` are referenced by the dispatcher but defined in Tasks 6-7. To keep this task's tests green in isolation, add temporary stubs at the bottom of the file now (they will be replaced):

```python
def _overview(event, target_id):
    return _resp(501, {"error": "not implemented"})

def _metrics(event, target_id):
    return _resp(501, {"error": "not implemented"})

def _logs_search(event, target_id):
    return _resp(501, {"error": "not implemented"})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/api/test_apm_handler.py tests/unit/api/test_tenancy_parity.py -q`
Expected: PASS (parity test still green — tenancy.py is byte-identical).

- [ ] **Step 6: Commit**

```bash
git add api/apm/ tests/unit/api/test_apm_handler.py
git commit -m "feat(apm): api/apm handler with targets CRUD and tenancy"
```

---

### Task 6: `api/apm` — overview + metrics (cache reads)

**Files:**
- Modify: `api/apm/handler.py` (replace the `_overview` / `_metrics` stubs)
- Test: `tests/unit/api/test_apm_reads.py`

**Interfaces:**
- Consumes: `_execute`, `_get_target`, `_target_visible` from Task 5.
- Produces: `_overview(event, target_id)` → latest value per metric_type + error/warn log-count sum over last hour; `_metrics(event, target_id)` → time series filtered by `metric_type` and `hours` query params.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_apm_reads.py
import importlib.util, json, base64
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    m = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(m); return m


def _event(qs=None):
    claims = {"cognito:groups": ["dbops-admin"], "cognito:username": "h"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"requestContext": {"http": {"method": "GET"}},
            "headers": {"Authorization": f"Bearer h.{payload}.s"},
            "queryStringParameters": qs or {}}


def test_overview_shapes_latest_metrics(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: {"target_id": t, "team": ""})
    monkeypatch.setattr(mod, "_execute", lambda sql, params=None: (
        [{"metric_type": "cpu", "value": 55.0}, {"metric_type": "latency_p99", "value": 120.0}]
        if "apm_metric_snapshots" in sql else [{"level": "ERROR", "total": 7}]))
    resp = mod._overview(_event(), "svc-a")
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["metrics"]["cpu"] == 55.0
    assert body["log_counts"]["ERROR"] == 7


def test_metrics_returns_series(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: {"target_id": t, "team": ""})
    monkeypatch.setattr(mod, "_execute", lambda sql, params=None: [
        {"ts": "2026-08-11T00:00:00Z", "value": 1.0}])
    resp = mod._metrics(_event({"metric_type": "cpu", "hours": "3"}), "svc-a")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["series"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/api/test_apm_reads.py -q`
Expected: FAIL (stubs return 501).

- [ ] **Step 3: Replace the stubs**

```python
def _overview(event, target_id):
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    rows = _execute(
        "SELECT DISTINCT ON (metric_type) metric_type, value "
        "FROM apm_metric_snapshots WHERE target_id = :tid "
        "ORDER BY metric_type, ts DESC",
        {"tid": target_id})
    metrics = {r["metric_type"]: r["value"] for r in rows}
    log_rows = _execute(
        "SELECT level, COALESCE(SUM(count),0) AS total FROM apm_log_level_counts "
        "WHERE target_id = :tid AND ts > NOW() - INTERVAL '1 hour' GROUP BY level",
        {"tid": target_id})
    log_counts = {r["level"]: int(r["total"]) for r in log_rows}
    return _resp(200, {"target_id": target_id, "metrics": metrics, "log_counts": log_counts})


def _metrics(event, target_id):
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    qs = event.get("queryStringParameters") or {}
    metric_type = qs.get("metric_type", "cpu")
    try:
        hours = max(1, min(168, int(qs.get("hours", "6"))))
    except ValueError:
        hours = 6
    rows = _execute(
        f"SELECT ts, value FROM apm_metric_snapshots "
        f"WHERE target_id = :tid AND metric_type = :mt "
        f"AND ts > NOW() - INTERVAL '{hours} hours' ORDER BY ts",
        {"tid": target_id, "mt": metric_type})
    return _resp(200, {"target_id": target_id, "metric_type": metric_type, "series": rows})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/api/test_apm_reads.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/apm/handler.py tests/unit/api/test_apm_reads.py
git commit -m "feat(apm): cache-first overview and metrics reads"
```

---

### Task 7: `api/apm` — on-demand log search (level filter, ERROR+WARN default)

**Files:**
- Modify: `api/apm/handler.py` (replace `_logs_search` stub; add `_session_for` + `_levels_filter`)
- Test: `tests/unit/api/test_apm_logs_search.py`

**Interfaces:**
- Consumes: `_get_target`, `_target_visible`, `_resp`.
- Produces: `_levels_filter(levels)` → Logs Insights filter clause string; default `["ERROR","WARN"]` when `levels` is falsy. `_logs_search(event, target_id)` assumes the spoke role, runs `start_query`/`get_query_results`, returns `{entries:[{ts,message}], count, compiled_query}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_apm_logs_search.py
import importlib.util, json, base64
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    m = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(m); return m


def _event(body):
    claims = {"cognito:groups": ["dbops-admin"], "cognito:username": "h"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"requestContext": {"http": {"method": "POST"}},
            "headers": {"Authorization": f"Bearer h.{payload}.s"},
            "body": json.dumps(body)}


def test_levels_filter_defaults_to_error_warn():
    mod = _load()
    clause = mod._levels_filter(None)
    assert "ERROR" in clause and "WARN" in clause
    assert "INFO" not in clause


def test_levels_filter_honors_explicit_levels():
    mod = _load()
    clause = mod._levels_filter(["INFO"])
    assert "INFO" in clause and "ERROR" not in clause


def test_logs_search_runs_query(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: {
        "target_id": t, "team": "", "region": "ap-northeast-2",
        "spoke_role_arn": "", "log_groups": ["/app/orders"]})

    class FakeLogs:
        def start_query(self, **kw):
            assert "ERROR" in kw["queryString"]  # default filter applied
            return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": [
                [{"field": "@timestamp", "value": "2026-08-11 00:00"},
                 {"field": "@message", "value": "ERROR boom"}]]}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    resp = mod._logs_search(_event({"log_group": "/app/orders"}), "svc-a")
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 1
    assert body["entries"][0]["message"] == "ERROR boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: FAIL.

- [ ] **Step 3: Replace the stub + add helpers**

```python
_DEFAULT_LEVELS = ["ERROR", "WARN"]


def _levels_filter(levels):
    """Server-side level gate. Default ERROR+WARN to avoid unbounded scans."""
    import re
    lv = [re.sub(r"[^A-Z]", "", (x or "").upper()) for x in (levels or _DEFAULT_LEVELS)]
    lv = [x for x in lv if x] or _DEFAULT_LEVELS
    ors = " or ".join(f"@message like /{x}/" for x in lv)
    return f"filter ({ors})"


def _session_for(region="", role_arn=""):
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn, RoleSessionName="dbops-apm", DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


def _logs_client_for(item):
    return _session_for(item.get("region", ""), item.get("spoke_role_arn", "")).client("logs")


def _logs_search(event, target_id):
    import re
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    body = json.loads(event.get("body") or "{}")
    log_group = body.get("log_group") or (item.get("log_groups") or [""])[0]
    if not log_group:
        return _resp(400, {"error": "no log_group for target"})
    try:
        hours = max(1, min(48, int(body.get("hours", 1))))
    except (ValueError, TypeError):
        hours = 1
    limit = min(int(body.get("limit", 100) or 100), 500)

    parts = [_levels_filter(body.get("levels"))]
    for raw in (body.get("query") or "").split():
        cleaned = re.sub(r"[^A-Za-z0-9_./:\-]", "", raw)
        if cleaned:
            parts.append(f"filter @message like /{cleaned}/")
    query_string = ("fields @timestamp, @message | " + " | ".join(parts)
                    + f" | sort @timestamp desc | limit {limit}")

    client = _logs_client_for(item)
    base = {"target_id": target_id, "log_group": log_group,
            "compiled_query": query_string, "entries": [], "count": 0}
    try:
        qid = client.start_query(
            logGroupName=log_group,
            startTime=int((time.time() - hours * 3600) * 1000),
            endTime=int(time.time() * 1000),
            queryString=query_string)["queryId"]
    except Exception as e:
        return _resp(200, {**base, "error": f"start_query failed: {e}"})
    for _ in range(25):
        r = client.get_query_results(queryId=qid)
        status = r.get("status")
        if status == "Complete":
            entries = []
            for row in r.get("results", []) or []:
                fields = {f["field"]: f["value"] for f in row}
                entries.append({"ts": fields.get("@timestamp"),
                                "message": fields.get("@message", "")})
            return _resp(200, {**base, "entries": entries, "count": len(entries)})
        if status in ("Failed", "Cancelled"):
            return _resp(200, {**base, "error": f"query {status.lower()}"})
        time.sleep(1)
    return _resp(200, {**base, "error": "query timed out"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: PASS.

- [ ] **Step 5: Run all APM unit tests**

Run: `python3 -m pytest tests/unit/api/test_apm_handler.py tests/unit/api/test_apm_reads.py tests/unit/api/test_apm_logs_search.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/apm/handler.py tests/unit/api/test_apm_logs_search.py
git commit -m "feat(apm): on-demand log search with ERROR+WARN default level filter"
```

---

### Task 8: CDK wiring — Lambda, routes, IAM, spoke role, OpenAPI

**Files:**
- Modify: `cdk/stacks/agent_stack.py` (Lambda def + `add_routes` + IAM; ETL env var for `APM_TARGETS_TABLE`)
- Modify: `cdk/stacks/data_stack.py` (add `APM_TARGETS_TABLE` env + `apm_targets_table.grant_read_data` + spoke assume already scoped — confirm)
- Modify: `cdk/cross-account/spoke-role-template.yaml` (read-only CloudWatch/Logs/EC2 actions)
- Regenerate: `frontend/public/openapi.json` via `python tools/openapi_gen.py`
- Test: `tests/cdk/test_apm_agent_stack.py`, and existing `tests/unit/test_openapi_spec.py`

**Interfaces:**
- Consumes: `foundation.apm_targets_table` (Task 2), `data.cache_db`.
- Produces: HTTP API routes `/api/apm/targets`, `/api/apm/targets/{id}`, `/api/apm/targets/{id}/overview`, `/api/apm/targets/{id}/metrics`, `/api/apm/targets/{id}/logs/search`.

- [ ] **Step 1: Write the failing CDK test**

```python
# tests/cdk/test_apm_agent_stack.py
"""Synth-level: the APM routes and Lambda exist. Mirrors other cdk route tests."""
from pathlib import Path


def test_agent_stack_declares_apm_routes():
    src = Path("cdk/stacks/agent_stack.py").read_text()
    assert '"/api/apm/targets"' in src
    assert '"/api/apm/targets/{id}"' in src
    assert '"/api/apm/targets/{id}/logs/search"' in src
    assert 'code=lambda_.Code.from_asset("../api/apm")' in src


def test_spoke_template_has_ec2_describe():
    tpl = Path("cdk/cross-account/spoke-role-template.yaml").read_text()
    assert "ec2:DescribeInstances" in tpl
    assert "logs:FilterLogEvents" in tpl
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/cdk/test_apm_agent_stack.py -q`
Expected: FAIL.

- [ ] **Step 3: Add the Lambda + routes in `agent_stack.py`**

Add the Lambda definition near `saved_queries_lambda`:

```python
        # APM API — EC2 Java/Spring Boot log + metric monitoring
        apm_lambda = lambda_.Function(
            self, "ApmApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/apm"),
            timeout=cdk.Duration.seconds(30),  # on-demand Logs Insights budget
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "APM_TARGETS_TABLE": foundation.apm_targets_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(apm_lambda)
        data.cache_db.grant_data_api_access(apm_lambda)
        foundation.apm_targets_table.grant_read_write_data(apm_lambda)
        foundation.team_members_table.grant_read_data(apm_lambda)
        apm_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-data:ExecuteStatement"], resources=["*"]))
        apm_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:*:{self.account}:secret:*"]))
        # On-demand log search assumes the target's spoke role (scoped).
        apm_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/dbops-spoke-role"]))
        # Local-account fallback (no spoke role): read-only CW Logs.
        apm_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["logs:StartQuery", "logs:GetQueryResults",
                     "logs:FilterLogEvents", "logs:DescribeLogGroups"],
            resources=["*"]))
```

Add the routes near the saved-queries integration:

```python
        apm_integration = integrations.HttpLambdaIntegration("ApmIntegration", apm_lambda)
        self.api.add_routes(
            path="/api/apm/targets",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=apm_integration)
        self.api.add_routes(
            path="/api/apm/targets/{id}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=apm_integration)
        self.api.add_routes(
            path="/api/apm/targets/{id}/overview",
            methods=[apigwv2.HttpMethod.GET], integration=apm_integration)
        self.api.add_routes(
            path="/api/apm/targets/{id}/metrics",
            methods=[apigwv2.HttpMethod.GET], integration=apm_integration)
        self.api.add_routes(
            path="/api/apm/targets/{id}/logs/search",
            methods=[apigwv2.HttpMethod.POST], integration=apm_integration)
```

- [ ] **Step 4: Add `APM_TARGETS_TABLE` to the ETL collector in `data_stack.py`**

Find the ETL Lambda's `environment={...}` and add `"APM_TARGETS_TABLE": foundation.apm_targets_table.table_name,`, then grant read: `foundation.apm_targets_table.grant_read_data(self.etl_lambda)`. Confirm the ETL already has the scoped `sts:AssumeRole` → `arn:aws:iam::*:role/dbops-spoke-role` policy (it does; no change needed). If `foundation` is not already passed to `data_stack`, pass `apm_targets_table` through the existing foundation reference used for `clusters_table`.

- [ ] **Step 5: Add read-only actions to the spoke-role template**

In `cdk/cross-account/spoke-role-template.yaml`, extend the `CloudWatch` and `CloudWatchLogs` SID actions and add EC2 describe:

```yaml
              - Sid: CloudWatch
                Effect: Allow
                Action:
                  - "cloudwatch:GetMetricData"
                  - "cloudwatch:GetMetricStatistics"
                  - "cloudwatch:ListMetrics"
                  - "cloudwatch:DescribeAlarms"
                Resource: "*"
              - Sid: CloudWatchLogs
                Effect: Allow
                Action:
                  - "logs:StartQuery"
                  - "logs:GetQueryResults"
                  - "logs:FilterLogEvents"
                  - "logs:DescribeLogGroups"
                Resource: "*"
              - Sid: ApmEc2Describe
                Effect: Allow
                Action:
                  - "ec2:DescribeInstances"
                Resource: "*"
```

- [ ] **Step 6: Regenerate the OpenAPI spec**

Run: `python tools/openapi_gen.py`
Expected: prints an increased path/operation count including `/api/apm/*`.

- [ ] **Step 7: Run the CDK + OpenAPI tests**

Run: `python3 -m pytest tests/cdk/test_apm_agent_stack.py tests/cdk -q && python3 -m pytest tests/unit/test_openapi_spec.py -q`
Expected: PASS (all four stacks synth; openapi committed file matches).

- [ ] **Step 8: Commit**

```bash
git add cdk/stacks/agent_stack.py cdk/stacks/data_stack.py cdk/cross-account/spoke-role-template.yaml frontend/public/openapi.json tests/cdk/test_apm_agent_stack.py
git commit -m "feat(apm): CDK Lambda, routes, IAM, read-only spoke perms, OpenAPI"
```

---

### Task 9: Frontend API client functions

**Files:**
- Modify: `frontend/src/lib/api-client.ts` (append APM functions near the end)
- Test: manual TypeScript build in Task 11 (no unit harness for api-client in this repo)

**Interfaces:**
- Produces: `fetchApmTargets()`, `createApmTarget(t)`, `deleteApmTarget(id)`, `fetchApmOverview(id)`, `fetchApmMetrics(id, metricType, hours)`, `searchApmLogs(id, {levels, query, hours, limit, log_group})`, plus an `ApmTarget` type.

- [ ] **Step 1: Append the functions**

```ts
// ---- APM (EC2 Java/Spring Boot monitoring) ----
export interface ApmTarget {
  target_id: string;
  instance_id?: string;
  region?: string;
  account_id?: string;
  spoke_role_arn?: string;
  log_groups?: string[];
  service_name?: string;
  team?: string;
}

export async function fetchApmTargets(): Promise<{ targets: ApmTarget[] }> {
  const res = await authedFetch(await api(`/api/apm/targets`));
  if (!res.ok) throw new Error(`APM 타겟 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function createApmTarget(t: ApmTarget) {
  const res = await authedFetch(await api(`/api/apm/targets`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(t),
  });
  if (!res.ok) throw new Error(`APM 타겟 등록 실패 (상태 ${res.status})`);
  return res.json();
}

export async function deleteApmTarget(id: string) {
  const res = await authedFetch(await api(`/api/apm/targets/${enc(id)}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`APM 타겟 삭제 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchApmOverview(id: string) {
  const res = await authedFetch(await api(`/api/apm/targets/${enc(id)}/overview`));
  if (!res.ok) throw new Error(`APM 요약 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchApmMetrics(id: string, metricType: string, hours = 6) {
  const res = await authedFetch(
    await api(`/api/apm/targets/${enc(id)}/metrics?metric_type=${enc(metricType)}&hours=${hours}`));
  if (!res.ok) throw new Error(`APM 지표 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function searchApmLogs(
  id: string,
  opts: { levels?: string[]; query?: string; hours?: number; limit?: number; log_group?: string },
) {
  const res = await authedFetch(await api(`/api/apm/targets/${enc(id)}/logs/search`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(opts),
  });
  if (!res.ok) throw new Error(`APM 로그 검색 실패 (상태 ${res.status})`);
  return res.json();
}
```

- [ ] **Step 2: Type-check compiles (deferred to Task 11 build)**

Run: `cd frontend && npx tsc --noEmit` (if available) — else defer to `npm run build` in Task 11.
Expected: no type errors in `api-client.ts`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api-client.ts
git commit -m "feat(apm): frontend api-client functions for targets, metrics, logs"
```

---

### Task 10: Frontend `/apm` page + nav menu item

**Files:**
- Modify: `frontend/src/components/app-shell.tsx` (NavItem after `/health`; `humanize` map)
- Create: `frontend/src/app/apm/page.tsx`

**Interfaces:**
- Consumes: `fetchApmTargets`, `fetchApmOverview`, `searchApmLogs` (Task 9); `useSmartPoll`; `PageBody`, `PageHeader`, `Section`, `Stat`, `StatRow` from `@/components/design-system/page-shell`.

- [ ] **Step 1: Add the nav item**

In `frontend/src/components/app-shell.tsx`, inside the **Configure** group's `items` array, immediately after the `/health` object (before the closing `],`), add:

```tsx
      {
        href: "/apm",
        label: "APM",
        icon: Activity,
        hint: "EC2 앱 로그·성능 모니터링 (Java/Spring Boot)",
      },
```

And add to the `humanize` map (after `health: "Health",`):

```tsx
    apm: "APM",
```

- [ ] **Step 2: Create the page**

```tsx
// frontend/src/app/apm/page.tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchApmTargets,
  fetchApmOverview,
  searchApmLogs,
  type ApmTarget,
} from "@/lib/api-client";
import { useSmartPoll } from "@/lib/use-smart-poll";
import {
  PageBody,
  PageHeader,
  Section,
  Stat,
  StatRow,
} from "@/components/design-system/page-shell";

const LEVELS = ["ERROR", "WARN", "INFO", "DEBUG"] as const;

interface LogEntry {
  ts: string;
  message: string;
}

export default function ApmPage() {
  const [targets, setTargets] = useState<ApmTarget[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [overview, setOverview] = useState<Record<string, unknown> | null>(null);
  const [levels, setLevels] = useState<string[]>(["ERROR", "WARN"]);
  const [query, setQuery] = useState("");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchApmTargets()
      .then((r) => {
        setTargets(r.targets || []);
        if (!selected && r.targets?.length) setSelected(r.targets[0].target_id);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [selected]);

  const loadOverview = useCallback(() => {
    if (!selected) return;
    fetchApmOverview(selected)
      .then(setOverview)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [selected]);
  useSmartPoll(loadOverview, 15_000, [selected]);

  const runSearch = useCallback(() => {
    if (!selected) return;
    setSearching(true);
    setError(null);
    searchApmLogs(selected, { levels, query, hours: 1, limit: 100 })
      .then((r) => setLogs(r.entries || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSearching(false));
  }, [selected, levels, query]);

  const toggleLevel = (lv: string) =>
    setLevels((cur) => (cur.includes(lv) ? cur.filter((x) => x !== lv) : [...cur, lv]));

  const metrics = (overview?.metrics as Record<string, number>) || {};
  const logCounts = (overview?.log_counts as Record<string, number>) || {};

  return (
    <PageBody>
      <PageHeader
        eyebrow="apm"
        title="APM"
        description="EC2 위 Java/Spring Boot 앱 로그·성능 모니터링. 지표는 캐시, 로그는 온디맨드 검색."
        actions={
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5"
          >
            {targets.length === 0 && <option value="">타겟 없음</option>}
            {targets.map((t) => (
              <option key={t.target_id} value={t.target_id}>
                {t.service_name || t.target_id}
              </option>
            ))}
          </select>
        }
      />

      {error && <div className="text-sm text-red-400">{error}</div>}

      {!selected ? (
        <Section title="APM 타겟 없음">
          <p className="text-sm text-zinc-400">
            먼저 APM 타겟(EC2 instance-id, region, spoke role, 로그 그룹)을 등록하세요.
          </p>
        </Section>
      ) : (
        <>
          <StatRow>
            <Stat label="Latency p99" value={metrics.latency_p99 ?? "—"} />
            <Stat label="Error rate" value={metrics.error_rate ?? "—"} />
            <Stat label="CPU %" value={metrics.cpu ?? "—"} />
            <Stat label="Mem %" value={metrics.mem ?? "—"} />
          </StatRow>
          <StatRow>
            <Stat label="ERROR (1h)" value={logCounts.ERROR ?? 0} />
            <Stat label="WARN (1h)" value={logCounts.WARN ?? 0} />
          </StatRow>

          <Section title="로그 검색">
            <div className="flex flex-wrap items-center gap-2 mb-3">
              {LEVELS.map((lv) => (
                <button
                  key={lv}
                  onClick={() => toggleLevel(lv)}
                  className={`text-xs px-2 py-1 rounded border ${
                    levels.includes(lv)
                      ? "bg-zinc-700 border-zinc-500"
                      : "bg-zinc-900 border-zinc-700 text-zinc-500"
                  }`}
                >
                  {lv}
                </button>
              ))}
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="검색어 (선택)"
                className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1 flex-1 min-w-40"
              />
              <button
                onClick={runSearch}
                disabled={searching}
                className="text-xs font-medium px-3 py-1.5 border border-zinc-700 rounded"
              >
                {searching ? "검색 중…" : "검색"}
              </button>
            </div>
            <div className="font-mono text-xs space-y-1 max-h-96 overflow-auto">
              {logs.length === 0 ? (
                <p className="text-zinc-500">결과 없음. 레벨·검색어·타겟을 확인하세요.</p>
              ) : (
                logs.map((e, i) => (
                  <div key={i} className="border-b border-zinc-800 py-1">
                    <span className="text-zinc-500 mr-2">{e.ts}</span>
                    <span className="text-zinc-200 whitespace-pre-wrap">{e.message}</span>
                  </div>
                ))
              )}
            </div>
          </Section>
        </>
      )}
    </PageBody>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/app-shell.tsx frontend/src/app/apm/page.tsx
git commit -m "feat(apm): /apm page (metric summary + level-filtered log viewer) and nav"
```

---

### Task 11: Full test sweep + frontend build + browser test

**Files:** none (verification task)

- [ ] **Step 1: Run the full unit suite**

Run: `python3 -m pytest tests/unit -q`
Expected: PASS (including all new APM tests + parity tests unchanged).

- [ ] **Step 2: Run the CDK synth suite**

Run: `python3 -m pytest tests/cdk -q`
Expected: PASS (all four stacks synth with the new table/Lambda/routes).

- [ ] **Step 3: Build the frontend (static export)**

Run: `cd frontend && npm run build`
Expected: build succeeds; `/apm` route emitted to `out/`.

- [ ] **Step 4: Browser smoke test (project hard rule)**

Start the frontend locally (or via the project's run skill), open `/apm`, and confirm: the APM item appears at the bottom of the Configure menu; the page renders the empty state when no target exists; with a target selected, the metric cards and log-search controls render and the level toggles default to ERROR+WARN. Record the result.

- [ ] **Step 5: Final commit (if any build artifacts changed)**

```bash
git add -A
git commit -m "chore(apm): frontend build artifacts for APM page"
```

---

## Self-Review

**Spec coverage:**
- Logs (on-demand, level filter, ERROR+WARN default) → Task 7 ✓
- APM metrics (cache-first) → Tasks 3, 6 ✓
- Host metrics → Tasks 3, 6 ✓
- Read-only instrumentation assumption → Global Constraints + Task 8 spoke perms (read-only only) ✓
- Explicit APM-target registry, separate from clusters/engine_family → Tasks 2, 5 ✓
- Log-centric + metric-summary landing screen → Task 10 ✓
- No raw log storage; per-level counts only → Tasks 1, 3 ✓
- Bottom menu item → Task 10 ✓
- Data model (3 tables) → Task 1 ✓
- REST routes table → Tasks 5-8 ✓
- IAM/spoke redeploy note → Task 8 ✓
- Tests (unit, cdk, openapi, frontend build/browser) → Tasks 1-11 ✓

**Placeholder scan:** No TBD/TODO; every code step has real code. ✓

**Type consistency:** `collect_apm(cw, logs_client, cache_execute, target)` consistent Tasks 3-4. `_levels_filter`, `_logs_client_for`, `_overview/_metrics/_logs_search` names consistent Tasks 5-7. Frontend `ApmTarget`, `fetchApm*`, `searchApmLogs` signatures consistent Tasks 9-10. ✓

**Note for implementer:** `tests/unit/test_openapi_spec.py` will fail until Task 8 Step 6 regenerates `openapi.json`. Run tasks in order.
</content>
