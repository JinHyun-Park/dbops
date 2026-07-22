# RDS Instance Engines — RDS for MySQL + RDS for SQL Server Support

**Date**: 2026-07-22
**Status**: Approved (design), implementation pending
**Program**: 5-spec vertical (R-1 … R-5), mirroring the DocumentDB/DynamoDB and ElastiCache programs

## Context

DBOps today supports Aurora MySQL/PostgreSQL (RELATIONAL family), DocumentDB, DynamoDB, and ElastiCache. Users are asking for RDS for SQL Server and RDS for MySQL (non-Aurora). These are **DB-instance-based** engines: no DB cluster, no reader endpoint, no RDS Data API. That last point is the defining constraint — every existing SQL execution and SQL-based collection path in the platform goes through RDS Data API (`resourceArn`/cluster ARN), which is Aurora-exclusive.

Full parity is the goal (registration → collection → dashboard → findings/RCA → approval-gated writes → simulation/cost), sequenced **MySQL first, then SQL Server**, because RDS MySQL reuses substantial Aurora MySQL assets (metric mapping, INNODB status parser, cheatsheet) while SQL Server is greenfield (T-SQL, DMVs, new driver).

## Program Decomposition

| Spec                       | Scope                                                                                                                                                                                                                                                                          | Notes                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------- |
| **R-1 Foundation**         | `rds_instance` family across all 5 classifier copies + capabilities; registration/discovery via `describe_db_instances`; CW metric collection (instance dimensions); cluster_meta; frontend grouping/badges/registration form/basic dashboard tabs; demo instances provisioned | Covers BOTH engines' control plane                  |
| **R-2 MySQL deep read**    | `mysql_direct.py` (pymysql); new in-VPC `rds_direct_collector` Lambda (MySQL path): performance_schema query stats, processlist activity, data_locks, `SHOW ENGINE INNODB STATUS`; dashboard query/session/lock/InnoDB panels; findings                                        | INNODB parser already handles RDS-MySQL-only fields |
| **R-3 MySQL SQL + writes** | `execute_sql` `sql_via='direct'` branch; write actions (reboot, instance class modify, snapshot, parameter group); agent prompts/cheatsheet                                                                                                                                    | 3-place action_type sync + tool_definitions parity  |
| **R-4 SQL Server full**    | `mssql_direct.py` (python-tds); DMV collectors (dm_exec_query_stats, dm_os_wait_stats, blocking via dm_exec_requests); dashboard panels; T-SQL execute path; write actions; cheatsheet                                                                                         | Reuses R-1/R-2/R-3 infrastructure                   |
| **R-5 Simulation/cost**    | Instance right-sizing (CW-driven), storage/IOPS cost, SQL Server license-aware cost (Express/Web/Standard/EE) for both engines                                                                                                                                                 | Positive-gate pattern (ElastiCache precedent)       |

Each spec ships with adversarial review, dev deploy, and live verification before the next begins.

## Architecture Decisions

### D1. One new engine family: `rds_instance`

A single new family for both engines, with dialect sub-dispatch on the `engine` string — exactly how RELATIONAL already holds `aurora-postgresql` and `aurora-mysql`. Early-return branches at every family dispatch point, same as DocumentDB/DynamoDB/ElastiCache.

Rejected alternatives:

- _RELATIONAL + `is_aurora` flag_: inherits frontend panels cheaply but requires guarding 13 `fam === "relational"` dashboard blocks (frontend/src/app/dashboard/page.tsx) and ~40 RDS Data API call sites; one missed guard = runtime boto3 error.
- _Two families (`rds_mysql`/`rds_sqlserver`)_: dialect differences are engine-level, not family-level; duplicates capability rows and frontend groups.

Classifier rules (order matters — must run BEFORE the RELATIONAL fallback):

- `'sqlserver' in engine` → `rds_instance` (covers `sqlserver-ee/se/ex/web`)
- `'mysql' in engine and 'aurora' not in engine` → `rds_instance` (RDS MySQL reports engine=`mysql`; Aurora MySQL reports `aurora-mysql`)

**Must be applied to all 5 copies in lockstep**: `api/clusters/engine_family.py`, `api/dashboard/engine_family.py`, `data-pipeline/etl_collector/collectors/engine_family.py`, `mcp-servers/mcp_servers/shared/engine_family.py`, and the TS mirror `frontend/src/lib/engine.ts`. A prior sync miss required a fix commit (0baead5); the existing parity unit test must be extended to cover the new family.

### D2. New capability key `sql_via: 'data_api' | 'direct'`

