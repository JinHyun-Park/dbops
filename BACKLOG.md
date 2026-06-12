# DBOps Backlog

Priorities are rough — `P1` is "first-impression breakers", `P4` is polish.
Items are scoped concretely so any of them can be picked up without re-design.

---

## P1 — First-impression breakers

### P1.1 Self-hosted login UX

**Why:** Cognito Hosted UI is the default AWS-branded ugly login form. First
impression decides whether someone sticks with the product.
**What:**

- Replace `getLoginUrl()` redirect with an in-app login page (`/login`).
- Use `amazon-cognito-identity-js` (or `@aws-sdk/client-cognito-identity-provider`)
  to call `InitiateAuth` (USER_SRP_AUTH) directly from the browser.
- Cognito User Pool Client must enable `ALLOW_USER_SRP_AUTH` + `ALLOW_REFRESH_TOKEN_AUTH`.
- Keep Hosted UI as a fallback for social SSO later.
  **Out of scope:** SAML/OIDC federation.

### P1.2 Forgot password flow

**Why:** Users locked out can't self-recover today.
**What:**

- `/login` → "Forgot password?" link → `/forgot` page.
- Call `ForgotPassword` API → email/SMS code → `/reset` page.
- Call `ConfirmForgotPassword` with code + new password.
- Cognito User Pool already has email verification configured.

### P1.3 Onboarding tour

**Why:** A first-time visitor lands on an empty dashboard and bounces.
**What:**

- One-time modal on first login (`localStorage` flag) walks through:
  1. Register your first cluster
  2. Wait for ETL (5 min) or click "Generate sample data"
  3. Open chat and try "analyze recent slow queries"
- Skippable via "Already familiar" link.
- Use `<EmptyState>` primitive on every page when `clusters.length === 0`.

### P1.4 Sample data / demo mode

**Why:** People want to evaluate the product before connecting their cluster.
**What:**

- "Generate sample cluster" button on `/clusters` that:
  1. Creates a fake `cluster_id = "sample-cluster"` row in DynamoDB
  2. Calls a SampleDataSeeder Lambda that populates 24h of synthetic
     metric_snapshots / query_stats / blocking_locks
  3. Marks the cluster `is_demo: true` so it can be deleted in one click
- Demo cluster shows a yellow "DEMO" badge across all pages.

---

## P2 — Core differentiation

### P2.1 Query plan visualizer ✅ (1·2·3단계 완료)

**Why:** pganalyze's strongest feature.
**What (DONE):**

- POST `/api/explain` → EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) → 구조화 JSON
- `<PlanTree>` 재귀 컴포넌트, self-time 기반 색상, hot nodes, planner misestimate 배지
- "Get AI insight" 버튼 → plan summary를 LLM에 보내 2–3개 구체적 권장사항
- SQL 에러 = 노란 warning (HTTP 400), 인프라 에러 = 빨간 error (502)
  **Remaining (5단계, optional polish):**
- localStorage plan history (최근 10개), 다시 열기
- "copy plan as JSON" / "share link" 액션

### P2.2 Mobile responsive ✅ (mobile tab bar)

**Why:** DBAs on-call use phones.
**What (DONE):**

- 사이드바가 < 768px에서 숨겨지고 하단 탭바(`md:hidden fixed bottom-0`)가 노출
- 5개 핵심 라우트만 (Fleet / Dashboard / Chat / Alerts / Clusters)
- main 영역 하단 패딩 추가로 콘텐츠 가림 방지
  **Remaining (refinement):**
- 테이블 → 카드뷰 변환 (현재는 가로 스크롤로 fallback)
- 대시보드 그리드 1열로 collapse + 메트릭 스트립 가로 스크롤

### P2.3 Slack / PagerDuty alert templates ✅

**What (DONE):**

