# R-2: RDS MySQL Deep Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate query*stats / long_running_queries / blocking_locks / cluster_settings / table_stats / innodb*\* metrics + findings for `rds_instance` MySQL via a new in-VPC direct-TCP collector Lambda — surfacing the perf/internals dashboard tabs and chat query tools for RDS MySQL.

**Architecture:** New dedicated Lambda `rds_direct_collector` (clone of docdb_mongo_collector: own SG/schedule/registry-scan/per-cluster isolation) connects over pymysql (fail-closed TLS) and runs the EXISTING 5 Aurora-MySQL target-read collectors UNMODIFIED through a Data-API-shape adapter (their SQL is vanilla MySQL 8.x — recon-verified). Cache-only collectors (param_fitness) run in the existing non-VPC etl_collector's rds_instance branch. Read side (dashboard API, panels, MCP tools) is already engine-agnostic — frontend only flips tab/panel gates.

**Tech Stack:** Python 3.12 Lambda (pymysql — NEW dependency, pure Python), CDK data_stack, Next.js 16.

## Global Constraints

- Commit messages: NO Co-Authored-By trailer. Prettier pre-commit may abort first frontend commit → `git add -A`, re-commit.
- API/tool error responses never include `str(e)`.
- `cdk deploy`: single process, never concurrent. Frontend build before frontend deploy (not needed unless frontend stack deployed).
- Vendored-copy convention: no shared layer spans data-pipeline dirs → collector files are duplicated VERBATIM with a disk-read parity test (engine_family.py precedent). Docker bundling fallback cannot reach sibling dirs — never `cp ../` in bundling commands.
- RDS-managed master secrets (`--manage-master-user-password`) contain ONLY `{"username","password"}` — host/port MUST come from the registry row's `endpoint`/`port` (R-1 stores them).
- TLS fail-closed: pymysql connect must verify against vendored `global-bundle.pem`; no CERT_NONE fallback ever.
- Target-DB SQL marker `/* source=dbops-etl */` is inline in each collector's SQL (unchanged); the new Lambda's CACHE writes use `/* source=dbops-rdsdirect */` via its cache_execute closure.
- `cdk/config/settings.py` never cp/overwrite/rm. Demo instances are standing resources.

---

### Task 1: `rds_direct_collector` Lambda package (adapter + handler + vendored collectors)

**Files:**

- Create: `data-pipeline/rds_direct_collector/handler.py`
- Create: `data-pipeline/rds_direct_collector/mysql_adapter.py`
- Create: `data-pipeline/rds_direct_collector/requirements.txt` (`pymysql>=1.1`)
- Create (verbatim copies from `data-pipeline/etl_collector/collectors/`): `mysql_query_stats.py`, `mysql_activity.py`, `mysql_locks.py`, `mysql_innodb_status.py`, `mysql_table_stats.py` into `data-pipeline/rds_direct_collector/`
- Test: `tests/unit/data_pipeline/test_rds_direct_collector.py` (new)

**Interfaces:**

- Consumes: registry rows `{engine_family:"rds_instance", engine:"mysql", endpoint, port, db_secret_arn}` (R-1); the 5 collectors' signatures `collect_mysql_*(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database)` (innodb additionally takes `snapshot_ts`).
- Produces: `MySQLDataApiAdapter` with `execute_statement(resourceArn=None, secretArn=None, database=None, sql, ...) -> {"records":[[field,...]]}` mapping pymysql rows to Data-API field dicts; handler env `CLUSTERS_TABLE/CACHE_DB_CLUSTER_ARN/CACHE_DB_SECRET_ARN/CACHE_DB_NAME`; result list per cluster `{cluster_id, collected: {...}, error?}`.

- [ ] **Step 1: Vendored copies + parity test (TDD).** Write the parity test FIRST in `tests/unit/data_pipeline/test_rds_direct_collector.py`:

```python
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline"

_VENDORED = ["mysql_query_stats.py", "mysql_activity.py", "mysql_locks.py",
             "mysql_innodb_status.py", "mysql_table_stats.py"]

def test_vendored_collectors_are_verbatim_identical():
    for name in _VENDORED:
        src = (_ROOT / "etl_collector" / "collectors" / name).read_text()
        cpy = (_ROOT / "rds_direct_collector" / name).read_text()
        assert src == cpy, f"{name} diverged from etl_collector/collectors copy"
```

