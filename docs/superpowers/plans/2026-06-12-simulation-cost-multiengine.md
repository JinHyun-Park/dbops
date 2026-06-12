# Simulation + Cost multi-engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) tracking.

**Goal:** Make the Simulator behave correctly for DocumentDB/DynamoDB — gate the Aurora-only Simulator (page + 6 MCP tools) off cleanly for NoSQL. (DocDB cost rightsizing — original Part 2 — is DEFERRED; see spec: DocDB lacks instance_class and uses a different CPU metric_type, so it needs collector plumbing first.)

**Architecture:** Capability gating (mirror #1's diagnosis gating + execute_sql's engine guard) at four layers — capability dict, simulation MCP handler, Cedar policy, frontend Simulator page + agent prompt.

**Tech Stack:** Python (MCP Lambda, ETL collector, CDK Cedar), Next.js/TS frontend, pytest. No shared Lambda layer → `engine_family.py` duplicated in 4 packages (keep byte-identical).

---

### Task 1: `simulation` capability key in all 4 engine_family.py copies

**Files (all 4, byte-identical):**

- Modify: `data-pipeline/etl_collector/collectors/engine_family.py`
- Modify: `api/clusters/engine_family.py`
- Modify: `api/dashboard/engine_family.py`
- Modify: `mcp-servers/mcp_servers/shared/engine_family.py`
- Test: `tests/unit/.../test_engine_family*.py` (whichever asserts CAPABILITIES) + `tests/unit/test_engine_family_sync.py` (md5 parity, if it exists)

- [ ] **Step 1:** Add `"simulation": True,` to the `RELATIONAL` capability block and `"simulation": False,` to both `DOCUMENTDB` and `DYNAMODB` blocks, in ALL FOUR copies identically.
- [ ] **Step 2:** Verify byte-identical: `md5 data-pipeline/etl_collector/collectors/engine_family.py api/clusters/engine_family.py api/dashboard/engine_family.py mcp-servers/mcp_servers/shared/engine_family.py` → all 4 hashes equal.
- [ ] **Step 3:** Run any engine_family unit test + the sync test. Expected: PASS.
- [ ] **Step 4:** Commit.

### Task 2: Simulation MCP handler engine guard

**Files:**

- Modify: `mcp-servers/mcp_servers/simulation/handler.py`
- Test: `tests/unit/mcp_servers/simulation/test_engine_guard.py` (create)

- [ ] **Step 1: Write failing tests.** A guard that: (a) for a documentdb/dynamodb cluster_id (cache returns `[{"engine":"docdb"}]` / `[{"engine":"dynamodb"}]`) → handler returns content whose JSON has `status == "unsupported_engine"` and the tool impl is NOT called; (b) for relational (`[{"engine":"aurora-postgresql"}]`) → impl IS called; (c) when cache returns a non-list/MagicMock or `[]` (unknown) → impl IS called (default-permit, mirroring execute_sql `9520191`). Drive via `lambda_handler` with a fake `context` exposing `client_context.custom.bedrockAgentCoreToolName`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** In `handler.py`: `from mcp_servers.shared.engine_family import engine_family, CAPABILITIES`. Add a helper resolving family from `cache.execute("SELECT engine FROM cluster_meta WHERE cluster_id = :cid", {"cid": cluster_id})` guarded by `isinstance(rows, list) and rows and isinstance(rows[0], dict)`; wrap in try/except → None. In `lambda_handler`, after `if tool_name and tool_name in TOOLS:` and before calling impl: resolve `cluster_id = (event or {}).get("cluster_id")`, resolve `fam`; `if isinstance(fam, str) and not CAPABILITIES.get(fam, {}).get("simulation", True):` return `{"content":[{"type":"text","text":json.dumps({"status":"unsupported_engine","engine_family":fam,"message":"시뮬레이션(업그레이드/파라미터/DDL/스케일링)은 Aurora 전용입니다. NoSQL 엔진은 get_maintenance_findings로 진단하고 용량/비용 변경은 AWS Console/CDK로 적용하세요."})}]}`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 3: `simulation_policy.cedar` + Cedar parity test extension

**Files:**

- Create: `cdk/policies/cedar/simulation_policy.cedar`
- Modify: `cdk/policies/README.md` (list the new policy + its deploy line)
- Modify: `tests/unit/test_tool_schema_parity.py` (add `"simulation": "simulation_policy.cedar"` to `_READONLY_POLICY`)

- [ ] **Step 1:** Create `simulation_policy.cedar` — header comment "Simulation MCP Server: READ-ONLY (what-if estimates, no mutation)" + a single `permit(principal, action in [ Action::"check_upgrade_compatibility", Action::"estimate_upgrade_impact", Action::"generate_upgrade_plan", Action::"simulate_parameter_change", Action::"simulate_scaling", Action::"simulate_ddl_impact" ], resource);`
- [ ] **Step 2:** Add `"simulation": "simulation_policy.cedar"` to `_READONLY_POLICY` in the parity test.
- [ ] **Step 3:** Add the `agentcore policy create --name dbops-simulation --file cedar/simulation_policy.cedar` line to README + a bullet under "Policy Files".
- [ ] **Step 4:** Run `pytest tests/unit/test_tool_schema_parity.py -q` → PASS (every simulation `*_impl` tool now in the allowlist).
- [ ] **Step 5:** Commit.

### Task 4: Run cost_check for the documentdb ETL branch — **DEFERRED**

> Deferred during planning: the Aurora cost collector reads `metric_type='cpu'` but DocDB stores
> `cpu_utilization`, and DocDB has no `instance_class` in cluster_meta/resource_details. A naive
> wire-up emits nothing (dead feature). Follow-up: extend the docdb collector to capture the writer
> instance class, then add a `docdb_cost_oversized` rule inside `docdb_findings.py`. Original steps
> retained below for the follow-up.

_(deferred — see note above)_

**Files:**

- Modify: `data-pipeline/etl_collector/handler.py` (documentdb branch of `_collect_one`)
- Test: `tests/unit/data_pipeline/test_cost_check.py` (add documentdb cases) or create `test_cost_check_docdb.py`

- [ ] **Step 1: Write failing test.** With a mocked `rds_data` whose `cluster_meta` row is `{"instance_class":"db.r6g.large","engine_mode":None,"serverlessv2_min_acu":None,"serverlessv2_max_acu":None}` and `metric_snapshots` CPU rows giving avg 12% / p95 20% / max 25% / 200 samples → `collect_cost_findings` emits a `cost_oversized` finding (assert the INSERT with check_type `cost_oversized` is issued). Second case: `instance_class="db.t3.medium"` (burstable) → NO `cost_oversized` (assert no such INSERT) — documents the burstable skip so DocDB on t3 is correctly silent.
- [ ] **Step 2:** Run → FAIL (cost_check not yet wired for documentdb / test new).
- [ ] **Step 3: Implement.** In `_collect_one`'s `documentdb` branch (after the existing docdb metric + findings collection, before the early `return result`), add: `result["cost"] = collect_cost_findings(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=run_ts)` using the SAME `run_ts` the docdb findings use. Import is already present at module top. Wrap in try/except mirroring the docdb findings block (set `result["cost_error"]` on failure, don't abort the cycle).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