`CAPABILITIES['sql']` is a boolean today; `rds_instance` needs "SQL-capable, but NOT via Data API". New key `sql_via` on every family (`'data_api'` for RELATIONAL, `'direct'` for `rds_instance`, absent/ignored for non-SQL families). `execute_sql` branches on it internally (see D5) so the agent-facing tool surface stays uniform.

`rds_instance` capability row: `sql=True`, `sql_via='direct'`, `rds_meta=True`, `perf_insights=True`, `simulation=False` (Aurora simulators stay blocked; R-5 adds its own positive-gated tools), `custom_endpoint=False`, `prewarm=False`, `scale_instance=False` (cluster/reader-topology concepts), `cw_namespace='AWS/RDS'` (shared with Aurora but **instance**-dimensioned), `findings` set defined in R-1.

### D3. Identity and registry shape

- `cluster_id` = DB instance identifier verbatim (RDS instance identifier charset is compatible with the existing `^[a-zA-Z0-9-]{1,63}$` validator — no DynamoDB-style slug needed; verify in R-1).
- `resource_type` = `f"rds-{engine}"` (e.g. `rds-mysql`, `rds-sqlserver-ex`), following the `<family>-<engine>` convention.
- Registry secret fields, mirroring the DocDB split (`mongo_secret_arn` / `mongo_write_secret_arn`):
  - `db_secret_arn` — read-only monitoring account (collector + read tools)
  - `db_write_secret_arn` — write-capable account (approval-gated `execute_sql` writes)
- `cluster_meta`: narrow upsert (docdb_cw_collector.py:72 precedent) populating the typed columns that genuinely overlap (`engine`, `engine_version`, `instance_class`, `status`, storage) plus `resource_details` JSONB for instance-shaped extras (MultiAZ, PI enablement, storage type/IOPS, license model). `engine` values fit VARCHAR(20).

### D4. Two-track collection

**Track 1 — CloudWatch (inside `etl_collector`)**: new early-return family branch in `_collect_one` (data-pipeline/etl_collector/handler.py:75-174) calling a new `rds_instance_cw_collector.py`. Namespace `AWS/RDS` with `DBInstanceIdentifier` dimension (the existing `CW_INSTANCE_METRICS` path in cw_collector.py:23-84 is the template; the `DBClusterIdentifier` path is inapplicable — that dimension does not exist for non-Aurora instances). Metrics land as the **standard metric_type names** (`cpu`, `db_connections`, `freeable_memory`, `read_latency`, `write_latency`, `read_iops`, `write_iops`, `free_storage`) so the engine-agnostic downstream — proactive_monitor, alert_evaluator, cluster-triage, capacity forecast — works unmodified. Performance Insights lookup uses `DBInstanceIdentifier` directly instead of the `db-cluster-id` filter (handler.py:192-200 must branch).

**Track 2 — deep internals (new dedicated in-VPC Lambda `rds_direct_collector`)**: follows the docdb_mongo_collector architecture exactly — its own Lambda, own SG, own schedule, own scan over the registry filtered by `engine_family == 'rds_instance'`, connecting directly over the wire protocol using `db_secret_arn`. NOT a branch inside etl_collector (which is deliberately non-VPC).

- MySQL (pymysql): performance_schema `events_statements_summary_by_digest` → query_stats rows; `information_schema.processlist` → activity; `performance_schema.data_locks` → blocking; `SHOW ENGINE INNODB STATUS` → reuse the existing parser (mysql_innodb_status.py already parses RDS-MySQL-only fields — redo log LSN/checkpoint — that Aurora lacks; parsing needs zero changes, only connectivity).
- SQL Server (python-tds): `sys.dm_exec_query_stats` + `sys.dm_exec_sql_text` → query_stats; `sys.dm_exec_requests`/`sys.dm_exec_sessions` → activity + blocking chains; `sys.dm_os_wait_stats` → wait profile metric_snapshots.
- Output lands in the **existing cache table shapes** (query_stats, metric_snapshots with new engine-scoped metric_type prefixes where needed) so dashboard panels and MCP tools reuse maximally. No new tables; metric_snapshots has no engine column by design (join via cluster_meta).

### D5. Connectivity modules