Run: `python3 -m pytest tests/unit/data_pipeline/test_rds_direct_collector.py -v` → FAIL (files missing). Then `cp` the 5 files from `data-pipeline/etl_collector/collectors/` and re-run → parity test passes.

- [ ] **Step 2: Adapter tests (RED).** Append:

```python
def _load(mod):
    spec = importlib.util.spec_from_file_location(
        mod, _ROOT / "rds_direct_collector" / f"{mod}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_adapter_maps_python_types_to_data_api_fields():
    ad = _load("mysql_adapter")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("abc", 42, 3.14, None, b"bin", datetime(2026, 7, 22, 1, 2, 3)),
    ]
    adapter = ad.MySQLDataApiAdapter(conn)
    out = adapter.execute_statement(sql="SELECT 1")
    row = out["records"][0]
    assert row[0] == {"stringValue": "abc"}
    assert row[1] == {"longValue": 42}
    assert row[2] == {"doubleValue": 3.14}
    assert row[3] == {"isNull": True}
    assert row[4] == {"stringValue": "bin"}          # bytes → utf-8 (errors=replace)
    assert row[5] == {"stringValue": "2026-07-22 01:02:03"}  # datetime → str

def test_adapter_decimal_maps_to_double():
    from decimal import Decimal
    ad = _load("mysql_adapter")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [(Decimal("7.5"),)]
    out = ad.MySQLDataApiAdapter(conn).execute_statement(sql="SELECT 1")
    assert out["records"][0][0] == {"doubleValue": 7.5}
```

Run → FAIL (module missing).

- [ ] **Step 3: Implement `mysql_adapter.py`:**

```python
"""Data-API-shape adapter over a live pymysql connection.

The vendored mysql_* collectors were written against RDS Data API
(execute_statement returning {"records": [[{"stringValue":...}, ...]]}).
RDS for MySQL has no Data API, so this adapter runs their SQL over a direct
pymysql connection and re-encodes rows in the exact field-dict shape the
collectors' _str/_long/_double helpers unwrap — the collectors stay verbatim
copies of the Aurora versions (parity-tested)."""
from datetime import date, datetime
from decimal import Decimal


def _field(v):
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, Decimal):
        return {"doubleValue": float(v)}
    if isinstance(v, bytes):
        return {"stringValue": v.decode("utf-8", errors="replace")}
    if isinstance(v, (datetime, date)):
        return {"stringValue": str(v)}
    return {"stringValue": str(v)}


class MySQLDataApiAdapter:
    """Duck-types the subset of the rds-data client the collectors use."""

    def __init__(self, conn):
        self._conn = conn

    def execute_statement(self, resourceArn=None, secretArn=None,
                          database=None, sql=None, parameters=None, **kwargs):
        # The vendored collectors pass static SQL only (no parameters) for
        # target reads; assert loudly if that assumption ever breaks.
        if parameters:
            raise ValueError("MySQLDataApiAdapter does not support parameters")
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return {"records": [[_field(v) for v in row] for row in rows]}
```

Run adapter tests → PASS.

