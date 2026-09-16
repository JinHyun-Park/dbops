/**
 * English translations, keyed by the Korean source string.
 *
 * See `../i18n.tsx` for why Korean is the key: `translate()` returns
 * `EN[ko] ?? ko`, so anything missing here renders as the Korean original
 * rather than as a blank or a raw key. That makes this file incrementally
 * extendable, and it is deliberately incomplete.
 *
 * COVERED (the page frame):
 *   1. the app-shell nav: group labels, item labels, hints
 *   2. PageHeader eyebrow / title / description on every page
 *   3. EmptyState eyebrow / title / description
 *   4. the page-level status / kind / state / action label maps
 *   5. the locale toggle itself
 *
 * NOT COVERED: the dense data panels (dashboard panels, RCA detail, simulator
 * forms, table bodies, toasts, form validation). Those stay Korean via the
 * fallback on purpose: a half-translated table reads worse than a consistently
 * Korean one.
 *
 * Keys are copied verbatim from the source literals, dashes and punctuation
 * included, because the lookup is an exact string match. `tools/i18n-check.mjs`
 * fails if a key here no longer appears anywhere in `src/`.
 *
 * DBA jargon this repo already keeps in English (Replica Lag, Tuples Returned,
 * EXPLAIN, wait events, burn-down, right-sizing, ...) stays in English on both
 * sides.
 */
