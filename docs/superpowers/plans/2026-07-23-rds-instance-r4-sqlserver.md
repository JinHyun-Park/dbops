# R-4: RDS SQL Server Full Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring RDS for SQL Server to the same depth as RDS MySQL — deep-read collection (DMVs), chat T-SQL execution over direct TCP, dashboard perf/internals tabs, prompts. Write ops (reboot/snapshot/class) already cover SQL Server from R-3 (boto3, engine-agnostic) — verify live only.

**Architecture:** New `mcp-servers/mcp_servers/shared/mssql_direct.py` (pytds, **TLS we enforce** since RDS SQL Server doesn't force it: `cafile` always + `validate_host=True`) + a Data-API-shape adapter (columnMetadata from cursor.description). DMV collectors run in the existing `rds_direct_collector` Lambda (new sqlserver branch). execute_sql's `sql_via="direct"` branch extends to sqlserver. sql_safety gains T-SQL awareness — **critical: T-SQL runs `;`-separated batches natively (pytds sends the whole string), so the classifier must parse the full batch** (it already parses whole text — verified it caught the MySQL `/*!*/` case). Recon facts + retired TLS-wheel risk: see `.superpowers/sdd/progress.md` R-4 section.

## Global Constraints

- NO Co-Authored-By trailer; prettier/pre-commit reformat → `git add -A` re-commit. `str(e)` never in tool/API responses.
- **TLS fail-closed, enforced by us**: RDS SQL Server `rds.force_ssl` defaults OFF — the connector MUST pass `cafile=<vendored global-bundle.pem>` and keep `validate_host=True`, never `enc_login_only=True`. CA-missing → RuntimeError before connect (pg_direct/mysql_direct pattern).
- **Deps**: `mcp-servers/requirements.txt` += `python-tds>=1.17`, `pyOpenSSL>=25` (pytds needs pyOpenSSL for TLS; cryptography/cffi pulled transitively — all resolve as manylinux2014_x86_64 wheels, dry-run-verified, operations Lambda is x86_64). Same shared requirements.txt ships to all 4 MCP lambdas (harmless-unused convention). The `rds_direct_collector` Lambda ALSO needs these in ITS own `data-pipeline/rds_direct_collector/requirements.txt`.
- **T-SQL batch safety**: `is_read_only_safe`/execute_sql classification must treat the FULL text as executable (T-SQL native multi-statement). Add T-SQL side-effecting/dangerous patterns; the existing multi-statement + DROP/TRUNCATE regexes already scan whole text.
- Vendored-copy + byte-parity convention for anything duplicated into `rds_direct_collector/`.
- `engine_family.py` CAPABILITIES edits = 4 py copies lockstep + TS mirror. Demo instances standing. `cdk/config/settings.py` untouchable. Single `cdk deploy` process; agent/ has **pycache** ban (ast.parse only).
- Korean user-facing text, English jargon.

---

### Task 1: `mssql_direct.py` (pytds connect + TLS + adapter) + deps

**Files:** Create `mcp-servers/mcp_servers/shared/mssql_direct.py`; Modify `mcp-servers/requirements.txt`; Test `tests/unit/mcp_servers/shared/test_mssql_direct.py` (new).

**Interfaces:**

- `connect(host, port, database, user, password)` — lazy `import pytds`; CA check (fail-closed RuntimeError) BEFORE import (mysql_direct pattern); `pytds.connect(server=host, port=int(port), database=database or None, user=user, password=password, cafile=<CA path>, validate_host=True, login_timeout=10, timeout=30, autocommit=True)`. CA path = `mcp_servers/global-bundle.pem` (two-levels-up, same as pg_direct/mysql_direct). `database or None` — empty → server default (SQL Server writes need explicit DB via `USE`/qualified names; mirror the MySQL asymmetry in Task 3, not here).
- `MSSQLDataApiAdapter(conn).execute_statement(sql=..., parameters=None, ...)` — ValueError on parameters; `cursor.execute(sql)`; if `cur.description is not None` → `{"records": [[field-dict…]], "columnMetadata": [{"name": d[0]} for d in cur.description]}` (description is 7-tuple, name is d[0]); else `{"records": [], "columnMetadata": [], "numberOfRecordsUpdated": cur.rowcount}`. `_field` mapping IDENTICAL to mysql_direct (None→isNull, bool→boolean, int→long, float/Decimal→double, bytes→stringValue utf-8 replace, datetime/date→stringValue str). (pytds returns Decimal/naive-datetime/str/bytes/int per recon — same arms cover it.)

- [ ] **Step 1 (RED):** tests — field mapping (all type arms), columnMetadata from mock 7-tuple description, numberOfRecordsUpdated when description None, ValueError on parameters, fail-closed connect (monkeypatch CA path missing → RuntimeError, no pytds import needed).
- [ ] **Step 2:** implement (docstring: TLS-we-enforce rationale — RDS force_ssl off; cite validate_host + no enc_login_only).
- [ ] **Step 3:** `python3 -m pytest tests/unit -q` green. **Step 4:** commit `feat(shared): mssql_direct — pytds connect (enforced TLS) + Data-API adapter (R-4)`.

---

### Task 2: T-SQL awareness in sql_safety

**Files:** Modify `mcp-servers/mcp_servers/shared/sql_safety.py`; Test extend `tests/unit/mcp_servers/shared/test_sql_safety.py`.

**Interfaces:** T-SQL side-effecting/dangerous statements classify correctly on FULL batch text. Add to SIDE*EFFECTING_PATTERNS (# T-SQL): `\bWAITFOR\s+DELAY\b`, `\bWAITFOR\s+TIME\b`, `\bxp_cmdshell\b`, `\bsp_configure\b`, `\bBULK\s+INSERT\b`, `\bOPENROWSET\b`, `\bOPENQUERY\b`, `\bsp_executesql\b`, `\bEXEC(UTE)?\s+(sys\.)?(sp*|xp\_)`. Add to DANGEROUS_PATTERNS if not covered: `\bDROP\b`/`\bTRUNCATE\b`already there; add`\bDBCC\b` (side-effecting admin) to SIDE_EFFECTING. Verify the multi-statement detector treats T-SQL batches (`SELECT 1; DROP TABLE x`) as multi → non-safe (it scans whole text — confirm with a test). PG/MySQL patterns unchanged.

- [ ] **Step 1 (RED):** tests — each new T-SQL statement → `is_read_only_safe` False; `SELECT 1; DROP TABLE x` (T-SQL batch) → not safe AND dangerous; plain `SELECT name FROM sys.databases` and `SELECT execution_count FROM sys.dm_exec_query_stats` stay safe (word boundaries: `sp_configure` must not match a column `sp_configured_flag` — verify); literal `'xp_cmdshell'` in a string stays safe (stripper).
- [ ] **Step 2:** add patterns. **Step 3:** GREEN + full suite (watch for existing tests asserting any of these safe). **Step 4:** commit `feat(safety): T-SQL side-effecting/batch patterns (R-4)`.

---

### Task 3: execute_sql direct branch → SQL Server

**Files:** Modify `mcp-servers/mcp_servers/operations/tools/execute_sql.py`; Test extend `tests/unit/mcp_servers/operations/test_execute_sql.py`.

**Interfaces:** Today the `sql_via="direct"` branch routes `"mysql" in engine` → mysql_direct, else → unsupported_engine (R-4 message). Change: `"sqlserver" in engine` → mssql_direct path (same structure — secret select read/write, `client_for_cluster` secret fetch, `/* source=dbops-agent */` marker, shared decode block, TOCTOU-free since it's SQL not infra). Keep mysql branch unchanged. Any rds_instance engine that is neither → keep a generic unsupported message. Write default-schema asymmetry: SQL Server writes with no db_name → `database=None` (server default; the agent must qualify with `[db].[schema].[table]` or `USE` — the static failure hint already added in R-3 applies; extend its wording to mention T-SQL qualification).

- [ ] **Step 1 (RED):** tests mirroring the mysql direct tests but for a `sqlserver-ex` row: safe SELECT → executed via mssql_direct (patch mssql_direct in module namespace), rds-data NOT called; approved write without db_write_secret_arn → fail-closed; with it → executes via write secret; mysql row still routes to mysql_direct (regression); Aurora still rds-data (regression).
- [ ] **Step 2:** implement (import mssql_direct; branch on engine substring). **Step 3:** GREEN + full suite. **Step 4:** commit `feat(operations): execute_sql direct T-SQL path for rds_instance SQL Server (R-4)`.

---

### Task 4: DMV collectors in rds_direct_collector (SQL Server branch)

**Files:**

- Create `data-pipeline/rds_direct_collector/mssql_query_stats.py`, `mssql_activity.py`, `mssql_waits.py` (NEW — no Aurora precedent; write against DMVs, output the SAME cache shapes the MySQL collectors use: query_stats table, long_running_queries/blocking_locks, metric_snapshots).
- Modify `data-pipeline/rds_direct_collector/handler.py` (`_process_cluster` — add sqlserver branch using MSSQLDataApiAdapter; `_eligible` already accepts all rds_instance — restrict per-engine dispatch inside), `data-pipeline/rds_direct_collector/requirements.txt` (+python-tds, pyOpenSSL), `data-pipeline/rds_direct_collector/mssql_adapter.py` (adapter — can import from a vendored copy or inline; keep it a vendored twin of mssql_direct's adapter for the no-columnMetadata collector use, OR reuse — decide in impl, parity-test if duplicated).
- Test `tests/unit/data_pipeline/test_rds_direct_collector.py` (extend).

**Interfaces (DMV SQL — time cols are microseconds → /1000 for ms):**

- `mssql_query_stats`: `SELECT TOP 100 qs.query_hash, SUBSTRING(st.text,...) AS query_text, qs.execution_count AS calls, qs.total_elapsed_time/1000.0 AS total_time_ms, (qs.total_elapsed_time/NULLIF(qs.execution_count,0))/1000.0 AS mean_time_ms, qs.total_rows AS rows_returned FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st ORDER BY qs.total_elapsed_time DESC` → INSERT into `query_stats` (same columns the MySQL collector uses; query_hash is binary → hex/str via adapter).
- `mssql_activity`: `sys.dm_exec_requests` r JOIN `sys.dm_exec_sessions` s — active count by status → metric_snapshots `conn_active`/`conn_idle` (map like the MySQL collector); long-running (r.total_elapsed_time > 5000ms) → long_running_queries; blocking (r.blocking_session_id <> 0) → blocking_locks.
- `mssql_waits`: `sys.dm_os_wait_stats` top waits → metric*snapshots `mssql_wait*\*` (or reuse a generic wait metric_type; keep engine-scoped names to avoid colliding with MySQL innodb metrics).
- Handler branch: `if "sqlserver" in engine:` connect via mssql_direct-style factory, run the 3 collectors through MSSQLDataApiAdapter, per-collector try/except isolation, shared run_ts.

- [ ] **Step 1 (RED):** collector unit tests (adapter-fed records → assert INSERT params + the DMV SQL constants contain the /1000 ms conversion and TOP 100); handler dispatch test (sqlserver row → mssql collectors called, mysql collectors NOT, rds-data NOT).
- [ ] **Step 2:** implement. **Step 3:** GREEN + full suite. **Step 4:** commit `feat(etl): SQL Server DMV deep-read collectors in rds_direct_collector (R-4)`.

---

### Task 5: Frontend — perf/internals tabs for SQL Server

**Files:** Modify `frontend/src/app/dashboard/page.tsx` (visibleTabs filter + internals panel gating); possibly `frontend/src/components/dashboard/engine-internals-panel.tsx` (add a sqlserver metric set alongside PG/MySQL).

**Interfaces:** Today `rds_instance` perf/internals tabs are gated `isMysql(activeEngine)` only. Extend to also show for SQL Server (`engineKind==="sqlserver"`). Perf panels (queries/sessions/locks/tablesizes) are generic cache reads → work as-is. Internals: `EngineInternalsPanel` branches `isMysql` for InnoDB metrics — add a sqlserver branch with the `mssql_wait_*` metric keys (or hide the InnoDB card for SQL Server and show waits). VacuumPanel stays PG-only; WaitEventsPanel stays engine-normalized (AAS — but SQL Server has no PI AAS unless enabled; it renders empty-state, honest). No InnoDB for SQL Server.

- [ ] **Step 1:** visibleTabs: extend the rds*instance perf/internals predicate to `isMysql(activeEngine) || engineKind(activeEngine)==="sqlserver"`. **Step 2:** EngineInternalsPanel sqlserver branch (metric keys from Task 4's `mssql_wait*\*`; if none collected yet, honest empty state). **Step 3:** `npx tsc --noEmit && npm run build`. **Step 4:** commit `feat(ui): perf/internals tabs for rds_instance SQL Server (R-4)`.

---

### Task 6: prompts + execute_sql message + write-tool verification

**Files:** Modify `agent/prompts/system_prompt.py`, `cheatsheet.py` (SQL Server SQL now supported — update the "SQL Server는 이후 릴리스" wording to "MySQL·SQL Server 모두 직접 실행 가능"); `mcp-servers/mcp_servers/operations/tools/execute_sql.py` if it carries a now-stale "R-4" message string (Task 3 already routes sqlserver, so remove/adjust the leftover message).

- [ ] **Step 1:** prompt updates (ast.parse check, no **pycache**). **Step 2:** confirm no execute_sql code path still returns the "SQL Server 이후 릴리스(R-4)" message for a supported sqlserver row (Task 3 handles it; this is a consistency sweep). **Step 3:** `npx tsc`/ast checks. **Step 4:** commit `feat(agent): SQL Server SQL now supported in prompts (R-4)`.

---

### Task 7: Deploy + live E2E (orchestrator)

- [ ] Full unit suite; frontend build; single `cdk deploy dbops-dev-data dbops-dev-agent dbops-dev-frontend` (data=collector+deps, agent=mcp deps+execute_sql+prompts, frontend=tabs).
- [ ] Backfill `db_secret_arn` + `db_write_secret_arn` on `dbops-demo-mssql` (its MasterUserSecret ARN via PATCH — the R-3 PATCH endpoint). Set `db_name` if a user DB is needed (SQL Server default `master` is fine for read DMVs; for write demo, CREATE DATABASE demo first).
- [ ] Invoke `rds_direct_collector` → sqlserver branch collects DMV query_stats/activity/waits, no error (pytds TLS connect works live — the leading risk; if pyOpenSSL/CA issue, this is where it surfaces — capture the exact error).
- [ ] Chat: T-SQL `SELECT @@VERSION` and `SELECT TOP 5 name FROM sys.databases` → executed with columns; a write (`CREATE DATABASE demo` then a qualified CREATE TABLE) → approval → I approve (delegated) → executed.
- [ ] Write-op verification (already-built R-3 tools on SQL Server): a `create_rds_snapshot` on dbops-demo-mssql → approval → execute → AWS snapshot exists. Confirms the engine-agnostic write path covers SQL Server.
- [ ] Browser: dbops-demo-mssql dashboard now shows perf/internals tabs with query/session data; honest empty states where SQL Server lacks a surface.
- [ ] Ledger + memory + report.

## Execution notes (orchestrator)

- Routing: T1 Sonnet, T2 Sonnet, T3 Opus, T4 Opus, T5 Sonnet, T6 Sonnet, T7 orchestrator. Sequential (shared checkout).
- Leading live risk = pytds TLS handshake against RDS SQL Server in-Lambda (pyOpenSSL present, CA vendored, force_ssl off so WE bring TLS). T7 step 3 is the proof; if it fails, the fix is contained to mssql_direct/bundling, not the whole spec.
- Write ops need NO new code (R-3 tools are engine-agnostic) — T7 verifies live only.
- Approvals for demo verification: handled by me via the UI-identical PUT path under the user's standing delegation (2026-07-23), reported each time.
