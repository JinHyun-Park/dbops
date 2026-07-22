# R-3: RDS MySQL SQL Execution + Write Ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chat SQL execution for `rds_instance` MySQL via direct TCP (execute_sql `sql_via="direct"` branch, same approval semantics as Aurora) + 3 approval-gated instance write tools (reboot / snapshot / instance-class modify) + prompts.

**Architecture:** New `mcp-servers/mcp_servers/shared/mysql_direct.py` (pymysql fail-closed TLS + Data-API-shape adapter WITH columnMetadata synthesis from cursor.description) plugs into execute_sql's existing decode path unchanged. Secrets resolve via `client_for_cluster` (cross-account-aware precedent — NOT the docdb bare-boto3 pattern). Writes fail closed without `db_write_secret_arn`; a PATCH extension backfills it. Write tools copy the N-③ preview→approve→TOCTOU-recheck→execute shape. Recon (verified file:line): `.superpowers/sdd/` r3 recon in ledger; key facts inline below.

**Deviation from spec D6 (approved by controller):** parameter-group change tool deferred to R-4 (param-group lifecycle = create/attach/reboot orchestration, its own design; reboot/snapshot/class-modify ship now).

## Global Constraints

- NO Co-Authored-By trailer; prettier/pre-commit reformat → `git add -A` re-commit. `str(e)` never in tool/API responses (log-only).
- Approval flow invariants: fail-closed, payload-hash-bound, single-use consume (`verify_approval`); action_type REAL gate = `request_approval.py` allowlist tuple (~L65-108) + `approval_guard._project()` (~L88-254). `operations/handler.py:697` enum is decorative (NOT gateway-facing) — update for hygiene AND fix its existing drift (4 ElastiCache actions missing). `cdk/tool_definitions.py` needs a `_tool()` entry per new tool exposing EVERY handler kwarg (parity test `tests/unit/test_tool_schema_parity.py` enforces).
- engine_family.py CAPABILITIES edits = 4 py copies lockstep via `cp` from canonical mcp-servers (byte-parity test enforces).
- TLS fail-closed (CA missing → RuntimeError; never CERT_NONE). CA pem is bundle-time vendored at `mcp_servers/global-bundle.pem` (agent_stack bundling already curls it — no CDK change needed for it).
- `mcp-servers/requirements.txt` is ONE file shared by all 4 MCP lambdas — adding pymysql ships it everywhere (harmless-unused convention).
- Write tools: pre-check applicability BEFORE offering approval (modify_scaling precedent); resolve billable/impactful values in PREVIEW so the approval hash binds them (add_reader_instance precedent); TOCTOU fresh re-verify after approval (remove_reader_instance precedent).
- Demo instances are standing resources. `cdk/config/settings.py` untouchable. Single `cdk deploy` process.

---

### Task 1: sql_safety MySQL side-effect patterns

**Files:** Modify `mcp-servers/mcp_servers/shared/sql_safety.py` (SIDE_EFFECTING_PATTERNS ~L22-39); Test `tests/unit/mcp_servers/shared/test_sql_safety.py` (find the existing sql_safety test file via grep; extend it).

**Interfaces:** Produces: MySQL statements `KILL …`, `SELECT SLEEP(…)`, `SELECT GET_LOCK/RELEASE_LOCK(…)`, `SELECT LOAD_FILE(…)`, `LOCK TABLES …`, `SELECT BENCHMARK(…)` classify as side-effecting → non-safe → approval path (NOT blocked). PG patterns and existing behavior unchanged.

- [ ] **Step 1 (RED):** extend the existing sql_safety tests:

```python
def test_mysql_side_effecting_patterns():
    for sql in [
        "KILL 1234",
        "SELECT SLEEP(600)",
        "SELECT GET_LOCK('x', 5)",
        "SELECT RELEASE_LOCK('x')",
        "SELECT LOAD_FILE('/etc/passwd')",
        "LOCK TABLES t WRITE",
        "SELECT BENCHMARK(100000000, MD5('x'))",
    ]:
        assert is_side_effecting(strip_sql_literals(sql)), sql

def test_mysql_plain_reads_stay_safe():
    for sql in ["SELECT * FROM t WHERE name = 'sleep(1)'",
                "SHOW ENGINE INNODB STATUS",
                "SELECT killed_count FROM stats"]:
        assert not is_side_effecting(strip_sql_literals(sql)), sql
```

