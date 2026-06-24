# ElastiCache EC-2 Findings + RCA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose ElastiCache from the cached metrics — emit health findings (eviction/hit-rate/memory/lag/CPU/connections) into the existing multi-engine findings pipeline, and add ElastiCache signals to incident RCA.

**Architecture:** A new `elasticache_findings.py` collector (mirrors `docdb_findings.py`) writes `elasticache_*` rows to `cluster_health_findings`; the ETL handler calls it in the existing `elasticache` branch with the shared `run_ts`; a new RCA signal source surfaces cache spikes in `diagnose_root_cause`. Findings surface through the existing engine-agnostic `_health_findings` endpoint + `get_maintenance_findings` MCP tool — no new route/tool.

**Tech Stack:** Python 3.12 (ETL Lambda collectors via RDS Data API; incident MCP Lambda).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **Read-only:** reads `metric_snapshots` + `cluster_meta`, writes only `cluster_health_findings`. No ElastiCache API call, no mutation, no new IAM/secret.
- **Shared snapshot_ts (CRITICAL GOTCHA):** the handler passes `snapshot_ts=run_ts`; every finding INSERT uses that one timestamp so the dashboard `MAX(snapshot_time)` batch keeps them together. Never substitute `datetime.now()` per-finding.
- **metric_snapshots reads filter cluster-level rows:** `AND (dimensions IS NULL OR dimensions::text = '{}')` (mirrors docdb_findings) — EC-1 wrote cluster-level rows with `dimensions='{}'`.
- **check_type prefix:** all EC-2 findings use `elasticache_*` (application-enforced; no DB constraint). The 6 types: `elasticache_evictions_spike`, `elasticache_low_hit_rate`, `elasticache_memory_pressure`, `elasticache_replication_lag`, `elasticache_high_cpu`, `elasticache_connection_surge`.
- **Engine branch:** Memcached (`cluster_meta.engine == "memcached"`) skips the replication-lag + memory-pressure rules and uses `get_hits`/`get_misses` for hit-rate; Redis/Valkey use `cache_hits`/`cache_misses` + `memory_usage_pct` + `replication_lag`.
- **Korean** `value_str`/`threshold_str`/`recommendation`; metric jargon (hit rate, eviction, replication lag) stays English.
- **RCA signal source is engine-safe:** wrapped in try/except → `skipped.append(...)` + `return []` on any failure (non-ElastiCache clusters just have no eviction/replication rows).

---

### Task 1: ElastiCache findings collector + ETL dispatch

**Files:**

- Create: `data-pipeline/etl_collector/collectors/elasticache_findings.py`
- Modify: `data-pipeline/etl_collector/handler.py` (import + call in the `elasticache` branch)
- Test: `tests/unit/data_pipeline/test_elasticache_findings.py` (create)

**Interfaces:**

- Consumes: `_execute(rds_data, cluster_arn, secret_arn, db_name, sql, params)` (mirror the one in `docdb_findings.py`); `cluster_meta.engine`; `metric_snapshots` rows.
- Produces: `collect_elasticache_findings(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None, window_hours=1) -> dict`. Writes `cluster_health_findings`.

- [ ] **Step 1: Read the template.** Read `data-pipeline/etl_collector/collectors/docdb_findings.py` in FULL — copy its `_execute` helper verbatim, its `add(...)`/`findings` pattern, the single-aggregation-query approach, and the insert loop. Read the `elasticache` branch in `data-pipeline/etl_collector/handler.py` (added in EC-1) to see where the findings call goes.

- [ ] **Step 2: Write the failing test.** Create `tests/unit/data_pipeline/test_elasticache_findings.py`:

```python
"""ElastiCache findings collector → cluster_health_findings."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/elasticache_findings.py"
_spec = importlib.util.spec_from_file_location("ec_findings", _C)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _fake_rds(meta_engine="redis", agg=None):
    """Mock rds_data so the FIRST execute_statement (cluster_meta engine) returns
    the engine, the SECOND (metric aggregation) returns agg, and INSERTs record."""
    rds = MagicMock()
    inserts = []
    calls = {"n": 0}
    agg = agg or {}

    def _exec(**kwargs):
        sql = kwargs.get("sql", "")
        if "FROM cluster_meta" in sql:
            return {"columnMetadata": [{"name": "engine"}],
                    "records": [[{"stringValue": meta_engine}]]}
        if "INSERT INTO cluster_health_findings" in sql:
            inserts.append({p["name"]: list(p["value"].values())[0] for p in kwargs.get("parameters", [])})
            return {"columnMetadata": [], "records": []}
        # aggregation query
        cols = list(agg.keys())
        rec = [({"isNull": True} if agg[c] is None else {"doubleValue": float(agg[c])}) for c in cols]
        return {"columnMetadata": [{"name": c} for c in cols], "records": [rec]}

    rds.execute_statement.side_effect = _exec
    rds._inserts = inserts
    return rds


def _run(rds):
    return mod.collect_elasticache_findings(rds, "arn", "sec", "db", "my-redis", snapshot_ts="2026-06-24T00:00:00Z")


def test_evictions_spike_critical():
    rds = _fake_rds("redis", {"sum_evictions": 1500, "sum_cache_hits": 900, "sum_cache_misses": 100,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_evictions_spike") == "critical"


def test_low_hit_rate_warning_and_shared_ts():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 80, "sum_cache_misses": 20,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    ins = {i["check_type"]: i for i in rds._inserts}
    assert "elasticache_low_hit_rate" in ins  # 80% < 85% warning
    # every finding shares the passed snapshot_ts
    assert all(i["ts"] == "2026-06-24T00:00:00Z" for i in rds._inserts)


def test_memory_and_lag_critical_redis():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 100, "sum_cache_misses": 0,
                              "max_memory_pct": 97, "max_replication_lag": 1500, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_memory_pressure") == "critical"
    assert types.get("elasticache_replication_lag") == "critical"


def test_memcached_skips_lag_and_memory_uses_get_hits():
    # Memcached: replication_lag + memory_pressure rules skipped; hit-rate from get_hits/get_misses
    rds = _fake_rds("memcached", {"sum_evictions": 0, "sum_get_hits": 50, "sum_get_misses": 50,
                                  "max_memory_pct": 99, "max_replication_lag": 9999, "max_engine_cpu": 5,
                                  "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"] for i in rds._inserts}
    assert "elasticache_replication_lag" not in types
    assert "elasticache_memory_pressure" not in types
    assert "elasticache_low_hit_rate" in types  # 50% hit-rate via get_hits


def test_high_cpu_warning():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 100, "sum_cache_misses": 0,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 85,
                              "max_cache_cpu": 50, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_high_cpu") == "warning"


def test_no_findings_when_healthy():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 1000, "sum_cache_misses": 0,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    res = _run(rds)
    assert res["findings_emitted"] == 0


def test_low_hit_rate_skipped_below_min_samples():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 1, "sum_cache_misses": 9,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 5})
    _run(rds)
    assert "elasticache_low_hit_rate" not in {i["check_type"] for i in rds._inserts}
```

- [ ] **Step 3: Run it to verify it fails.**

Run: `python -m pytest tests/unit/data_pipeline/test_elasticache_findings.py -q`
Expected: FAIL (module missing).

- [ ] **Step 4: Create `data-pipeline/etl_collector/collectors/elasticache_findings.py`:**