export const EN: Record<string, string> = {
  // ── Locale toggle ────────────────────────────────────────────────────────
  언어: "Language",
  한국어: "Korean",
  영어: "English",

  // ── App shell: nav hints ─────────────────────────────────────────────────
  "전체 클러스터 한눈에 트리아지": "Triage the whole fleet at a glance",
  "서비스별 DB 청사진": "Per-service DB blueprint",
  "조치 효과 이력: 입증된 권장 조치 우선":
    "Remediation outcome history: proven actions rank first",
  "단일 클러스터 심층 분석": "Single-cluster deep dive",
  "클러스터 간, 기간 간 비교": "Cluster vs cluster, period vs period",
  "가용성과 지연 SLO, 에러 버짓": "Availability and latency SLOs, error budget",
  "FK 계보, 테이블 의존성": "FK lineage, table dependencies",
  "자연어로 운영 작업": "Run operations in natural language",
  "SQL 분석 + EXPLAIN": "SQL analysis + EXPLAIN",
  "쓰기 작업 DBA 승인 게이트": "DBA approval gate for write operations",
  "리더 추가와 자동 예열 작업 상태, 예열 전 취소":
    "Reader add and auto-warmup status, cancellable before warmup starts",
  "자연어 fleet 질의 + Saved views":
    "Natural-language fleet queries + Saved views",
  "AI 진단 + 처방 재사용": "Reuse AI diagnoses and their prescriptions",
  "업그레이드, 파라미터, 스케일링, DDL what-if":
    "Upgrade, parameter, scaling and DDL what-if",
  "경보 자동 RCA, 예약, 수동 실행 등 에이전트 작업과 결과":
    "Agent tasks and their results: alert-driven auto RCA, scheduled, manual",
  "장애 시나리오를 실행해 실제 자동 RCA가 원인과 조치를 분석":
    "Inject a failure scenario and let the real auto RCA work the cause",
  "알림, 이벤트, 쓰기 통합 인시던트 피드":
    "One incident feed: alerts, events, writes",
  "누가 무엇을 승인하고 실행했는지: 감사와 회고용":
    "Who approved and ran what, for audit and retro",
  "두 시점 사이 쿼리 워크로드 변화":
    "Query workload change between two points in time",
  "알림 룰 + SNS 구독자": "Alert rules + SNS subscribers",
  "클러스터 등록 + 연결 상태": "Cluster registration + connection status",
  "정기 운영 요약 리포트": "Scheduled operations summary reports",
  "모델별 Bedrock 비용": "Bedrock cost by model",
  "에이전트가 기억하는 내용": "What the agent remembers about you",
  "기능 토글: 티켓팅과 리포트 전달 (관리자)":
    "Feature toggles for ticketing and report delivery (admin)",
  "지정 승인자 라우팅: 클러스터와 액션별 승인자 (관리자)":
    "Designated approver routing, per cluster and action (admin)",
  "사용자 역할 관리: admin/viewer (관리자)":
    "User role management, admin / viewer (admin)",
  "팀 관리: 멤버와 클러스터 가시성 (관리자)":
    "Team management: members and cluster visibility (admin)",
  "에이전트 참조 컨텍스트 업로드 (관리자)":
    "Upload reference context for the agent (admin)",
  "멤버 계정 연결 위저드 (관리자)": "Member account onboarding wizard (admin)",
  "DBOps 자체 모니터링: Lambda, Aurora, DDB 상태":
    "DBOps self-monitoring: Lambda, Aurora, DynamoDB status",

  // ── PageHeader eyebrows ──────────────────────────────────────────────────
  모니터: "Monitor",
  자동화: "Automate",
  설정: "Configure",
  재무: "Finance",
  개발자: "Developer",

  // ── PageHeader titles ────────────────────────────────────────────────────
  "Agent가 기억하는 것": "What the agent remembers",
  "RCA 받은함": "RCA inbox",
  "클러스터 레지스트리": "Cluster registry",
  "Aurora / RDS 비용": "Aurora / RDS cost",
  "DBOps 플랫폼 운영 비용": "DBOps platform running cost",
  "Bedrock 토큰 사용량": "Bedrock token usage",
  "ElastiCache 비용": "ElastiCache cost",
  "커밋 할인 (RI / Savings Plan)": "Commitment discounts (RI / Savings Plan)",
  "Bedrock 비용": "Bedrock cost",
  "알림 규칙": "Alert rules",
  "장애 시나리오": "Failure scenarios",
  "스케일 관리": "Scale management",
  리포트: "Reports",
  "승인 센터": "Approval center",
  "Fleet 개요": "Fleet overview",
  "API 문서": "API reference",

  // ── PageHeader descriptions ──────────────────────────────────────────────
  "기능 토글: 티켓팅 연동과 리포트 전달 제어 (관리자 전용)":
    "Feature toggles for the ticketing integration and report delivery (admin only).",
  "기능 토글: 티켓팅 연동과 리포트 전달 제어. 변경 사항은 즉시 적용됩니다.":
    "Feature toggles for the ticketing integration and report delivery. Changes take effect immediately.",
  "EXPLAIN 버튼은 plan tree를 바로 렌더링하고, AI 분석은 SQL을 agent에 보내 자연어 해석을 받아옵니다.":
    "EXPLAIN renders the plan tree directly; AI analysis sends the SQL to the agent and returns a plain-language reading of it.",
  "AgentCore Memory에 저장된 당신의 선호와 사실. 잘못된 정보가 박혀 있으면 여기서 삭제하세요. 이후 대화부터 다시 학습됩니다.":
    "The preferences and facts AgentCore Memory holds about you. Delete anything wrong here and the agent relearns it from later conversations.",
  "클러스터를 하나씩 열지 않고 전체 RCA 리포트를 한 화면에서 읽습니다. 최근 도착한 리포트가 위에 오고, 마지막 방문 이후 도착한 리포트에는 표시가 붙습니다.":
    "Read every RCA report in one place instead of opening clusters one by one. The newest arrivals come first, and anything published since your last visit on this browser is marked.",
  "이 브라우저에서 마지막 방문 이후 도착한 리포트 기준입니다. 읽음 여부가 아닙니다.":
    "Counted against your last visit on this browser. Not a read/unread state.",
  "자연어로 '최근 24h CPU 80% 넘은 클러스터' 처럼 물어보면 즉시 필터 결과를 카드로 보여줍니다. 필터는 편집 + 저장 가능.":
    'Ask in plain language, such as "clusters over 80% CPU in the last 24h", and get the matching clusters as cards. Filters can be edited and saved.',
  "DBOps에서 일어난 모든 쓰기 의사결정의 시간순 기록: 누가 요청했고 누가 승인했고 언제 실행됐는지. 컴플라이언스 감사와 사후 회고 (post-incident retro) 용도.":
    "Every write decision made in DBOps, in order: who asked, who approved, when it ran. Built for compliance audits and post-incident retros.",
  "권장 조치가 실제로 증상을 해소했는지 자동 측정해 누적한 효과 이력: 입증된 조치를 우선합니다.":
    "Outcome history built by automatically measuring whether a recommended action actually cleared the symptom. Proven actions rank first.",
  "DBOps 자체의 운영 상태: Lambda 함수, Aurora cache, DynamoDB 테이블의 상태를 한 화면에. 30초마다 자동 새로고침.":
    "DBOps' own operational state: Lambda functions, the Aurora cache and the DynamoDB tables on one screen. Auto-refreshes every 30s.",
  "Aurora 클러스터 등록과 cross-account 연결 관리. 메트릭/실시간 상태는 Fleet 또는 Dashboard에서 확인하세요.":
    "Register Aurora clusters and manage cross-account connections. For metrics and live state, use Fleet or Dashboard.",
  "계정의 Aurora/RDS 비용: Cost Explorer로 사용 유형(Aurora I/O, 스토리지, 인스턴스 시간, 백업)별로 분해합니다. 클러스터별 분리는 cost-allocation 태그를 활성화해야 합니다. CE는 약 24시간 지연됩니다.":
    "Account-wide Aurora / RDS cost, broken down by usage type (Aurora I/O, storage, instance hours, backup) through Cost Explorer. Per-cluster breakdown needs cost-allocation tags enabled. CE lags about 24 hours.",
  "DBOps 자체를 운영하는 데 드는 전체 비용: Application=DBOps 태그가 붙은 모든 리소스(Lambda, 캐시 Aurora, DynamoDB, CloudFront, AgentCore 등)를 서비스별로 분해합니다. 모니터링 대상 고객 DB 클러스터는 포함되지 않습니다.":
    "What it costs to run DBOps itself: every resource tagged Application=DBOps (Lambda, the cache Aurora, DynamoDB, CloudFront, AgentCore) broken down by service. The monitored database clusters are not included.",
  "계정 전체 Bedrock 토큰 사용량(모델별): CloudWatch AWS/Bedrock 메트릭 기반. 태그 필터 불가로 계정 전체 집계입니다.":
    "Account-wide Bedrock token usage by model, from the CloudWatch AWS/Bedrock metrics. These metrics cannot be tag-filtered, so the total is account-wide.",
  "계정의 ElastiCache 비용: Cost Explorer로 사용 유형(노드 시간, 데이터 스토리지, I/O)별로 분해합니다. 클러스터별 분리는 cost-allocation 태그를 활성화해야 합니다. CE는 약 24시간 지연됩니다.":
    "Account-wide ElastiCache cost, broken down by usage type (node hours, data storage, I/O) through Cost Explorer. Per-cluster breakdown needs cost-allocation tags enabled. CE lags about 24 hours.",
  "등록된 Aurora 계정의 Reserved Instance / Savings Plan 현황: 스케일링 권장이 RI 커버리지를 깨뜨려 오히려 비용이 늘지 않는지 확인합니다. 만료 임박, 미사용 RI를 함께 표시합니다.":
    "Reserved Instance and Savings Plan coverage across the registered Aurora accounts, so a scaling recommendation does not break coverage and cost more. Expiring and unused RIs are called out.",
  "DBOps 호출의 Bedrock 비용: Application=DBOps 태그가 박힌 Application Inference Profile을 경유합니다. Cost Explorer는 약 24시간 지연돼서 반영됩니다.":
    "Bedrock cost for DBOps' own calls, routed through an Application Inference Profile tagged Application=DBOps. Cost Explorer reflects it after about 24 hours.",
  "사용자 역할 관리 (관리자 전용)": "User role management (admin only).",
  "사용자 목록과 역할(admin/viewer)을 관리합니다. 변경 사항은 즉시 적용됩니다.":
    "Manage the user list and each user's role (admin / viewer). Changes take effect immediately.",
  "팀 관리: 멤버와 클러스터 가시성 (관리자 전용)":
    "Team management: members and cluster visibility (admin only).",
  "팀을 만들고 멤버와 클러스터 가시성을 관리합니다.":
    "Create teams and manage their members and cluster visibility.",
  "AI 진단 + 권장 조치를 재사용 가능한 playbook으로 저장. 동일 패턴 재발 시 같은 처방을 곧바로 참조합니다.":
    "Save an AI diagnosis and its recommended actions as a reusable playbook, so the same pattern gets the same prescription next time.",
  "에이전트 참조 컨텍스트 파일 관리 (관리자 전용)":
    "Manage the reference context files the agent reads (admin only).",
  "에이전트가 작업할 때 참조하는 운영 컨텍스트 파일을 관리합니다. 업로드한 내용은 매 호출마다 에이전트에 참조 데이터로 주입되며, 명령(command)으로 해석되지 않습니다.":
    "Manage the operational context files the agent reads while it works. Uploaded content is injected on every call as reference data, never interpreted as a command.",
  "단일 클러스터 deep dive: 시계열, wait events, locks, vacuum, schema changes 등 17개 패널.":
    "Single-cluster deep dive: 17 panels covering time series, wait events, locks, vacuum and schema changes.",
  "현재 클러스터의 외래키(FK) 관계를 라이브로 추출해 표 의존성을 시각화합니다. PostgreSQL 전용.":
    "Reads the current cluster's foreign keys live and draws the table dependency graph. PostgreSQL only.",
  "계정의 DB를 Region → VPC로 묶어 본 아키텍처 청사진: 노드를 클릭하면 해당 대시보드로 이동하고 전역 선택이 바뀝니다.":
    "An architecture blueprint of the account's databases grouped Region to VPC. Clicking a node opens its dashboard and moves the global selection.",
  "업그레이드, 파라미터, 스케일링, DDL 영향을 실제 실행 전에 추정합니다. 모든 결과는 추정치이며 프로덕션 적용 전 별도 검증 필수.":
    "Estimates the impact of an upgrade, a parameter change, scaling or a DDL before you run it. Every number is an estimate and needs its own check before production.",
  "버튼을 누르면 실제와 같은 장애 신호가 주입되고, 실제 자동 RCA가 원인과 조치를 분석합니다.":
    "One click injects realistic failure signals and the real auto RCA works out the cause and the fix.",
  "멀티 클러스터 비교 또는 같은 클러스터의 시간대별 변화를 사이드바이사이드로 확인.":
    "Compare clusters side by side, or the same cluster across two time windows.",
  "두 시점의 쿼리 워크로드(pg_stat_statements)를 비교: 배포 이후 새로 등장한 쿼리, 갑자기 느려진 쿼리, 사라진 쿼리를 자동 검출. '배포하고 느려졌다'는 신고에 30초 안에 용의자를 좁힙니다.":
    'Diffs the query workload (pg_stat_statements) between two points in time: queries that appeared, regressed or vanished after a deploy. Narrows down a "it got slow after the release" report in about 30 seconds.',
  "리더 추가(scale-out)와 자동 버퍼풀 예열 작업의 진행 상태입니다. 예열이 시작되기 전(리더 생성 중, 승인 대기)인 작업은 취소할 수 있습니다.":
    "Progress of reader scale-out and automatic buffer-pool warmup. Anything that has not started warming yet (reader provisioning, awaiting approval) can still be cancelled.",
  "가용성 + 쿼리 지연 SLO 목표 대비 실측 + 에러 버짓 burn-down. 목표값은 클러스터별 브라우저에 저장됩니다.":
    "Measured availability and query latency against their SLO targets, plus error-budget burn-down. Targets are stored per cluster in this browser.",
  "report_generator Lambda가 매일 자정에 작성하는 클러스터별 운영 요약. AAS, 슬로우 쿼리, 알림, 스토리지 변화를 한 화면에 모아둡니다.":
    "Per-cluster operations summaries written nightly by the report_generator Lambda: AAS, slow queries, alerts and storage change on one page.",
  "Agent 또는 대시보드가 제안한 변경 작업(DDL, parameter, scaling, maintenance, snapshot/restore, Data API 활성화)을 DBA가 검토하고 승인하는 게이트입니다. 승인됨 탭에는 실행 완료(consumed)된 건도 함께 표시됩니다.":
    "The gate where a DBA reviews and approves changes proposed by the agent or the dashboard (DDL, parameter, scaling, maintenance, snapshot / restore, enabling the Data API). The Approved tab also lists requests that have already been consumed.",
  "클러스터와 액션별 지정 승인자 라우팅 (관리자 전용)":
    "Designated approver routing per cluster and action (admin only).",
  "클러스터와 액션 타입별로 지정 승인자를 라우팅합니다. 매칭된 정책이 있으면 목록에 없는 관리자는 승인 불가. 미매칭 요청은 모든 관리자에게 fallback.":
    "Routes approvals to designated approvers per cluster and action type. Once a policy matches, admins outside its list cannot approve; unmatched requests fall back to every admin.",
  "단일 cluster의 모든 운영 신호를 시간축 한 줄에. 알림 발화, RDS 이벤트, 스키마 변경, 실행된 쓰기 작업이 모두 같은 흐름에 보입니다. 사고 시점 컨텍스트를 한 화면에 잡아두는 용도.":
    "Every operational signal for one cluster on a single time axis: alert firings, RDS events, schema changes and executed writes in the same stream. Built to hold the context of an incident on one screen.",
  "멤버 계정 연결 위저드 (관리자 전용)":
    "Member account onboarding wizard (admin only).",
  "멤버 AWS 계정에 스포크 역할을 배포하고 DBOps Hub에 연결합니다.":
    "Deploys the spoke role into a member AWS account and connects it to the DBOps hub.",
  "DBOps REST API. 모든 경로는 Cognito JWT(Authorization: Bearer)가 필요합니다. Slack 웹훅(HMAC)과 /health 제외.":
    "The DBOps REST API. Every route needs a Cognito JWT (Authorization: Bearer), except the Slack webhooks (HMAC) and /health.",
  // Interpolated headers: {n} is substituted at the call site so the rendered
  // Korean stays byte-identical to what it was before the wrap.
  "총 {n}개 클러스터, 위험도 순 정렬, 30초마다 자동 새로고침":
    "{n} clusters, sorted by risk, auto-refreshing every 30s",
  "총 {n}개, 5분마다 metric_snapshots를 평가해서 조건 충족 시 발화합니다.":
    "{n} rules. Each is evaluated against metric_snapshots every 5 minutes and fires when its condition holds.",

  // ── EmptyState eyebrows ──────────────────────────────────────────────────
  "접근 제한": "Restricted",
  "클러스터 없음": "No clusters",
  "추적 미시작": "Not tracked yet",
  "데이터 없음": "No data",
  "비어 있음": "Empty",
  "리포트 없음": "No reports",
  "규칙 없음": "No rules",
  "데이터 부족": "Not enough data",
  미지원: "Unsupported",
  "RI 없음": "No RIs",
  "AZ 스케일아웃": "AZ scale-out",

  // ── EmptyState titles ────────────────────────────────────────────────────
  "관리자 전용 페이지": "Admins only",
  "저장된 기록이 없습니다": "Nothing stored yet",
  "작업 없음": "No tasks",
  // RCA candidate categories (CATEGORY_LABEL in rca-candidate-detail.tsx).
  // These render INSIDE an otherwise English sentence, so leaving them out
  // produced "Leading hypothesis (이벤트): ..." on an English browser.
  "스키마 변경": "Schema change",
  이벤트: "Event",
  "락 경합": "Lock contention",
  "카운터 급증": "Counter spike",
  "메트릭 급증": "Metric spike",
  "슬로우 쿼리": "Slow query",
  기타: "Other",
  신호: "signal",
  "이 종류로는 아직 아무것도 없습니다. 범위를 모든 종류로 바꿔 보거나, 위에서 클러스터를 선택해 RCA를 직접 실행할 수 있습니다.":
    "Nothing of this kind yet. Widen the scope to every kind, or pick a cluster above and run an RCA yourself.",
  "조건을 만족하는 클러스터 없음": "No cluster matches the filter",
  "기록된 활동이 없습니다": "No activity recorded",
  "불러오지 못했습니다": "Could not load",
  "아직 학습된 결과가 없습니다": "Nothing learned yet",
  "첫 Aurora 클러스터를 등록해보세요": "Register your first Aurora cluster",
  "Cost allocation 태그가 활성화되어 있지 않습니다":
    "Cost allocation tags are not enabled",
  "플랫폼 비용 데이터가 없습니다": "No platform cost data",
  "Aurora / RDS 비용 데이터가 없습니다": "No Aurora / RDS cost data",
  "ElastiCache 비용 데이터가 없습니다": "No ElastiCache cost data",
  "Bedrock 토큰 메트릭이 없습니다": "No Bedrock token metrics",
  "활성 Reserved Instance가 없습니다": "No active Reserved Instances",
  "사용자가 없습니다": "No users",
  "팀이 없습니다": "No teams",
  "저장된 Runbook 없음": "No saved runbooks",
  "등록된 파일 없음": "No files",
  "첫 알림 규칙을 등록해보세요": "Create your first alert rule",
  "클러스터가 없습니다": "No clusters registered",
  "클러스터를 불러오지 못했습니다": "Could not load clusters",
  "등록된 DB가 없습니다": "No databases registered",
  "DocumentDB 시뮬레이션은 지원 예정": "DocumentDB simulation is not ready yet",
  "사용률 데이터가 충분하지 않습니다": "Not enough utilization data",
  "실행 이력 없음": "No runs yet",
  "두 시점을 골라 비교를 실행하세요": "Pick two points in time and compare",
  "진행 중인 스케일 작업이 없습니다": "No scale operation in progress",
  "Aurora 클러스터가 없습니다": "No Aurora clusters",
  "아직 등록된 클러스터가 없습니다": "No clusters registered yet",
  "대기 중인 승인 요청이 없습니다": "No approval requests waiting",
  "아직 승인된 작업이 없습니다": "Nothing approved yet",
  "거부된 작업이 없습니다": "Nothing rejected",
  "엔드포인트 없음": "No endpoints",
  "확인하지 못한 signal이 있습니다": "Some signals could not be read",
  "이 윈도우에 신호가 없습니다": "No signals in this window",
  "아직 생성된 리포트가 없습니다": "No reports generated yet",
  "등록된 정책 없음": "No policies",
  "비용 비교를 위한 데이터가 부족합니다": "Not enough data to compare cost",
  "이 테이블은 비용 비교를 지원하지 않습니다":
    "This table does not support cost comparison",

  // ── EmptyState descriptions ──────────────────────────────────────────────
  "이 설정은 관리자만 변경할 수 있습니다.": "Only an admin can change this.",
  "이 페이지는 관리자만 볼 수 있습니다.": "Only an admin can view this page.",
  "채팅을 진행하면 Agent가 당신의 선호 (응답 길이, 분석 스타일, 선호 명령)를 자동으로 추출해 여기에 누적합니다.":
    "As you chat, the agent extracts your preferences (answer length, analysis style, favourite commands) and collects them here.",
  "Agent는 대화에서 사실을 추출해 여기에 누적합니다. 아직 학습이 충분치 않을 수 있어요.":
    "The agent extracts facts from your conversations and collects them here. It may simply not have learned enough yet.",
  "경보가 발생하면 자동 RCA가 여기에 쌓입니다. 위에서 클러스터를 선택해 RCA를 직접 실행할 수도 있습니다.":
    "Auto RCA runs land here when an alert fires. You can also pick a cluster above and run one yourself.",
  "필터의 metric/threshold를 조정해보세요.":
    "Try adjusting the metric or threshold in the filter.",
  "아직 DBOps를 통해 실행된 쓰기 작업이 없거나, 현재 필터에 매칭되는 기록이 없습니다.":
    "Either no write has been executed through DBOps yet, or nothing matches the current filter.",
  "권장 조치가 적용되고 평가 윈도우가 지나면 효과 이력이 쌓입니다.":
    "Outcome history builds up once a recommended action has been applied and its evaluation window has passed.",
  "Cluster ID, account, region을 입력하면 RDS Data API 기반 메트릭 수집이 시작됩니다.":
    "Enter a cluster ID, account and region and metric collection starts over the RDS Data API.",
  "아직 모델 호출 기록이 없거나 CloudWatch 메트릭 전파 전입니다.":
    "Either no model has been called yet, or the CloudWatch metrics have not propagated.",
  "등록된 Aurora 계정에서 활성 RI를 찾지 못했습니다. 모두 온디맨드 과금 중이거나, RI가 다른 계정/리전에 있습니다.":
    "No active RI was found in the registered Aurora accounts. Either everything is on demand, or the RIs live in another account or region.",
  "이 사용자 풀에 등록된 사용자가 없습니다.": "This user pool has no users.",
  "아래에서 첫 번째 팀을 만들어 보세요.": "Create your first team below.",
  "필터를 비우거나 다른 클러스터를 선택해보세요.":
    "Clear the filter, or pick a different cluster.",
  "Chat에서 AI 진단을 받은 뒤 '✓ Runbook 저장' 버튼으로 저장하거나, 위의 '+ 새 Runbook'으로 수동 작성하세요.":
    "Get an AI diagnosis in Chat and save it with the Save runbook button, or write one by hand with + New runbook above.",
  "아래에서 첫 번째 컨텍스트 파일을 업로드하세요. 파일이 없으면 에이전트는 기본 참조 정보만 사용합니다.":
    "Upload your first context file below. With none, the agent works from its built-in reference material only.",
  "위 폼에서 cluster + metric + threshold를 고르면 됩니다. evaluator가 5분마다 실행되고 SNS / Slack / PagerDuty 구독자에게 fan-out 됩니다.":
    "Pick a cluster, a metric and a threshold in the form above. The evaluator runs every 5 minutes and fans out to the SNS, Slack and PagerDuty subscribers.",
  "Clusters 페이지에서 먼저 PostgreSQL 클러스터를 등록하세요.":
    "Register a PostgreSQL cluster on the Clusters page first.",
  "Clusters 페이지에서 클러스터를 먼저 등록하세요.":
    "Register a cluster on the Clusters page first.",
  "시뮬레이션을 실행하려면 Clusters 페이지에서 먼저 등록하세요.":
    "Register a cluster on the Clusters page before running a simulation.",
  "업그레이드, 파라미터, DDL, 스케일링 시뮬레이션은 Aurora PostgreSQL/MySQL 전용입니다. DocumentDB의 용량/비용 권장은 대시보드의 Maintenance Health 패널과 Chat 진단을 참고하세요.":
    "Upgrade, parameter, DDL and scaling simulation are Aurora PostgreSQL / MySQL only. For DocumentDB capacity and cost advice, use the Maintenance Health panel on the dashboard and a Chat diagnosis.",
  "right-sizing 권장을 산출하려면 CloudWatch 사용률 데이터가 더 필요합니다.":
    "A right-sizing recommendation needs more CloudWatch utilization data than this.",
  "위에서 시나리오를 실행하면 여기에 기록되고, 각 실행에서 생성된 RCA 리포트로 바로 이동할 수 있습니다.":
    "Run a scenario above and it is recorded here, with a direct link to the RCA report it produced.",
  "기본값은 '24시간 전 → 지금'. 배포 직전 시각을 Before에 넣으면 그 배포가 워크로드에 무엇을 했는지 바로 보입니다.":
    'The default is "24 hours ago to now". Put the moment just before a deploy in Before and you see exactly what that deploy did to the workload.',
  "채팅에서 scale_out_with_warmup로 리더를 추가하면 여기에 진행 상태가 표시됩니다.":
    "Add a reader from Chat with scale_out_with_warmup and its progress shows up here.",
  "AZ 스케일아웃은 Aurora PostgreSQL/MySQL 클러스터 전용입니다.":
    "AZ scale-out is for Aurora PostgreSQL / MySQL clusters only.",
  "SLO 계산을 위해 Clusters 페이지에서 먼저 등록하세요.":
    "Register a cluster on the Clusters page so an SLO can be computed.",
  "Aurora 클러스터를 등록하면 CPU, AAS, connection, lock 등 메트릭이 30초 주기로 이 페이지에 스트리밍됩니다.":
    "Register an Aurora cluster and its CPU, AAS, connection and lock metrics stream onto this page every 30 seconds.",
  "Agent가 쓰기 작업을 제안하면 이 페이지에 검토 항목으로 올라옵니다.":
    "When the agent proposes a write, it shows up here for review.",
  "openapi.json에 경로가 없습니다.": "openapi.json declares no paths.",
  "현재 필터에 매칭되는 신호가 없습니다. clear 를 눌러 전체를 보세요.":
    "Nothing matches the current filter. Press clear to see everything.",
  "읽을 수 있었던 signal에는 이 윈도우에 아무것도 없습니다. 위 배너에 표시된 범위는 확인하지 못했습니다.":
    "Nothing in this window among the signals that could be read. The range named in the banner above was never checked.",
  "이 cluster에서 최근 발생한 알림, RDS 이벤트, 스키마 변경, 승인 실행이 없습니다. 윈도우를 늘려보세요.":
    "No recent alert, RDS event, schema change or approved write on this cluster. Try widening the window.",
  "ETL이 메트릭을 충분히 모으면 report_generator 가 첫 일/주간 요약을 생성합니다. 즉시 받아보고 싶으면 채팅에서 요청해보세요.":
    "Once the ETL has collected enough metrics, report_generator writes the first daily and weekly summary. Ask in Chat if you want one now.",
  "아래 폼에서 첫 번째 정책을 추가하세요. 정책이 없으면 모든 관리자가 모든 승인 요청을 처리할 수 있습니다.":
    "Add your first policy in the form below. With none, every admin can act on every approval request.",
  "소비 용량 데이터포인트가 충분히 수집되지 않았습니다.":
    "Not enough consumed-capacity datapoints have been collected.",
  "이 테이블 유형은 비용 시뮬레이션을 지원하지 않습니다.":
    "This table type does not support cost simulation.",

  // ── EmptyState action labels ─────────────────────────────────────────────
  "Chat으로 이동": "Go to Chat",
  "대기 중인 승인 보기": "View pending approvals",
  "+ 클러스터 등록": "+ Register cluster",
  "먼저 에이전트에게 물어보기": "Ask the agent first",
  "Agent에게 물어보기": "Ask the agent",
  "채팅으로 즉시 생성하기": "Generate one now in Chat",
  "CloudWatch 메트릭 확인": "Open CloudWatch metrics",
  Dashboard로: "Go to Dashboard",
  "승인 대기": "Pending",
  승인됨: "Approved",
  거부됨: "Rejected",

  // ── Label maps: task kind / status / interval ────────────────────────────
  "자동 RCA": "Auto RCA",
  "수동 RCA": "Manual RCA",
  "예약 리포트": "Scheduled report",
  대기: "Pending",
  "분석 중": "Running",
  완료: "Done",
  실패: "Failed",
  매시간: "Hourly",
  매일: "Daily",
  매주: "Weekly",

  // ── Label maps: activity action types ────────────────────────────────────
  "SQL 실행": "Execute SQL",
  "Parameter 수정": "Modify parameter",
  "Scaling 조정": "Adjust scaling",
  "Maintenance 관리": "Manage maintenance",
  "Data API 활성화": "Enable Data API",
  "DynamoDB Capacity 변경": "Change DynamoDB capacity",
  "DynamoDB TTL 변경": "Change DynamoDB TTL",
  "DynamoDB PITR 변경": "Change DynamoDB PITR",
  "DocumentDB Profiler 설정": "Set DocumentDB profiler",
  "DocumentDB Index 생성": "Create DocumentDB index",

  // ── Label maps: learning outcome status ──────────────────────────────────
  해결됨: "Resolved",
  미해결: "Persisted",
  "판정 보류": "Inconclusive",

  // ── Label maps: scenario run status ──────────────────────────────────────
  "주입 중": "Injecting",
  "RCA 진행": "RCA running",
  "정리 완료": "Cleaned up",

  // ── Label maps: scale-out operation state ────────────────────────────────
  "리더 생성 중": "Provisioning reader",
  "예열 승인 대기": "Warmup awaiting approval",
  "예열 승인됨": "Warmup approved",
  "예열 중": "Warming",
  "예열 완료": "Warmed",
  취소됨: "Cancelled",
  "리더 생성 실패": "Reader provisioning failed",
  "예열 실패": "Warmup failed",

  // ── Fleet: new RCA reports summary ───────────────────────────────────────
  // "가설" stays "hypothesis" on both sides, and no wording here may promote a
  // rank into a proven cause or a confidence figure.
  "새 RCA 리포트": "New RCA reports",
  "첫 방문": "First visit",
  "새 리포트 없음": "No new reports",
  "{n}건 신규": "{n} new",
  "{n}건 이상 신규": "{n}+ new",
  "받은함 열기": "Open inbox",
  "유력 가설": "Leading hypothesis",
  "리포트 도착": "Report arrived",
  "인시던트 발생": "Incident at",
  "후보 {n}건 중 1순위": "ranked 1st of {n} candidates",
  "순위는 조사 우선순위이며 확정된 원인이 아닙니다":
    "Ranking is an investigation order, not a confirmed cause",
  "정기 점검 결과이며 원인 분석이 아닙니다":
    "A scheduled health check, not a root-cause analysis",
  "아직 읽을 수 있는 리포트가 없습니다": "No readable report yet",

  // ── Label maps: compare period shift ─────────────────────────────────────
  "직전 1시간": "Previous 1h",
  "직전 6시간": "Previous 6h",
  "어제 같은 시간": "Same time yesterday",
  "지난주 같은 시간": "Same time last week",
};