(Adapt call names to the module's actual API — read it first. Note `'sleep(1)'` inside a string literal must NOT match → patterns run on the stripped SQL; `killed_count` must NOT match → use word boundaries.)

- [ ] **Step 2:** add to SIDE_EFFECTING_PATTERNS (mirror existing regex style, one pattern per line with comment `# MySQL`):
      `\bKILL\b`, `\bSLEEP\s*\(`, `\bGET_LOCK\s*\(`, `\bRELEASE_LOCK\s*\(`, `\bLOAD_FILE\s*\(`, `\bLOCK\s+TABLES\b`, `\bBENCHMARK\s*\(`.
- [ ] **Step 3:** GREEN + full suite. **Step 4: Commit** `feat(safety): MySQL side-effecting SQL patterns (R-3)`.

---

### Task 2: `mysql_direct.py` (mcp-servers/shared) + pymysql dep

**Files:** Create `mcp-servers/mcp_servers/shared/mysql_direct.py`; Modify `mcp-servers/requirements.txt` (+`pymysql>=1.1`); Test `tests/unit/mcp_servers/shared/test_mysql_direct.py` (new).

**Interfaces:**

- Produces: `connect(host, port, database, user, password)` — lazy pymysql import, fail-closed TLS against `mcp_servers/global-bundle.pem` (CA path resolution copies `pg_direct.py:19-22`'s two-levels-up pattern), `charset="utf8mb4"`, `connect_timeout=8, read_timeout=30, autocommit=True`, module-level `_CONNECT_FACTORY` hook.
- Produces: `MySQLDataApiAdapter(conn)` with `execute_statement(resourceArn=None, secretArn=None, database=None, sql=None, parameters=None, includeResultMetadata=None, **kw)` → `{"records": [[field-dict…]], "columnMetadata": [{"name": d[0]} for d in cursor.description or []], "numberOfRecordsUpdated": cursor.rowcount for non-SELECT}`. Field mapping IDENTICAL to `data-pipeline/rds_direct_collector/mysql_adapter.py:13-28` (read it; bool before int; Decimal→doubleValue; bytes→utf-8 replace; datetime/date→str). Raises ValueError on `parameters` (callers pass literal SQL only).

- [ ] **Step 1 (RED):** tests — field mapping (same cases as the R-2 adapter test), columnMetadata synthesis from a mock cursor.description, rowcount surfaced as numberOfRecordsUpdated when description is None (write statements), fail-closed connect (CA missing → RuntimeError; monkeypatch the CA path).
- [ ] **Step 2:** implement (docstring: why a second adapter exists — the collector's copy is positional-only/no-metadata by design; this one feeds execute_sql's name-keyed decode).
- [ ] **Step 3:** GREEN + full suite. **Step 4: Commit** `feat(shared): mysql_direct — pymysql connect + Data-API adapter with columnMetadata (R-3)`.

---

### Task 3: execute_sql `sql_via="direct"` branch

**Files:** Modify `mcp-servers/mcp_servers/operations/tools/execute_sql.py`; Test extend `tests/unit/mcp_servers/operations/test_execute_sql.py`.

**Interfaces:**

- Consumes: registry row fields `endpoint`, `port`, `db_secret_arn`, `db_write_secret_arn`, `engine` (must contain "mysql" — SQL Server direct lands R-4); `client_for_cluster(cluster_id, "secretsmanager")` from `shared/cluster_targets.py` (cross-account-aware); Task 2's `connect` + `MySQLDataApiAdapter`; Task 1's classification.
- Produces: same response shapes as the Data API path (`executed`/`approval_required`/`approval_denied`/`blocked`/`execution_failed`/`unsupported_engine`), same `/* source=dbops-agent */` marker, same `_decode_field` decode.

Decision points (recon-verified lines):

1. Replace the `:158` fail-closed gate with a branch: `sql_via == "direct"` AND `"mysql" in engine` → direct path; `sql_via == "direct"` AND not mysql → keep the existing unsupported_engine response (SQL Server direct = R-4 message).
2. Direct path target resolution: `endpoint`/`port` from the row; **read** statements (is_safe) use `db_secret_arn`; **non-safe** (approved) statements use `db_write_secret_arn` — if the needed secret is empty → fail-closed `{"status":"unsupported_engine", "reason":"no (write) credentials configured — register db_write_secret_arn"}` static-message pattern (set_docdb_profiler `:83-89` shape, Korean message fine). Secret fetched via `client_for_cluster(cluster_id, "secretsmanager")` (prewarm precedent — NOT bare boto3).
3. Execute: `connect(...)` → `MySQLDataApiAdapter(conn).execute_statement(sql=f"/* source=dbops-agent */ {sql}")` in try/except/finally-close; feed the response into the SAME existing decode block. Non-SELECT: surface `numberOfRecordsUpdated` (the existing Data API path's behavior for writes — mirror its response key).
4. The HttpEndpoint-disabled hint (`:202-209`) must remain Data-API-branch-only.
5. Approval flow order UNCHANGED — classification/approval checks happen BEFORE target resolution, so direct-path writes get identical approval semantics (verify_approval already binds `{"sql": sql}`).

- [ ] **Step 1 (RED):** tests (registry row fixture with endpoint/port/secrets, `_CONNECT_FACTORY`-style patch via monkeypatching mysql_direct in the execute_sql module namespace):
  - safe SELECT on rds_instance mysql → executed, rows decoded with REAL column names (adapter columnMetadata), rds-data client NOT called.
  - non-safe on rds_instance without approval → approval_required (same as Aurora).
  - approved write WITHOUT db_write_secret_arn → fail-closed static message; connect NEVER called.
  - approved write WITH db_write_secret_arn → executes via write secret (assert which secret ARN was fetched).
  - sqlserver-ex row → unsupported_engine (R-4 message).
  - Aurora relational row → still executes via rds-data (regression guard, existing test).
- [ ] **Step 2:** implement. **Step 3:** GREEN + full suite. **Step 4: Commit** `feat(operations): execute_sql direct-TCP path for rds_instance MySQL (R-3)`.

---

### Task 4: `db_write_secret_arn` backfill via PATCH meta

**Files:** Modify `api/clusters/handler.py` (`_handle_update_meta` ~L948-990); Test extend the clusters meta PATCH tests (grep `update_meta` in tests/unit/api).

**Interfaces:** PATCH `/api/clusters/{id}/meta` (already admin-gated + `attribute_exists` guarded) additionally accepts `db_secret_arn` and `db_write_secret_arn` — each validated `^$|^arn:aws:secretsmanager:` (empty string allowed = clear). When db_secret_arn set via PATCH, also set `db_secret_source="override"`.

- [ ] **Step 1 (RED):** tests — PATCH sets both fields; invalid ARN → 400 static message; existing purpose/service_tags behavior unchanged; admin gate + attribute_exists preserved (mirror existing test style).
- [ ] **Step 2:** implement (extend the allowed-fields handling; keep the ConditionExpression).
- [ ] **Step 3:** GREEN + full suite. **Step 4: Commit** `feat(clusters): PATCH meta accepts db(_write)_secret_arn backfill (R-3)`.

---

### Task 5: 3 write tools (reboot / snapshot / instance-class) + wiring

**Files:**

- Create `mcp-servers/mcp_servers/operations/tools/reboot_rds_instance.py`, `create_rds_snapshot.py`, `modify_rds_instance_class.py`
- Modify: `operations/tools/request_approval.py` (allowlist +3), `shared/approval_guard.py` (`_project` +3 branches), `operations/handler.py` (imports + TOOLS entries + dispatch + `_ENGINE_GATED_TOOLS` +3 with new cap key + request_approval enum hygiene fix incl. the 4 missing ElastiCache actions), canonical `shared/engine_family.py` (`RDS_INSTANCE` + `"instance_write": True`) + cp to 3 copies, `cdk/tool_definitions.py` (+3 `_tool` entries), `cdk/stacks/agent_stack.py` (IAM +`rds:RebootDBInstance`, `rds:CreateDBSnapshot`, `rds:ModifyDBInstance`)
- Tests: new `tests/unit/mcp_servers/operations/test_rds_instance_write_tools.py`; extend engine_family + approval tests as needed; parity test must stay green.

**Interfaces (all three copy the N-③ tool shape — READ add_reader_instance.py + remove_reader_instance.py + modify_scaling.py first):**

- `reboot_rds_instance_impl(cache, cluster_id, approved=False, approval_id="")` — action_type `reboot_rds_instance`. Preview: describe_db_instances via `client_for_cluster(cluster_id, "rds")`; must be `available` else `not_applicable`; payload binds `{"cluster_id"}`. Execute: TOCTOU fresh describe (still available, still not an Aurora member) → `reboot_db_instance`.
- `create_rds_snapshot_impl(cache, cluster_id, snapshot_id="", approved=False, approval_id="")` — action_type `create_rds_snapshot`. Preview resolves `snapshot_id` default `f"dbops-{cluster_id}-{YYYYMMDDHHMM}"` NOW (bind it — add_reader precedent; execute refuses empty). Execute: TOCTOU describe (available) → `create_db_snapshot`.
- `modify_rds_instance_class_impl(cache, cluster_id, target_class, approved=False, approval_id="")` — action_type `modify_rds_instance_class`. Preview: describe → `not_applicable` if target==current or instance not available; payload binds `{"cluster_id","target_class","current_class"}`. Execute: TOCTOU (current_class unchanged since approval else refuse) → `modify_db_instance(DBInstanceClass=target_class, ApplyImmediately=True)`.
- All: family gate via `_ENGINE_GATED_TOOLS` cap key `instance_write` (fail-closed None-family deny); responses static-message only; Korean user-facing text, English jargon.

- [ ] **Step 1 (RED):** tests per tool: preview binds resolved values; not_applicable pre-checks skip approval; approved+verify_approval mock ok → executes; TOCTOU mismatch → refuses without calling the mutating API; family gating test (dynamodb row → unsupported_engine through handler gate); allowlist/\_project round-trip (canonical_action_hash projects the bound payload for each new action_type).
- [ ] **Step 2:** implement tools + wiring + IAM + capability (+enum hygiene fix). **Step 3:** GREEN + full suite + `cdk synth dbops-dev-agent` OK. **Step 4: Commit** `feat(operations): approval-gated RDS instance write tools — reboot/snapshot/class (R-3)`.

---

### Task 6: frontend approval-card + agent prompts

**Files:** Modify `frontend/src/components/approval/approval-card.tsx` (ACTION_RISK/ACTION_GUIDE maps — read the existing prewarm/add_reader entries and mirror; reboot+class=high, snapshot=low) ; `agent/prompts/system_prompt.py` + `agent/prompts/cheatsheet.py` (new rds_instance section: SQL 실행은 MySQL만(직접 연결)·SQL Server는 R-4, write 3종은 승인 필수, Aurora 전용 툴 호출 금지 목록 재확인; mirror the DocDB/DynamoDB section style).

- [ ] **Step 1:** approval-card entries (Korean guides, risk levels). **Step 2:** prompt sections. **Step 3:** `npx tsc --noEmit && npm run build` + `python3 -m ast agent/prompts/system_prompt.py`-style syntax check (NO py_compile in agent/ — **pycache** ban; use `python3 -c "import ast; ast.parse(open('agent/prompts/system_prompt.py').read())"`). **Step 4: Commit** `feat(ui)+feat(agent): rds_instance write approval cards + prompts (R-3)`.

---

### Task 7: Deploy + live E2E (orchestrator)

- [ ] Full unit suite; frontend build; single `cdk deploy dbops-dev-data dbops-dev-agent dbops-dev-frontend`.
- [ ] PATCH demo row: set `db_write_secret_arn` = the instance's master secret ARN (write-capable for demo) via the new PATCH endpoint (admin token from browser).
- [ ] Chat live: ① `SELECT 1` on dbops-demo-mysql → **executed with rows** (직접 연결 경로). ② `CREATE TABLE demo_orders (...); INSERT ...` 요청 → approval_required → **사용자 UI 승인 요청(유일한 정지점)** → 승인 후 executed → 이후 수집 tick에서 table_stats 채워짐(= I1 수정의 n_dead_tup/seq_scan 실값 확인 보너스). ③ `create_rds_snapshot` 요청 → 승인 요청 등록(사용자 같은 방문에서 승인 가능).
- [ ] AgentCore env 무변경이므로 웜컨테이너 지연 무관(MCP Lambda 코드는 즉시 반영).
- [ ] Ledger + memory + report.

## Execution notes (orchestrator)

- Routing: T1·T2 Sonnet(정밀 사양·소형), T3·T5 Opus(핵심 통합), T4 Sonnet, T6 Sonnet, T7 orchestrator. 순차.
- 승인 UI 정지점은 T7 한 곳으로 몰아 사용자 방문 1회로 처리.