- [ ] **Step 4: Handler tests (RED).** Append (mirror docdb_mongo_collector's testability: a patchable `_CONNECT_FACTORY` hook):

```python
def test_handler_filters_rds_instance_mysql_with_secret():
    h = _load("handler")
    rows = [
        {"cluster_id": "a", "engine_family": "rds_instance", "engine": "mysql",
         "db_secret_arn": "arn:x", "endpoint": "h", "port": 3306},
        {"cluster_id": "b", "engine_family": "rds_instance", "engine": "sqlserver-ex",
         "db_secret_arn": "arn:y", "endpoint": "h", "port": 1433},   # R-4, skip
        {"cluster_id": "c", "engine_family": "rds_instance", "engine": "mysql",
         "db_secret_arn": "", "endpoint": "h", "port": 3306},        # no secret, skip
        {"cluster_id": "d", "engine_family": "relational", "engine": "aurora-mysql"},
    ]
    assert [r["cluster_id"] for r in h._eligible(rows)] == ["a"]

def test_process_cluster_never_raises_and_isolates_failures():
    h = _load("handler")
    h._CONNECT_FACTORY = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    res = h._process_cluster(
        {"cluster_id": "a", "endpoint": "h", "port": 3306, "db_secret_arn": "arn:x"},
        secrets=MagicMock(), cache_execute=lambda *a, **k: None, run_ts="2026-07-22T00:00:00+00:00")
    assert res["cluster_id"] == "a" and "error" in res
```

- [ ] **Step 5: Implement `handler.py`.** Structure (clone docdb_mongo_collector/handler.py — read it first; reuse its `_scan_all`, `_make_cache_execute` shapes):

```python
"""RDS MySQL deep-read collector (rds_instance family) — in-VPC, direct TCP.

Clone of docdb_mongo_collector's architecture: own registry scan + per-cluster
isolation; connects with pymysql over TLS (fail-closed, vendored
global-bundle.pem) using the registry row's endpoint/port + db_secret_arn
(RDS-managed master secrets carry ONLY username/password), then runs the
vendored Aurora-MySQL collectors unmodified through MySQLDataApiAdapter.
Cache writes go to the Aurora PG cache via RDS Data API, marker
/* source=dbops-rdsdirect */."""
```

Key parts (write in full):

- `_CA_BUNDLE_PATH = os.path.join(os.path.dirname(__file__), "global-bundle.pem")`
- `_connect(host, port, user, password, database)`: lazy `import pymysql`; **fail-closed TLS**: `if not os.path.exists(_CA_BUNDLE_PATH): raise RuntimeError(...)`; `pymysql.connect(host=..., port=int(...), user=..., password=..., database=database, ssl_ca=_CA_BUNDLE_PATH, ssl_verify_cert=True, ssl_verify_identity=True, connect_timeout=8, read_timeout=30, autocommit=True)`. Module-level `_CONNECT_FACTORY = _connect` hook for tests.
- `_eligible(rows)`: `engine_family=="rds_instance" and "mysql" in engine and db_secret_arn and endpoint`.
- `_process_cluster(row, secrets, cache_execute, run_ts)`: try/except → never raises; secret JSON `username/password` (host/port from ROW); `database = row.get("db_name") or "mysql"` (system schema always exists — gives the session a default schema so its own statements appear in digests); build adapter; call in order (each in its own try/except like etl handler): `collect_mysql_query_stats(adapter, cache_execute, "", "", cluster_id, database)` → `collect_mysql_table_stats` → `collect_mysql_locks` → `collect_mysql_activity` → `collect_mysql_innodb_status(..., snapshot_ts=run_ts)`; finally `conn.close()`.
- `lambda_handler`: env read, single `run_ts = datetime.now(timezone.utc).isoformat()` shared across clusters this tick (finding-batch rule), `_scan_all` + loop + result list.

Run all Task 1 tests → PASS. Full suite: `python3 -m pytest tests/unit -q` → no regressions.

- [ ] **Step 6: Commit**

```bash
git add data-pipeline/rds_direct_collector tests/unit/data_pipeline/test_rds_direct_collector.py
git commit -m "feat(etl): rds_direct_collector — pymysql deep-read for rds_instance MySQL (R-2)"
```

---

### Task 2: CDK wiring (data_stack) for the new Lambda

**Files:**

- Modify: `cdk/stacks/data_stack.py` (clone the docdb_mongo_lambda block at ~L250-314 — SG, Lambda, schedule, IAM)

**Interfaces:**

- Consumes: Task 1's asset dir `data-pipeline/rds_direct_collector/`, env names from its handler.
- Produces: Lambda `RdsDirectCollector` in `self.vpc` with its own SG, 5-min schedule, and grants.

- [ ] **Step 1: Read the docdb block** (`data_stack.py` ~L250-314) and clone it adjacent, adapted:
  - SG `rds_direct_sg` (description ASCII only: `"dbops rds-instance direct collector - egress to mysql 3306"`; allow_all_outbound=True)
  - Lambda: runtime PYTHON_3_12, handler `handler.lambda_handler`, `from_asset("../data-pipeline/rds_direct_collector", bundling=...)` — same `_PipLocalBundling` + Docker-fallback command pattern INCLUDING the global-bundle.pem curl vendor step (copy the docdb block's exact command strings, path-adjusted). timeout 5 min, memory 512, vpc + security_groups.
  - env: `CLUSTERS_TABLE`, `CACHE_DB_CLUSTER_ARN`, `CACHE_DB_SECRET_ARN`, `CACHE_DB_NAME` (match docdb block's sources).
  - IAM: `cache_db.secret.grant_read`, `cache_db.grant_data_api_access`, `clusters_table.grant_read_data`, plus `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:*:{account}:secret:*` (docdb precedent — per-cluster secret ARNs are arbitrary).
  - Schedule: `events.Schedule.rate(Duration.minutes(5))` + target (copy docdb's Rule block).
- [ ] **Step 2: Synth check.** Run: `cd cdk && cdk synth dbops-dev-data > /dev/null && echo SYNTH_OK` → `SYNTH_OK`.
- [ ] **Step 3: Commit**

```bash
git add cdk/stacks/data_stack.py
git commit -m "feat(cdk): rds_direct_collector lambda — VPC/SG/schedule/IAM (R-2)"
```

---

### Task 3: Secret auto-resolve at registration + cache-only findings in etl_collector

**Files:**

- Modify: `api/clusters/handler.py` (`_register_rds_instance` ~L620-663)
- Modify: `data-pipeline/etl_collector/handler.py` (rds_instance branch ~L170-190)
- Modify: 4× `engine_family.py` copies via cp from canonical (findings set update)
- Test: extend `tests/unit/api/test_register_rds_instance.py`, `tests/unit/data_pipeline/test_etl_dispatch.py`, `tests/unit/data_pipeline/test_engine_family.py`

**Interfaces:**

- Consumes: `describe_db_instances` response `MasterUserSecret.SecretArn`; `_convention_secret_for(session, cluster_id)`-style lookup (Aurora precedent at api/clusters/handler.py:333-343 — read it; the rds path may reuse or replicate the convention helper).
- Produces: registry rows whose `db_secret_arn` auto-resolves as: body override > convention secret (`dbops/<cluster_id>/readonly`) > `MasterUserSecret.SecretArn` > `""`; plus `db_secret_source` field (`"override"|"convention"|"master_fallback"|"missing"`). etl_collector's rds_instance branch additionally calls `collect_mysql_param_fitness` (cache-only — no VPC need) for mysql engines. `CAPABILITIES[RDS_INSTANCE]["findings"] = {"param_fitness"}`.

- [ ] **Step 1: Tests (RED).** Registration: master-fallback resolution when body has no db_secret_arn and describe returns MasterUserSecret; body override wins; `db_secret_source` recorded. ETL: rds_instance+mysql calls `collect_mysql_param_fitness` with the cache args + shared run_ts, sqlserver does NOT. engine_family: findings set == {"param_fitness"} + copies identical.
- [ ] **Step 2: Implement.** Registration: after the engine allowlist check, resolve in the stated precedence (convention lookup mirrors the Aurora helper — same secret naming `dbops/<id>/readonly`). ETL branch: after the PI call, add:

```python
        if "mysql" in engine:
            try:
                result["param_fitness"] = collect_mysql_param_fitness(
                    cache_rds_data, cache_cluster_arn, cache_secret_arn,
                    cache_db_name, cluster_id, snapshot_ts=run_ts)
            except Exception as e:
                result["param_fitness_error"] = str(e)
                print(f"[{cluster_id}] param_fitness error: {e}")
```

(import already exists in handler for the relational branch — verify). Canonical engine_family.py: `"findings": {"param_fitness"},` + comment update (innodb finding comes from the VPC collector), then `cp` to the 3 other copies.

- [ ] **Step 3: Run full suite** → green. **Step 4: Commit** `feat(clusters)+feat(etl): secret auto-resolve + mysql cache-only findings for rds_instance (R-2)`.

---

### Task 4: Frontend — perf/internals tabs for rds_instance MySQL

**Files:**

- Modify: `frontend/src/app/dashboard/page.tsx` (TABS_BY_FAMILY ~L182-188; visibleTabs ~L472-475; perf tab body ~L963-982; internals tab body ~L1015-1027)

**Interfaces:**

- Consumes: `fam === "rds_instance"`, `isMysql(activeEngine)`/`isPostgres(activeEngine)` from engine.ts (already correct for bare `mysql`).
- Produces: rds_instance MySQL sees perf(쿼리/세션/락/테이블) + internals(InnoDB) tabs; SQL Server sees neither yet (R-4).

- [ ] **Step 1:** `TABS_BY_FAMILY.rds_instance = ["overview", "perf", "internals", "audit"]`.
- [ ] **Step 2:** visibleTabs filter — internals+perf only for MySQL within the family (SQL Server has no deep data until R-4):

```tsx
const visibleTabs: TabKey[] = selectedCluster
  ? TABS_BY_FAMILY[fam].filter(
      (t) =>
        fam !== "rds_instance" ||
        !["perf", "internals"].includes(t) ||
        isMysql(activeEngine),
    )
  : [];
```

- [ ] **Step 3:** Perf tab gate `fam === "relational"` → `(fam === "relational" || fam === "rds_instance")`; INSIDE it, keep `LiveTopPanel` (already PG-string-gated) and additionally gate `WaitEventsPanel` with `isPostgres(activeEngine) &&` (PG wait-events only — renders empty noise otherwise). QueriesPanel/ActiveSessionsPanel/LongRunningPanel/LocksPanel/TableSizesPanel stay for both (generic cache reads; honest empty states until data flows).
- [ ] **Step 4:** Internals tab gate `fam === "relational"` → `(fam === "relational" || fam === "rds_instance")`; `VacuumPanel` already PG-string-gated (verify), `EngineInternalsPanel` branches isMysql internally — no panel change.
- [ ] **Step 5:** `cd frontend && npx tsc --noEmit && npm run build` → clean. **Step 6: Commit** (prettier retry) `feat(ui): perf/internals tabs for rds_instance MySQL (R-2)`.

---

### Task 5: Deploy + live end-to-end verification (orchestrator)

- [ ] **Step 1:** Full unit suite green.
- [ ] **Step 2:** `cd frontend && npm run build`, then single `cdk deploy dbops-dev-data dbops-dev-agent dbops-dev-frontend --require-approval never` (agent stack carries api/clusters change). Verify 3× UPDATE_COMPLETE via CloudFormation.
- [ ] **Step 3:** Re-register `dbops-demo-mysql` via live API (idempotent put) → response 201; registry row now has `db_secret_arn` (master_fallback) + `db_secret_source`.
- [ ] **Step 4:** Invoke `rds_direct_collector` Lambda directly → result shows collected sections, no error. Invoke a second time (first run's own digest queries land in performance_schema with SCHEMA_NAME='mysql').
- [ ] **Step 5:** Cache checks via dashboard API (browser authed fetch): `/slow-queries`(or `/overview` top_queries) rows exist for dbops-demo-mysql; `/settings` shows max_connections etc.; `/timeseries?metric=innodb_history_list_length` (+checkpoint_age — RDS MySQL HAS a local redo log so this metric appears, unlike Aurora); `/blocking-locks`·`/long-running` respond 200 (empty OK).
- [ ] **Step 6:** Browser: dashboard perf탭(쿼리 통계 rows)·internals탭(InnoDB 차트 4종) 렌더; mssql은 perf/internals 탭 자체가 안 보임; chat: "dbops-demo-mysql 최근 쿼리 통계 보여줘" → get_top_queries가 실데이터 반환.
- [ ] **Step 7:** Ledger + memory update, report.

## Execution notes (orchestrator)

- Routing: T1·T3 Opus, T2 Opus(소형이지만 CDK 정합), T4 Sonnet, T5 orchestrator. 순차 실행(공유 체크아웃).
- 데모 인스턴스 SG는 이미 VPC CIDR에서 3306 허용(R-1) — Lambda가 같은 VPC라 추가 SG 작업 불필요.
- Post-commit Codex 테스터 findings는 각 태스크 리뷰와 병합 처리.