```python
"""ElastiCache Findings Collector — eviction spike, low hit-rate, memory pressure,
replication lag, high CPU, connection surge.

Reads the cached metric_snapshots only (no live AWS). Writes elasticache_* rows
to cluster_health_findings, all sharing the handler's snapshot_ts. Memcached
skips replication-lag + memory-pressure and uses get_hits/get_misses for hit-rate.
All metric_snapshots rows are cluster-level (dimensions='{}')."""

import json
from datetime import datetime, timezone

EVICTIONS_WARNING = 100.0
EVICTIONS_CRITICAL = 1000.0
HIT_RATE_WARNING = 0.85
HIT_RATE_CRITICAL = 0.70
MIN_HIT_SAMPLES = 20
MEMORY_WARNING_PCT = 85.0
MEMORY_CRITICAL_PCT = 95.0
REPL_LAG_WARNING_MS = 100.0
REPL_LAG_CRITICAL_MS = 1000.0
CPU_WARNING_PCT = 80.0
CPU_CRITICAL_PCT = 90.0
CONN_SURGE_WARNING = 60000.0


def _execute(rds_data, cluster_arn, secret_arn, db_name, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=db_name,
        sql=f"/* source=dbops-ecfind */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for idx, field in enumerate(rec):
            col = cols[idx] if idx < len(cols) and cols[idx] else f"col_{idx}"
            if field.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in field:
                    row[col] = field[typ]
                    break
        out.append(row)
    return out


def collect_elasticache_findings(
    rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
    cluster_id, snapshot_ts=None, window_hours=1,
):
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()
    errors = []

    # engine → hit-rate metric keys + which rules apply
    engine = "redis"
    try:
        meta = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
        if meta and meta[0].get("engine"):
            engine = str(meta[0]["engine"]).lower()
    except Exception as e:
        errors.append(f"meta: {e}")
    is_memcached = engine == "memcached"

    agg = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  SUM(CASE WHEN metric_type='evictions' THEN value ELSE 0 END) AS sum_evictions, "
        "  SUM(CASE WHEN metric_type='cache_hits' THEN value ELSE 0 END) AS sum_cache_hits, "
        "  SUM(CASE WHEN metric_type='cache_misses' THEN value ELSE 0 END) AS sum_cache_misses, "
        "  SUM(CASE WHEN metric_type='get_hits' THEN value ELSE 0 END) AS sum_get_hits, "
        "  SUM(CASE WHEN metric_type='get_misses' THEN value ELSE 0 END) AS sum_get_misses, "
        "  MAX(CASE WHEN metric_type='memory_usage_pct' THEN value END) AS max_memory_pct, "
        "  MAX(CASE WHEN metric_type='replication_lag' THEN value END) AS max_replication_lag, "
        "  MAX(CASE WHEN metric_type='engine_cpu' THEN value END) AS max_engine_cpu, "
        "  MAX(CASE WHEN metric_type='cache_cpu' THEN value END) AS max_cache_cpu, "
        "  MAX(CASE WHEN metric_type='curr_connections' THEN value END) AS max_curr_connections, "
        "  COUNT(CASE WHEN metric_type IN ('cache_hits','get_hits') THEN 1 END) AS hit_samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "hours": str(window_hours)},
    )
    if not agg:
        return {"cluster_id": cluster_id, "findings_emitted": 0, "errors": errors}
    r = agg[0]

    def _f(key):
        v = r.get(key)
        return float(v) if v is not None else None

    findings = []

    def add(check_type, severity, subject, value_str, threshold_str, recommendation, details):
        findings.append({
            "check_type": check_type, "severity": severity, "subject": subject,
            "value_str": value_str, "threshold_str": threshold_str,
            "recommendation": recommendation, "details": json.dumps(details),
        })

    # Rule 1: eviction spike
    ev = _f("sum_evictions") or 0.0
    if ev > EVICTIONS_WARNING:
        sev = "critical" if ev > EVICTIONS_CRITICAL else "warning"
        add("elasticache_evictions_spike", sev, "ElastiCache Eviction Spike",
            f"evictions {int(ev)}건 / {window_hours}시간",
            f"evictions > {int(EVICTIONS_CRITICAL)}건" if sev == "critical" else f"evictions > {int(EVICTIONS_WARNING)}건",
            f"최근 {window_hours}시간 eviction이 {int(ev)}건 발생했습니다. 메모리 용량 증설 또는 maxmemory-policy(LRU/TTL) 재검토를 권장합니다.",
            {"sum_evictions": ev, "window_hours": window_hours})

    # Rule 2: low hit-rate (engine-branched keys)
    if is_memcached:
        hits, misses = _f("sum_get_hits") or 0.0, _f("sum_get_misses") or 0.0
    else:
        hits, misses = _f("sum_cache_hits") or 0.0, _f("sum_cache_misses") or 0.0
    samples = int(r.get("hit_samples") or 0)
    total = hits + misses
    if samples >= MIN_HIT_SAMPLES and total > 0:
        hr = hits / total
        if hr < HIT_RATE_WARNING:
            sev = "critical" if hr < HIT_RATE_CRITICAL else "warning"
            pct = round(hr * 100, 1)
            add("elasticache_low_hit_rate", sev, "ElastiCache Low Hit Rate",
                f"hit rate {pct}%",
                f"hit rate < {int(HIT_RATE_CRITICAL*100)}%" if sev == "critical" else f"hit rate < {int(HIT_RATE_WARNING*100)}%",
                f"최근 {window_hours}시간 cache hit rate가 {pct}%입니다. 캐시 키 설계·TTL·워킹셋 크기 또는 메모리 증설을 점검하세요.",
                {"hit_rate": round(hr, 4), "hits": hits, "misses": misses, "window_hours": window_hours})

    # Rule 3: memory pressure (Redis/Valkey only)
    if not is_memcached:
        mem = _f("max_memory_pct")
        if mem is not None and mem >= MEMORY_WARNING_PCT:
            sev = "critical" if mem >= MEMORY_CRITICAL_PCT else "warning"
            add("elasticache_memory_pressure", sev, "ElastiCache Memory Pressure",
                f"memory {mem:.1f}%",
                f"memory ≥ {int(MEMORY_CRITICAL_PCT)}%" if sev == "critical" else f"memory ≥ {int(MEMORY_WARNING_PCT)}%",
                f"최근 {window_hours}시간 메모리 사용률 peak이 {mem:.1f}%입니다. eviction/OOM 위험 — 노드 타입 상향 또는 샤드 추가를 권장합니다.",
                {"max_memory_usage_pct": mem, "window_hours": window_hours})

    # Rule 4: replication lag (Redis/Valkey only)
    if not is_memcached:
        lag = _f("max_replication_lag")
        if lag is not None and lag >= REPL_LAG_WARNING_MS:
            sev = "critical" if lag >= REPL_LAG_CRITICAL_MS else "warning"
            add("elasticache_replication_lag", sev, "ElastiCache Replication Lag",
                f"peak {lag:.0f} ms",
                f"replication lag ≥ {int(REPL_LAG_CRITICAL_MS)} ms" if sev == "critical" else f"replication lag ≥ {int(REPL_LAG_WARNING_MS)} ms",
                f"최근 {window_hours}시간 replication lag peak이 {lag:.0f} ms입니다. 쓰기 부하 완화 또는 리드 레플리카 확장을 점검하세요.",
                {"max_replication_lag_ms": lag, "window_hours": window_hours})

    # Rule 5: high CPU (prefer engine_cpu — Redis single-threaded bottleneck)
    cpu = _f("max_engine_cpu")
    cpu_label = "engine CPU"
    if cpu is None:
        cpu, cpu_label = _f("max_cache_cpu"), "CPU"
    if cpu is not None and cpu >= CPU_WARNING_PCT:
        sev = "critical" if cpu >= CPU_CRITICAL_PCT else "warning"
        add("elasticache_high_cpu", sev, "ElastiCache High CPU",
            f"{cpu_label} {cpu:.1f}%",
            f"{cpu_label} ≥ {int(CPU_CRITICAL_PCT)}%" if sev == "critical" else f"{cpu_label} ≥ {int(CPU_WARNING_PCT)}%",
            f"최근 {window_hours}시간 {cpu_label} peak이 {cpu:.1f}%입니다. 핫 키·비싼 명령(KEYS/SORT) 점검 또는 노드 타입 상향을 권장합니다.",
            {"max_cpu_pct": cpu, "cpu_metric": cpu_label, "window_hours": window_hours})

    # Rule 6: connection surge
    conn = _f("max_curr_connections")
    if conn is not None and conn > CONN_SURGE_WARNING:
        add("elasticache_connection_surge", "warning", "ElastiCache Connection Surge",
            f"peak {int(conn)} connections",
            f"connections > {int(CONN_SURGE_WARNING)}",
            f"최근 {window_hours}시간 연결 수 peak이 {int(conn)}개입니다(Redis 한도 65000). connection pooling·클라이언트 누수 점검을 권장합니다.",
            {"max_curr_connections": conn, "window_hours": window_hours})

    for f in findings:
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, "
            "value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
            ":value_str, :threshold_str, :recommendation, :details::jsonb)",
            {"cluster_id": cluster_id, "ts": ts, "check_type": f["check_type"],
             "severity": f["severity"], "subject": f["subject"], "value_str": f["value_str"],
             "threshold_str": f["threshold_str"], "recommendation": f["recommendation"],
             "details": f["details"]},
        )

    return {"cluster_id": cluster_id, "engine": engine,
            "findings_emitted": len(findings), "errors": errors}
```