### Task 5: Frontend Simulator gating

**Files:**

- Modify: `frontend/src/app/simulator/page.tsx`
- Reference: `frontend/src/lib/engine.ts` (`engineFamily`)

(DocDB cost labels dropped — Part 2 deferred, no new DocDB finding is emitted.)

- [ ] **Step 1:** In `simulator/page.tsx`, compute `const fam = engineFamily(current?.engine)`. When a cluster is selected but `fam !== "relational"` (i.e. documentdb/dynamodb), render an `EmptyState` titled "시뮬레이션은 Aurora 전용" with description "업그레이드·파라미터·DDL·스케일링 시뮬레이션은 Aurora PostgreSQL/MySQL 클러스터에만 적용됩니다. DynamoDB 용량/비용 권장은 대시보드의 Maintenance Health 패널과 Chat 진단을 참고하세요." Keep the existing four-panel block for relational.
- [ ] **Step 2:** `cd frontend && npx tsc --noEmit && npx eslint src/app/simulator/page.tsx` → clean.
- [ ] **Step 3:** Commit (two-step if prettier reformats: `git add -A` then re-commit).

### Task 6: Agent prompt — simulation is Aurora-only

**Files:**

- Modify: `agent/prompts/system_prompt.py`
- Modify: `agent/prompts/cheatsheet.py`

- [ ] **Step 1:** Add one line to the engine-family section: simulation tools (check_upgrade_compatibility/estimate_upgrade_impact/generate_upgrade_plan/simulate_parameter_change/simulate_scaling/simulate_ddl_impact) are **Aurora-only**; for DynamoDB capacity/cost or DocumentDB scaling, do NOT call them — use `get_maintenance_findings` and recommend the AWS Console/CDK path.
- [ ] **Step 2:** Validate with `python -c "import ast; ast.parse(open('agent/prompts/system_prompt.py').read()); ast.parse(open('agent/prompts/cheatsheet.py').read())"` — **ast.parse ONLY, never run/import the module** (a `__pycache__` in `agent/` makes the Runtime deploy reject the image). Confirm no `agent/**/__pycache__` exists before deploy.
- [ ] **Step 3:** Commit.

### Final: full suite, deploy, live-verify, Codex checkpoint

- [ ] Run FULL backend suite `pytest tests/unit -q` → all green (do NOT trust per-task subsets).
- [ ] `cd frontend && npm run build` clean.
- [ ] Deploy `dbops-dev-agent` (sim guard + Gateway + prompt) and `dbops-dev-frontend` (simulator gating). No data-stack deploy (Part 2 deferred). Remember `aws s3 sync out/ --delete` must EXCLUDE the CDK-injected `/config.json`.
- [ ] Live-verify (authenticated browser session): (1) Simulator with DynamoDB scenario table selected → Aurora-only EmptyState, not broken panels; same for `dbops-docdb-test`. (2) Simulator with a relational cluster → all 4 panels still work. (3) Chat: ask to "simulate an upgrade" on the DynamoDB table → agent declines, points to findings/console.
- [ ] Codex adversarial final checkpoint (tight verdict-only prompt). Reconcile any FIX-FIRST.
- [ ] Merge/push per the user's flow.