- `mcp-servers/mcp_servers/shared/mysql_direct.py` (pymysql) and `mssql_direct.py` (python-tds), cloning the `pg_direct.py` module shape: lazy driver import, `connect()`/`query()` pair, fail-closed TLS against the vendored `global-bundle.pem` (RDS CA covers both engines).
- **TLS verification logic must be re-derived per driver** — pg_direct's `ssl.create_default_context(cafile=...)` is pg8000-specific; pymysql takes `ssl_ca`/`ssl_verify_cert`, pytds takes `cafile`+`validate_host`. Each module raises (fail-closed) if the CA bundle is missing; a silent CERT_NONE fallback carrying DB credentials is a regression.
- Both drivers are pure Python (no C extensions) — one line each in `mcp-servers/requirements.txt`; the shared asset bundling (cdk/bundling.py `_PipLocalBundling` + the Docker path) needs no change. The rds_direct_collector Lambda gets its own per-function `requirements.txt` (data-pipeline convention).
- The operations Lambda already runs in-VPC with `ops_mcp_sg` (allow_all_outbound) — only the SG description comment needs 3306/1433 added. The new collector Lambda mirrors the docdb_mongo_lambda VPC/SG/schedule wiring in data_stack.
- **Cross-account direct TCP is explicitly OUT OF SCOPE for v1.** Hub-spoke role chaining covers AWS API calls only; a raw TCP socket needs actual network routing (peering/TGW) to the spoke VPC. This is the same pre-existing gap pg_direct has for spoke-account Aurora readers. v1 targets same-account, hub-VPC-reachable instances; the registration path records (does not resolve) the gap for spoke instances.

### D6. SQL execution & write operations

- `execute_sql` (operations/tools/execute_sql.py) branches on `CAPABILITIES[fam]['sql_via']`: `'data_api'` → existing rds-data call (lines 164-172, unchanged); `'direct'` → resolve endpoint via `client_for_cluster(cluster_id,'rds').describe_db_instances`, creds via `db_secret_arn`/`db_write_secret_arn`, execute through mysql_direct/mssql_direct. Agent-facing tool name, parameters, and approval flow are identical across engines.
- `sql_safety.py` classification applies as-is to both dialects. T-SQL notes (verified): DROP/TRUNCATE/DELETE matching is dialect-neutral; bracket identifiers (`[Drop]`) are not stripped by `strip_sql_literals` — false-positive direction only (over-asks for approval, safe); `EXEC`/`EXECUTE` is neither SAFE nor explicit-write → falls into the generic "non-SELECT requires approval" path (safe). The MySQL `/*!...*/` executable-comment preservation is inert for T-SQL. R-4 adds bracket-identifier stripping only if live testing shows material over-approval friction (YAGNI otherwise).
- New write action_types (R-3/R-4): `reboot_rds_instance`, `modify_rds_instance_class`, `create_rds_snapshot`, `modify_rds_instance_params`. Each addition follows the known **3-place sync**: request_approval.py allowlist tuple (~L65-108) + approval_guard.py `_project()` (~L88-247) + operations/handler.py request_approval input_schema enum (~L697) — plus `cdk/tool_definitions.py` gateway-schema parity for any new tool (both prior P0s). New tools register in `_ENGINE_GATED_TOOLS` (fail-closed). IAM: `rds:RebootDBInstance`, `rds:ModifyDBInstance`, `rds:CreateDBSnapshot` are net-new grants on the operations Lambda (agent_stack).
- Approve-time auto-execute (`origin="ui"` stamping) is NOT extended to these actions in this program.

### D7. Registration & discovery