- [ ] **Step 5: Wire the ETL dispatch.** In `data-pipeline/etl_collector/handler.py`: add the import next to the other findings-collector imports:

```python
from collectors.elasticache_findings import collect_elasticache_findings
```

In `_collect_one`, inside the existing `if family == "elasticache":` branch, AFTER the metrics call and BEFORE `return result`, add:

```python
        try:
            result["elasticache_findings"] = collect_elasticache_findings(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
                cluster_id, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["elasticache_findings_error"] = str(e)
            print(f"[{cluster_id}] elasticache findings error: {e}")
```

(Match the exact parameter names the dynamodb/docdb findings calls use in this handler — read those two branches to confirm `cache_rds_data`/`cache_cluster_arn`/`cache_secret_arn`/`cache_db_name`/`run_ts` are the in-scope names.)

- [ ] **Step 6: Run tests.**

Run: `python -m pytest tests/unit/data_pipeline/test_elasticache_findings.py -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression.

- [ ] **Step 7: Commit.**

```bash
git add data-pipeline/etl_collector/collectors/elasticache_findings.py data-pipeline/etl_collector/handler.py tests/unit/data_pipeline/test_elasticache_findings.py
git commit -m "feat(elasticache): findings collector (eviction/hit-rate/memory/lag/cpu/connections) + ETL dispatch"
```

---

### Task 2: ElastiCache RCA signal source

**Files:**

- Modify: `mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py` (add `_collect_elasticache_signals` + `BASE_WEIGHTS["elasticache_spike"]` + the `candidates.extend(...)` call)
- Test: `tests/unit/mcp_servers/incident/test_elasticache_signals.py` (create — match the existing incident test layout; if `tests/unit/mcp_servers/incident/` doesn't exist, place it where other diagnose_root_cause tests live — search for `test_diagnose_root_cause`)

**Interfaces:**

- Consumes: `cache.execute(sql, params).rows`, `_recency_factor(when, anchor, win)`, `BASE_WEIGHTS` (all already in `diagnose_root_cause.py`).
- Produces: `_collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped) -> list` of candidate dicts.

- [ ] **Step 1: Read the template.** Read `mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py`: the `BASE_WEIGHTS` dict (~line 35), the `_collect_blocking`/`_collect_metric_spikes` signal collectors (candidate dict shape: `category, score, score_breakdown, summary, evidence, when, suggested_action`), `_recency_factor`, and the `candidates.extend(_collect_*())` calls in `diagnose_root_cause_impl` (~lines 182-188). Also find where the existing diagnose_root_cause tests live.

- [ ] **Step 2: Write the failing test.** Create the test (adapt path to where incident tests live):

```python
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[?] / "mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py"
# (set parents[?] to reach repo root from the test's location)
_spec = importlib.util.spec_from_file_location("drc", _C)
drc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drc)