- 새 RDS 테이블 `alert_subscribers_managed` (schema_v5)
- `/api/alert-subscriptions` POST/GET/DELETE이 `slack-webhook` / `pagerduty-events-v2` 프로토콜 지원
- Alert evaluator가 Slack Block Kit (header + fields + context block) 페이로드 빌드
- PagerDuty Events API v2 payload (`dedup_key=dbops-rule-<id>`로 반복 트리거 그룹화)
- 호출 결과를 `last_used_at` / `last_error`에 기록
  **Remaining:**
- 알림 트리거 시 dashboard URL 자동 첨부 (Slack action link)
- 토픽 dedup 윈도우 조정 (현재는 영구 dedup)

### P2.4 RBAC (admin / viewer) ✅ (frontend gate)

**What (DONE):**

- Cognito 그룹 `dbops-admin`, `dbops-viewer` 생성 (foundation_stack)
- `auth.ts`에 `isAdmin()` / `isViewer()` (id token `cognito:groups` 디코드)
- Default model: 모든 사용자는 admin (`dbops-viewer` 명시 가입 시에만 viewer로 강등)
- `/clusters`, `/alerts` 페이지에서 mutation 버튼/폼이 viewer 사용자에겐 숨김
- "read-only · viewer" 뱃지로 모드 표시
  **Remaining (P2.4.2):**
- API Gateway JWT authorizer 또는 per-Lambda 검증으로 서버 측 RBAC 강제
  (현재는 DevTools에서 직접 API 호출 시 mutation 가능 — frontend gate만 있음)

### P2.5 Bulk cluster discovery + register

**Why:** Manually entering cluster_id / account_id / region / spoke role per
cluster is fine for 1-2 clusters; a fleet of 50+ makes onboarding painful.
**What:**

- `/clusters` page gets a "Discover" panel:
  - Same-account: enumerate via `rds:DescribeDBClusters` in selected region(s)
  - Cross-account: input role ARN → assume → enumerate
- Show a table of discovered clusters with checkboxes (cluster_id, engine,
  endpoint, status, already-registered flag)
- "Register selected" button → confirmation modal that warns:
  - "DBOps will run read-only inspection queries against the data plane"
  - "Bedrock costs accrue for any chat or AI insight you trigger"
  - "Cancel anytime by deleting the cluster row"