- `_handle_register` (api/clusters/handler.py:590) gets a 4th family branch `_register_rds_instance` (copy the `_register_docdb` pattern at :489): validate → `describe_db_instances(DBInstanceIdentifier=...)` → **reject if the instance has `DBClusterIdentifier` set** (it's an Aurora/DocDB cluster member, registered via its cluster) → build item with explicit `engine`/`engine_family`/`resource_type` → put_item. This also fixes the current silent failure mode where such engines fall into the Aurora path, get `connection_status='failed'` + 207, and produce a useless row.
- Discovery (:308-448): the Aurora loop keeps its `startswith('aurora')` guard; a new best-effort block enumerates `describe_db_instances` (paginated — MagicMock-paginate test gotcha applies) and surfaces instances whose engine is `mysql` or `sqlserver-*` with no `DBClusterIdentifier`.
- Frontend registration form (frontend/src/app/clusters/page.tsx:854-857): add options (`mysql (RDS)`, `sqlserver-ex/se/ee/web`) + a `handleRegister` branch; the same-account/cross-account toggle conditions (:868-869, :925-926) get the new engine literals OR'd in, cross-account disabled for v1 (D5).

### D8. Frontend

- `engine.ts`: EngineKind + `'sqlserver'`; **fix engineKind ordering** so `aurora-mysql` is checked before the bare `mysql` match (today RDS MySQL would silently classify as Aurora MySQL); EngineGroup + `'rds-mysql'`, `'rds-sqlserver'` (display tier is per-engine, like aurora-postgresql vs aurora-mysql); EngineFamily + `'rds_instance'`. The exhaustive `Record` literals in group-by-family.ts and consumers make TS enforce completeness.
- Dashboard: `TABS_BY_FAMILY['rds_instance']` lists only tabs whose data actually flows (overview, queries, sessions/locks, storage/config as delivered per spec). New `rds-instance-overview-panel.tsx` (own resource_details interface — mind the 3-tier parity pitfall: collector JSON ↔ untyped API passthrough ↔ hand-written TS interface; verify against real producer output, not invented fixtures). Query/session panels reuse existing components where cache shapes match, gated per-panel.
- `FAMILY_PANELS` in engine.ts is dead code (verified) — do not wire anything through it; gating stays in dashboard/page.tsx.
- Badges: `sqlserver` pill; EOL rows for RDS MySQL 8.0/8.4 and SQL Server 2019/2022 (nice-to-have; eolFor returns null harmlessly otherwise).
- Korean UX convention applies: DBA jargon stays English, explanatory text Korean.

### D9. Agent prompts

system_prompt.py engine-dispatch section + cheatsheet.py gain an `rds_instance` section: which tools work, dialect-specific diagnostic guidance (performance_schema vs DMVs), explicit "Aurora-only simulators/endpoint/prewarm tools are unsupported_engine here". SQL comment tag `/* source=dbops-agent */` applies unchanged (valid in both dialects).

### D10. Demo instances (live verification + standing samples)

Created by me during R-1 (one-off test resources → AWS CLI direct is allowed by project convention; identifying tags; but these are **standing demo resources**, so tagged as such and NOT torn down):

- `dbops-demo-mysql` — RDS MySQL 8.x, db.t4g.micro, 20 GiB gp3, data VPC, PI enabled
- `dbops-demo-mssql` — SQL Server Express 2022, db.t3.small, 20 GiB gp3, data VPC, PI enabled
- Instance SGs allow ingress 3306/1433 from ops_mcp_sg and the rds_direct_collector SG. Secrets Manager entries for monitoring + write accounts seeded, ARNs recorded on the registry rows. Small synthetic workload seeded so dashboards/queries have data. (~$50/month combined, user-approved.)

## Error Handling

- Registration: hard-fail (400) on Aurora-member instances and on describe failures — no more silent 207 rows for this family.
- Collectors: per-cluster try/except with `connect_failed`/`collect_failed` statuses recorded (docdb collector precedent); one bad instance never blocks the fleet sweep.
- Direct SQL: connection errors surface as tool errors with static reasons (no `str(e)` leakage in API responses — known rule); TLS misconfiguration fails closed.
- execute_sql direct path enforces the same approval_guard consume-before-execute semantics as the Data API path.

## Testing

- **Unit**: classifier parity test extended to `rds_instance` across all 5 copies; registration branch (describe_db_instances mocked with `isinstance`-guarded pagination markers); CW collector dimension assertions; execute_sql `sql_via` dispatch; sql_safety T-SQL cases (bracket identifiers, EXEC); approval 3-place sync test extended.
- **Contract/parity**: handler TOOLS ↔ tool_definitions.py diff test covers new tools.
- **Live (per spec)**: R-1 register both demo instances → CW metrics visible on dashboard within one collection interval; R-2 MySQL query stats/sessions/locks panels show seeded workload; R-3 approval-gated write executed end-to-end via UI approval (no self-approval — user approves); R-4 same for SQL Server; R-5 right-sizing recommendation sanity-checked against CW data.
- **E2E**: registration-to-findings scenario appended to the operational scenario suite.

## Risks & Constraints

1. **Data API absence is pervasive** — every reused "relational" code path must be audited for rds-data assumptions before being enabled for this family; the family early-return pattern (D1) is the containment strategy.
2. **5-copy classifier sync** and **3-place action_type sync** are the two known recurring failure modes; both have tests and both are called out per-spec.
3. **Cross-account direct TCP** deliberately deferred; registration UX must not imply spoke-account deep collection works.
4. **python-tds capability check** early in R-4: confirm TLS + pure-python wheel behavior in Lambda before building on it (fallback candidate: pymssql is C-based — NOT acceptable; if pytds fails, R-4 pauses for re-design rather than shipping a CERT_NONE path).
5. SQL Server Express limits (10 GB/db, no SQL Agent) are fine for demo but mean some DMV-adjacent features (e.g. Query Store defaults) may differ from Standard/EE — collectors must degrade gracefully (`not_applicable`, honest empty states).

## Model Routing (implementation)

Design/orchestration: Fable (main session). Heavy implementation (connectivity modules, collectors, execute_sql branch, write gates): Opus 4.8 subagents. Light implementation (frontend mappings, badges, cheatsheet, unit boilerplate): Sonnet 5 subagents. Adversarial review workflows (ultracode) gate every spec's merge.