class _Res:
    def __init__(self, rows): self.rows = rows


def test_elasticache_signals_eviction_and_lag():
    cache = MagicMock()
    cache.execute.return_value = _Res([
        {"ts": "2026-06-24T00:05:00Z", "metric_type": "evictions", "value": 500},
        {"ts": "2026-06-24T00:06:00Z", "metric_type": "replication_lag", "value": 1500},
    ])
    examined, skipped = {}, []
    out = drc._collect_elasticache_signals(
        cache, "my-redis", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z",
        "2026-06-24T00:08:00Z", 10, examined, skipped)
    cats = {c["category"] for c in out}
    assert "elasticache_spike" in cats
    assert all("score" in c and "when" in c for c in out)


def test_elasticache_signals_skips_on_error():
    cache = MagicMock()
    cache.execute.side_effect = Exception("no table")
    examined, skipped = {}, []
    out = drc._collect_elasticache_signals(
        cache, "x", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z", "2026-06-24T00:08:00Z", 10, examined, skipped)
    assert out == [] and "elasticache_signals" in skipped


def test_base_weight_present():
    assert drc.BASE_WEIGHTS.get("elasticache_spike") == 2.5
```

- [ ] **Step 3: Run it to verify it fails.**

Run: `python -m pytest <test path> -q` → FAIL (`_collect_elasticache_signals` / weight missing).

- [ ] **Step 4: Add `BASE_WEIGHTS["elasticache_spike"] = 2.5`** to the `BASE_WEIGHTS` dict.

- [ ] **Step 5: Add the signal collector** (mirror `_collect_metric_spikes` shape):

```python
def _collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped):
    """ElastiCache cache-specific signals from metric_snapshots: eviction spikes
    and replication-lag spikes near the incident. Engine-safe — non-ElastiCache
    clusters have no such rows, so this yields nothing."""
    out = []
    sql = """
        SELECT ts, metric_type, value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND metric_type IN ('evictions', 'replication_lag')
          AND ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
          AND (dimensions IS NULL OR dimensions::text = '{}')
          AND ((metric_type = 'evictions' AND value > 100)
               OR (metric_type = 'replication_lag' AND value >= 100))
        ORDER BY value DESC
    """
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] elasticache_signals source skipped: {e}")
        skipped.append("elasticache_signals")
        return out
    examined["elasticache_signals"] = len(rows)
    for row in rows:
        when = row.get("ts")
        mtype = row.get("metric_type")
        value = row.get("value")
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        rf = _recency_factor(when, anchor, win)
        score = BASE_WEIGHTS["elasticache_spike"] * rf
        if mtype == "replication_lag":
            title = "ElastiCache Replication Lag Spike"
            desc = f"replication lag {value:.0f} ms near the incident"
            action = "Check write load / failover; replication lag often coincides with a primary failover or load surge."
        else:
            title = "ElastiCache Eviction Spike"
            desc = f"{int(value)} evictions in a minute near the incident"
            action = "Memory pressure — evictions spiking suggests the working set exceeds capacity; check maxmemory-policy and node size."
        out.append({
            "category": "elasticache_spike",
            "score": score,
            "score_breakdown": {"base_weight": BASE_WEIGHTS["elasticache_spike"], "recency_factor": round(rf, 3), "formula": "base × recency"},
            "summary": title,
            "evidence": {"metric_type": mtype, "value": value, "metric_time": when},
            "when": when,
            "suggested_action": action,
        })
    return out