- On confirm: bulk POST to `/api/clusters` (one row per checked cluster).
- DDB record auto-populates cluster_arn + secret_arn (from RDS describe + the
  cluster's master secret) so EXPLAIN works without manual backfill.

---

## P3 — Depth

### P3.1 ML anomaly detection ✅ (seasonal baseline, robust z-score)

**What (DONE):**

- `metric_baselines` 테이블 (cluster × metric × hour_of_week PK, median + IQR + sample_count)
- `pg_baseline_trainer.py` — 14일 history → `PERCENTILE_CONT`로 robust 통계,
  1시간 간격 time-gate, dimensions 빈 row만 학습 (per-wait-event 폭주 방지)
- `_anomalies` 엔드포인트 재작성 — 현재 hour-of-week 버킷의 robust z-score
  `(max - median) / IQR`, 버킷 미존재 시 flat fallback
- AnomaliesPanel에 `seasonal` / `flat` 모드 배지 + tooltip
- statsmodels / numpy 추가 없음 (Lambda zip 가벼움)

### P3.2 MySQL parity ✅

**What (DONE):**

- `mysql_query_stats` — `performance_schema.events_statements_summary_by_digest`
- `mysql_table_stats` — `information_schema.tables` + `table_io_waits_summary_by_index_usage`
- `mysql_locks` — `sys.innodb_lock_waits` + `global_variables`
- `mysql_activity` — `performance_schema.threads`
- ETL handler에 engine 분기 (`"mysql" in engine`)
- `/api/dashboard/{id}/table-indexes` MySQL용 분기 (`information_schema.statistics`)
- Wait events SQL의 wait_type CASE에 MySQL `wait/io/*` / `wait/lock/*` / `wait/synch/*` 매핑
- 프론트 dashboard 페이지 — MySQL일 때 VacuumPanel 숨김 (InnoDB equivalent 없음)
- WaitEventsPanel color map에 Sync / Idle 추가
- SettingsPanel 헤더 `engine` prop으로 PostgreSQL/MySQL 자동 라벨링
- Dashboard `_make_query` helper — MySQL aggregate alias의 `name` 빈 columnMetadata 시 `label` 폴백

### P3.3 Cost optimization recommendations ✅ (MVP)

**What (DONE):**

- `cost_check.py` 컬렉터 (engine-agnostic) — 7일 CPU `AVG/P95` 계산
- 임계 (avg<30% AND p95<60% AND not burstable/serverless) 시 `cost_oversized`
  finding을 기존 `cluster_health_findings`에 INSERT
- MaintenanceHealthPanel에 **Cost** 탭 추가
  **Remaining (P3.3.2):**
- Aurora Serverless v2 min/max ACU 권장 (DescribeDBClusters + ScalingConfiguration)
- Reserved Instance / Savings Plan 매칭 (Cost Explorer 통합)
- Storage rightsizing

### P3.4 Lock dependency graph ✅

**What (DONE):**

- LocksPanel에 list / chain 토글
- 클라이언트에서 edges → DAG 빌드, root holders 식별, BFS로 트리 트래버스
- 인덴트 트리 렌더링 (depth별 색상, lock mode + duration, cycle 감지, 쿼리 미리보기)
- 별도 백엔드 변경 없이 기존 blocking_locks 데이터 재사용

### P3.5 Comparative views ✅

- Multi-cluster diff page (pick A and B, see metric-by-metric side-by-side).
- Time-period diff (e.g., this week vs last week) overlay.

### P3.6 Multi-engine support — DocumentDB / DynamoDB ✅ (program #1–#5 shipped 2026-06)

**Shipped** (engine-family approach, not the old `service`-enum plan): a thin
`engine_family()` + `CAPABILITIES` layer (4 byte-identical copies + `lib/engine.ts`)
keyed off `cluster_id`. DynamoDB = Table-as-resource (slug `ddb-<12hex>`), grouped
by account/region. Delivered across 5 sequenced specs:

- **#1 Foundation** — engine-family model, ETL dispatch (`_collect_one` branches
  before any RDS/PI/CW call), per-family registration/discovery, `resource_details`
  JSONB on `cluster_meta`, capability-gated dashboard shell + Fleet grouping.
- **#2 DocumentDB diagnosis** — `docdb_findings.py` (connection saturation,
  replica lag, cursor timeout, low cache hit), AWS/DocDB metrics + connections-limit.
- **#3 DynamoDB diagnosis** — `dynamodb_findings.py` (throttling, capacity
  under/over-provisioned, hot partition, on-demand high throughput, per-GSI
  throttle), PK/SK/GSI/LSI structural surfacing.
- **#4 MCP tools + AI** — `get_maintenance_findings` (engine-agnostic) + engine-aware
  `get_health_status`; agent prompt is engine-family-aware; chat diagnoses NoSQL.
- **#5 Simulation gating** — simulation tools/page are Aurora-only and cleanly
  refuse NoSQL (`unsupported_engine`); `simulation_policy.cedar`; Cedar parity test.
- **Dashboard parity (audit follow-up)** — ⛶ Expandable metric-expand + time-range
  selector wiring + previously-uncollected-but-now-charted metrics on the
  NoSQL overview panels (Codex parity audit TOP 3).

**Remaining follow-ups (concretely scoped, pick up independently):**

- **DocDB cost rightsizing.** Aurora `cost_check` can't be reused as-is: DocDB
  stores CPU as `cpu_utilization` (not `cpu`) and has no `instance_class` in
  `cluster_meta`/`resource_details`. Plan: extend `docdb_cw_collector`/meta to
  capture the writer instance class, then add a `docdb_cost_oversized` rule
  inside `docdb_findings.py` (read `cpu_utilization` + class; skip burstable t-family).