```

- [ ] **Step 6: Wire the call** in `diagnose_root_cause_impl`, next to the other `candidates.extend(...)` calls:

```python
    candidates.extend(_collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
```

- [ ] **Step 7: Run tests.**

Run: `python -m pytest <test path> -q` → PASS.
Run: `python -m pytest tests/unit -q` → no regression (existing diagnose_root_cause tests still pass; the new source is additive + engine-safe).

- [ ] **Step 8: Commit.**

```bash
git add mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py tests/unit/...
git commit -m "feat(elasticache): RCA signal source (eviction + replication-lag spikes)"
```

---

## Post-implementation (controller, after both tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD` — focus: collector reads cluster-level rows (`dimensions='{}'` filter) + shares `snapshot_ts` to every INSERT; the 6 rules' thresholds + Memcached branch (skips lag/memory, uses get*hits/get_misses) are correct; `check_type` strings are `elasticache*\*` and match nothing the dashboard would mis-route; RCA source is engine-safe (try/except → skipped, returns []) and doesn't perturb existing signal scoring; read-only (no mutation/IAM/secret).
- Deploy dev: `cdk deploy dbops-dev-data` (ETL collector — the elasticache findings collector is packaged with the ETL Lambda) + `cdk deploy dbops-dev-agent` (incident MCP Lambda — the RCA change; confirm which stack the incident MCP Lambda lives in and deploy it). No frontend change.
- Live smoke: after one ETL cycle on a registered ElastiCache cluster (if one exists), `GET /api/dashboard?cluster_id=<ec>&page=health` returns `elasticache_*` findings; the agent's `get_maintenance_findings` returns them. If no ElastiCache cluster is registered (admin-gated + needs a real cluster), the findings + RCA paths are unit-covered (same constraint as EC-1) — verify no regression to existing engines' findings (an Aurora cluster's `page=health` still returns its findings; `diagnose_root_cause` on an Aurora cluster is unchanged).
- Then `superpowers:finishing-a-development-branch`.