- **Dashboard parity — backend panels for new engines** (Codex audit, med/low):
  - Health score for DynamoDB/DocDB (synthesize from throttle/latency/capacity
    resp. CPU/connections/lag/cache — findings panel partially covers this today).
  - Backup/snapshot panel: DocDB cluster snapshots + restore window; DynamoDB
    PITR / on-demand backups (Aurora `BackupPanel` is relational-gated today).
  - Events panel for DocDB (RDS-family events); capacity/usage forecast;
    replication/topology view for DocDB cluster members; connection
    pressure/headroom for DocDB; engine settings panel (DDB billing/PITR/TTL/
    stream/GSI status; DocDB params/maintenance/deletion-protection).
  - Per-GSI metric lines on the DynamoDB panel (collected with `dimensions.gsi`,
    not yet exposed as selectable per-index series).
- **NoSQL write / remediation tools** + Cedar policies + approval binding
  (capacity change, TTL, index ops) — mirrors the operations server's approval flow.
- **Mongo-protocol deep diagnosis** for DocumentDB (`serverStatus`, `currentOp`,
  profiler/slow-op) — needs the connectivity decision from
  `docs/adr/2026-06-12-aws-managed-mcp-servers.md` (thin pymongo read collector
  in a VPC Lambda vs. read-only AWS DocDB MCP with credential-level least-privilege).
- **DynamoDB capacity-mode cost simulator** (Provisioned↔On-Demand $ what-if) —
  net-new; needs a region-specific Pricing-API-backed estimate (a hardcoded
  pricing table goes stale; a wrong number is worse than none). Advice is
  already delivered via #3's `ddb_capacity_overprovisioned` finding + #4 chat.

**Still future (separate):** RDS non-Aurora (MySQL/PG/MariaDB) storage rightsize
(`AllocatedStorage` vs used); cross-account ETL for new-engine resources (ETL
`get_client` is local-account today). **Out of scope:** Redshift / OpenSearch /
RDS Custom — different operational shapes.

---

## P4 — Operational polish

- OpenAPI / Swagger docs for `/api/*` endpoints.
- SSE/WebSocket push for real-time alerts (replace 5-min polling).
- Audit log visual timeline (current is text table).
- PDF / markdown runbook export for resolved incidents.
- Inbound webhook from Datadog / PagerDuty incidents to auto-start a chat session.
- Per-org retention policy + archival to S3 Glacier.
- CDN cache tuning for `/config.json` (currently no-store).

---

## Done (recent)

- Dashboard expansion: 17 panels, 24 metrics, ETL collectors (pi/cw/locks/activity/table_stats)
- Application Inference Profile setup + Cost dashboard
- Chat history (localStorage) + model selector (Bedrock list_inference_profiles)
- Operations MCP cluster_id resolution fix
- IA redesign: AppShell sidebar, page primitives, Claude Design language
- Portability: runtime config.json, Cognito callback auto-registration, schema migrator
- Cross-account spoke role validation at cluster registration

### Maintenance Health (Phase 1 + 2)

- `cluster_health_findings` 테이블 (schema_v6) — check_type / severity / subject /
  value / threshold / recommendation / details(JSONB)
- `pg_health_checks.py` — 7개 check_type (txid_age, dead_tuples, vacuum_overdue,
  index_unused, extension_missing, setting_misconfigured, table_bloat). 단일
  snapshot_time으로 모든 finding 동시 emit (NOW() per-row 버그 픽스)
- `pg_extensions.py` (schema_v7) — `pg_extension` 동기화, UPSERT + drop된 항목 DELETE
- `/api/dashboard/{id}/health-findings` — 최신 snapshot의 ranked findings (severity 정렬)
- `/api/dashboard/{id}/extensions` — installed 목록 + 권장 매트릭스 (severity별 ✓/✗)
- `MaintenanceHealthPanel` — 상단 배치, severity 배지, 7개 필터 탭(All/VACUUM/Bloat/Indexes/Config/Extensions/Cost),
  finding 행 클릭 → AI explain 모달 (3-section: Why matters / Concrete fix / How to verify)
- `ExtensionsCard` — 권장 6개 ✓/✗ 그리드 + "Other installed" 토글
- SettingsPanel — pg_locks.py settings 7개 로깅 파라미터 확장 수집,
  권장값 매핑으로 셀별 ✓/⚠ 배지 + "recommended: X" 라벨

### Format / UX polish

- `frontend/src/lib/format.ts` — fmtNumber/fmtBytes/fmtDuration/fmtPct/fmtExact 공통
- 대시보드 패널 (Table Sizes / Index Recommendations / Vacuum & Bloat / Top Queries)
  human-readable 변환 + `tabular-nums` + tooltip-with-exact
- % 컬럼 헤더 명확화 (Seq → "Seq / total scans", Bloat → "Dead / total", Index % → "Indexes / total")
- 테이블 → 인덱스 detail expand: TableSizesPanel 행 클릭 시 lazy fetch + 인덱스 목록
  (PK/UQ/unused/invalid 배지)

### Query Lab (P2.1) + RBAC (P2.4 + P2.4.2)

- `/api/explain` 엔드포인트 — EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) → 구조화 JSON
- `<PlanTree>` 재귀 컴포넌트 — self-time 기반 색상, hot nodes, planner misestimate 배지
- "Get AI insight" 버튼 — plan summary → 2-3개 구체적 권장사항
- SQL 에러 = 노란 warning (HTTP 400), 인프라 에러 = 빨간 error (502)
- Plan history (localStorage) + share link
- Bulk SQL review (세미콜론 구분 다중 SQL → markdown table verdict)
- Cognito 그룹 (`dbops-admin`/`dbops-viewer`) + 프론트 mutation UI 숨김
- 서버 측 RBAC — `_decode_jwt_payload` + `_is_admin` + `_forbid_viewer` 헬퍼,
  mutation 경로(`POST /clusters`, `POST/PATCH/DELETE /alert-rules`, …) 모두 차단
- 프론트 api-client `authHeaders()` — mutation 호출에 ID token 자동 첨부

### Auth + Theme

- Self-hosted login (`/login`, `/forgot`, `/reset`) — `amazon-cognito-identity-js` SRP
- Cognito User Pool Client `USER_PASSWORD_AUTH` + Access/ID token **12시간** validity
- AuthGuard 토큰 자동 refresh (마운트 / 45분 interval / focus / pre-flight)
- Light/dark 테마 토글 (헤더 우측, 2-state pill), localStorage 영속화, FOUC 방지
  inline script, Linear/Notion 톤 v4 팔레트 + prose-invert 색 var 재정의
- 모바일 하단 탭바 (Fleet/Dashboard/Chat/Alerts/Clusters)

### Cluster registration UX (P2.5) + Alerts (P2.3)

- Bulk Discover panel — same-account/cross-account `rds:DescribeDBClusters` 자동 enumerate
- "Register selected" 확인 모달 + bulk `/api/clusters/bulk-register`
- Slack Block Kit + PagerDuty Events API v2 (dedup_key=rule_id) — `alert_subscribers_managed` (schema_v5)

### Dashboard events / anomalies (UX)

- `event_processor` 분류 강화 — CloudTrail wrapper 이벤트의 `detail.eventName` 사용 (unknown 99% 해소)
- EventsPanel 카드 클릭 → 모달 (Key Facts + AI explain + raw JSON 탭)
- AnomaliesPanel 카드 클릭 → 모달 (4 stats + AI diagnose)
- Wait Events "unknown" 제거 (PI total AAS 행 필터 + name prefix로 type 추론)
- 친근한 라벨 (`unknown` → "Other (RDS)", snake/CamelCase → Title Case)
