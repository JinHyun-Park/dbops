/**
 * English translations, keyed by the Korean source string.
 *
 * See `../i18n.tsx` for why Korean is the key: `translate()` returns
 * `EN[ko] ?? ko`, so anything missing here renders as the Korean original
 * rather than as a blank or a raw key. That makes this file incrementally
 * extendable.
 *
 * Coverage started at the page frame (nav, PageHeader, EmptyState, the status
 * and action label maps, the locale toggle) and now runs through the dense
 * content too: dashboard panels, RCA detail, the simulator forms, table bodies,
 * toasts and form validation. Anything still missing renders Korean, so a gap
 * is a gap, never a blank.
 *
 * NOT IN HERE, on purpose:
 *   - src/lib/rca-report-model.ts. Its Korean CHANGE_WORDS / CHECK_WORDS are
 *     matched against the model's own Korean output to tell a read-only step
 *     from a change, and tools/rca-report-check.mjs imports the module under
 *     bare node. Wrapping its text would break the classifier and that gate.
 *   - the Korean regex matchers in app/ask/page.tsx and the 가-힣 range in the
 *     runbooks slug generator. Not display text.
 *   - the prompts this frontend builds and sends to the agent. They are model
 *     input, not UI, and several pin the answer language on purpose.
 *  Adding a key here is always safe: no matcher ever reads this table. The
 *  hazard is only ever wrapping a literal at a logic site.
 *
 * Keys are copied verbatim from the source literals, dashes and punctuation
 * included, because the lookup is an exact string match. `tools/i18n-check.mjs`
 * fails if a key here no longer appears anywhere in `src/`.
 *
 * DBA jargon this repo already keeps in English (Replica Lag, Tuples Returned,
 * EXPLAIN, wait events, burn-down, right-sizing, ...) stays in English on both
 * sides.
 */
import { EN_SERVER } from "./en-server";

/** Frontend literals. `EN` below spreads `EN_SERVER` in first, so a key that
 *  appears in both resolves to this file: these are the ones
 *  `tools/i18n-check.mjs` can actually verify against `src/`. */
const EN_UI: Record<string, string> = {
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
  // /settings: the deployment default for prose nobody requested.
  "자동 리포트 언어": "Automated report language",
  "사람이 실행한 RCA는 실행한 운영자의 콘솔 언어로 작성됩니다. 이 설정은 경보나 스케줄이 자동으로 만든 RCA와 운영 리포트처럼 요청한 사람이 없는 경우에만 쓰입니다. 모델이 쓴 문장은 나중에 번역할 수 없어서 생성 시점에 언어가 정해집니다.":
    "An RCA a person runs is written in that operator's console language. This setting applies only where there is no requester: an RCA opened by an alert or a schedule, and the operations reports. Model prose cannot be translated afterwards, so its language is fixed when it is generated.",
  // RCA COVERAGE GAPS AND RANKING CAVEATS. Rendered RAW before this, so an
  // English operator read the single-sample uncertainty caveat in Korean,
  // and that caveat is the mechanism that stops a lone peak reading as a
  // confirmed cause. coverageGaps() now returns {source, kind} instead of a
  // pre-composed Korean string, so the label and the suffix are keyed
  // separately and the consumer composes them. The label keys below are
  // satisfied by SOURCE_LABEL in the frozen rca-report-model.ts, which the
  // orphan check sees because it matches substrings across all of src.
  "스키마 변경 이력": "Schema change history",
  "스키마 변경 이력 (읽기 실패)": "Schema change history (read failed)",
  "스키마 변경 이력 (수집기 확인 실패)":
    "Schema change history (collector check failed)",
  "스키마 변경 이력 (관측 범위 확인 실패)":
    "Schema change history (observed scope check failed)",
  "스키마 변경 이력 (수집 테이블 없음)":
    "Schema change history (collector table missing)",
  "스키마 변경 이력 (관측 범위 미확인)":
    "Schema change history (observed scope unconfirmed)",
  "스키마 변경 이력 (이 엔진은 미지원)":
    "Schema change history (unsupported on this engine)",
  "이벤트 로그": "Event log",
  "엔진 카운터": "Engine counters",
  "ElastiCache 신호": "ElastiCache signals",
  "엔진 패밀리 (미확인, 메트릭 집합은 추정값)":
    "Engine family (unconfirmed, so the metric set is a guess)",
  "{n} 확인 불가": "{n}: could not be checked",
  "{n} 데이터 없음": "{n}: no data",
  "1순위 근거가 구간 최댓값 단일 샘플에 의존합니다":
    "The top candidate rests on a SINGLE peak sample, not a window average",
  "구간 평균이 아니라 최댓값으로 판정된 신호입니다":
    "This signal qualified on its peak, not on the window average",
  // RENDER-SITE CONSTANT TABLES. Each of these is a Korean literal in a
  // module-level const that reaches the UI through t(<expr>), e.g.
  // {t(d.label)}. That shape is invisible to a scan for t("...") literals, so
  // all 19 rendered Korean to an English user with tsc, the build and
  // i18n-check all green. tools/i18n-check.mjs now fails on this class.
  // dashboard/page.tsx TAB_DEFS, the tab bar on the most-visited screen.
  개요: "Overview",
  "성능/쿼리": "Performance / queries",
  "AI 자문": "AI advisory",
  "엔진 내부": "Engine internals",
  "구성/백업": "Config / backup",
  "변경/감사": "Changes / audit",
  // tasks/page.tsx KIND_SCOPE, the RCA inbox scope filter.
  "원인 분석만": "RCA only",
  "예약 리포트만": "Scheduled reports only",
  "모든 종류": "All kinds",
  // NOTE for engine-config-panel.tsx: the "활성" LITERAL at line 39 stays
  // UNWRAPPED on purpose, because line 144 compares stream.text === "활성".
  // The 활성 / 비활성 keys further down serve the other, wrapped sites, and a
  // table entry can never reach that comparison: the matchers never read this
  // table.
  // data-api-banner.tsx approval action_description, which is POSTed and then
  // rendered back in the Approval Center.
  "RDS Data API(HttpEndpoint) 활성화": "Enable the RDS Data API (HttpEndpoint)",
  // preferences/page.tsx KIND_OPTIONS hints: what AgentCore Memory keeps.
  "Agent가 추론한 당신의 운영 스타일: 선호하는 응답 어조, 분석 깊이, 자주 쓰는 명령 등":
    "What the agent has inferred about how you work: preferred tone, how deep to analyse, the commands you reach for",
  "Agent가 대화에서 추출한 사실: 클러스터 환경, 도메인 지식, 반복되는 패턴":
    "Facts the agent pulled out of your conversations: your cluster environment, domain knowledge, recurring patterns",
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
    "Ranking is an investigation priority, not a confirmed cause",
  "정기 점검 결과이며 원인 분석이 아닙니다":
    "A scheduled health check, not a root-cause analysis",
  "아직 읽을 수 있는 리포트가 없습니다": "No readable report yet",

  // ── Label maps: compare period shift ─────────────────────────────────────
  "직전 1시간": "Previous 1h",
  "직전 6시간": "Previous 6h",
  "어제 같은 시간": "Same time yesterday",
  "지난주 같은 시간": "Same time last week",

  // ── The dense content: panels, tables, forms, toasts ─────────────────────
  // Grouped by the file the literal is wrapped in. One entry per key, and the
  // key is byte-identical to the literal, both ellipsis spellings included.

  // ── approval-card: card chrome, warnings and CLI preview ─────────────────
  "이 작업의 위험도": "Risk level of this action",
  "스케일아웃이 자동으로 대기열에 올린 예열 승인입니다":
    "Prewarm approval auto-queued by a scale-out",
  "스케일아웃 자동 예열": "Scale-out auto prewarm",
  "리스크와 고려사항": "Risks and considerations",
  리스크: "Risks",
  "승인 전 점검": "Pre-approval checks",
  거부: "Reject",
  "새 클러스터를 생성합니다 (과금 발생). 소스 클러스터는 변경되지 않습니다.":
    "Creates a new cluster (billable). The source cluster is not modified.",
  "PITR 비활성화: 연속 백업 보호가 사라집니다 (force=true).":
    "PITR disable: continuous backup protection is removed (force=true).",
  "이 커스텀 엔드포인트를 삭제합니다. 이 DNS 이름을 참조하는 클라이언트는 연결이 끊깁니다. writer/reader 내장 엔드포인트는 영향받지 않습니다.":
    "Deletes this custom endpoint. Clients pointing at this DNS name will lose their connection. The built-in writer/reader endpoints are not affected.",
  "실행될 CLI (동등 명령)": "CLI to be run (equivalent command)",
  복사됨: "Copied",
  복사: "Copy",

  // ── approval-card: ACTION_GUIDE, what this action does plus its risks and pre-approval checks ─
  "임의 SQL(DDL/DML)을 대상 클러스터에서 직접 실행합니다.":
    "Runs arbitrary SQL (DDL/DML) directly on the target cluster.",
  "락 경합: DDL은 테이블 잠금을 유발해 운영 쿼리를 막을 수 있습니다.":
    "Lock contention: DDL takes table locks and can block production queries.",
  "롤백 난이도: DML은 트랜잭션이지만 DDL은 대부분 즉시 확정됩니다.":
    "Rollback: DML is transactional, but most DDL commits immediately.",
  "force=true면 DROP/TRUNCATE/DELETE 같은 파괴적 구문입니다.":
    "force=true means a destructive statement such as DROP/TRUNCATE/DELETE.",
  "실행 SQL이 의도한 스키마/테이블만 건드리는지 확인":
    "Confirm the SQL touches only the intended schema and tables",
  "트래픽이 적은 시간대(유지보수 윈도우) 권장":
    "Low-traffic window (maintenance window) recommended",
  "대형 테이블이면 CONCURRENTLY/배치 분할 검토":
    "For a large table, consider CONCURRENTLY or batch splitting",
  "DB 파라미터 그룹의 설정값을 변경합니다.":
    "Changes a value in the DB parameter group.",
  "static 파라미터는 적용에 인스턴스 재시작이 필요. 짧은 다운타임 발생.":
    "A static parameter needs an instance restart to apply. Expect brief downtime.",
  "메모리 계열(work_mem 등)은 max_connections와 곱해져 OOM을 유발할 수 있습니다.":
    "Memory parameters (work_mem and friends) multiply by max_connections and can cause OOM.",
  "플래너 파라미터는 쿼리 플랜을 바꿔 성능 회귀 가능.":
    "Planner parameters change query plans and can regress performance.",
  "static/dynamic 여부 확인: static이면 재시작 타이밍 계획":
    "Check static vs dynamic: if static, plan the restart timing",
  "Simulator의 파라미터 영향 추정으로 사전 검토":
    "Pre-check with the parameter impact estimate in Simulator",
  "변경 후 변경 영향 회고 패널로 전후 비교":
    "After the change, compare before and after in the change impact panel",
  "Serverless v2 ACU 범위를 변경합니다.":
    "Changes the Serverless v2 ACU range.",
  "min ACU를 너무 낮추면 콜드스타트 지연, max를 낮추면 스파이크 시 throttle.":
    "Too low a min ACU adds cold-start latency; a lowered max throttles on spikes.",
  "프로비저닝 클러스터에는 적용되지 않습니다(인스턴스 클래스 변경 필요).":
    "Does not apply to a provisioned cluster (change the instance class instead).",
  "관측된 평균/피크 ACU 대비 적정 범위인지 확인":
    "Confirm the range fits the observed average and peak ACU",
  "비용 영향은 Cost 탭과 Simulator로 추정":
    "Estimate the cost impact in the Cost tab and Simulator",
  "유지보수 윈도우를 조회하거나 변경합니다.":
    "Reads or changes the maintenance window.",
  "윈도우를 트래픽 피크 시간으로 옮기면 자동 패치가 운영에 영향.":
    "Moving the window into peak traffic lets auto-patching hit production.",
  "저트래픽 시간대로 설정": "Set a low-traffic window",
  "백업 윈도우와 겹치지 않게": "Do not overlap the backup window",
  "수동 클러스터 스냅샷(백업)을 생성합니다.":
    "Creates a manual cluster snapshot (backup).",
  "비파괴적: 데이터 변경 없음. 스냅샷 스토리지 비용만 발생.":
    "Non-destructive: no data change. Snapshot storage cost only.",
  "대용량이면 생성에 시간 소요": "A large volume takes time to create",
  "보존 정책 확인": "Check the retention policy",
  "스냅샷 또는 특정 시점(PITR)을 새 클러스터로 복원합니다.":
    "Restores a snapshot or a point in time (PITR) into a new cluster.",
  "원본 클러스터는 영향 없음. 새 클러스터가 생성됩니다.":
    "The source cluster is untouched. A new cluster is created.",
  "새 클러스터는 과금 대상. 사용 후 정리 필요.":
    "The new cluster is billable. Clean it up when you are done.",
  "복원에 수십 분 소요될 수 있습니다.": "A restore can take tens of minutes.",
  "복원 대상 시점/스냅샷이 정확한지 확인":
    "Confirm the target time or snapshot is the right one",
  "새 클러스터 ID와 엔드포인트 전환 계획":
    "Plan the new cluster ID and the endpoint cutover",
  "RDS Data API(HttpEndpoint)를 활성화합니다.":
    "Enables the RDS Data API (HttpEndpoint).",
  "데이터 변경은 없으나 SQL 실행 경로가 네트워크 경계에서 IAM 경계로 열립니다.":
    "No data change, but the SQL path moves from a network boundary to an IAM boundary.",
  "rds-data:ExecuteStatement + 시크릿 권한이 있으면 어디서든 쿼리 가능.":
    "Anyone with rds-data:ExecuteStatement plus the secret can query from anywhere.",
  "다운타임 없음, 설정 변경만": "No downtime, a setting change only",
  "활성화 후 라이브 SQL 수집과 에이전트 SQL이 동작":
    "Once enabled, live SQL collection and agent SQL start working",
  "DynamoDB 테이블의 프로비저닝 RCU/WCU 또는 빌링 모드(Provisioned↔On-Demand)를 변경합니다.":
    "Changes a DynamoDB table's provisioned RCU/WCU or its billing mode (Provisioned or On-Demand).",
  "RCU/WCU를 낮추면 스파이크 시 throttle, 빌링 모드 전환은 비용 구조가 바뀝니다.":
    "Lowering RCU/WCU throttles on spikes; a billing mode switch changes the cost structure.",
  "빌링 모드 전환은 AWS가 테이블당 rate-limit 합니다.":
    "AWS rate-limits billing mode switches per table.",
  "GSI가 있는 테이블은 v1에서 차단됩니다(per-GSI 용량 미지원).":
    "A table with a GSI is blocked in v1 (per-GSI capacity is not supported).",
  "관측 소비량 대비 적정 용량인지 Cost Simulator로 확인":
    "Check the capacity against observed consumption in Cost Simulator",
  "On-Demand는 트래픽이 불규칙할 때, Provisioned는 안정적일 때 유리":
    "On-Demand suits spiky traffic, Provisioned suits steady traffic",
  "DynamoDB 테이블의 속성 TTL(자동 만료)을 활성화하거나 비활성화합니다.":
    "Enables or disables attribute TTL (auto expiry) on a DynamoDB table.",
  "TTL을 켜면 해당 속성의 epoch가 지난 항목이 백그라운드로 삭제됩니다. 비가역.":
    "With TTL on, items whose attribute epoch has passed are deleted in the background. Irreversible.",
  "TTL 변경은 테이블당 약 1시간에 한 번만 가능합니다.":
    "A TTL change is allowed only about once an hour per table.",
  "만료 타임스탬프 속성 이름이 정확한지 확인(잘못된 속성이면 삭제 안 됨)":
    "Confirm the expiry timestamp attribute name is exact (a wrong attribute deletes nothing)",
  "삭제 대상 데이터가 아카이브 불필요한지 확인":
    "Confirm the data to be deleted needs no archive",
  "DynamoDB 테이블의 Point-in-Time Recovery(PITR)를 켜거나 끕니다.":
    "Turns Point-in-Time Recovery (PITR) on or off for a DynamoDB table.",
  "켜기는 데이터 보호 강화(35일 연속 백업), 약간의 추가 비용.":
    "Turning it on strengthens protection (35 days of continuous backup) at a small extra cost.",
  "끄기는 데이터 보호 저하. 복구 가능 시점이 즉시 단절됩니다(force=true 필요).":
    "Turning it off weakens protection. The recoverable window is cut immediately (force=true required).",
  "비활성화 요청이면 force=true가 포함됐는지, 정말 보호를 끌 의도인지 확인":
    "For a disable request, confirm force=true is present and that dropping protection is really intended",
  "다운타임 없음, 백업 설정 변경만":
    "No downtime, a backup setting change only",
  "Aurora 커스텀 클러스터 엔드포인트를 생성합니다 (선택한 리더 집합을 가리키는 안정적 DNS).":
    "Creates an Aurora custom cluster endpoint (a stable DNS name for a chosen set of readers).",
  "잘못된 멤버 구성은 트래픽을 의도치 않은 인스턴스로 보낼 수 있습니다.":
    "A wrong member list can send traffic to unintended instances.",
  "writer/reader 내장 엔드포인트는 건드리지 않습니다 (커스텀만 생성).":
    "The built-in writer/reader endpoints are untouched (custom only).",
  "static_members(포함) / excluded_members(제외)는 하나만 사용":
    "Use only one of static_members (include) or excluded_members (exclude)",
  "endpoint_type은 READER 또는 ANY (WRITER 불가)":
    "endpoint_type must be READER or ANY (WRITER is not allowed)",
  "기존 커스텀 엔드포인트의 멤버 목록(StaticMembers/ExcludedMembers)을 변경합니다.":
    "Changes the member list (StaticMembers/ExcludedMembers) of an existing custom endpoint.",
  "멤버 변경은 이 엔드포인트로 가는 신규 커넥션의 라우팅을 즉시 바꿉니다.":
    "A member change immediately reroutes new connections to this endpoint.",
  "대상이 CUSTOM 엔드포인트인지 확인 (내장 엔드포인트는 보호됨)":
    "Confirm the target is a CUSTOM endpoint (built-in endpoints are protected)",
  "변경 후 애플리케이션 커넥션 분포 확인":
    "Check the application connection spread after the change",
  "CUSTOM 클러스터 엔드포인트를 삭제합니다.":
    "Deletes a CUSTOM cluster endpoint.",
  "이 DNS 이름을 사용하는 클라이언트는 연결이 끊깁니다. 사용처 확인 필수.":
    "Clients using this DNS name will lose their connection. You must check who uses it.",
  "내장 writer/reader 엔드포인트는 이 도구로 삭제할 수 없습니다.":
    "The built-in writer/reader endpoints cannot be deleted with this tool.",
  "삭제 대상 엔드포인트를 참조하는 앱/커넥션 문자열이 없는지 확인":
    "Confirm no app or connection string references the endpoint being deleted",
  "콜드 리더 인스턴스의 버퍼풀을 프로덕션 트래픽 전에 예열합니다.":
    "Prewarms a cold reader's buffer pool before production traffic reaches it.",
  "CREATE EXTENSION(pg_prewarm/pg_buffercache)은 writer에서 실행되는 DDL입니다.":
    "CREATE EXTENSION (pg_prewarm/pg_buffercache) is DDL and runs on the writer.",
  "pg_prewarm은 대상 리더에 상당한 읽기 I/O를 유발합니다.":
    "pg_prewarm drives substantial read I/O on the target reader.",
  "엔드포인트 지정 시 예열 중 리더를 제외했다 재편입. 라우팅이 잠시 바뀝니다.":
    "With an endpoint given, the reader is excluded during prewarm and re-added after. Routing changes briefly.",
  "대상이 writer가 아닌 reader 인스턴스인지 확인":
    "Confirm the target is a reader instance, not the writer",
  "엔드포인트를 지정하면 제외→예열→재편입이 자동으로 처리됩니다":
    "Give an endpoint and the exclude, prewarm, re-add cycle is handled automatically",
  "top_n이 과도하면 예열에 시간이 오래 걸립니다(상한 적용)":
    "An oversized top_n makes prewarm take a long time (a cap applies)",
  "리더 인스턴스를 추가해 읽기 용량을 확장합니다 (scale-out).":
    "Adds a reader instance to expand read capacity (scale-out).",
  "신규 인스턴스는 과금 대상이며 생성에 수 분이 걸립니다.":
    "The new instance is billable and takes several minutes to create.",
  "생성될 인스턴스 클래스가 아래에 명시되어 있습니다. 이 클래스로 승인되고 생성됩니다.":
    "The instance class to be created is stated below. That is the class you approve and get.",
  "명시된 인스턴스 클래스가 필요한 읽기 용량과 비용에 적정한지 확인":
    "Confirm the stated instance class fits the read capacity you need and the cost",
  "리더 인스턴스를 삭제해 읽기 용량을 축소합니다 (scale-in).":
    "Deletes a reader instance to shrink read capacity (scale-in).",
  "삭제는 비가역입니다.": "Deletion is irreversible.",
  "writer와 클러스터의 마지막 인스턴스는 보호되어 삭제되지 않습니다.":
    "The writer and the cluster's last instance are protected and will not be deleted.",
  "삭제 대상 리더로 가는 커스텀 엔드포인트/커넥션이 없는지 확인":
    "Confirm no custom endpoint or connection points at the reader being deleted",
  "삭제 후 남은 리더가 읽기 부하를 감당할 수 있는지 확인":
    "Confirm the remaining readers can carry the read load afterwards",
  "리더를 추가하고, available되면 버퍼풀 예열 승인을 자동으로 대기열에 올립니다 (scale-out + 자동 예열, 2단계 승인 중 1단계).":
    "Adds a reader, then auto-queues a buffer pool prewarm approval once it is available (scale-out plus auto prewarm, step 1 of a 2-step approval).",
  "이 승인은 리더 생성만 합니다. 예열은 별도 2차 승인이 필요합니다.":
    "This approval only creates the reader. Prewarm needs a separate second approval.",
  "리더가 available되면 예열 승인이 자동으로 승인 대기열에 나타납니다":
    "Once the reader is available, the prewarm approval appears in the queue automatically",
  "독립형(비-Aurora) RDS 인스턴스를 재부팅합니다.":
    "Reboots a standalone (non-Aurora) RDS instance.",
  "재부팅 동안 짧은 다운타임이 발생합니다(수 분).":
    "A reboot causes brief downtime (a few minutes).",
  "Multi-AZ가 아니면 재부팅 중 연결이 끊깁니다.":
    "Without Multi-AZ, connections drop during the reboot.",
  "Aurora 클러스터 멤버 인스턴스는 이 툴로 재부팅할 수 없습니다(거부됨).":
    "An Aurora cluster member cannot be rebooted with this tool (it is refused).",
  "Multi-AZ 여부 확인: 아니라면 연결 끊김에 대비":
    "Check for Multi-AZ: without it, prepare for dropped connections",
  "저트래픽 시간대 권장": "Low-traffic window recommended",
  "독립형(비-Aurora) RDS 인스턴스의 수동 스냅샷(백업)을 생성합니다.":
    "Creates a manual snapshot (backup) of a standalone (non-Aurora) RDS instance.",
  "비파괴적: 무중단, 데이터 변경 없음. 스토리지 비용만 소폭 발생.":
    "Non-destructive: no outage, no data change. A small storage cost only.",
  "스냅샷 식별자는 이 승인 시점에 고정됩니다(재승인 시 새 이름으로 생성)":
    "The snapshot identifier is fixed at this approval (re-approving creates a new name)",
  "DocumentDB 프로파일러를 켜거나 끕니다. 커스텀 클러스터 파라미터 그룹의 profiler / profiler_threshold_ms / profiler_sampling_rate를 변경하고, profiler 로그 타입을 CloudWatch Logs(/aws/docdb/<cluster>/profiler)로 export합니다.":
    "Turns the DocumentDB profiler on or off. Changes profiler / profiler_threshold_ms / profiler_sampling_rate in the custom cluster parameter group and exports the profiler log type to CloudWatch Logs (/aws/docdb/<cluster>/profiler).",
  "클러스터 파라미터 그룹은 공유 자원입니다. 같은 그룹을 쓰는 다른 모든 클러스터에 같은 변경이 적용됩니다(아래 parameter_group 확인 필수).":
    "A cluster parameter group is a shared resource. Every other cluster on the same group gets the same change (you must check parameter_group below).",
  "threshold_ms를 100ms 미만으로 낮추면 처리량이 높은 클러스터에서 성능 문제가 발생할 수 있습니다(AWS 권장: 500ms에서 시작).":
    "Dropping threshold_ms below 100ms can cause performance problems on a high-throughput cluster (AWS recommends starting at 500ms).",
  "프로파일러 로그는 CloudWatch Logs 수집과 보관 비용이 발생합니다.":
    "Profiler logs incur CloudWatch Logs ingestion and retention cost.",
  "AWS 기본(default.*) 파라미터 그룹은 수정할 수 없어 거부됩니다. 커스텀 그룹을 먼저 연결해야 합니다.":
    "An AWS default (default.*) parameter group cannot be modified, so it is refused. Attach a custom group first.",
  "아래 parameter_group이 이 클러스터 전용 그룹인지 확인, 공유 그룹이면 영향 범위를 먼저 파악":
    "Confirm the parameter_group below is dedicated to this cluster; if it is shared, map the blast radius first",
  "승인은 {enabled, threshold_ms, sampling_rate}와 파라미터 그룹까지 묶여 있어, 승인 후 그룹이 바뀌면 실행이 거부됩니다":
    "The approval is bound to {enabled, threshold_ms, sampling_rate} and to the parameter group, so if the group changes after approval the run is refused",
  "느린 op는 Mongo의 system.profile이 아니라 /aws/docdb/<cluster>/profiler 로그 그룹에서 확인합니다":
    "Slow ops show up in the /aws/docdb/<cluster>/profiler log group, not in Mongo's system.profile",
  "DocumentDB 컬렉션에 인덱스를 생성합니다 (background=true).":
    "Creates an index on a DocumentDB collection (background=true).",
  "background 빌드라 primary를 차단하지는 않지만, 대형 컬렉션에서는 I/O와 스토리지 사용이 늘어납니다.":
    "A background build does not block the primary, but on a large collection it raises I/O and storage use.",
  "복합 인덱스의 필드 순서는 의미가 있습니다. 승인된 순서 그대로 생성됩니다.":
    "Field order matters in a compound index. It is created exactly in the approved order.",
  "인덱스 삭제는 이 도구의 범위가 아닙니다(되돌리려면 별도 작업).":
    "Dropping an index is out of scope for this tool (undoing it is a separate task).",
  "같은 이름의 인덱스가 이미 있으면 변경 없이 종료됩니다(멱등)":
    "If an index of the same name exists, it exits with no change (idempotent)",
  "키 순서와 방향이 실제 쿼리 패턴과 맞는지 확인":
    "Confirm the key order and direction match the real query pattern",
  "ElastiCache replication group의 노드 타입(인스턴스 클래스)을 변경합니다.":
    "Changes the node type (instance class) of an ElastiCache replication group.",
  "스케일 업/다운 중 페일오버가 발생해 짧은 연결 끊김이 생길 수 있습니다.":
    "A scale up or down can trigger a failover and a brief connection drop.",
  "노드 타입에 따라 시간당 비용이 달라집니다.":
    "Hourly cost changes with the node type.",
  "다운사이즈는 메모리가 줄어 eviction과 OOM 위험이 커집니다.":
    "Downsizing cuts memory and raises the risk of eviction and OOM.",
  "현재 사용 메모리와 evictions 대비 목표 노드의 메모리가 충분한지 확인":
    "Confirm the target node has enough memory for current usage and evictions",
  "저트래픽 시간대 권장 (Multi-AZ면 페일오버로 흡수)":
    "Low-traffic window recommended (Multi-AZ absorbs it through failover)",
  "ElastiCache(Redis/Valkey) 수동 스냅샷을 생성합니다.":
    "Creates a manual ElastiCache (Redis/Valkey) snapshot.",
  "비파괴적입니다(데이터 변경 없음). 스냅샷 스토리지 비용만 발생합니다.":
    "Non-destructive (no data change). Snapshot storage cost only.",
  "단일 노드 클러스터에서는 스냅샷 중 메모리와 성능에 일시적 영향이 있을 수 있습니다.":
    "On a single-node cluster the snapshot can briefly affect memory and performance.",
  "Memcached는 스냅샷을 지원하지 않습니다.":
    "Memcached does not support snapshots.",
  "가능하면 replica가 있는 구성에서 실행(replica에서 백업)":
    "Run it where a replica exists if you can (the backup is taken on the replica)",
  "스냅샷 이름은 이 승인 시점에 고정됩니다":
    "The snapshot name is fixed at this approval",
  "replication group의 primary 노드를 재부팅합니다.":
    "Reboots the primary node of a replication group.",
  "재부팅 동안 짧은 중단이 발생합니다.": "A reboot causes a brief outage.",
  "Redis는 재부팅 시 캐시가 비워질 수 있어(구성에 따라) 재예열 동안 백엔드 부하가 늘어납니다.":
    "Redis may come back with an empty cache (depending on config), so backend load rises while it refills.",
  "파라미터 변경 적용 목적이면 대상 노드가 맞는지 확인해야 합니다.":
    "If the point is to apply a parameter change, you must confirm the target node is correct.",
  "캐시 미스 폭주를 감당할 수 있는 시간대인지 확인":
    "Confirm the window can absorb a burst of cache misses",
  "Multi-AZ 자동 페일오버 여부 확인":
    "Check whether Multi-AZ auto failover is on",
  "replication group의 노드 그룹에 대해 자동 페일오버를 테스트합니다.":
    "Tests auto failover on a node group of a replication group.",
  "실제 페일오버입니다. primary가 replica로 승격되며 수 초간 쓰기가 실패할 수 있습니다.":
    "This is a real failover. A replica is promoted to primary and writes can fail for a few seconds.",
  "replica가 없는 노드 그룹에서는 실행되지 않습니다.":
    "It will not run on a node group with no replica.",
  "Memcached는 페일오버를 지원하지 않습니다.":
    "Memcached does not support failover.",
  "애플리케이션의 재연결과 재시도 로직이 준비됐는지 확인":
    "Confirm the application's reconnect and retry logic is ready",
  "DR 훈련 목적이면 저트래픽 시간대 권장":
    "For a DR drill, a low-traffic window is recommended",
  "독립형(비-Aurora) RDS 인스턴스의 컴퓨트 클래스를 변경합니다(ApplyImmediately).":
    "Changes the compute class of a standalone (non-Aurora) RDS instance (ApplyImmediately).",
  "ApplyImmediately로 즉시 적용되며 인스턴스 재기동으로 짧은 다운타임 발생.":
    "ApplyImmediately makes it take effect at once, and the instance restart causes brief downtime.",
  "인스턴스 클래스 변경은 시간당 과금이 달라집니다.":
    "An instance class change changes the hourly bill.",
  "승인 이후 클래스가 드리프트되면 안전을 위해 변경이 거부됩니다.":
    "If the class drifts after approval, the change is refused for safety.",
  "target class / current class(아래 표시)를 확인: 의도한 방향(업/다운사이즈)인지":
    "Check target class / current class (shown below): is the direction (up or down) the one you meant",
  "Multi-AZ 여부 확인: 아니라면 재기동 중 연결 끊김":
    "Check for Multi-AZ: without it, connections drop during the restart",
  "독립형(비-Aurora) RDS 인스턴스의 DB 파라미터 그룹에서 파라미터 1개를 변경합니다.":
    "Changes one parameter in the DB parameter group of a standalone (non-Aurora) RDS instance.",
  "파라미터 그룹은 여러 인스턴스가 공유할 수 있습니다. 같은 그룹을 쓰는 모든 인스턴스에 적용됩니다.":
    "A parameter group can be shared by several instances. It applies to every instance on that group.",
  "dynamic 파라미터는 ApplyMethod=immediate로 즉시 반영됩니다(재시작 없이 동작이 바뀝니다).":
    "A dynamic parameter applies at once with ApplyMethod=immediate (behavior changes with no restart).",
  "static 파라미터는 pending-reboot로 등록만 되며, 인스턴스 재시작 전까지 동작값은 바뀌지 않습니다.":
    "A static parameter is only queued as pending-reboot; the effective value does not change until the instance restarts.",
  "승인 이후 인스턴스의 파라미터 그룹이 바뀌면 안전을 위해 변경이 거부됩니다.":
    "If the instance's parameter group changes after approval, the change is refused for safety.",
  "parameter / value / parameter group(아래 표시)을 확인, 특히 그룹을 공유하는 인스턴스가 있는지":
    "Check parameter / value / parameter group (shown below), especially whether other instances share the group",
  "메모리 관련 파라미터(innodb_buffer_pool_size, 'max server memory (mb)')는 인스턴스 메모리 대비 합계를 먼저 계산":
    "For memory parameters (innodb_buffer_pool_size, 'max server memory (mb)'), add up the total against instance memory first",
  "SQL Server 파라미터 이름은 RDS API 표기(소문자)로 기록됩니다. Configuration 탭의 sys.configurations 표기('max server memory (MB)')와 대소문자만 다르며 같은 파라미터입니다":
    "SQL Server parameter names are recorded in RDS API form (lower case). The Configuration tab shows the sys.configurations form ('max server memory (MB)'): the same parameter, differing only in case",
  "static 파라미터면 재시작 윈도우를 함께 계획(reboot_rds_instance)":
    "If the parameter is static, plan the restart window too (reboot_rds_instance)",

  // ── cost ─────────────────────────────────────────────────────────────────
  "커밋 할인 (RI/SP)": "Commitments (RI/SP)",
  "DBOps 플랫폼": "DBOps platform",
  토큰: "Tokens",
  "태그 활성화 후 약 24시간 기다리세요. (과거 비용은 소급 반영되지 않으며, 활성화 이후 호출분부터 집계됩니다.)":
    "Activate the tag, then wait about 24 hours. (Past cost is not backfilled; only calls made after activation are counted.)",
  추이: "Trend",
  "일별 Bedrock 사용액": "Daily Bedrock spend",
  "세부 분석": "Breakdown",
  "모델 + token 방향별 비용": "Cost by model and token direction",
  "동작 원리": "How it works",
  "CDK가 deploy 시점에 각 Claude 모델별로 Application Inference Profile (AIP) 6종을 만들고":
    "At deploy time CDK creates 6 Application Inference Profiles (AIP), one per Claude model, and applies the tags",
  "태그를 부여합니다.": "to each.",
  'AgentCore Runtime이 base model 대신 AIP ARN으로 invoke하면 모든 토큰 비용이 자동으로 태그에 attributed됩니다. AWS Billing console에서 cost allocation tag로 "Application"을 활성화한 뒤 24시간 후부터 이 대시보드가 실 비용을 보여줍니다.':
    'When AgentCore Runtime invokes the AIP ARN instead of the base model, every token cost is attributed to those tags automatically. Activate "Application" as a cost allocation tag in the AWS Billing console and this dashboard shows real cost 24 hours later.',
  "플랫폼 비용 조회 실패: {n}": "Platform cost fetch failed: {n}",
  "USD, {n}일 윈도우": "USD, {n} day window",
  "일 평균": "Daily average",
  "윈도우 평균": "avg over window",
  "월 환산": "Monthly projection",
  "일 평균 × 30": "daily avg × 30",
  "일별 플랫폼 운영비": "Daily platform spend",
  "아직 집계된 비용이 없습니다": "no spend recorded yet",
  비용: "cost",
  "세부 분해": "Breakdown",
  "서비스별 플랫폼 비용": "Platform cost by service",
  "서비스별 분해 데이터가 없습니다.": "No per-service breakdown yet.",
  "Cost Explorer가 활성화돼 있는지 확인한 뒤 ~24h 후 다시 확인하세요.":
    "Check that Cost Explorer is enabled, then look again in about 24h.",
  "일별 Aurora / RDS 사용액": "Daily Aurora / RDS spend",
  클러스터별: "Per cluster",
  "Aurora 클러스터별 비용": "Cost per Aurora cluster",
  "cost-allocation 태그가 활성화된 경우에만 클러스터 단위로 분리됩니다.":
    "Split per cluster only when a cost-allocation tag is active.",
  "클러스터별 비용 분리는 cost-allocation 태그(예: dbops:cluster)를 활성화하고 Aurora 클러스터에 부여해야 합니다.":
    "Per-cluster cost needs a cost-allocation tag (for example dbops:cluster) activated and applied to the Aurora clusters.",
  "사용 유형별 비용": "Cost by usage type",
  "Aurora/RDS 비용은 AWS Cost Explorer GetCostAndUsage를 SERVICE =":
    "Aurora / RDS cost comes from AWS Cost Explorer GetCostAndUsage filtered on SERVICE =",
  "로 필터링해 집계합니다. 사용 유형(USAGE_TYPE)별 분해는 추가 비용 없이 바로 제공됩니다.":
    "and the USAGE_TYPE breakdown comes with it at no extra charge.",
  "클러스터별 분리는 Aurora 클러스터가 DBOps 소유가 아니므로 자동 태깅되지 않습니다. AWS Billing console에서 cost-allocation 태그(예":
    "Per-cluster split is not automatic, because the Aurora clusters are not owned by DBOps. Activate a cost-allocation tag (for example",
  ")를 활성화하고 클러스터에 부여하면 ~24시간 후부터 클러스터별 비용이 채워집니다. 활성화 이전 비용은 소급 적용되지 않으며, resource-level CE 데이터(추가 과금)는 사용하지 않습니다.":
    ") in the AWS Billing console and apply it to the clusters, and per-cluster cost fills in about 24 hours later. Cost from before activation is not backfilled, and resource-level CE data (billed extra) is not used.",
  "일별 ElastiCache 사용액": "Daily ElastiCache spend",
  "ElastiCache 클러스터별 비용": "Cost per ElastiCache cluster",
  "클러스터별 비용 분리는 cost-allocation 태그(예: dbops:cluster)를 활성화하고 ElastiCache 클러스터에 부여해야 합니다.":
    "Per-cluster cost needs a cost-allocation tag (for example dbops:cluster) activated and applied to the ElastiCache clusters.",
  "ElastiCache 비용은 AWS Cost Explorer GetCostAndUsage를 SERVICE =":
    "ElastiCache cost comes from AWS Cost Explorer GetCostAndUsage filtered on SERVICE =",
  "클러스터별 분리는 ElastiCache 클러스터가 DBOps 소유가 아니므로 자동 태깅되지 않습니다. AWS Billing console에서 cost-allocation 태그(예":
    "Per-cluster split is not automatic, because the ElastiCache clusters are not owned by DBOps. Activate a cost-allocation tag (for example",
  모델별: "By model",
  "토큰 사용량 (모델별)": "Token usage by model",
  모델: "model",
  "입력 토큰": "input tokens",
  "출력 토큰": "output tokens",
  합계: "total",
  비율: "share",
  "일별 토큰 사용량": "Daily token usage",
  "일별 토큰 데이터가 없습니다.": "No daily token data yet.",
  입력: "input",
  출력: "output",
  만료됨: "expired",
  "커밋 할인 현황 조회 실패: {n}": "Commitments fetch failed: {n}",
  "보유 RI": "RIs held",
  "활성 Reserved Instance 수량": "active Reserved Instances",
  "만료 임박 (30일)": "Expiring (30d)",
  "30일 내 만료되는 RI 수량": "RIs expiring within 30 days",
  "미사용 추정": "Likely unused",
  "실행 인스턴스 없는 RI (추정)": "RIs with no running instance (estimate)",
  커버리지: "Coverage",
  "RI / Savings Plan 커버리지": "RI / Savings Plan coverage",
  "허브 계정 기준, 최근 30일 Cost Explorer 커버리지입니다.":
    "Cost Explorer coverage for the last 30 days, hub account only.",
  "CE 커버리지 미조회: 권한/데이터 없음":
    "CE coverage unavailable: no permission or no data",
  "Savings Plan (전체 컴퓨트)": "Savings Plan (all compute)",
  "Savings Plan 미조회": "Savings Plan unavailable",
  "활성 RI 목록": "Active RIs",
  "만료 임박순. D-day가 amber(≤30일)/rose(≤7일)면 갱신 또는 스케일링 결정을 재검토하세요.":
    "Soonest expiry first. An amber (≤30d) or rose (≤7d) D-day is a cue to revisit renewal or a scaling decision.",
  계정: "account",
  리전: "Region",
  클래스: "class",
  수량: "count",
  "약정 유형": "offering type",
  만료: "expiry",
  "활성 Savings Plan": "Active Savings Plans",
  "Savings Plan 미조회: API/권한 없음 또는 이 계정에 Savings Plan이 없습니다.":
    "Savings Plan unavailable: no API access or permission, or this account has none.",
  "활성 Savings Plan이 없습니다.": "No active Savings Plan.",
  이상치: "Anomalies",
  "일별 사용액 spike 감지": "Daily spend spike detection",
  "7일 baseline 대비 z-score > 2 + relative 50%↑ + 절대 차이 $0.5↑ 모두 만족하는 날을 표시합니다.":
    "Flags a day only when all three hold against the 7 day baseline: z-score > 2, relative 50%↑, and absolute delta $0.5↑.",
  "대형 spike는 트렌드 차트의 점으로도 표시됩니다":
    "large spikes are also dotted on the trend chart",
  "Application=DBOps cost allocation tag 활성화 한 번이면 끝:":
    "One activation of the Application=DBOps cost allocation tag is all it takes:",
  "위 단계를 따라가세요": "follow the steps above",
  "클릭해서 단계 보기": "click to see the steps",
  "AWS Billing → Cost allocation tags 페이지 열기":
    "Open AWS Billing → Cost allocation tags",
  "관리자 권한이 필요합니다(AWS Organizations 환경이면 management account).":
    "Needs admin rights (the management account under AWS Organizations).",
  "직접 이동": "Go there",
  탭에서: "tab, find",
  "찾고 체크 →": "and check it →",
  "여유가 되면": "If you have a moment, also check",
  "도 함께 체크해 env별(dev/prod) 분리도 활성화하세요.":
    ", to split cost per env (dev/prod) too.",
  "~24시간 대기 후 이 페이지 새로고침": "Wait ~24 hours, then reload this page",
  "AWS가 새 데이터를 인덱싱하면 차트와 모델별 분해표가 자동으로 채워집니다.":
    "Once AWS indexes the new data, the chart and the per-model table fill in on their own.",
  "활성화 시점 이전 비용은 소급 적용되지 않습니다.":
    "Cost from before activation is never backfilled.",
  "과거 spend는 영구히 untagged로 남습니다.":
    "Past spend stays untagged forever.",
  "AWS는 보안상 cost allocation tag 활성화를 관리자 콘솔 액션으로만 허용합니다. CDK도 API도 활성화 자체는 못 합니다. 한 번 활성화하면 이후 모든 DBOps 비용이 자동 attribute 됩니다.":
    "For security AWS only allows a cost allocation tag to be activated from the admin console. Neither CDK nor an API can do the activation itself. Activate it once and every DBOps cost is attributed automatically from then on.",
  "Aurora (캐시 DB와 샘플)": "Aurora (cache DB and samples)",

  // ── scaleout ─────────────────────────────────────────────────────────────
  "조회 실패": "Fetch failed",
  "취소되었습니다.": "Cancelled.",
  "다른 예열 설정(top_n, 엔드포인트)을 원하면 이 작업을 취소한 뒤 채팅에서 prewarm_reader로 재요청하세요. 리더는 유지됩니다.":
    "For different prewarm settings (top_n, endpoint), cancel this op and ask for prewarm_reader again in Chat. The reader stays.",
  "스케일 작업을 불러오지 못했습니다": "Could not load the scale operations",
  "네트워크 또는 인증 문제일 수 있습니다. 빈 목록이 아니라 조회 실패 상태입니다.":
    "This may be a network or auth problem. It is a failed fetch, not an empty list.",
  "리더 인스턴스": "reader instance",
  상태: "Status",
  엔드포인트: "endpoint",
  생성: "Create",
  액션: "action",
  "취소할까요?": "Cancel it?",
  확인: "Confirm",
  아니오: "No",
  취소: "Cancel",
  "AZ 스케일아웃 (선점)": "AZ scale-out (preemptive)",
  "정상 AZ에 리더를 미리 분산 배치합니다. 위험이 예상되는 AZ 하나를 제외하고, 나머지 AZ에 리더 {n}대를 라운드로빈으로 계획합니다. 각 리더는 과금 대상인 개별 인스턴스이며, 승인 센터에서 하나씩 검토하고 승인해야 실제로 생성됩니다.":
    "Spreads readers across the healthy AZs ahead of time. It excludes one AZ you expect trouble in and plans {n} readers round-robin over the rest. Each reader is a separate billable instance, and none is created until you review and approve it in the Approval Center.",
  "제외할 AZ": "AZ to exclude",
  "AZ 불러오는 중…": "Loading AZs…",
  "(제외 없음)": "(exclude none)",
  "리더 수 (1-10)": "Readers (1-10)",
  "생성 중…": "Creating…",
  "리더 추가 승인 요청 생성": "Create add-reader approval requests",
  "승인 센터에서 검토/승인": "Review and approve in the Approval Center",

  // ── simulator ────────────────────────────────────────────────────────────
  "버전 업그레이드 시뮬레이션": "Version upgrade simulation",
  "현재 {engine} 클러스터에서 target 버전으로 업그레이드할 때의 호환성, 메서드별 시간/다운타임/리스크, 단계별 실행 계획을 한 번에 추정합니다.":
    "Estimates, for this {engine} cluster, the compatibility of a target version plus the time, downtime and risk of each method, and a step by step plan.",
  "예: 16.4": "e.g. 16.4",
  "추정 중…": "Estimating…",
  "시뮬레이션 실행": "Run simulation",
  "호환 가능": "Compatible",
  "직접 업그레이드 불가": "No direct upgrade",
  "유효한 직접 대상: ": "Valid direct targets: ",
  메이저: "Major",
  마이너: "Minor",
  "{n}단계": "{n} steps",
  "클릭하여 추정 근거 보기": "Click to see how this was estimated",
  "★ 권장": "★ Recommended",
  "~{n}분": "~{n} min",
  "{a}-{b}분": "{a}-{b} min",
  "{method} ~{n}분 추정 근거": "{method} ~{n} min: how it was estimated",
  "추정 범위 {a}-{b}분": "Estimated range {a}-{b} min",
  ", 신뢰도 {c}": ", confidence {c}",
  "권장 근거": "Why recommended",
  "({a}-{b}분)": "({a}-{b} min)",
  "다운타임 {n}": "Downtime {n}",
  "target 버전을 입력하고": "Enter a target version and press",
  "을 누르세요.": ".",
  "파라미터 변경 시뮬레이션": "Parameter change simulation",
  "동적/정적 여부, 재시작 필요 여부, 영향 영역을 즉시 추정합니다.":
    "Estimates at once whether the parameter is dynamic or static, whether a restart is needed, and what it affects.",
  "예: work_mem": "e.g. work_mem",
  "예: 16MB": "e.g. 16MB",
  "(엔진 기본값)": "(engine default)",
  "수정 불가": "Not modifiable",
  "허용: {n}": "Allowed: {n}",
  "카탈로그 미등록: 결과는 보수적 기본값입니다":
    "Not in the catalog: these are conservative defaults",
  "파라미터명과 값을 입력하세요. 등록된":
    "Enter a parameter name and a value. Autocomplete covers the",
  "개의 파라미터에 대해 자동완성이 동작합니다.": "parameters in the catalog.",
  "스케일링 비용 시뮬레이션": "Scaling cost simulation",
  "Aurora Serverless v2(ACU min/max) 또는 프로비저닝 인스턴스 클래스를 조정했을 때의 월 비용 변화를, 클러스터 리전과 에디션(I/O-Optimized) 기준 실시간 Pricing 단가로 추정합니다.":
    "Estimates the monthly cost change from adjusting Aurora Serverless v2 (ACU min/max) or the provisioned instance class, priced with live AWS Pricing rates for the cluster region and edition (I/O-Optimized).",
  "모드 감지 실패. 아래 오류 확인":
    "Mode detection failed. See the error below",
  "클러스터 모드 감지 중…": "Detecting cluster mode…",
  "비용 추정": "Estimate cost",
  현재: "Current",
  제안: "Proposed",
  "월 차액": "Monthly delta",
  "관측 ACU {n} 기준": "Based on observed ACU {n}",
  "중간값 ACU 추정 (관측 데이터 없음)":
    "Midpoint ACU estimate (no observed data)",
  "현재 구성과 월 비용을 불러오는 중입니다. 클러스터 모드(Serverless v2 / 프로비저닝)에 맞는 입력이 곧 표시됩니다.":
    "Loading the current configuration and monthly cost. The inputs for this cluster mode (Serverless v2 or provisioned) will appear shortly.",
  "DDL 영향 시뮬레이션": "DDL impact simulation",
  "ALTER / CREATE INDEX 등 DDL을 실행했을 때의 락 타입, 예상 소요 시간, 디스크 추가 사용량을 추정합니다.":
    "Estimates the lock type, the expected duration and the extra disk a DDL such as ALTER or CREATE INDEX would need.",
  "샘플 채우기": "Fill with a sample",
  "영향 추정": "Estimate impact",
  "추정 처리량 ~{n} MB/s": "Estimated throughput ~{n} MB/s",
  "추정 근거": "How this was estimated",
  "DDL SQL을 입력하면 테이블 크기 추정과 락/온라인 가능 여부를 분석합니다.":
    "Enter DDL SQL for a table size estimate and the lock / online-DDL verdict.",
  "노드 리사이즈 비용 시뮬레이션": "Node resize cost simulation",
  "ElastiCache 노드 타입과 노드 수를 변경했을 때의 월 비용 변화를 리전별 실시간 AWS Pricing 단가로 추정합니다. 노드-시간 비용만 대상이며 데이터 전송, 스냅샷, 예약 노드는 제외합니다.":
    "Estimates the monthly cost change from a different ElastiCache node type or node count, priced with live AWS Pricing rates per region. Node-hours only: data transfer, snapshots and reserved nodes are excluded.",
  "새 노드 타입": "New node type",
  "예: cache.r7g.large": "e.g. cache.r7g.large",
  "노드 수": "Node count",
  "× {n} 노드": "× {n} nodes",
  "부분 추정: 일부 단가 미조회":
    "Partial estimate: some rates were not retrieved",
  "현재 ${n}/hr/node": "current ${n}/hr/node",
  "제안 ${n}/hr/node": "proposed ${n}/hr/node",
  "노드 타입 또는 노드 수를 입력하고":
    "Enter a node type or a node count and press",
  "을 누르세요. 입력 없이 실행하면 현재 구성 기준 월 비용을 조회합니다.":
    ". Run it with no input to look up the monthly cost of the current configuration.",
  "인스턴스 라이트사이징과 비용 시뮬레이션":
    "Instance right-sizing and cost simulation",
  "최근 CloudWatch 사용률(CPU, 연결, IOPS)을 기준으로 인스턴스 클래스 적정성을 진단하고, AWS Price List 실시간 단가로 현재 대비 제안 인스턴스의 월 비용을 추정합니다.":
    "Diagnoses whether the instance class fits recent CloudWatch utilization (CPU, connections, IOPS), and estimates the monthly cost of the proposed instance against the current one using live AWS Price List rates.",
  "다시 계산": "Recalculate",
  "지금 계산": "Calculate now",
  "CloudWatch 사용률과 AWS Price List 단가를 조회하는 중입니다…":
    "Fetching CloudWatch utilization and AWS Price List rates…",
  "시뮬레이션에 실패했습니다.": "The simulation failed.",
  "{n}h 윈도우": "{n}h window",
  "피크 연결 수": "Peak connections",
  "실시간 가격을 가져오지 못해 추정치입니다.":
    "Live pricing was unavailable, so this is an estimate.",
  "다운사이즈 권장": "Suggest downsize",
  "업사이즈 권장": "Suggest upsize",
  "현재 유지": "Hold",
  높음: "high",
  보통: "medium",
  낮음: "low",
  "신뢰도 높음": "High confidence",
  "신뢰도 보통": "Medium confidence",
  "신뢰도 낮음": "Low confidence",

  // ── compare ──────────────────────────────────────────────────────────────
  인스턴스: "Instance",
  "이 클러스터는 인스턴스가 1대뿐이라 비교할 수 없습니다. 인스턴스가 여러 대인 클러스터를 선택하세요.":
    "This cluster has only one instance, so there is nothing to compare. Pick a cluster with several instances.",
  "비교하려면 서로 다른 인스턴스를 선택하세요.":
    "Pick two different instances to compare.",
  "A({a})와 B({b})가 다른 엔진 패밀리입니다. 같은 패밀리끼리만 비교할 수 있습니다. B를 초기화합니다.":
    "A ({a}) and B ({b}) are different engine families. Only the same family can be compared, so B is reset.",
  "B 초기화": "Reset B",
  "클러스터 목록을 불러오지 못했습니다. 네트워크/세션 문제일 수 있습니다.":
    "Could not load the cluster list. This may be a network or session problem.",
  "Cluster vs Cluster 비교에는 등록된 클러스터가 2개 이상 필요합니다.":
    "Cluster vs Cluster needs at least 2 registered clusters. Open",
  "페이지에서 추가 등록하거나 샘플 클러스터를 생성하세요.":
    "to register another, or create a sample cluster.",
  "(인스턴스 없음)": "(no instances)",

  // ── workload-diff ────────────────────────────────────────────────────────
  "Before (기준 시점)": "Before (baseline)",
  "After (비교 시점)": "After (comparison)",
  "Regression 임계 (%)": "Regression threshold (%)",
  "비교 중…": "Comparing…",
  "비교 실행": "Run comparison",
  "🆕 New queries: before엔 없던 쿼리": "🆕 New queries: not present before",
  "👻 Disappeared: after엔 사라진 쿼리 (참고용)":
    "👻 Disappeared: gone after, for reference",

  // ── alerts ───────────────────────────────────────────────────────────────
  "이 cluster + metric 조합으로 수집된 metric_snapshot이 없습니다. 클러스터 등록 상태와 ETL 파이프라인을 확인하세요":
    "No metric_snapshot has been collected for this cluster + metric pair. Check the cluster registration and the ETL pipeline",
  "읽기 전용, viewer 권한. 쓰기 액션은 숨겨집니다":
    "Read only, viewer role. Write actions are hidden",
  "새 규칙": "New rule",
  "알림 임계값 정의": "Define an alert threshold",
  단순: "Simple",
  "복합 (AND/OR)": "Compound (AND/OR)",
  "규칙 추가": "Add rule",
  "Template (선택)": "Template (optional)",
  "결합 logic": "Join logic",
  "Operands: 모두": "All operands joined with",
  "로 결합": "logic",
  "평가 윈도우 (분): 이 시간 내 데이터로 agg 계산":
    "Evaluation window (minutes): agg is computed over the data inside it",
  "이 조건 삭제": "Remove this condition",
  "+ 조건 추가 ({n}/8)": "+ Add condition ({n}/8)",
  "규칙 이름 (선택)": "Rule name (optional)",
  "자동 생성됨: 첫 operand + AND/OR + N":
    "Auto-generated: first operand + AND/OR + N",
  "복합 규칙 추가": "Add compound rule",
  "평가 시점:": "Evaluated as:",
  "알림 채널": "Alert channels",
  구독자: "Subscribers",
  "SNS 토픽 {n}을 통해 fan-out": "Fanned out through SNS topic {n}",
  "SNS 토픽이 설정되어 있지 않음": "No SNS topic configured",
  "https://<조직>.webhook.office.com/webhookb2/...":
    "https://<org>.webhook.office.com/webhookb2/...",
  "Incoming Webhook URL 또는 Workflows URL 모두 사용 가능합니다.":
    "Either an Incoming Webhook URL or a Workflows URL works.",
  "구독 추가": "Add subscriber",
  활성: "Enabled",
  "구독 해지": "Unsubscribe",
  "아직 구독자가 없습니다. 이메일/SMS/Slack webhook을 추가하면 알림 발생 시 전달됩니다.":
    "No subscribers yet. Add an email, SMS or Slack webhook and alerts get delivered there.",
  규칙: "Rule",
  "등록된 알림 규칙 {n}개": "{n} alert rules",
  "evaluator는 5분마다 실행되며, metric 데이터가 stale이거나 없는 규칙은 건너뜁니다.":
    "The evaluator runs every 5 minutes and skips any rule whose metric data is stale or missing.",
  "전체 스누즈 대상 클러스터": "Cluster to snooze in bulk",
  "30분": "30 min",
  "1시간": "1 hour",
  "4시간": "4 hours",
  "24시간": "24 hours",
  "이 클러스터의 모든 규칙을 스누즈 (롤/인스턴스 단위 스누즈는 미지원, 클러스터 단위)":
    "Snooze every rule on this cluster (cluster level only, no per-role or per-instance snooze)",
  "클러스터 전체 스누즈": "Snooze whole cluster",
  "전체 해제": "Clear all",
  데이터: "Data",
  "마지막 발생": "Last fired",
  작업: "Actions",
  중지: "Stopped",
  "발화 이력 없음": "Never fired",
  "스누즈됨 ~{n}까지": "Snoozed until {n}",
  "이 룰이 발화한 시점의 슬로우 쿼리, 이벤트, 동시 알림":
    "Slow queries, events and concurrent alerts around the moment this rule fired",
  영향도: "Impact",
  "알림 스누즈": "Snooze alert",
  스누즈: "Snooze",
  해제: "Clear",
  "Slack 양방향 Ack 설정": "Slack two-way Ack setup",
  "Slack 알림 메시지의 ✓ Ack 버튼을 활성화하려면 Slack 앱 측 설정이 한 번 필요합니다.":
    "Enabling the ✓ Ack button in Slack alerts takes a one-time setup on the Slack app side.",
  "× 가이드 닫기": "× Close guide",
  "셋업 가이드 열기": "Open setup guide",
  "Slack 앱 생성": "Create the Slack app",
  "에서 `From scratch`로 새 앱 생성 → 워크스페이스 선택.":
    ": create a new app with `From scratch`, then pick your workspace.",
  "Signing Secret 복사": "Copy the Signing Secret",
  "앱 페이지 좌측 메뉴 → `Basic Information` → `App Credentials` 섹션의 `Signing Secret`을 복사해서 `cdk/config/settings.py`의 `SLACK_SIGNING_SECRET`에 붙여넣기.":
    "In the app's left menu, open `Basic Information` → `App Credentials`, copy `Signing Secret` and paste it into `SLACK_SIGNING_SECRET` in `cdk/config/settings.py`.",
  "Interactivity Request URL 등록": "Register the Interactivity Request URL",
  "좌측 메뉴 → `Interactivity & Shortcuts`를 켜고 `Request URL`에 아래 주소를 붙여넣기:":
    "In the left menu, turn on `Interactivity & Shortcuts` and paste the address below into `Request URL`:",
  "(URL 로딩 중…)": "(loading the URL…)",
  "✓ 복사됨": "✓ Copied",
  "클립보드 접근이 차단되었습니다. 위 주소를 직접 선택해 복사하세요.":
    "Clipboard access is blocked. Select the address above and copy it by hand.",
  "Incoming Webhook 추가 + 재배포": "Add an Incoming Webhook, then redeploy",
  "좌측 메뉴 → `Incoming Webhooks`를 켜고 채널 webhook URL 발급 → 위 `Subscribers` 섹션에 protocol `slack-webhook`으로 등록.":
    "In the left menu, turn on `Incoming Webhooks`, issue a channel webhook URL, then register it under `Subscribers` above with protocol `slack-webhook`.",
  "마지막으로 `cdk deploy dbops-dev-agent`로 새 signing secret을 Lambda 환경에 반영.":
    "Finally, run `cdk deploy dbops-dev-agent` to push the new signing secret into the Lambda environment.",
  "동작 확인:": "Check it works:",
  "알림이 한 번 발사되면 Slack 메시지의 `✓ Ack alert` 버튼을 누르세요. 위 룰 테이블에":
    "Once an alert has fired, press `✓ Ack alert` in the Slack message. The rule table above should show an",
  "배지가 즉시 표시되면 정상.": "badge straight away.",
  "트러블슈팅:": "Troubleshooting:",
  "버튼 클릭 시 `SLACK_SIGNING_SECRET not configured` 메시지가 뜨면 2~4단계 중 한 단계가 누락된 상태.":
    "If the button answers `SLACK_SIGNING_SECRET not configured`, one of steps 2 to 4 is still missing.",
  "이 룰은 아직 발화 이력이 없습니다.": "This rule has never fired.",
  "기준 시각": "Centered on",
  ", ±{n}분 윈도우": ", ±{n} min window",
  "윈도우 안에 슬로우 쿼리 기록이 없습니다.":
    "No slow query was recorded inside the window.",
  "동시 이벤트": "Concurrent events",
  "윈도우 안에 운영 이벤트가 없습니다.":
    "No operational event inside the window.",
  "동시 발화 알림 (cascading)": "Alerts that fired together (cascading)",
  "같은 윈도우에 다른 알림은 없었습니다.":
    "No other alert fired in the same window.",
  "CPU 지속 스파이크": "Sustained CPU spike",
  "Connection 폭주": "Connection flood",
  "AAS 과부하": "AAS overload",
  "Deadlock 발생": "Deadlocks firing",

  // ── approval-policies ────────────────────────────────────────────────────
  "* (전체) 또는 특정 cluster id": "* (all) or a single cluster id",
  "= 모든 클러스터에 적용": "= applies to every cluster",
  "* 또는 execute_sql, create_custom_endpoint, add_reader_instance …":
    "* or execute_sql, create_custom_endpoint, add_reader_instance …",
  "승인 요청의 action_type / tool_name과 매칭: SQL과 파라미터뿐 아니라 엔드포인트와 스케일 변경(create_custom_endpoint, add_reader_instance 등)도 지정 가능. 값이 요청의 action_type과":
    "Matched against the request's action_type / tool_name: not only SQL and parameters, endpoint and scale changes (create_custom_endpoint, add_reader_instance and so on) can be named too. The value must",
  "정확히 일치": "match exactly",
  "해야 적용됩니다 (오타 시 정책이 매칭되지 않아 미승인 상태로 남음).":
    " for the policy to apply (a typo means no match, and the request stays unapproved).",
  "쉼표 또는 줄바꿈으로 구분, 최소 1명 필수":
    "Comma or newline separated, at least one required",
  "정책 설명 (선택)": "Policy description (optional)",
  "수정 중: {n}": "Editing {n}",
  "수정 저장": "Save changes",
  승인자: "Approvers",
  없음: "None",
  수정: "Edit",
  "이 정책을 삭제하면 해당 조건의 승인 요청이 기본 승인(모든 관리자)으로 fallback됩니다. 계속할까요?":
    "Deleting this policy falls its approval requests back to the default, every admin. Continue?",
  "정책 매칭 규칙": "Policy matching rules",
  "와일드카드는 모든 cluster_id 또는 action_type에 매칭됩니다.":
    "wildcard matches every cluster_id or action_type.",
  ": cluster_id + action_type 둘 다 구체적인 정책이 우선 적용됩니다.":
    ": a policy that spells out both cluster_id and action_type wins.",
  "정책이 매칭되면": "Once a policy matches,",
  "목록에 없는 관리자는 승인할 수 없습니다":
    "an admin outside its list cannot approve",
  "(명시된 승인자만 가능). 매칭되는 정책이 없으면 기본 동작(모든 관리자 승인 가능)으로 fallback.":
    "(only the listed approvers can). With no matching policy it falls back to the default, where every admin can approve.",
  "은 승인 요청의 action_type / tool_name과 비교합니다. 예:":
    " is compared against the request's action_type / tool_name. For example:",
  "등록된 정책": "Registered policies",
  "{n}개": "{n} total",
  "정책 추가": "Add policy",
  "같은 cluster_id + action_type 조합이 여러 개면, 매칭 시 승인자 목록이 합산됩니다.":
    "When several policies share a cluster_id + action_type, their approver lists are merged on a match.",

  // ── slo ──────────────────────────────────────────────────────────────────
  "데이터를 불러오는 중…": "Loading data…",
  "로딩 중…": "Loading…",
  "target {a}%, 윈도우 {b}d": "target {a}%, {b}d window",
  "avg(mean_time_ms) ≤ {a}ms, 윈도우 {b}d":
    "avg(mean_time_ms) ≤ {a}ms, {b}d window",
  "이 윈도우에서 query_stats 샘플 없음. {n} 확인하세요.":
    "No query_stats sample in this window. Check {n}.",
  "performance_schema 문장 수집(events_statements_*)이 켜져 있는지":
    "whether performance_schema statement collection (events_statements_*) is on",
  "pg_stat_statements 수집이 켜져 있는지":
    "whether pg_stat_statements collection is on",
  "버짓 소진: 신규 배포 보류 권장":
    "Budget spent: consider holding new deploys",
  "버짓 절반 이상 소진: 변경 일정 재검토":
    "Over half the budget spent: revisit the change schedule",
  "버짓 여유": "Budget healthy",
  "일별 타임라인": "Daily timeline",
  "각 칸 = 하루. 좌측이 가장 오래된 날.":
    "One cell per day, oldest on the left.",
  정상: "OK",
  "가용성 미달": "Availability miss",
  "지연 미달": "Latency miss",
  "전 기간 데이터 없음. ETL 수집기가 동작 중인지 확인하세요.":
    "No data for the whole window. Check that the ETL collector is running.",
  "{n}, 데이터 없음": "{n}, no data",

  // ── learning ─────────────────────────────────────────────────────────────
  이력: "Track record",
  "신뢰도 (Wilson)": "Confidence (Wilson)",
  "Fleet 전체: 조치별 효과": "Fleet-wide: outcome by action",
  "최근 평가 결과": "Recent evaluations",

  // ── clusters registry ────────────────────────────────────────────────────
  "클러스터 조회 실패": "Cluster lookup failed",
  "최소 1개 region이 필요합니다.": "At least one region is required.",
  "검색된 Aurora 클러스터가 없습니다.": "No Aurora clusters found.",
  "등록 {a}개, 스킵 {b}개, 실패 {c}개":
    "{a} registered, {b} skipped, {c} failed",
  "실패: {n}": "failed: {n}",
  "일괄 등록에 실패했습니다": "Bulk registration failed",
  "Table name / account_id / region 모두 필요합니다.":
    "Table name, account_id and region are all required.",
  "Cluster identifier / account_id / region 모두 필요합니다.":
    "Cluster identifier, account_id and region are all required.",
  "cluster_id / account_id / region 모두 필요합니다.":
    "cluster_id, account_id and region are all required.",
  "등록됨: {n}, 연결 검증 통과": "Registered: {n}, connection verified",
  "등록은 됐지만 연결 검증 실패: {n}":
    "Registered, but the connection check failed: {n}",
  "AWS console 확인": "check the AWS console",
  "등록됨: {n}": "Registered: {n}",
  "데모용 sample-cluster를 생성합니다. 24시간치 합성 메트릭/쿼리/이상 징후가 캐시 DB에 채워지고, 모든 페이지에서 DEMO 배지로 식별됩니다. 진행할까요?":
    "This creates the demo sample-cluster. 24 hours of synthetic metrics, queries and anomalies are seeded into the cache DB, and every page marks it with a DEMO badge. Proceed?",
  "Sample 클러스터가 준비됐습니다 ({n} 행 시드). Dashboard에서 sample-cluster를 선택해 확인하세요.":
    "Sample cluster is ready ({n} rows seeded). Pick sample-cluster on the Dashboard to see it.",
  "샘플 생성에 실패했습니다": "Sample creation failed",
  "데모 클러스터 {n} 및 합성 데이터 전체를 삭제합니다.":
    "This deletes the demo cluster {n} and all of its synthetic data.",
  "{n}를 레지스트리에서 해제합니다. 캐시 DB의 과거 메트릭은 그대로 남습니다.":
    "This unregisters {n}. Past metrics stay in the cache DB.",
  "{n} 삭제됨.": "{n} deleted.",
  "Fleet 전체 보기 →": "See the whole fleet →",
  "DBOps 전용 read-only 계정 + Secrets Manager 등록 가이드":
    "How to set up a DBOps read-only account and its Secrets Manager secret",
  "📋 설정 가이드": "📋 Setup guide",
  "합성 데이터로 sample-cluster 생성":
    "Create sample-cluster from synthetic data",
  "🎲 샘플 생성": "🎲 Create sample",
  "탐색 닫기": "Close discovery",
  "🔎 클러스터 자동 탐색": "🔎 Discover clusters",
  "viewer, 읽기 전용": "viewer, read-only",
  "일괄 탐색": "bulk discovery",
  "Aurora 클러스터 자동 탐색": "Discover Aurora clusters",
  "현재 계정 또는 cross-account role을 통해 RDS에서 Aurora 클러스터를 자동 enumerate. 선택한 항목만 한 번에 등록합니다.":
    "Enumerate Aurora clusters from RDS in this account or through a cross-account role. Only what you select is registered, in one pass.",
  "role ARN을 비우면 DBOps Lambda의 IAM role로 same-account에서 직접 조회합니다. cross-account의 경우 해당 role이":
    "Leave the role ARN empty and DBOps queries same-account directly with its Lambda IAM role. For cross-account, that role needs",
  "권한을 가져야 합니다.": "permission.",
  "검색 중…": "Searching…",
  "DBOps 플랫폼 자체 리소스입니다 (캐시 DB 또는 컨트롤 플레인 테이블). 모니터링 대상으로 등록할 필요가 없어 자동 선택에서 제외했습니다.":
    "This is a DBOps platform resource (the cache DB or a control-plane table). It does not need monitoring, so it is left out of the auto-selection.",
  "DBOps 내부": "DBOps internal",
  "DBOps는 등록된 클러스터에 대해": "On a registered cluster DBOps runs",
  "read-only 인스펙션 쿼리": "read-only inspection queries",
  "(pg_stat_*, information_schema 등)를 실행하고 메트릭을 캐시 DB에 저장합니다.":
    " (pg_stat_*, information_schema and the like) and stores the metrics in the cache DB.",
  "채팅과 AI insight를 사용할 때마다": "Every chat and AI insight call incurs",
  "Bedrock 토큰 비용": "Bedrock token cost",
  "이 발생합니다. Cost 탭에서 모니터링 가능합니다.":
    ". Monitor it on the Cost tab.",
  "언제든 클러스터 행에서 등록을 해제할 수 있습니다.":
    "You can unregister a cluster from its row at any time.",
  "등록 중…": "Registering…",
  "동의하고 {n}개 등록": "Agree and register {n}",
  "신규 등록": "new registration",
  "클러스터 / 리소스 등록": "Register a cluster or resource",
  "DynamoDB는 CloudWatch 메트릭만 수집합니다 (시크릿 불필요).":
    "DynamoDB collects CloudWatch metrics only (no secret needed).",
  "기본은 CloudWatch 메트릭 수집입니다. Mongo 읽기 시크릿을 넣으면 서버 상태와 장기 실행 op 딥 리드까지 수집합니다. 선택 항목이며, 이후 PATCH /api/clusters/{id}/meta 로도 채울 수 있습니다.":
    "CloudWatch metrics are collected by default. Add a Mongo read secret and DBOps also deep-reads server status and long-running ops. Optional, and you can fill it in later with PATCH /api/clusters/{id}/meta.",
  "배포 방식": "deployment",
  "Mongo 읽기 시크릿 ARN (선택)": "Mongo read secret ARN (optional)",
  "Mongo 쓰기 시크릿 ARN (선택)": "Mongo write secret ARN (optional)",
  "같은 계정의 Aurora를 등록합니다. DBOps의 Lambda execution role이 직접":
    "Registers an Aurora cluster in this account. If the DBOps Lambda execution role holds",
  "권한을 가지면 추가 설정 없이 연결됩니다.":
    "directly, it connects with no extra setup.",
  "Cross-account 등록 시 spoke 계정에 STS AssumeRole + RDS describe 권한이 있는 role이 필요합니다. 등록 시점에 STS":
    "Cross-account registration needs a role in the spoke account with STS AssumeRole plus RDS describe permission. At registration DBOps verifies the connection with STS",
  "로 연결을 검증한 뒤 저장합니다.": "before saving.",
  "Cross-account 설정 가이드 →": "Cross-account setup guide →",
  등록: "Register",
  "등록 + 연결 검증": "Register and verify",
  "cluster_id + region 이 필요합니다.": "cluster_id and region are required.",
  "저장 없이 AssumeRole + DescribeDBClusters 만 실행해 보기":
    "Run AssumeRole and DescribeDBClusters only, without saving",
  "테스트 중…": "Testing…",
  "연결만 테스트": "Test connection only",
  "Pre-flight 결과:": "Pre-flight result:",
  "등록 현황": "registered",
  "등록된 클러스터 {n}개": "{n} registered clusters",
  "이 페이지는 등록/검증/관리 전용입니다. 실시간 메트릭은 Fleet 또는 Dashboard에서.":
    "This page is for registering, verifying and managing only. Live metrics are on Fleet or Dashboard.",
  "클러스터 목록을 불러오지 못했습니다": "Could not load the cluster list",
  "기존 등록 클러스터가 사라진 것이 아니라 조회 실패 상태입니다.":
    "Your registered clusters have not disappeared, the lookup just failed.",
  "데모 클러스터 및 합성 데이터 삭제":
    "Delete the demo cluster and its synthetic data",
  "레지스트리에서 해제": "Unregister",
  "dbops/<cluster_id>/readonly 컨벤션 시크릿 자동 연결 (권장)":
    "Auto-linked to the dbops/<cluster_id>/readonly convention secret (recommended)",
  "컨벤션 시크릿이 없어 master 시크릿으로 폴백. 프로덕션 사용 전 전용 계정 등록 권장.":
    "No convention secret, so this fell back to the master secret. Register a dedicated account before production use.",
  "사용 가능한 시크릿이 없습니다. 설정 가이드를 참고하세요.":
    "No usable secret. See the setup guide.",
  "이 계정의 Aurora": "Aurora in this account",
  "DBOps와 같은 AWS 계정에서 실행 중인 클러스터":
    "A cluster running in the same AWS account as DBOps",
  "다른 계정 (cross-account)": "Another account (cross-account)",
  "STS AssumeRole로 접근할 spoke role 필요":
    "Needs a spoke role reachable by STS AssumeRole",

  // ── db map ───────────────────────────────────────────────────────────────
  "{n}: 노트 편집": "{n}: edit note",
  "목적 (한 줄)": "Purpose (one line)",
  "예: 체크아웃 서비스 주 DB": "e.g. primary DB for the checkout service",
  "연결 서비스 (쉼표로 구분)": "Services (comma-separated)",
  "노트 편집": "Edit note",
  "연결 서비스": "Services",
  "목적 미설정 (편집으로 추가)": "No purpose set (add one with edit)",
  "목적 미설정": "No purpose set",
  "Serverless / VPC 외": "Serverless / outside a VPC",
  "정상 (available)": "Healthy (available)",
  "주의 (상태 전이 중)": "Caution (state in transition)",
  "위험 (중단/실패 상태)": "Critical (stopped or failed)",
  "상태 미수집": "No status collected",

  // ── dbops health ─────────────────────────────────────────────────────────
  "확인 중…": "Checking…",
  예: "Yes",
  켜짐: "On",
  꺼짐: "Off",

  // ── onboarding ───────────────────────────────────────────────────────────
  "Hub 계정 정보": "Hub account",
  "원격 조치(Remediation) 포함": "Include remediation",
  "에이전트가 파라미터 수정, 재시작 등 쓰기 작업을 수행할 수 있게 합니다. 읽기 전용 모니터링만 필요하면 비활성으로 두세요.":
    "Lets the agent perform write operations such as parameter changes and restarts. Leave it off if you only need read-only monitoring.",
  "원격 조치 포함 토글": "Toggle remediation",
  "CloudFormation 템플릿": "CloudFormation template",
  "복사됨 ✓": "Copied ✓",
  다운로드: "Download",
  "배포 방법": "How to deploy",
  "멤버 계정의 AWS CLI에서 아래 명령을 실행하세요. 파일을 다운로드한 디렉터리에서 실행해야 합니다.":
    "run the command below from the member account's AWS CLI, in the directory you downloaded the file to.",
  "배포가 완료되면 아래 Step 2에서 연결을 확인한 뒤, Step 3에서 클러스터를 등록하세요.":
    "Once it is deployed, verify the connection in Step 2, then register clusters in Step 3.",
  "멤버 계정 12자리 숫자": "12-digit member account number",
  "Aurora 클러스터 식별자": "Aurora cluster identifier",
  "클러스터가 위치한 리전": "Region the cluster runs in",
  테스트: "Test",
  "연결 성공": "Connection OK",
  "연결 실패. 아래 단계를 확인하세요":
    "Connection failed. Check the steps below",
  "스포크 역할 배포와 연결 확인이 완료되면":
    "Once the spoke role is deployed and the connection verified, the",
  "페이지에서 클러스터를 탐색하고 등록합니다.":
    "page is where you discover and register clusters.",
  "Clusters 페이지의": "The Clusters page's",
  "기능이 멤버 계정의 Aurora 클러스터를 자동으로 탐색합니다. 탐색된 클러스터를 선택해 한번에 등록할 수 있습니다.":
    "finds every Aurora cluster in the member account. Select the ones you want and register them in one pass.",
  "Clusters 페이지로 이동": "Go to the Clusters page",
  "스포크 역할 생성": "Create the spoke role",
  "멤버 계정에 DBOps Hub가 AssumeRole할 수 있는 IAM 역할을 배포합니다.":
    "Deploy an IAM role in the member account that the DBOps Hub can assume.",
  "아래 CloudFormation 템플릿을 멤버 계정에 배포하세요.":
    "Deploy the CloudFormation template below in the member account.",
  "템플릿 로딩 실패": "Template failed to load",
  "연결 확인": "Verify the connection",
  "스포크 역할 배포 후, Hub에서 AssumeRole + DescribeDBClusters가 정상 동작하는지 확인합니다.":
    "After the spoke role is deployed, check that AssumeRole and DescribeDBClusters work from the Hub.",
  "멤버 계정 정보를 입력하고 테스트 버튼을 클릭하세요.":
    "Enter the member account details and click Test.",
  "클러스터 등록": "Register clusters",
  "연결이 확인된 계정의 클러스터를 Clusters 페이지에서 탐색하고 등록합니다.":
    "Discover and register a verified account's clusters on the Clusters page.",
  "Clusters 페이지에서 멤버 계정을 탐색해 클러스터를 등록합니다.":
    "Discover the member account on the Clusters page and register its clusters.",

  // ── app/activity ─────────────────────────────────────────────────────────
  "감사 export 실패 ({n}). 다시 시도해 주세요.":
    "Audit export failed ({n}). Please try again.",
  "CSV 내보내기": "Export CSV",
  "모든 cluster": "All clusters",
  "{n}건": "{n} events",
  "상태: {n}": "Status: {n}",
  요청: "requested",
  "날짜 없음": "No date",
  오늘: "Today",
  어제: "Yesterday",

  // ── app/query-lab ────────────────────────────────────────────────────────
  "클러스터를 먼저 선택하세요.": "Select a cluster first.",
  "AI 진단은 PostgreSQL과 MySQL 플랜에 대해 제공됩니다. 이 엔진의 플랜 형식은 아직 지원하지 않습니다.":
    "AI diagnosis covers PostgreSQL and MySQL plans. This engine's plan format is not supported yet.",
  "먼저 클러스터를 선택하세요.": "Select a cluster first.",
  "원본 SQL의 plan-only EXPLAIN을 가져오지 못했습니다 (권한 또는 구문 오류).":
    "Could not get a plan-only EXPLAIN for the original SQL (permissions or a syntax error).",
  "EXPLAIN 권한이 없습니다. plan 비교를 건너뜁니다.":
    "No EXPLAIN permission. Skipping the plan comparison.",
  "제안 SQL의 plan-only EXPLAIN을 가져오지 못했습니다 (구문 검증 필요).":
    "Could not get a plan-only EXPLAIN for the proposed SQL (needs a syntax check).",
  "quick presets: 템플릿을 클립보드에 복사하고 AI 분석 프롬프트를 준비합니다":
    "quick presets: copies the template to your clipboard and arms the AI analysis prompt",
  "프리셋 해제": "Clear preset",
  "현재 편집기의 SQL을 라이브러리에 저장":
    "Save the editor's SQL to the library",
  "먼저 SQL을 작성하거나 EXPLAIN을 실행하세요":
    "Write some SQL or run EXPLAIN first",
  "+ 현재 SQL 저장": "+ Save current SQL",
  "저장된 쿼리가 없습니다. 자주 쓰는 진단/감사 SQL을 라이브러리에 넣어두면 다른 기기에서도 그대로 불러올 수 있습니다.":
    "No saved queries. Put the diagnostic and audit SQL you reuse in the library and it loads on any other device.",
  '"{n}" 삭제할까요?': 'Delete "{n}"?',
  "EXPLAIN 실행 중…": "Running EXPLAIN…",
  "에디터에 SELECT를 붙여넣고": "Paste a SELECT in the editor and press the",
  "버튼을 눌러주세요.": "button.",
  "AI 진단: 이 plan 기준": "AI diagnosis: based on this plan",
  "다시 진단": "Diagnose again",
  "AI 진단 받기": "Get AI diagnosis",
  "버튼을 누르면 이 plan에 맞춰 2~3단계 권장안을 받아볼 수 있어요 (raw plan이 아니라 구조화된 요약만 보내므로 토큰 비용 적음).":
    "to get 2 to 3 recommended steps for this plan (only a structured summary is sent, not the raw plan, so it costs few tokens).",
  "프리셋을 고르면 템플릿이 클립보드에 복사됩니다. 에디터에 SQL을 붙여넣고":
    "Pick a preset and its template is copied to your clipboard. Paste your SQL in the editor and press",
  "AI 분석": "AI analysis",
  "리라이팅 제안 생성 중…": "Generating a rewrite proposal…",
  "에디터에 SQL을 붙여넣고": "Paste your SQL in the editor and press the",
  "리라이팅 제안": "Suggest rewrite",
  "AI 제안: 실행 전 동등성과 성능을 직접 검증하세요 (아래 비교는 실행 없이 planner 추정 cost)":
    "AI suggestion: verify equivalence and performance yourself before running it (the comparison below is planner-estimated cost, nothing was executed)",
  "plan-only EXPLAIN 비교 (실행 없음)":
    "plan-only EXPLAIN comparison (not executed)",
  "원본 cost:": "Original cost:",
  "제안 cost:": "Proposed cost:",
  개선: "better",
  악화: "worse",
  "원본 plan": "Original plan",
  "제안 plan": "Proposed plan",
  "쿼리 저장": "Save query",
  "현재 SQL을 라이브러리에 저장": "Save the current SQL to the library",
  제목: "Title",
  "예: prod-pg-1 capacity probe": "e.g. prod-pg-1 capacity probe",
  "설명 (선택)": "Description (optional)",
  "목록에서 한 줄로 보일 메모": "A one-line note for the list",
  "태그 (쉼표로 구분)": "Tags (comma separated)",
  "SQL 미리보기": "SQL preview",
  "(미리보기할 SQL이 없습니다)": "(no SQL to preview)",
  "제목을 입력하세요": "Enter a title",
  "저장할 SQL이 없습니다": "No SQL to save",
  "인덱스 추천": "Index advice",
  "락 충돌 진단": "Lock contention triage",
  "성능 개선 리라이트": "Performance rewrite",

  // ── app/runbooks ─────────────────────────────────────────────────────────
  "× 작성 닫기": "× Close form",
  "+ 새 Runbook": "+ New runbook",
  "새 Runbook": "New runbook",
  "수동 작성": "Write by hand",
  필터: "Filter",
  "등록된 Runbook": "Saved runbooks",
  "총 {n}개": "{n} total",
  "모든 클러스터": "All clusters",
  태그: "Tag",
  "이 Runbook을 채팅으로 가져가 에이전트가 단계별로 검토와 실행 (쓰기는 승인 필요)":
    "Hand this runbook to Chat and let the agent review and run it step by step (writes still need approval)",
  "▶ 에이전트로 실행": "▶ Run with agent",
  "제목과 본문은 필수입니다": "Title and body are required",
  "예: idle-in-tx 누적 시 자동 cleanup":
    "e.g. auto cleanup when idle-in-tx piles up",
  "Cluster (선택)": "Cluster (optional)",
  "(클러스터 무관)": "(any cluster)",
  "요약 (선택, 한 줄)": "Summary (optional, one line)",
  "목록에 표시되는 한 줄 요약": "The one-line summary shown in the list",
  "본문 (Markdown)": "Body (Markdown)",
  "## 증상\n...\n\n## 진단\n...\n\n## 조치\n```sql\n...":
    "## Symptom\n...\n\n## Diagnosis\n...\n\n## Action\n```sql\n...",
  "태그 (콤마 구분, 최대 16개)": "Tags (comma separated, up to 16)",
  "작성 중 내용은 자동으로 브라우저에 저장됩니다.":
    "Your draft is saved in this browser automatically.",
  "Runbook 저장": "Save runbook",
  "클러스터: {n}": "Cluster: {n}",
  "작성: {n}": "Created: {n}",
  "작성자: {n}": "Author: {n}",
  "태그: {n}": "Tags: {n}",
  "✕ 닫기": "✕ Close",
  "YAML front-matter + 본문을 .md 파일로 내보냅니다":
    "Exports YAML front-matter plus the body as a .md file",
  "⬇ Markdown 내보내기": "⬇ Export Markdown",
  "렌더된 런북을 PDF로 인쇄/저장합니다 (브라우저 인쇄 대화상자 → Save as PDF)":
    "Prints or saves the rendered runbook as a PDF (browser print dialog, then Save as PDF)",
  "⬇ PDF 내보내기": "⬇ Export PDF",
  "삭제 중…": "Deleting…",
  "정말 삭제": "Yes, delete",

  // ── app/schema ───────────────────────────────────────────────────────────
  "FK 그래프는 PostgreSQL 전용입니다": "The FK graph is PostgreSQL only",
  "추출 중…": "Extracting…",
  "FK 추출 실행": "Extract FKs",
  "선택된 클러스터는 MySQL입니다. FK 그래프는 pg_constraint 기반의 PostgreSQL 전용 기능입니다. 우측 상단에서 PostgreSQL 클러스터로 전환하세요.":
    "The selected cluster is MySQL. The FK graph reads pg_constraint, so it is PostgreSQL only. Switch to a PostgreSQL cluster at the top right.",
  "버튼을 누르면 pg_constraint를 조회해 외래키 그래프를 만듭니다.":
    "queries pg_constraint and builds the foreign-key graph.",
  "들어오고 나가는 FK 없음": "No inbound or outbound FK",
  "FK 합계 ≥ 3": "FK total ≥ 3",
  "해당 스키마에 테이블이 없습니다.": "No tables in this schema.",
  "graph: 테이블을 클릭하면 해당 FK가 하이라이트됩니다":
    "graph: click a table to highlight its FKs",
  "이 테이블이 다른 테이블을 참조": "This table references others",
  "다른 테이블이 이 테이블을 참조. 변경시 영향도 확인 필요":
    "Other tables reference this one. Check the impact before changing it",
  "tables: 클릭하면 FK 상세": "tables: click for FK detail",

  // ── app/timeline ─────────────────────────────────────────────────────────
  "워크로드 비교 →": "Workload diff →",
  "읽지 못한 signal:": "Signals that could not be read:",
  "아래 타임라인에 이 category가 없는 것은 발생하지 않았다는 뜻이 아니라, 조회에 실패해 확인하지 못했다는 뜻입니다.":
    "The absence of this category below does not mean it did not happen: the query failed, so nobody could check.",
  "schema_change는 schema_snapshots에서 읽습니다: cache DB에 schema_v26 마이그레이션이 적용됐는지 확인하세요.":
    "schema_change is read from schema_snapshots: check that the schema_v26 migration is applied to the cache DB.",
  "schema_change 판정 범위": "schema_change coverage",

  // ── src/lib/format.ts, relative time (the copy every caller reuses) ──────
  방금: "just now",
  "{n}분 전": "{n}m ago",
  "{n}시간 전": "{n}h ago",
  "{n}일 전": "{n}d ago",

  // ── src/lib/api-client.ts, REST failures, non-hook translate() route ─────
  "대시보드 조회 실패 (상태 {n})": "Dashboard fetch failed (status {n})",
  "시계열 조회 실패 (상태 {n})": "Timeseries fetch failed (status {n})",
  "배치 시계열 조회 실패 (상태 {n})":
    "Batch timeseries fetch failed (status {n})",
  "Wait events 조회 실패 (상태 {n})": "Wait events fetch failed (status {n})",
  "Slow queries 조회 실패 (상태 {n})": "Slow queries fetch failed (status {n})",
  "쿼리 상세 조회 실패 (상태 {n})": "Query detail fetch failed (status {n})",
  "Extensions 조회 실패 (상태 {n})": "Extensions fetch failed (status {n})",
  "헬스 점검 항목 조회 실패 (상태 {n})":
    "Health check fetch failed (status {n})",
  "인덱스 조회 실패 (상태 {n})": "Index fetch failed (status {n})",
  "Vacuum 통계 조회 실패 (상태 {n})": "Vacuum stats fetch failed (status {n})",
  "인덱스 추천 조회 실패 (상태 {n})":
    "Index recommendation fetch failed (status {n})",
  "장기 실행 쿼리 조회 실패 (상태 {n})":
    "Long-running query fetch failed (status {n})",
  "Locks 조회 실패 (상태 {n})": "Locks fetch failed (status {n})",
  "라이브 세션 조회 실패 (상태 {n})": "Live session fetch failed (status {n})",
  "설정 조회 실패 (상태 {n})": "Settings fetch failed (status {n})",
  "스키마 변경 조회 실패 (상태 {n})": "Schema change fetch failed (status {n})",
  "이상 징후 조회 실패 (상태 {n})": "Anomaly fetch failed (status {n})",
  "감사 로그 조회 실패 (상태 {n})": "Audit log fetch failed (status {n})",
  "변경 영향 조회 실패 (상태 {n})": "Change impact fetch failed (status {n})",
  "로그 인사이트 조회 실패 (상태 {n})":
    "Log Insights fetch failed (status {n})",
  "중복 인덱스 조회 실패 (상태 {n})":
    "Redundant index fetch failed (status {n})",
  "스키마 그래프 조회 실패 (상태 {n})":
    "Schema graph fetch failed (status {n})",
  "SLO 조회 실패 (상태 {n})": "SLO fetch failed (status {n})",
  "토폴로지 조회 실패 (상태 {n})": "Topology fetch failed (status {n})",
  "활성 세션 조회 실패 (상태 {n})": "Active session fetch failed (status {n})",
  "구성 정보 조회 실패 (상태 {n})": "Configuration fetch failed (status {n})",
  "용량 예측 조회 실패 (상태 {n})":
    "Capacity forecast fetch failed (status {n})",
  "멀티 클러스터 개요 조회 실패 (상태 {n})":
    "Fleet overview fetch failed (status {n})",
  "테이블 크기 조회 실패 (상태 {n})": "Table size fetch failed (status {n})",
  "알림 규칙 조회 실패 (상태 {n})": "Alert rule fetch failed (status {n})",
  "알림 규칙 생성 실패 (상태 {n})": "Alert rule create failed (status {n})",
  "알림 규칙 수정 실패 (상태 {n})": "Alert rule update failed (status {n})",
  "알림 스누즈 실패 (상태 {n})": "Alert snooze failed (status {n})",
  "클러스터 전체 스누즈 실패 (상태 {n})":
    "Cluster-wide snooze failed (status {n})",
  "알림 규칙 삭제 실패 (상태 {n})": "Alert rule delete failed (status {n})",
  "알림 영향 조회 실패 (상태 {n})": "Alert impact fetch failed (status {n})",
  "알림 구독 조회 실패 (상태 {n})":
    "Alert subscription fetch failed (status {n})",
  "구독 생성 실패 (상태 {n})": "Subscription create failed (status {n})",
  "구독 삭제 실패 (상태 {n})": "Subscription delete failed (status {n})",
  "인스턴스 목록 조회 실패 (상태 {n})":
    "Instance list fetch failed (status {n})",
  "클러스터 조회 실패 (상태 {n})": "Cluster fetch failed (status {n})",
  "메타 저장 실패 (상태 {n})": "Metadata save failed (status {n})",
  "시나리오 목록 조회 실패 (상태 {n})":
    "Scenario list fetch failed (status {n})",
  "시나리오 이력 조회 실패 (상태 {n})":
    "Scenario history fetch failed (status {n})",
  "다른 시나리오가 실행 중입니다": "Another scenario is already running",
  "시나리오 실행 실패 (상태 {n})": "Scenario run failed (status {n})",
  "작업 조회 실패 (상태 {n})": "Task fetch failed (status {n})",
  "작업 상세 조회 실패 (상태 {n})": "Task detail fetch failed (status {n})",
  "작업 통계 조회 실패 (상태 {n})": "Task stats fetch failed (status {n})",
  "작업 생성 실패 (상태 {n})": "Task create failed (status {n})",
  "예약 작업 조회 실패 (상태 {n})": "Schedule fetch failed (status {n})",
  "예약 생성 실패 (상태 {n})": "Schedule create failed (status {n})",
  "예약 삭제 실패 (상태 {n})": "Schedule delete failed (status {n})",
  "비용 조회 실패 (상태 {n})": "Cost fetch failed (status {n})",
  "토큰 사용량 조회 실패 (상태 {n})": "Token usage fetch failed (status {n})",
  "모델 조회 실패 (상태 {n})": "Model fetch failed (status {n})",
  "스케일 작업 조회 실패 (상태 {n})":
    "Scale operation fetch failed (status {n})",
  "취소 실패 (상태 {n})": "Cancel failed (status {n})",
  "AZ 스케일아웃 실패 (상태 {n})": "AZ scale-out failed (status {n})",
  "클러스터 등록 실패 (상태 {n})": "Cluster registration failed (status {n})",
  "연결 테스트 실패 (상태 {n})": "Connection test failed (status {n})",
  "샘플 생성 실패 (상태 {n})": "Sample create failed (status {n})",
  "삭제 실패 (상태 {n})": "Delete failed (status {n})",
  "클러스터 탐색 실패 (상태 {n})": "Cluster discovery failed (status {n})",
  "일괄 등록 실패 (상태 {n})": "Bulk registration failed (status {n})",
  "EXPLAIN 실패": "EXPLAIN failed",
  "시뮬레이션 요청 실패 (상태 {n})": "Simulation request failed (status {n})",
  "파라미터 카탈로그 조회 실패 (상태 {n})":
    "Parameter catalog fetch failed (status {n})",
  "런북 목록 조회 실패 (상태 {n})": "Runbook list fetch failed (status {n})",
  "런북 조회 실패 (상태 {n})": "Runbook fetch failed (status {n})",
  "런북 생성 실패 (상태 {n})": "Runbook create failed (status {n})",
  "런북 삭제 실패 (상태 {n})": "Runbook delete failed (status {n})",
  "채팅 세션 목록 조회 실패 (상태 {n})":
    "Chat session list fetch failed (status {n})",
  "채팅 세션 조회 실패 (상태 {n})": "Chat session fetch failed (status {n})",
  "채팅 세션 저장 실패 (상태 {n})": "Chat session save failed (status {n})",
  "채팅 세션 삭제 실패 (상태 {n})": "Chat session delete failed (status {n})",
  "저장된 쿼리 목록 조회 실패 (상태 {n})":
    "Saved query list fetch failed (status {n})",
  "저장된 쿼리 조회 실패 (상태 {n})": "Saved query fetch failed (status {n})",
  "쿼리 저장 실패 (상태 {n})": "Query save failed (status {n})",
  "저장된 쿼리 삭제 실패 (상태 {n})": "Saved query delete failed (status {n})",
  "리소스 상세 조회 실패 (상태 {n})":
    "Resource detail fetch failed (status {n})",
  "헬스 상태 조회 실패 (상태 {n})": "Health status fetch failed (status {n})",
  "활동 조회 실패 (상태 {n})": "Activity fetch failed (status {n})",
  "백업 조회 실패 (상태 {n})": "Backup fetch failed (status {n})",
  "엔드포인트 조회 실패 (상태 {n})": "Endpoint fetch failed (status {n})",
  "승인 요청 실패 (상태 {n})": "Approval request failed (status {n})",
  "스냅샷 생성 실패 (상태 {n})": "Snapshot create failed (status {n})",
  "복원 실패 (상태 {n})": "Restore failed (status {n})",
  "Workload diff 조회 실패 (상태 {n})":
    "Workload diff fetch failed (status {n})",
  "타임라인 조회 실패 (상태 {n})": "Timeline fetch failed (status {n})",
  "메모리 목록 조회 실패 (상태 {n})": "Memory list fetch failed (status {n})",
  "메모리 삭제 실패 (상태 {n})": "Memory delete failed (status {n})",
  "사용자 조회 실패 (상태 {n})": "User fetch failed (status {n})",
  "역할 변경 실패 (상태 {n})": "Role change failed (status {n})",
  "팀 조회 실패 (상태 {n})": "Team fetch failed (status {n})",
  "팀 상세 조회 실패 (상태 {n})": "Team detail fetch failed (status {n})",
  "팀 생성 실패 (상태 {n})": "Team create failed (status {n})",
  "팀 삭제 실패 (상태 {n})": "Team delete failed (status {n})",

  // ── src/lib/report-download.ts, downloaded markdown report ───────────────
  "DBOps 리포트": "DBOps report",
  유형: "Type",
  요약: "Summary",
  "24시간 지표": "24h metrics",
  "| 항목 | 값 |": "| Item | Value |",
  "피크 AAS": "Peak AAS",
  "AAS > {n} 샘플": "AAS > {n} samples",
  분: "min",
  "활성 연결": "Active connections",
  "스토리지 변화": "Storage change",
  "스토리지 시작": "Storage start",
  "스토리지 종료": "Storage end",
  "AAS 샘플 수": "AAS samples",
  "이벤트 분포": "Event distribution",

  // ── src/lib/remediation.ts ───────────────────────────────────────────────
  "이력 없음": "No history",
  "{n}회 해결": "{n} resolved",

  // ── src/lib/use-alert-badge.ts, alert toasts ─────────────────────────────
  "{n} critical 전환": "{n} went critical",
  "{n}개 클러스터 critical 전환": "{n} clusters went critical",
  "경보 증가": "Alerts up",
  "에이전트 작업 완료": "Agent task done",
  "외부 인시던트": "External incident",
  "새 경보": "New alert",

  // ── src/lib/api-client.ts, REST failures, non-hook translate() route ─────
  "멤버 추가 실패 (상태 {n})": "Add member failed (status {n})",
  "멤버 제거 실패 (상태 {n})": "Remove member failed (status {n})",
  "클러스터 할당 실패 (상태 {n})": "Cluster assign failed (status {n})",
  "클러스터 할당 해제 실패 (상태 {n})": "Cluster unassign failed (status {n})",

  // ── src/lib/metric-glossary.ts, hover hints, translated on the way out of metricDef() ─
  "인스턴스 CPU 사용률 (%).": "Instance CPU utilization (%).",
  "지속적으로 높으면 쿼리 비효율 또는 인스턴스 under-provisioning. 70% 경고, 90% 위험.":
    "Sustained highs point to inefficient queries or an under-provisioned instance. 70% warns, 90% is critical.",
  "Average Active Sessions: 평균적으로 동시에 일하고 있던 세션 수.":
    "Average Active Sessions: how many sessions were working at once, on average.",
  "vCPU 수를 넘으면 세션이 CPU/IO/lock을 기다리며 대기 중이라는 신호. Performance Insights의 핵심 지표.":
    "Above the vCPU count, sessions are waiting on CPU, IO or locks. The headline Performance Insights metric.",
  "활성 + 유휴 DB 커넥션 총수.": "Total DB connections, active plus idle.",
  "max_connections에 근접하면 신규 연결이 거부됨. 급증은 connection pool 누수 또는 트래픽 폭주.":
    "Near max_connections new connections are refused. A spike means a connection pool leak or a traffic surge.",
  "활성 + 유휴 DB 커넥션 총수 (CloudWatch DatabaseConnections).":
    "Total DB connections, active plus idle (CloudWatch DatabaseConnections).",
  "현재 쿼리를 실행 중인 (idle 아닌) 커넥션 수.":
    "Connections currently running a query, not idle.",
  "active가 높고 AAS도 높으면 실제 작업 부하. active는 낮은데 total이 높으면 idle 연결 누적.":
    "High active with high AAS is real work. Low active with a high total means idle connections piling up.",
  "Reader 인스턴스가 Writer를 따라잡지 못한 지연 (ms).":
    "How far a reader instance trails the writer (ms).",
  "읽기 복제본에서 stale 데이터를 반환할 수 있는 시간. 쓰기 폭주나 reader 과부하 시 증가.":
    "The window in which a read replica can return stale data. Grows under write bursts or reader overload.",
  "분당 감지된 데드락 수.": "Deadlocks detected per minute.",
  "0이 정상. 0보다 크면 트랜잭션이 서로의 lock을 순환 대기 → 한쪽이 강제 abort. 애플리케이션 lock 순서 문제.":
    "0 is normal. Above 0, transactions wait on each other's locks in a cycle and one is aborted. An application lock-ordering problem.",
  "초당 읽기 I/O 연산 수.": "Read I/O operations per second.",
  "buffer cache miss로 디스크를 때리는 정도. 갑작스러운 증가는 대용량 스캔 또는 cache eviction.":
    "How hard buffer cache misses are hitting the disk. A sudden jump means a large scan or cache eviction.",
  "초당 쓰기 I/O 연산 수.": "Write I/O operations per second.",
  "WAL flush + dirty page write 부하. checkpoint, 대량 INSERT/UPDATE, vacuum 시 급증.":
    "WAL flush plus dirty page write load. Spikes on checkpoints, bulk INSERT/UPDATE and vacuum.",
  "클러스터 볼륨 사용량 (bytes).": "Cluster volume usage (bytes).",
  "Aurora는 자동 확장되지만, 증가 속도가 급격하면 bloat / 미정리 데이터 / 로그 누적 의심.":
    "Aurora grows on its own, but a steep climb suggests bloat, uncleaned data or log buildup.",
  "인스턴스 가용 메모리 (bytes).": "Memory available on the instance (bytes).",
  "지속적으로 낮으면 buffer cache 압박 → 디스크 I/O 증가 → 성능 저하. swap 발생 직전 신호.":
    "Persistently low means buffer cache pressure, then more disk I/O, then slower queries. The signal just before swapping.",
  "buffer cache에서 처리된 블록 읽기 비율.":
    "Share of block reads served from the buffer cache.",
  "PostgreSQL은 보통 99%+ 가 정상. 떨어지면 working set이 메모리를 초과했다는 의미.":
    "On PostgreSQL 99%+ is usually normal. A drop means the working set no longer fits in memory.",
  "초당 스캔되어 반환된 row 수.": "Rows scanned and returned per second.",
  "tup_fetched 대비 과도하게 높으면 인덱스 없이 풀스캔하는 쿼리가 많다는 신호.":
    "Far above tup_fetched suggests many queries full-scanning without an index.",
  "초당 커밋된 트랜잭션 수.": "Transactions committed per second.",
  "처리량(throughput)의 직접 지표. rollback 비율이 높이 동반되면 애플리케이션 오류 의심.":
    "The direct throughput measure. A high rollback share alongside it suggests application errors.",
  "인스턴스 스토리지 잔여 용량 (CloudWatch FreeStorageSpace).":
    "Storage left on the instance volume (CloudWatch FreeStorageSpace).",
  "Aurora와 달리 RDS 인스턴스 볼륨은 자동 확장되지 않는다(storage autoscaling 미설정 시). 소진되면 STORAGE_FULL로 쓰기가 멈추므로 낮을수록 위험.":
    "Unlike Aurora, an RDS instance volume does not grow on its own unless storage autoscaling is set. Once it runs out, writes stop with STORAGE_FULL, so lower is riskier.",
  "읽기 I/O 1건당 평균 소요 시간 (CloudWatch ReadLatency, 수집은 초 단위).":
    "Average time per read I/O (CloudWatch ReadLatency, collected in seconds).",
  "스토리지 포화와 IOPS 한계 신호. 20ms를 넘어 지속되면 쿼리 응답 시간이 그대로 늘어난다.":
    "A sign of storage saturation and an IOPS ceiling. Sustained above 20ms it shows up directly in query response time.",
  "쓰기 I/O 1건당 평균 소요 시간 (CloudWatch WriteLatency, 수집은 초 단위).":
    "Average time per write I/O (CloudWatch WriteLatency, collected in seconds).",
  "커밋 지연의 직접 원인. gp2 버스트 소진, 프로비저닝 IOPS 부족, 대량 쓰기 시 상승.":
    "The direct cause of commit delay. Rises on exhausted gp2 burst, too little provisioned IOPS, or bulk writes.",
  "Redis/Valkey 엔진 스레드의 CPU 사용률 (EngineCPUUtilization).":
    "CPU used by the Redis/Valkey engine thread (EngineCPUUtilization).",
  "명령 처리는 단일 스레드다. 노드 전체 CPU가 여유로워도 이 값이 높으면 이미 포화. 스케일업/샤딩 판단의 실제 기준.":
    "Command processing is single threaded. Even with node CPU to spare, a high value here already means saturation. This is the real basis for a scale-up or sharding call.",
  "캐시 노드 전체 CPU 사용률 (CPUUtilization).":
    "CPU across the whole cache node (CPUUtilization).",
  "멀티스레드인 Memcached에서는 이 값이 주 포화 지표. Redis에서는 복제와 스냅샷 같은 백그라운드 작업까지 포함한다.":
    "On multi-threaded Memcached this is the main saturation signal. On Redis it also covers background work such as replication and snapshots.",
  "maxmemory 대비 사용 중 메모리 비율 (DatabaseMemoryUsagePercentage).":
    "Memory in use against maxmemory (DatabaseMemoryUsagePercentage).",
  "100%에 가까우면 eviction 정책이 키를 버리기 시작하고, noeviction이면 쓰기가 거부된다.":
    "Near 100% the eviction policy starts dropping keys, and under noeviction writes are refused.",
  "메모리 확보를 위해 삭제된 키 수.": "Keys dropped to free memory.",
  "0이 정상. 계속 발생하면 working set이 노드 메모리를 초과한 상태다. 스케일업 또는 TTL과 키 정리가 필요하다.":
    "0 is normal. If it keeps happening the working set no longer fits the node. Scale up, or clean up TTLs and keys.",
  "캐시 노드의 현재 클라이언트 커넥션 수 (CurrConnections).":
    "Client connections on the cache node right now (CurrConnections).",
  "maxclients(기본 65000)에 근접하면 신규 연결이 거부된다. 급증은 커넥션 풀 누수 신호.":
    "Near maxclients (65000 by default) new connections are refused. A spike signals a connection pool leak.",
  "replica가 primary를 따라잡지 못한 지연.":
    "How far a replica trails the primary.",
  "replica 읽기에서 stale 값을 반환할 수 있는 시간. 쓰기 폭주, 네트워크 지연, 대형 키 복제 시 증가.":
    "The window in which a replica read can return a stale value. Grows on write bursts, network delay or large-key replication.",
  "노드가 사용 중인 swap 크기 (SwapUsage).":
    "Swap in use on the node (SwapUsage).",
  "AWS 권장은 50MB 미만. 그 이상은 메모리가 부족해 디스크를 쓰는 상태로, 레이턴시가 급격히 나빠진다.":
    "AWS recommends under 50MB. Above that the node is short on memory and writing to disk, and latency degrades sharply.",
  "인스턴스 CPU 사용률 (CloudWatch CPUUtilization).":
    "Instance CPU utilization (CloudWatch CPUUtilization).",
  "요청 블록을 버퍼 캐시에서 처리한 비율.":
    "Share of requested blocks served from the buffer cache.",
  "DocumentDB는 보통 95%+ 가 정상. 떨어지면 working set이 인스턴스 메모리를 초과했다는 의미다. 인덱스 추가나 스케일업을 검토한다.":
    "On DocumentDB 95%+ is usually normal. A drop means the working set no longer fits instance memory. Consider adding an index or scaling up.",
  "타임아웃으로 정리된 커서 수.": "Cursors reaped after a timeout.",
  "0이 정상. 증가하면 애플리케이션이 커서를 끝까지 읽지 않고 방치해 서버 리소스를 잡고 있다는 신호.":
    "0 is normal. A rise signals the application abandoning cursors unread and holding server resources.",
  "프로비저닝된 읽기 용량을 초과해 스로틀된 이벤트 수.":
    "Events throttled for exceeding provisioned read capacity.",
  "0이 정상. 발생하면 읽기가 지연되거나 거부된다. 파티션 편중(hot key)이거나 RCU가 부족한 상태.":
    "0 is normal. When it fires, reads are delayed or refused. Either a hot key skewing partitions, or too few RCUs.",
  "프로비저닝된 쓰기 용량을 초과해 스로틀된 이벤트 수.":
    "Events throttled for exceeding provisioned write capacity.",
  "0이 정상. 발생하면 쓰기가 실패한다(재시도 필요). WCU 증설 또는 On-Demand 전환 검토.":
    "0 is normal. When it fires, writes fail and need a retry. Consider raising WCUs or switching to On-Demand.",
  "용량 초과로 거부된 요청 수 (ProvisionedThroughputExceeded).":
    "Requests refused for exceeding capacity (ProvisionedThroughputExceeded).",
  "테이블과 인덱스 단위 스로틀의 총량. 지속되면 애플리케이션에 그대로 에러로 노출된다.":
    "The total of table-level and index-level throttling. Sustained, it surfaces in the application as errors.",
  "GetItem 요청의 평균 응답 시간 (SuccessfulRequestLatency).":
    "Average GetItem response time (SuccessfulRequestLatency).",
  "단일 항목 조회는 보통 한 자리 ms. 상승하면 항목 크기 증가나 스로틀 재시도를 의심.":
    "A single-item read is usually single-digit ms. A rise suggests larger items or throttle retries.",
  "Query 요청의 평균 응답 시간 (SuccessfulRequestLatency).":
    "Average Query response time (SuccessfulRequestLatency).",
  "스캔 범위가 넓거나 필터로 대량 항목을 버리면 상승한다. 키 설계와 GSI 재검토 신호.":
    "Rises when the scan range is wide or a filter discards many items. A signal to revisit key design and GSIs.",
  "요청 페이지를 디스크 없이 버퍼 풀에서 처리한 비율. Buffer cache hit ratio를 짝 base 카운터로 나눈 값이다(원시 cntr_value는 비율이 아니다).":
    "Share of requested pages served from the buffer pool without touching disk. Buffer cache hit ratio divided by its paired base counter (the raw cntr_value is not a ratio).",
  "정상 워크로드에서는 95% 이상. 낮으면 working set이 버퍼 풀을 넘어 디스크를 읽고 있다는 뜻이라 max server memory 또는 인스턴스 메모리를 함께 본다. 단독으로는 신뢰도가 낮아 Page Life Expectancy와 같이 읽어야 한다.":
    "95% or better on a healthy workload. Lower means the working set has outgrown the buffer pool and is reading from disk, so check max server memory and instance memory too. On its own it is not reliable, read it next to Page Life Expectancy.",
  "데이터 페이지가 버퍼 풀에 머무는 기대 시간(초).":
    "How long a data page is expected to stay in the buffer pool (seconds).",
  "메모리 압박의 가장 직접적인 신호다. 급격히 떨어지면 버퍼 풀이 계속 밀려나며 재읽기가 일어나고 있다는 뜻. 절대 임계치는 메모리 크기에 따라 달라지므로 이 클러스터의 평소 추세와 비교한다.":
    "The most direct signal of memory pressure. A sharp drop means the buffer pool keeps being evicted and pages are re-read. The absolute threshold depends on memory size, so compare against this cluster's own usual trend.",
  "SQL Server가 지금 확보한 메모리(Total Server Memory)를 확보 목표(Target Server Memory)로 나눈 비율. 건강도 점수가 아니라 버퍼 풀 확보가 어디까지 진행됐는지를 나타낸다.":
    "Memory SQL Server holds now (Total Server Memory) divided by what it is aiming for (Target Server Memory). Not a health score: it shows how far buffer pool acquisition has got.",
  "낮은 값이 곧 문제는 아니다. 수요가 없으면 SQL Server는 Target까지 올릴 이유가 없어 Total이 Target 밑에 오래 머무는 것이 정상 정상상태다(실측: 유휴 상태의 dbops-demo-mssql에서 37~43%, 같은 시점 Page Life Expectancy 26,429초, Memory Grants Pending 0, Processes Blocked 0). 100%에 붙어 있으면 목표만큼 다 확보한 상태로, 더 필요하면 max server memory 상한을 본다. 이 지표만으로는 유휴와 메모리 압박을 구분할 수 없으므로, 압박 여부는 Page Life Expectancy와 Memory Grants Pending으로 판단한다.":
    "A low value is not itself a problem. With no demand SQL Server has no reason to climb to Target, so Total sitting below Target for a long time is a normal steady state (measured: 37 to 43% on an idle dbops-demo-mssql, with Page Life Expectancy 26,429s, Memory Grants Pending 0 and Processes Blocked 0 at the same moment). Pinned at 100% it has acquired everything it aimed for, and if it needs more the max server memory ceiling is the thing to check. This metric alone cannot tell idle apart from memory pressure, so judge pressure from Page Life Expectancy and Memory Grants Pending.",
  "지금 다른 세션의 락을 기다리며 차단된 프로세스 수.":
    "Processes blocked right now waiting on another session's lock.",
  "0이 정상. 0이 아닌 값이 지속되면 블로킹 체인이 있다는 뜻으로, blocked process threshold를 설정해 블로킹 리포트를 남기고 원인 트랜잭션을 찾는다.":
    "0 is normal. A non-zero value that persists means a blocking chain, so set blocked process threshold to capture a blocking report and find the transaction behind it.",
  "쿼리 실행에 필요한 메모리 그랜트를 받지 못해 대기 중인 쿼리 수.":
    "Queries waiting because they have not been granted the memory they need to run.",
  "0이 정상. 0보다 크면 정렬과 해시 조인이 메모리를 못 받아 대기 중이라는 뜻으로, 메모리 부족이 이미 쿼리 지연으로 나타나고 있는 상태다.":
    "0 is normal. Above 0, sorts and hash joins are waiting for memory, which means the shortage is already showing up as query delay.",

  // ── shared keys, wrapped at call sites all over the app ──────────────────
  클러스터: "Cluster",

  // ── aas-chart.tsx ────────────────────────────────────────────────────────
  "메트릭 데이터가 없습니다": "No metric data",

  // ── active-sessions-panel.tsx ────────────────────────────────────────────
  "활성 세션 (고해상 ~5초)": "Active sessions (high-res, ~5s)",
  "최근 1시간, pg_stat_activity / processlist 5초 샘플: 5분 ETL이 놓치는 순간 스파이크 포착":
    "Last 1 hour, pg_stat_activity / processlist sampled every 5s: catches the spikes the 5-minute ETL misses",
  "현재, peak {n}": "now, peak {n}",
  "샘플 없음 (샘플러 수집 대기)": "No samples yet (waiting for the sampler)",

  // ── anomalies-panel.tsx ──────────────────────────────────────────────────
  "CPU 사용률": "CPU utilization",
  "활성 세션 (AAS)": "Active sessions (AAS)",
  "활성 커넥션": "Active connections",
  데드락: "Deadlocks",
  "블로킹 락": "Blocking locks",
  "스토리지 사용량": "Storage used",
  "버퍼 캐시 적중률": "Buffer cache hit ratio",
  "복제 지연": "Replica Lag",
  지표: "Metric",
  "z-score ≥ 2.5 (7일 베이스라인 대비)": "z-score ≥ 2.5 vs the 7-day baseline",
  "요일/시간대별 과거 분포(중앙값 + IQR)와 비교":
    "Compared with the past distribution for this weekday and hour (median + IQR)",
  "해당 시간대의 seasonal 베이스라인이 아직 학습되지 않아 단순 7일 평균±표준편차로 대체":
    "No seasonal baseline for this hour yet, so a plain 7-day mean ± stddev is used instead",
  "베이스라인 {n}, 최근 최댓값": "Baseline {n}, recent max",
  "이상 징후를 조회하지 못했습니다": "Could not read anomalies",
  '1분 뒤 자동으로 다시 조회합니다. 계속 실패하면 새로 고쳐 주세요. 지금 화면은 "이상 없음"이 아니라 "확인하지 못한 상태"입니다.':
    'Retrying automatically in a minute. If it keeps failing, refresh. Right now this screen reads "not checked", not "nothing wrong".',
  "최근 4시간 지표가 없어 판단할 수 없습니다":
    "No metrics in the last 4 hours, so nothing can be judged",
  "최근 4시간 구간에 이 클러스터의 클러스터 레벨 지표가 한 건도 없습니다. baseline 학습을 기다리는 상태가 아니라 비교할 데이터 자체가 없는 상태입니다. 방금 등록한 클러스터라면 첫 수집 주기를 기다리고, 그렇지 않다면 이 클러스터의 지표 수집(ETL)이 도는지 확인해 주세요.":
    "Not a single cluster-level metric for this cluster in the last 4 hours. This is not a baseline still training, there is no data to compare against at all. If you just registered the cluster, wait for the first collection cycle; otherwise check that metric collection (ETL) is running for it.",
  "baseline 학습 전이라 아직 판단할 수 없습니다":
    "Baseline not trained yet, so nothing can be judged",
  "지표는 수집되고 있지만 비교 기준이 되는 baseline이 아직 없습니다(요일/시간대별 seasonal, 7일 flat 모두). seasonal baseline은 이 시간대 지표가 약 2주치 쌓이면 자동으로 학습되니 그때까지 기다려 주세요.":
    "Metrics are arriving, but there is still no baseline to compare them with (neither the per weekday and hour seasonal one nor the 7-day flat one). The seasonal baseline trains itself once about two weeks of this hour has accumulated, so wait until then.",
  "지표 {n}개를 baseline과 비교했습니다.":
    "Compared {n} metrics against their baseline.",
  "이 시간대의 seasonal baseline이 아직 없어 7일 flat 평균±σ 기준으로 판정했습니다(신뢰도 낮음).":
    "No seasonal baseline for this hour yet, so the call used the 7-day flat mean ± σ (low confidence).",
  "최근 4시간 동안 이상 징후 없음": "No anomaly in the last 4 hours",
  "이상 징후, σ{n}": "Anomaly, σ{n}",
  "베이스라인 {a} ± {b}, 최근 최댓값": "Baseline {a} ± {b}, recent max",
  "최근 최댓값": "Recent max",
  "최근 평균": "Recent avg",
  "베이스라인 평균": "Baseline mean",
  "베이스라인 σ": "Baseline σ",
  "AI 진단": "AI diagnosis",
  "분석 중…": "Analyzing…",
  "원인 진단 + 다음 점검": "Diagnose cause + next check",
  "버튼을 누르면 추정 원인, 운영 영향, 다음 점검 단계를 한 번에 받아볼 수 있어요.":
    "gives you the likely cause, the operational impact and the next check in one go.",

  // ── audit-log-panel.tsx ──────────────────────────────────────────────────
  "{m}월 {d}일": "{m}/{d}",
  "DBA가 승인한 작업 및 변경 이력": "Operations and changes a DBA approved",
  "전체 작업": "All actions",
  "감사 기록이 없습니다": "No audit records",

  // ── backup-panel.tsx ─────────────────────────────────────────────────────
  "스냅샷 {n} 생성 시작": "Snapshot {n} started",
  "복원 시작: {n}": "Restore started: {n}",
  "읽기 전용": "Read only",
  비활성: "Disabled",
  "Point-in-Time Recovery 윈도우": "Point-in-Time Recovery window",
  "온디맨드 백업이 없습니다. PITR로 위 구간의 임의 시점 복원이 가능합니다.":
    "No on-demand backup. PITR can restore any point in the window above.",
  "PITR가 비활성이고 온디맨드 백업이 없습니다. DynamoDB 백업은 AWS Console 또는 CDK에서 설정하세요 (현재 플랫폼은 읽기 전용).":
    "PITR is off and there is no on-demand backup. Set up DynamoDB backups in the AWS Console or CDK (this panel is read only).",
  "+ 스냅샷 생성": "+ Create snapshot",
  "수동 스냅샷을 생성합니다. 이름을 비워두면 자동으로 생성됩니다 (예: manual-…-타임스탬프). 스냅샷 생성은 데이터를 변경하지 않는 안전한 작업입니다.":
    "Creates a manual snapshot. Leave the name empty and one is generated for you (for example manual-…-timestamp). Taking a snapshot does not change any data, it is a safe operation.",
  "snapshot id (선택)": "snapshot id (optional)",
  "새 클러스터": "new cluster",
  "⚠ 복원은": "⚠ Restore creates a",
  "를 생성합니다 (과금 발생). 소스 클러스터":
    " (which is billed). The source cluster",
  "는 변경되지 않습니다. 클러스터가 available 되면 writer 인스턴스가 자동 생성되고 DBOps에 자동 등록됩니다 (수 분 소요).":
    "is not modified. Once the cluster is available, a writer instance is created and it is registered in DBOps automatically (takes a few minutes).",
  스냅샷: "Snapshot",
  "에서 복원": "will be restored",
  "Point-in-Time 복원": "Point-in-Time restore",
  "최신 복원 가능 시점으로 복원 (latest restorable time)":
    "Restore to the latest restorable time",
  "새 클러스터 id": "new cluster id",
  "확인을 위해 새 클러스터 id 를 다시 입력":
    "retype the new cluster id to confirm",
  "복원 실행": "Run restore",
  "복원 시작 중…": "Starting restore…",
  "확인 입력이 일치해야 실행됩니다":
    "The confirmation has to match before this runs",
  "이 구간의 아무 시점으로 복원할 수 있습니다 (PITR)":
    "Any point in this window can be restored (PITR)",
  "↻ 시점으로 복원 (새 클러스터)": "↻ Restore to a point in time (new cluster)",
  "이 스냅샷에서 새 클러스터로 복원":
    "Restore this snapshot into a new cluster",
  복원: "Restore",

  // ── capacity-forecast-panel.tsx ──────────────────────────────────────────
  "Read Capacity (RCU/분)": "Read Capacity (RCU/min)",
  "Write Capacity (WCU/분)": "Write Capacity (WCU/min)",
  안정: "Stable",
  여유: "Headroom",
  위험: "Critical",
  주의: "Warning",
  "증가 추세": "Trending up",
  "소진 추세": "Trending to depletion",
  "감소 추세": "Trending down",
  "최근 30일 metric_snapshots 선형 회귀로 30/60/90일 후 사용량 + 한도 도달 시점 추정. 값이 한도로 늘어나는 경우와 0으로 줄어드는 경우를 모두 다룹니다.":
    "Linear regression over the last 30 days of metric_snapshots, projecting usage 30/60/90 days out plus when the limit is reached. Covers both a value growing into its limit and one shrinking toward 0.",
  "이 지표는 현재 클러스터에서 용량 예측을 제공하지 않습니다.":
    "This metric has no capacity forecast on the current cluster.",
  "데이터 부족 ({n}개 샘플). 신뢰성 있는 예측을 위해 최소 7개 이상의 일별 데이터 포인트가 필요합니다.":
    "Not enough data ({n} samples). A forecast worth trusting needs at least 7 daily datapoints.",
  "현재 여유": "Free now",
  "할당 대비": "of allocated,",
  "한도 {n} 중": "of the {n} limit,",
  "{n}% 사용,": "{n}% used,",
  "한도 미확인,": "limit unknown,",
  "현재 추세대로면 {n}일 후 {x}": "At this rate, {x} in {n} days",
  "한도 도달": "limit reached",
  소진: "depleted",
  "현재 추세대로면 소진하지 않음": "At this rate it does not run out",
  "현재 추세대로면 한도에 도달하지 않음":
    "At this rate it does not reach the limit",
  "소진 예상": "Projected depletion",
  "이미 소진": "Already out",
  "이미 한도": "At the limit",
  "eviction 중": "evicting",
  "{n}일 후": "in {n} days",
  "예측 보류": "No forecast",
  "소진 예상 없음": "No depletion expected",
  "예측 한도 안전": "Projected within limit",
  ", 추세": ", trend",
  "/일": "/day",
  "할당 100%": "100% allocated",
  "30일 후": "In 30 days",
  "60일 후": "In 60 days",
  "90일 후": "In 90 days",
  "기준: 최근 {a}일, {b}개 샘플": "Basis: last {a} days, {b} samples",
  ", 단순 선형 회귀 (시즌성/스파이크 미반영)":
    ", plain linear regression (no seasonality or spikes)",

  // ── change-impact-panel.tsx ──────────────────────────────────────────────
  "(변경 전후 평균)": "(mean before vs after)",
  "변경 영향 회고": "Change impact retro",
  "전후 윈도우": "Before/after window",
  "최근 7일 RDS 변경 이벤트(파라미터, 스케일링, 재시작 등)를 앵커로 전후 ±{n}시간 워크로드를 자동 비교합니다. 콘솔/CLI 직접 변경도 포착합니다.":
    "Uses RDS change events from the last 7 days (parameter, scaling, restart) as anchors and compares the workload ±{n}h around each one. Console and CLI changes are caught too.",
  "불러오지 못했습니다:": "Could not load:",
  "최근 7일간 기록된 변경 이벤트가 없습니다.":
    "No change event recorded in the last 7 days.",
  "(설명 없음)": "(no description)",
  "전후 메트릭 표본이 부족해 비교를 생략했습니다(변경 직후이거나 수집 데이터 부족).":
    "Too few metric samples on either side, so the comparison was skipped (the change is too recent, or collection is thin).",

  // ── cluster-overview.tsx ─────────────────────────────────────────────────
  "등록된 클러스터가 없습니다.": "No clusters registered.",
  "클러스터 등록 →": "Register a cluster →",
  "Fleet 전체 →": "All of Fleet →",
  "+{n}개 더 → Fleet": "+{n} more → Fleet",

  // ── connection-breakdown.tsx ─────────────────────────────────────────────
  "max_connections {a} 중 {b}개 사용": "{b} of max_connections {a} in use",
  "아직 커넥션 데이터 없음": "No connection data yet",

  // ── data-api-banner.tsx ──────────────────────────────────────────────────
  "RDS Data API(HttpEndpoint) 비활성": "RDS Data API (HttpEndpoint) is off",
  "CloudWatch 지표는 정상 수집되지만, 라이브 SQL 기반 패널(Vacuum & Bloat, Table Sizes, Connection Activity, Top Queries, Configuration)과 AI 에이전트의 SQL 실행은 이 클러스터에서 동작하지 않습니다. 다운타임 없이 활성화할 수 있습니다. 활성화 시 IAM 권한 기반으로 SQL 실행 경로가 열립니다.":
    "CloudWatch metrics still collect, but the live SQL panels (Vacuum & Bloat, Table Sizes, Connection Activity, Top Queries, Configuration) and the agent's SQL execution do not work on this cluster. It can be enabled with no downtime, which opens the SQL path under IAM permissions.",
  "활성화 승인 대기 중:": "Waiting for approval to enable:",
  "Approval Center에서 검토": "Review in the Approval Center",
  "요청 등록 중…": "Submitting…",
  "활성화 요청 (DBA 승인 필요)": "Request enable (needs DBA approval)",
  "요청 실패:": "Request failed:",
  "CLI로 직접 활성화": "Enable it from the CLI",

  // ── docdb-overview-panel.tsx ─────────────────────────────────────────────
  "클러스터 개요": "Cluster overview",
  "인스턴스 수": "Instances",
  "엔진 버전": "Engine version",
  "인스턴스 목록": "Instance list",
  "연산 카운터 (Opcounters)": "Operation counters (Opcounters)",
  "스토리지 / 메모리 / 레이턴시": "Storage / memory / latency",

  // ── dynamodb-capacity-simulator.tsx ──────────────────────────────────────
  "용량 모드 비용 시뮬레이션": "Capacity mode cost simulation",
  "테이블의 실제 소비 용량(consumed RCU/WCU)을 기준으로 Provisioned ↔ On-Demand 월 비용을 실시간 AWS Pricing 단가로 비교합니다. 용량(capacity) 비용만 대상이며 storage, backup, replication은 제외합니다.":
    "Compares Provisioned vs On-Demand monthly cost from the table's actual consumed capacity (consumed RCU/WCU), priced with live AWS Pricing rates. Capacity cost only: storage, backup and replication are excluded.",
  "소비 용량과 리전별 단가를 불러오는 중입니다…":
    "Loading consumed capacity and per-region rates…",
  "현재 모드": "Current mode",
  "h 윈도우,": "h window,",
  "현재 월 비용": "Current monthly cost",
  "{n} 기준": "on {n}",
  "On-Demand 월 비용 (추정)": "On-Demand monthly (est.)",
  "Provisioned 월 비용 (추정)": "Provisioned monthly (est.)",
  "로 전환 시 월": "would save",
  "({n}%) 절감이 예상됩니다.": "({n}%) per month.",
  "현재 모드(": "The current mode (",
  ")가 두 모드 중 더 저렴합니다. 전환 이점이 없습니다.":
    ") is the cheaper of the two. Switching gains nothing.",
  "일부 단가를 확인하지 못해 권장 모드를 산출하지 않았습니다(아래 Pricing 출처 참고). 확인된 비용만 표시합니다.":
    "Some rates could not be resolved, so no recommended mode was computed (see the pricing source below). Only the costs that did resolve are shown.",
  "추정 가정 보기": "Show the assumptions",

  // ── dynamodb-overview-panel.tsx ──────────────────────────────────────────
  "테이블 개요": "Table overview",
  "아이템 수": "Items",
  "테이블 크기": "Table size",
  "GSI 없음": "No GSI",
  "용량 (Capacity Units)": "Capacity (Capacity Units)",
  "스로틀 이벤트 (Throttles)": "Throttle events (Throttles)",
  "GSI별 용량/스로틀 (Per-GSI)": "Capacity / throttles per GSI",
  "레이턴시 (Latency)": "Latency",
  "처리량 (Throughput)": "Throughput",

  // ── elasticache-overview-panel.tsx ───────────────────────────────────────
  "메모리 / Hit Rate": "Memory / Hit Rate",
  "네트워크 처리량 (Network Throughput)": "Network throughput",

  // ── endpoints-panel.tsx ──────────────────────────────────────────────────
  "+ 커스텀 엔드포인트 추가": "+ Add custom endpoint",
  "커스텀 엔드포인트 생성, 수정, 삭제는 DBA 승인이 필요합니다. 요청하면 승인 센터에 등록되고, 승인 즉시 실행됩니다.":
    "Creating, editing and deleting a custom endpoint needs DBA approval. A request lands in the Approval Center and runs the moment it is approved.",
  "커스텀 엔드포인트 변경은 관리자(DBA)만 요청할 수 있습니다. 이 패널은 읽기 전용입니다.":
    "Only an admin (DBA) can request a custom endpoint change. This panel is read only for you.",
  "승인 센터로 이동": "Go to the Approval Center",
  "READER 는 읽기 전용 리더만, ANY 는 writer와 reader 모두 라우팅 대상입니다. 멤버를 지정하지 않으면 모든 리더가 포함됩니다.":
    "READER routes to read-only readers, ANY routes to both the writer and the readers. Leave the members empty and every reader is included.",
  "요청 중…": "Requesting…",
  "승인 요청": "Request approval",
  "엔드포인트가 없습니다.": "No endpoints.",
  "전체 리더": "All readers",
  "포함(static)": "Include (static)",
  "제외(excluded)": "Exclude (excluded)",
  "인스턴스 id (쉼표/공백 구분)": "instance ids (comma or space separated)",
  포함: "Included",
  제외: "Excluded",
  "멤버 편집": "Edit members",
  "커스텀 엔드포인트": "Custom endpoint",
  "삭제 승인을 요청합니다. writer/reader 내장 엔드포인트는 영향받지 않습니다.":
    "deletion needs approval. The built-in writer and reader endpoints are untouched.",
  "삭제 승인 요청": "Request delete approval",

  // ── engine-config-panel.tsx ──────────────────────────────────────────────
  "이 테이블의 구성 정보를 표시할 수 없습니다.":
    "This table's configuration cannot be shown.",
  "삭제 방지 (Deletion Protection)": "Deletion Protection",
  "암호화 (SSE)": "Encryption (SSE)",
  "AWS 소유 키 (기본)": "AWS owned key (default)",
  "활성 ({n})": "Enabled ({n})",
  "활성 (토큰)": "Enabled (token)",
  "활성 (RBAC)": "Enabled (RBAC)",
  "이 클러스터의 구성 정보를 표시할 수 없습니다.":
    "This cluster's configuration cannot be shown.",
  "파라미터 그룹": "Parameter group",
  "유지보수 윈도우 (Maintenance Window)": "Maintenance Window",
  "스냅샷 보관 (Retention)": "Snapshot retention",
  "스냅샷 윈도우 (Snapshot Window)": "Snapshot Window",
  "저장 시 암호화 (At-Rest)": "Encryption at rest",
  "전송 중 암호화 (In-Transit / TLS)": "Encryption in transit (TLS)",
  "자동 Failover": "Automatic failover",
  "파라미터 (parameter group)": "Parameters (parameter group)",
  "스토리지 암호화 (Encrypted)": "Storage encryption",
  "클러스터 파라미터 그룹": "Cluster parameter group",
  "백업 보관 기간 (Retention)": "Backup retention",

  // ── engine-internals-panel.tsx ───────────────────────────────────────────
  "캐시 히트율 (shared buffers)": "Cache hit ratio (shared buffers)",
  "트랜잭션 롤백 비율": "Transaction rollback ratio",
  "강제 체크포인트 비율": "Forced checkpoint ratio",
  "Temp 파일 스필 (누적)": "Temp file spill (cumulative)",
  "버퍼 풀 히트율": "Buffer pool hit rate",
  "대기 중 I/O": "Pending I/O",
  "Row Ops 처리량": "Row ops throughput",
  "버퍼 캐시 히트율": "Buffer cache hit ratio",
  "Total / Target 메모리": "Total / Target memory",
  "차단된 프로세스": "Blocked processes",
  "대기 중 메모리 그랜트": "Pending memory grants",
  "wait 데이터가 없어요": "No wait data",
  "엔진 내부 지표": "Engine internals",

  // ── event-detail-modal.tsx ───────────────────────────────────────────────
  동작: "Action",
  "클러스터 ID": "Cluster ID",
  "인스턴스 ID": "Instance ID",
  "스냅샷 ID": "Snapshot ID",
  엔진: "Engine",
  "에러 코드": "Error code",
  "에러 메시지": "Error message",
  "호출 주체": "Invoked by",
  "소스 IP": "Source IP",
  "이벤트 시각": "Event time",
  "cluster_id를 가져오지 못했어요. 대시보드를 새로고침하세요":
    "Could not read cluster_id. Refresh the dashboard",
  "요약 + AI": "Summary + AI",
  "원본 이벤트": "Raw event",
  "주요 정보": "Key facts",
  "다시 분석": "Re-analyze",
  "원인 설명 + 조치": "Explain cause + action",
  "버튼을 누르면 변경 사항, 영향, 권장 조치 한 가지를 받을 수 있어요.":
    "gives you what changed, its impact and one recommended action.",

  // ── events-panel.tsx ─────────────────────────────────────────────────────
  "기타 (RDS)": "Other (RDS)",
  "이벤트 클릭 시 상세 + AI 설명": "Click an event for detail + an AI reading",
  "최근 이벤트 없음": "No recent events",
  "메시지 없음: 클릭해 원본 이벤트 확인":
    "No message: click to see the raw event",
  "이 인시던트를 Chat에서 RCA 진단": "Run an RCA on this incident in Chat",
  "💬 진단": "💬 Diagnose",

  // ── extensions-card.tsx ──────────────────────────────────────────────────
  "PostgreSQL 전용 패널입니다.": "This panel is PostgreSQL only.",
  "DBOps가 PG 클러스터에 권장하는 모듈: 미설치 항목은 hover로 이유 확인.":
    "Modules DBOps recommends for a PG cluster. Hover an uninstalled one for why.",

  // ── incident-summary.tsx ─────────────────────────────────────────────────
  "즉시 확인이 필요한 신호가 있습니다": "Signals that need attention right now",
  "주의가 필요한 신호가 있습니다": "Signals worth a look",
  "타임라인 →": "Timeline →",

  // ── index-recs-panel.tsx ─────────────────────────────────────────────────
  "sequential scan이 index scan보다 우세한 테이블: 신규 인덱스 후보":
    "Tables where sequential scans beat index scans: index candidates",
  "후보 없음: 인덱스 상태 양호!": "No candidates, indexing looks healthy",
  "추정 live 행 수 (pg_stat_user_tables.n_live_tup)":
    "Estimated live rows (pg_stat_user_tables.n_live_tup)",
  "통계 리셋 이후의 sequential scan 횟수":
    "Sequential scans since the stats reset",
  "sequential scan ÷ (sequential + index scan). 값이 클수록 인덱스를 활용하지 못하는 쿼리가 많다는 의미.":
    "sequential scan / (sequential + index scan). The higher it is, the more queries are missing an index.",
  "sequential scan으로 읽은 행 수 (pg_stat_user_tables.seq_tup_read)":
    "Rows read by sequential scans (pg_stat_user_tables.seq_tup_read)",

  // ── live-top-panel.tsx ───────────────────────────────────────────────────
  "라이브 조회 실패": "Live read failed",
  "버퍼풀 조회에 실패했습니다": "Could not read the buffer pool",
  "라이브 세션 (top)": "Live sessions (top)",
  일시정지: "Pause",
  재개: "Resume",
  "닫기 (Esc)": "Close (Esc)",
  "라이브 (이 창이 열려 있는 동안에만 대상 DB를 폴링합니다, ~2초)":
    "Live (polls the target DB only while this panel is open, every ~2s)",
  ", 탭 비활성: 일시중단됨": ", tab hidden: suspended",
  ", 일시정지됨": ", paused",
  "라이브 조회를 사용할 수 없습니다.": "Live read is not available.",
  "블로킹 감지 ({n})": "Blocking detected ({n})",
  "활성 세션": "Active sessions",
  ", age 내림차순": ", by age desc",
  "활성 세션이 없습니다": "No active sessions",
  "버퍼풀 (pg_buffercache)": "Buffer pool (pg_buffercache)",
  새로고침: "Refresh",
  "조회 중…": "Reading…",
  "무거운 조회입니다. 폴링에 포함되지 않으며 버튼을 눌러 1회만 조회합니다.":
    "This read is heavy. It is never polled, the button runs it once.",
  사용: "Used",
  버퍼: "buffers",

  // ── locks-panel.tsx ──────────────────────────────────────────────────────
  "다른 락에 의해 대기 중인 트랜잭션 (최근 15분)":
    "Transactions waiting on another lock (last 15 min)",
  "평면 목록: (대기, 보유) 쌍을 한 행씩":
    "Flat list: one row per (waiter, holder) pair",
  "의존성 체인: 루트 보유자 → 대기자, 재귀 구조":
    "Dependency chain: root holder → waiters, recursive",
  "블로킹 락 없음": "No blocking locks",
  "순환 데드락 감지. 명확한 루트 보유자가 없습니다. 원본 엣지는 목록 뷰에서 확인하세요.":
    "Circular deadlock detected, there is no single root holder. The raw edges are in the list view.",

  // ── log-insights-panel.tsx ───────────────────────────────────────────────
  "기간 내 모든 로그 라인": "Every log line in the window",
  "log_min_duration_statement에 잡힌 라인":
    "Lines caught by log_min_duration_statement",
  "automatic vacuum / analyze 라인": "automatic vacuum / analyze lines",
  "ERROR / FATAL / PANIC 라인": "ERROR / FATAL / PANIC lines",
  "CloudWatch Logs Insights에서 카테고리별로 PostgreSQL 로그 라인을 가져옵니다. CW 스캔 비용이 발생하므로 자동 새로고침은 없습니다.":
    "Pulls PostgreSQL log lines by category from CloudWatch Logs Insights. A CW scan costs money, so there is no auto-refresh.",
  "검색어 (space=AND)": "search (space=AND)",
  "공백으로 구분된 키워드가 모두 포함된 라인만 보여줍니다. 예: 'ERROR connection refused'":
    "Shows only the lines that contain every space-separated keyword. For example 'ERROR connection refused'",
  "로그 가져오기": "Fetch logs",
  갱신: "refreshed",
  "CloudWatch 콘솔에서 원본 로그 보기":
    "Open the raw logs in the CloudWatch console",
  "CW 콘솔 열기 →": "Open CW console →",
  "버튼을 눌러 카테고리를 선택하세요. 첫 호출은 CW Logs Insights 쿼리를 시작하며 5~10초 정도 걸립니다.":
    "runs the query for the selected category. The first call takes 5 to 10 seconds.",
  "활성화 방법:": "How to enable:",
  'RDS 콘솔 → Modify cluster → "Log exports" 섹션에서':
    'RDS console → Modify cluster → "Log exports", tick',
  "체크 + 적용. 또는 파라미터 그룹에서":
    "and apply. Or, in the parameter group,",
  "+ 인스턴스 reboot.": "+ reboot the instance.",
  "AWS 문서 →": "AWS docs →",
  "해당 카테고리에 매칭되는 로그 라인 없음 ({n}h 기준)":
    "No log line matches this category ({n}h window)",

  // ── long-running-panel.tsx ───────────────────────────────────────────────
  "5초 이상 실행 중인 활성 쿼리 (최근 15분)":
    "Active queries running over 5s (last 15 min)",
  "장기 실행 쿼리 없음": "No long-running queries",

  // ── dashboard/maintenance-health-panel ───────────────────────────────────
  "DBA가 조치할 항목을 심각도 순으로 정렬했어요. 행을 클릭하면 AI가 조치를 제안합니다.":
    "Items for a DBA to act on, sorted by severity. Click a row and the AI proposes a fix.",
  "{n} 갱신": "updated {n}",
  "발견된 이슈가 없어요. 클러스터 상태 양호 🎉":
    "No issues found. The cluster looks healthy 🎉",
  "{n} 카테고리에 해당하는 항목이 없어요": "Nothing in the {n} category",
  신뢰도: "confidence",
  "초기 권장 조치": "Initial recommendation",
  "상세 컨텍스트": "Detail context",
  "AI 조치 제안": "AI fix proposal",
  "원인 + 조치": "Cause + fix",
  "버튼을 누르면 리스크 설명 + 정확한 명령어 + 검증 방법을 받아볼 수 있어요.":
    "button gives you the risk, the exact command and a way to verify.",
  "설명만 확인했다면 닫아도 됩니다. 실제 조치가 필요하면:":
    "If you only wanted the explanation, close this. If you need to act:",
  "Chat에서 조치 진행 →": "Act on it in Chat →",

  // ── dashboard/queries-panel ──────────────────────────────────────────────
  "쿼리 없음": "No queries",
  "해당 정규화 쿼리가 이 윈도우 안에서 실행된 횟수":
    "How many times this normalized query ran in this window",
  "모든 호출의 누적 실행 시간": "Cumulative execution time across all calls",
  "호출당 평균 시간 (총 시간 ÷ 호출 수)":
    "Average time per call (total time / calls)",
  "(원본 없음)": "(no source text)",

  // ── dashboard/query-detail-modal ─────────────────────────────────────────
  "Chat에서 분석": "Analyze in Chat",
  "스냅샷 없음": "No snapshots",

  // ── dashboard/rds-instance-overview-panel ────────────────────────────────
  "인스턴스 개요": "Instance overview",
  "인스턴스 상세 정보를 불러오지 못했습니다.":
    "Could not load the instance details.",
  "리소스 사용률 (Resource Usage)": "Resource usage",
  "메트릭을 불러오지 못했습니다. 잠시 후 다시 시도합니다.":
    "Could not load the metrics. Retrying shortly.",

  // ── dashboard/redundant-indexes-panel ────────────────────────────────────
  "prefix-covered / 완전 중복 / unused 인덱스를 라이브 클러스터에서 검출. PostgreSQL 전용.":
    "Finds prefix-covered, fully duplicate and unused indexes on the live cluster. PostgreSQL only.",
  "{n}개 인덱스 스캔": "{n} indexes scanned",
  "검출 실행": "Run detection",
  "버튼을 누르면 라이브 클러스터의 pg_index를 한 번 조회해서 후보를 뽑아냅니다. 인덱스가 많을수록 1~3초 정도 걸립니다.":
    "runs one pg_index query on the live cluster and pulls out the candidates. Expect 1 to 3 seconds, longer with many indexes.",
  "검출된 중복/미사용 인덱스 없음. 인덱스 구성 양호 🎉":
    "No duplicate or unused indexes found. Index layout looks good 🎉",
  "회수 가능 디스크 ≈": "Reclaimable disk ≈",
  ", 드롭 전에 항상": ", always verify the real query impact with",
  "으로 실제 쿼리 영향 검증 권장": " before dropping",
  "통계 리셋 이후 scan 횟수": "Scans since the last stats reset",
  "이 인덱스 컬럼이 다른 인덱스의 앞부분에 그대로 포함됩니다. 더 긴 인덱스가 같은 쿼리를 처리할 수 있어 드롭 후보.":
    "This index's columns are the leading prefix of another index. The longer index can serve the same queries, so it is a drop candidate.",
  "다른 인덱스와 컬럼 구성이 완전히 같습니다. 마이그레이션 잔여물일 가능성. 작은 쪽을 드롭.":
    "Column list is identical to another index. Likely a migration leftover. Drop the smaller one.",
  "통계 리셋 이후 idx_scan = 0. unique/PK 제약을 받쳐주지 않으면 드롭 검토 대상.":
    "idx_scan = 0 since the last stats reset. A drop candidate unless it backs a unique or PK constraint.",

  // ── dashboard/replication-topology-panel ─────────────────────────────────
  "Writer + readers: 인스턴스별 Replica Lag (CloudWatch 15분 윈도우 최신 datapoint)":
    "Writer + readers: Replica Lag per instance (latest datapoint in a 15 minute CloudWatch window)",
  "클러스터 멤버 정보가 비어 있습니다.":
    "The cluster member list came back empty.",
  "writer 없음": "no writer",
  "reader 없음: single-node 클러스터입니다. 운영 환경이면 최소 1개 reader 추가를 권장 (failover RTO 단축).":
    "No reader: this is a single-node cluster. In production at least one reader is recommended (shorter failover RTO).",
  "AuroraReplicaLag: CloudWatch 15분 윈도우 최신값":
    "AuroraReplicaLag: latest value in a 15 minute CloudWatch window",
  "Aurora promotion tier: 숫자가 낮을수록 failover 우선순위가 높습니다":
    "Aurora promotion tier: a lower number means higher failover priority",

  // ── dashboard/schema-changes-panel ───────────────────────────────────────
  "{n} 스키마는 최근 카탈로그 읽기에서 확인되지 않았습니다":
    "{n} was not confirmed in the latest catalog read",
  "(마지막 확인 {n})": "(last confirmed {n})",
  ". 삭제됐을 수도 있고 읽기가 도달하지 못한 것일 수도 있어 삭제로 단정하지 않습니다.":
    ". It may have been dropped, or the read may not have reached it, so it is not called a drop.",
  "DDL 비교 {a}개 schema, 행 수 비교 {b}개 table":
    "DDL compared across {a} schemas, row counts across {b} tables",
  "이 구간에서 감지된 변경 없음": "No change detected in this window",
  "일부 신호만 판정됨: 변경 없음이라고 볼 수 없음":
    "Only some signals were compared: this is not a verdict of no change",
  "수집 이력이 없어 변경 여부를 판정할 수 없음":
    "No collection history, so no verdict on whether anything changed",
  "비교 가능한 이력이 부족해 변경 여부를 판정할 수 없음":
    "Too little comparable history for a verdict on whether anything changed",
  "이 엔진은 스키마 변경 판정 대상이 아님: 기다려도 판정되지 않음":
    "This engine is out of scope for a schema change verdict: waiting will not produce one",
  "변경 여부를 판정할 수 없음 (알 수 없는 응답 상태)":
    "No verdict on whether anything changed (unknown response status)",
  "table_stats는 매 주기 상위 100개 테이블만 기록하므로 이 테이블의 행 수는 수집되지 않았습니다":
    "table_stats records only the 100 largest tables each cycle, so this table's row count was not collected",
  "증감 {n}": "Change {n}",
  "이 변경 유형을 해석할 수 있는 화면 버전이 아닙니다. 위 유형 이름과 테이블 이름은 서버가 보낸 값입니다.":
    "This UI version cannot interpret this change type. The type name and table name above are what the server sent.",
  "테이블 생성, 삭제, 이름변경 또는 행 수가 크게 변한 항목":
    "Tables created, dropped, renamed, or with a large row-count change",
  "최근 1일": "Last 1 day",
  "최근 7일": "Last 7 days",
  "최근 30일": "Last 30 days",
  "스키마 변경 조회 실패": "Schema change lookup failed",
  "변경이 없다는 뜻이 아닙니다. 잠시 후 다시 시도하세요.":
    "That does not mean nothing changed. Try again shortly.",
  "전체 {a}건 중 {b}건만 표시": "Showing {b} of {a} changes",
  "행 수 미상": "row count unknown",
  "알 수 없는 변경 유형": "unknown change type",
  행: "rows",
  "행 손실": "rows lost",

  // ── dashboard/settings-panel ─────────────────────────────────────────────
  "설정값은 5분 주기로 수집됩니다. 파라미터 변경은 pending-reboot로 적용되어":
    "Settings are collected every 5 minutes. A parameter change applies as pending-reboot, so the running value changes",
  "인스턴스 재시작 후": "after an instance restart",
  "동작값에 반영됩니다.": "only.",
  "수집된 설정이 없습니다": "No settings collected",
  파라미터: "Parameters",
  "(변경 {n})": "({n} changed)",
  "이 엔진에는 파라미터 그룹 디폴트 비교가 적용되지 않습니다":
    "This engine has no parameter group default comparison",
  "디폴트 비교 미조회": "Default comparison not retrieved",
  "수집된 파라미터가 없습니다": "No parameters collected",
  "파라미터 이름 검색": "Search parameter name",
  "변경만 보기": "Changed only",
  "일치하는 파라미터가 없습니다": "No matching parameters",
  이전: "Prev",
  다음: "Next",
  현재값: "Current",
  디폴트값: "Default",
  변경됨: "Changed",
  "체크포인트 타이밍 분석에 필요합니다.":
    "Needed for checkpoint timing analysis.",
  "pgBadger 세션 리포트에 필요합니다.":
    "Needed for the pgBadger session report.",
  "락 경합 진단이 이 설정에 의존합니다.":
    "Lock contention diagnosis depends on this setting.",
  "0이면 모든 autovacuum을 로깅합니다. pgBadger가 bloat와 상관분석합니다.":
    "0 logs every autovacuum. pgBadger correlates it with bloat.",
  "1초 미만 쿼리는 로깅 제외가 적절합니다. 1000ms가 합리적인 하한입니다.":
    "Queries under 1s are reasonably left unlogged. 1000ms is a sensible floor.",
  "디스크로 스필하는 쿼리를 잡아냅니다.": "Catches queries that spill to disk.",
  "느린 쿼리 로깅을 켜야 성능 분석이 가능합니다.":
    "Slow query logging has to be on for performance analysis.",
  "1초 이상 쿼리를 느린 쿼리로 기록. 워크로드에 따라 조정.":
    "Logs queries over 1s as slow. Tune to the workload.",
  "1이 완전한 ACID 내구성. 성능을 위해 2로 낮추는 건 트레이드오프.":
    "1 is full ACID durability. Dropping to 2 for performance is a tradeoff.",
  "바이너리 로그(PITR/복제). Aurora는 클러스터 스토리지가 대신 처리하고, 표준 RDS MySQL은 이 설정이 리드 리플리카와 PITR의 전제입니다.":
    "Binary log (PITR / replication). Aurora handles it in cluster storage; on standalone RDS MySQL this setting is the prerequisite for read replicas and PITR.",

  // ── dashboard/table-sizes-panel ──────────────────────────────────────────
  "전체 {a}, {b}개 테이블 (상위 30)": "{a} total across {b} tables (top 30)",
  "아직 테이블 크기 데이터 없음 (PG 전용, 다음 ETL 사이클에서 수집)":
    "No table size data yet (PG only, collected on the next ETL cycle)",
  "디스크상 heap 크기 (인덱스/TOAST 제외)":
    "Heap size on disk (excludes indexes and TOAST)",
  "이 테이블의 모든 인덱스 크기 합계":
    "Total size of every index on this table",
  "Heap + 인덱스 + TOAST": "Heap + indexes + TOAST",
  "인덱스 크기 ÷ 전체 크기. 50%를 넘으면 인덱스가 heap보다 큰 상태. 중복 인덱스 검토 필요.":
    "Index size / total size. Over 50% means the indexes outweigh the heap. Review for redundant indexes.",
  "클릭해서 이 테이블의 인덱스 보기": "Click to see this table's indexes",
  "인덱스 없음 (heap 전용 테이블)": "No indexes (heap only table)",
  "이 인덱스가 쿼리에서 사용된 횟수 (pg_stat_user_indexes.idx_scan)":
    "How often queries used this index (pg_stat_user_indexes.idx_scan)",
  "인덱스 디스크 크기": "Index size on disk",
  "기본 키": "Primary key",
  "유니크 인덱스": "Unique index",
  "유효하지 않은 인덱스 (CONCURRENT 빌드 실패 등)":
    "Invalid index (a failed CONCURRENTLY build, for example)",
  "통계 리셋 이후 한 번도 사용 안 됨: DROP 후보":
    "Never used since the last stats reset: a DROP candidate",

  // ── dashboard/timeseries-chart ───────────────────────────────────────────
  "최대: {n}": "Max: {n}",

  // ── dashboard/vacuum-panel ───────────────────────────────────────────────
  "<1시간": "<1h",
  "bloat 비율(dead / 전체 튜플) 내림차순 정렬":
    "Sorted by bloat ratio (dead / total tuples), descending",
  "테이블 통계 없음 (PG 전용, 5분 주기 수집)":
    "No table stats (PG only, collected every 5 minutes)",
  "Dead tuples: VACUUM 대상으로 남아 있는 미회수 행 버전":
    "Dead tuples: unreclaimed row versions waiting for VACUUM",
  "Dead ÷ (live + dead). 30% 초과 시 bloat 심각: VACUUM 권장":
    "Dead / (live + dead). Over 30% is serious bloat: VACUUM recommended",
  "age(relfrozenxid): 마지막 FREEZE 이후 트랜잭션 수. 2억 = 경고, 15억 = wraparound 위험":
    "age(relfrozenxid): transactions since the last FREEZE. 200M = warning, 1.5B = wraparound risk",
  "마지막 autovacuum 또는 수동 VACUUM 이후 경과 시간":
    "Time since the last autovacuum or a manual VACUUM",

  // ── dashboard/wait-events-panel ──────────────────────────────────────────
  "wait event 데이터가 없어요": "No wait event data",

  // ── shared keys, also wrapped outside these panels ───────────────────────
  "불러오는 중…": "Loading…",
  닫기: "Close",
  테이블: "Table",
  인덱스: "Index",
  크기: "Size",
  전체: "All",

  // ── dashboard/schema-changes-panel (chip labels, consumer-wrapped) ───────
  "DDL 판정됨": "DDL compared",
  "DDL 미수집": "DDL not collected",
  "DDL 비교 불가": "DDL not comparable",
  "DDL baseline만": "DDL baseline only",
  "DDL 구간 밖": "DDL outside the window",
  "DDL 조회 불가": "DDL read unavailable",
  "DDL 판정 미지원 엔진": "DDL comparison unsupported on this engine",
  "스키마 존재 확인됨": "Schemas confirmed present",
  "확인 안 된 스키마 있음": "Some schemas unconfirmed",
  "스키마 관측 기록 없음": "No schema observation on record",
  "스키마 관측 조회 불가": "Schema observation read unavailable",
  "스키마 관측 미지원 엔진": "Schema observation unsupported on this engine",
  "행 수 비교됨": "Row counts compared",
  "행 수 이력 부족": "Row count history too short",
  "행 수 미수집": "Row counts not collected",
  "수집 최신": "Collection current",
  "수집 지연": "Collection stale",
  "수집 기록 없음": "No collection on record",

  // ── dashboard/settings-panel ─────────────────────────────────────────────
  적용: "Apply",

  // ── app-shell (nav) ──────────────────────────────────────────────────────
  "알림 없음": "No alerts",
  "알림 {n}개: critical {c}, warning {w}":
    "{n} alerts: {c} critical, {w} warning",
  "알림 닫기": "Dismiss alert",

  // ── app/approvals ────────────────────────────────────────────────────────
  // The caveat sentence that follows this headline is shared with scaleout:
  // "…빈 목록이 아니라 조회 실패 상태입니다." An operator must not read a
  // failed fetch as an empty approval queue.
  "승인 목록을 불러오지 못했습니다": "Could not load the approval list",

  // ── approval/approval-card ───────────────────────────────────────────────
  승인: "Approve",

  // ── auth-button ──────────────────────────────────────────────────────────
  로그인: "Sign in",
  로그아웃: "Sign out",

  // ── chat/chat-panel ──────────────────────────────────────────────────────
  "알 수 없는 스트림 오류": "Unknown stream error",
  "대화 {n}개를 모두 삭제할까요? 되돌릴 수 없습니다.":
    "Delete all {n} conversations? This cannot be undone.",
  "를 눌러 시작하세요": "to start",
  "마지막 오류": "Last error",
  "알 수 없는 오류": "Unknown error",
  "새 대화를 시작하세요": "Start a new conversation",
  "대화를 Markdown(.md)으로 다운로드":
    "Download the conversation as Markdown (.md)",
  "대화 전체를 PDF로 저장. 채팅 내용만 담긴 브라우저 인쇄 창이 열립니다.":
    "Save the whole conversation as PDF. Opens a browser print window with the chat content only.",
  "이 대화의 모든 메시지를 지울까요?":
    "Clear every message in this conversation?",
  "이 대화를 삭제할까요?": "Delete this conversation?",
  "자연어로 Aurora 운영을 위임하세요. agent가 MCP 툴로 메트릭/스키마/EXPLAIN을 호출합니다.":
    "Delegate Aurora operations in plain language. The agent reaches metrics, schema and EXPLAIN through MCP tools.",
  "최근 1시간 동안 가장 느린 쿼리 5개 분석해줘":
    "Analyze the 5 slowest queries in the last hour",
  "현재 클러스터의 health score는?":
    "What is the current cluster's health score?",
  "blocking lock 있으면 보여줘": "Show me any blocking locks",
  "vacuum 안 된 테이블 찾아줘": "Find tables that have not been vacuumed",
  "## 질문": "## Question",
  "## 진단 + 조치": "## Diagnosis and actions",
  "예: prod-cluster의 slow query를 분석해줘":
    "e.g. Analyze the slow queries on prod-cluster",
  "Runbook으로 저장": "Save as runbook",
  "본문 (Markdown: 자동 채워짐, 편집 가능)":
    "Body (Markdown, prefilled and editable)",
  "태그 (콤마 구분)": "Tags (comma separated)",
  "저장 중…": "Saving…",
  "Runbook으로 저장됨.": "Saved as a runbook.",
  "보기 →": "View →",
  "AI 근본원인 분석": "AI root cause analysis",

  // ── chat/message-list ────────────────────────────────────────────────────
  "이 진단을 Runbook으로 저장: 같은 패턴 재발 시 곧바로 참조":
    "Save this diagnosis as a runbook, ready the next time the pattern recurs",
  "✓ Runbook 저장": "✓ Save runbook",
  "응답이 중단되었습니다": "The response was interrupted",
  "다시 생성": "Regenerate",
  "DBA 승인이 등록되었습니다": "A DBA approval has been filed",
  "Approval Center 열기 →": "Open Approval Center →",

  // ── clusters/setup-guide-modal ───────────────────────────────────────────
  "-- 1) DBOps 전용 read-only 롤 생성":
    "-- 1) Create the dedicated DBOps read-only role",
  "CREATE ROLE dbops_readonly LOGIN PASSWORD '<생성한-비밀번호-붙여넣기>';":
    "CREATE ROLE dbops_readonly LOGIN PASSWORD '<paste-the-generated-password>';",
  "-- 2) DBOps에 필요한 최소 권한만 부여":
    "-- 2) Grant only the privileges DBOps needs",
  "GRANT pg_monitor TO dbops_readonly;            -- pg_stat_statements, pg_stat_activity 등":
    "GRANT pg_monitor TO dbops_readonly;            -- pg_stat_statements, pg_stat_activity, etc.",
  "GRANT pg_read_all_stats TO dbops_readonly;     -- PG15+: pg_stat_user_indexes / tables 포함":
    "GRANT pg_read_all_stats TO dbops_readonly;     -- PG15+: includes pg_stat_user_indexes / tables",
  "-- (선택) 특정 스키마 내부까지 인스펙션이 필요할 때만:":
    "-- (optional) only if you need to inspect inside a specific schema:",
  "-- 1) DBOps 전용 read-only 유저 생성":
    "-- 1) Create the dedicated DBOps read-only user",
  "CREATE USER 'dbops_readonly'@'%' IDENTIFIED BY '<생성한-비밀번호-붙여넣기>';":
    "CREATE USER 'dbops_readonly'@'%' IDENTIFIED BY '<paste-the-generated-password>';",
  "GRANT SELECT ON mysql.* TO 'dbops_readonly'@'%';  -- 유저 감사용":
    "GRANT SELECT ON mysql.* TO 'dbops_readonly'@'%';  -- for user auditing",
  "# 3) 강력한 랜덤 비밀번호 생성 (32자)":
    "# 3) Generate a strong random password (32 chars)",
  "# → 위 CREATE ROLE / CREATE USER 단계에서 이 값을 사용":
    "# → use this value in the CREATE ROLE / CREATE USER step above",
  "# 4) DBOps 컨벤션 이름으로 AWS Secrets Manager에 등록":
    "# 4) Store it in AWS Secrets Manager under the DBOps convention name",
  "# DBOps Discover가 이 시크릿을 자동으로 찾아 연결합니다. ARN 수동 입력 불필요.":
    "# DBOps Discover finds and attaches this secret automatically. No manual ARN needed.",
  "클러스터 설정 가이드": "Cluster setup guide",
  "DBOps 전용 계정 + Secrets Manager 등록 (프로덕션 권장)":
    "Dedicated DBOps account + Secrets Manager (recommended for production)",
  대상: "Target",
  "아래 SQL을": "Run the SQL below with the",
  "대상 클러스터에서 admin(master)": "target cluster's admin (master)",
  "계정으로 실행하세요. 생성되는 롤에는 모니터링/읽기 권한만 부여되며 DDL, write, 롤 관리 권한은 포함되지 않습니다.":
    "account. The role it creates gets monitoring and read privileges only, not DDL, write or role management.",
  "강력한 비밀번호 생성": "Generate a strong password",
  "DBOps 네이밍 컨벤션으로": "Register it in",
  "에 등록: bulk Discover가 자동으로 찾아 연결합니다":
    "under the DBOps naming convention: bulk Discover finds and attaches it automatically",
  "왜 전용 계정을 만들어야 하나요?": "Why a dedicated account?",
  "Aurora master 계정은 클러스터 전체에 대한 admin 권한을 가집니다. DBOps에 그 수준의 접근권을 부여하면 DBOps Lambda가 침해됐을 때 모든 스키마와 자격증명이 노출됩니다. 위에서 만든":
    "The Aurora master account holds admin rights over the whole cluster. Granting DBOps that level exposes every schema and credential if the DBOps Lambda is ever compromised. The",
  "롤은 모니터링 카탈로그와 테이블 통계는 읽을 수 있지만 데이터 변경이나 롤 관리는 불가능. blast radius가 훨씬 작습니다.":
    "role created above can read the monitoring catalogs and table statistics but cannot change data or manage roles. The blast radius is far smaller.",
  "컨벤션 이름": "Convention name",
  "Bulk Discover가 이 시크릿을 발견하면":
    "When bulk Discover finds this secret it attaches automatically with a",
  "배지와 함께 자동 연결. 시크릿이 없으면 master 시크릿으로 폴백하면서":
    "badge. With no such secret it falls back to the master secret and shows a",
  "경고가 표시됩니다. 런타임은 동작하지만 프로덕션 전환 전에 전용 계정을 등록하는 게 좋습니다.":
    "warning. The runtime still works, but register a dedicated account before going to production.",
  "복사됨!": "Copied!",

  // ── design-system/cluster-dropdown ───────────────────────────────────────
  "{n}: 클러스터 전환": "{n}: switch cluster",
  "클러스터 선택": "Select a cluster",
  "클러스터 검색…": "Search clusters…",
  "결과 없음": "No matches",

  // ── design-system/command-palette ────────────────────────────────────────
  "페이지 검색...": "Search pages...",
  "⌘K로 열기, Enter로 이동, Esc로 닫기":
    "⌘K to open, Enter to go, Esc to close",
  "Fleet: 전체 클러스터": "Fleet: all clusters",
  "Dashboard: 단일 클러스터": "Dashboard: one cluster",
  "Compare: 비교 분석": "Compare: side by side",
  "SLO: 가용성과 지연 예산": "SLO: availability and latency budget",
  "Schema: FK 계보와 의존성": "Schema: FK lineage and dependencies",
  "Chat: AI 대화": "Chat: AI conversation",
  "Query Lab: SQL 분석": "Query Lab: SQL analysis",
  "Approvals: 승인 센터": "Approvals: approval center",
  "Ask the fleet: 자연어 질의": "Ask the fleet: natural-language query",
  "Runbooks: 진단과 처방": "Runbooks: diagnosis and remedy",
  "Simulator: what-if 시뮬": "Simulator: what-if runs",
  "Timeline: 통합 인시던트 피드": "Timeline: unified incident feed",
  "Activity: 감사와 회고 로그": "Activity: audit and retrospective log",
  "Workload diff: 쿼리 변화": "Workload diff: query change",
  "Alerts: 규칙과 구독자": "Alerts: rules and subscribers",
  "Clusters: 클러스터 관리": "Clusters: cluster management",
  "Reports: 예약 리포트": "Reports: scheduled reports",
  "Cost: Bedrock 비용": "Cost: Bedrock spend",
  "Memory: 에이전트 기억": "Memory: what the agent remembers",
  "Settings: 기능 토글, 티켓팅, 리포트 전달":
    "Settings: feature toggles, ticketing, report delivery",
  "Approval policies: 지정 승인자 라우팅":
    "Approval policies: designated approver routing",
  "Context files: 에이전트 참조 컨텍스트":
    "Context files: agent reference context",
  "Onboarding: 멤버 계정 연결 위저드": "Onboarding: member account wizard",
  "Users: 사용자 역할 관리": "Users: role management",
  "Health: 자체 모니터링": "Health: self-monitoring",

  // ── design-system/metric-hint ────────────────────────────────────────────
  "{n} 설명": "{n} explained",

  // ── design-system/rca-button ─────────────────────────────────────────────
  "AI 에이전트가 최근 신호를 상관분석해 근본 원인 후보를 정리합니다":
    "The AI agent correlates recent signals and lists root cause candidates",

  // ── design-system/searchable-cluster-select ──────────────────────────────
  "검색…": "Search…",

  // ── onboarding-modal ─────────────────────────────────────────────────────
  환영합니다: "Welcome",
  "AI 기반 Aurora 운영 콘솔": "AI-powered Aurora operations console",
  "DBOps는 Amazon Aurora MySQL / PostgreSQL을 플릿 단위로 운영하는 DBA를 위해 만든 콘솔입니다. 모든 패널이 에이전트와 연결돼 있어, 이상 징후, 이벤트, 발견 항목을 클릭하면":
    "DBOps is a console for DBAs running Amazon Aurora MySQL / PostgreSQL at fleet scale. Every panel is wired to the agent, so clicking an anomaly, an event or a finding gets you",
  "한 번에 원인 분석과 조치 제안":
    "root cause analysis and a suggested fix in one step",
  "을 받을 수 있습니다.": ".",
  "1단계": "Step 1",
  " 페이지에서 시작하세요. 한두 개라면 수동 등록 폼을, 플릿 규모라면":
    " is where you start. For one or two, use the manual form. At fleet scale, use the",
  "버튼을 사용하면 됩니다. DBOps가 계정 내(또는 크로스 어카운트 롤 경유) 모든 Aurora 클러스터를 나열하고, 체크한 것만 등록합니다.":
    "button. DBOps lists every Aurora cluster in the account (or through a cross-account role) and registers only the ones you check.",
  "Clusters 페이지로 이동 →": "Go to Clusters →",
  "2단계": "Step 2",
  "약 5분 대기": "Wait about 5 minutes",
  "ETL이 5분 주기로 메트릭, 테이블 통계, 락, 점검 결과를 수집합니다. 첫 사이클 전까지 대시보드는":
    "ETL collects metrics, table statistics, locks and check results every 5 minutes. Until the first cycle lands, the dashboard reads",
  "으로 표시됩니다. 이 시간 동안": ". Meanwhile, in",
  "에서 Slack / PagerDuty 구독자를 등록해두면, 임계치를 초과하는 즉시 알림을 받을 수 있습니다.":
    "you can register Slack / PagerDuty subscribers and be notified the moment a threshold is crossed.",
  "Alerts 설정하기 →": "Set up Alerts →",
  "3단계": "Step 3",
  "자연어로 운영하기": "Operate in plain language",
  "을 열고 다음과 같이 물어보세요:": "is where you ask something like:",
  "prod-pg에서 최근 슬로우 쿼리 분석해줘":
    "Analyze the recent slow queries on prod-pg",
  또는: "or",
  "analytics 클러스터에서 CPU가 왜 튀는지 알려줘":
    "Tell me why CPU is spiking on the analytics cluster",
  "에이전트가 MCP 툴로 Performance Insights를 보고, EXPLAIN을 돌리고, 조치를 제안합니다. 기본은 읽기 전용이며, 변경 작업은 Approval Center를 통해서만 적용됩니다.":
    "The agent reads Performance Insights through MCP tools, runs EXPLAIN and proposes a fix. It is read-only by default, and a change only lands through the Approval Center.",
  "Chat 열기 →": "Open Chat →",
  "{n}단계로 이동": "Go to step {n}",
  "나중에 보기": "Later",
  시작하기: "Get started",

  // ── query-lab/plan-tree ──────────────────────────────────────────────────
  "{a}: access_type=ALL, {b} 행을 전부 훑습니다. WHERE와 JOIN 컬럼에 인덱스를 검토하세요.":
    "{a}: access_type=ALL, scans all {b} rows. Consider an index on the WHERE and JOIN columns.",
  "{a}: {b} 행을 읽어 약 {c} 행만 남깁니다 (filtered {d}%). 더 선택적인 인덱스가 읽는 행 수를 줄입니다.":
    "{a}: reads {b} rows and keeps only about {c} (filtered {d}%). A more selective index cuts the rows read.",
  "using_filesort: 정렬을 인덱스 순서로 처리하지 못해 옵티마이저가 직접 정렬합니다. ORDER BY와 GROUP BY 컬럼에 맞는 인덱스로 정렬을 없앨 수 있습니다.":
    "using_filesort: index order does not serve the sort, so the optimizer sorts it itself. An index matching the ORDER BY and GROUP BY columns can remove the sort.",
  "using_temporary_table: 내부 임시 테이블을 만듭니다 (인덱스로 해결되지 않는 GROUP BY, DISTINCT, UNION에서 흔합니다). 커지면 디스크로 스필합니다.":
    "using_temporary_table: an internal temp table is built (common for a GROUP BY, DISTINCT or UNION no index resolves). It spills to disk once it grows.",
  "query_cost {n}, 전체적으로 비싼 플랜입니다.":
    "query_cost {n}, an expensive plan overall.",
  "join order: 추정값입니다 (실행 통계 아님)":
    "join order: estimated, not execution statistics",
  "MySQL의 plan-only EXPLAIN에는 옵티마이저 소요 시간과 실제 행 수가 없어, 추정 대비 실제 괴리(통계 부정확)와 디스크 스필 여부는 이 플랜으로 알 수 없습니다. EXPLAIN ANALYZE는 JSON을 내주지 않습니다.":
    "MySQL's plan-only EXPLAIN carries no optimizer time and no actual row counts, so this plan cannot tell you how far the estimates drifted (stale statistics) or whether anything spilled to disk. EXPLAIN ANALYZE does not emit JSON.",
  "이 엔진의 플랜 형식은 아직 구조화 렌더링을 지원하지 않습니다. 원본 응답을 그대로 표시합니다.":
    "This engine's plan format has no structured rendering yet. Showing the raw response.",
  "대형 테이블 Seq Scan": "Seq Scan over a large table",
  "선택성 높은 컬럼에 인덱스 추가, 또는 WHERE 절을 인덱스로 cover 되게 재작성":
    "Add an index on a selective column, or rewrite the WHERE clause so an index covers it",
  "정렬이 디스크로 스필됨": "Sort spilled to disk",
  "work_mem를 늘려 in-memory 정렬 유도 (세션 내 SET work_mem), 또는 LIMIT을 더 빠른 단계로 push down":
    "Raise work_mem to keep the sort in memory (SET work_mem in the session), or push LIMIT down to an earlier stage",
  "Hash 멀티배치 (디스크 스필)": "Hash multi-batch (spilled to disk)",
  "work_mem 부족: 빌드측 테이블이 hash table에 안 맞음. work_mem 증가 또는 join order 변경 검토":
    "work_mem is too small: the build side does not fit the hash table. Raise work_mem or reconsider the join order",
  "Bitmap recheck에서 다량 행 폐기": "Bitmap recheck discarded many rows",
  "work_mem 부족으로 lossy bitmap이 됨. 해당 인덱스 selectivity 재확인 또는 work_mem 상향":
    "Low work_mem made the bitmap lossy. Recheck that index's selectivity, or raise work_mem",
  "행 수 추정 오차": "Row count misestimate",
  "ANALYZE 실행으로 통계 갱신, default_statistics_target 상향, CREATE STATISTICS로 다중 컬럼 의존성 통계 추가":
    "Run ANALYZE to refresh statistics, raise default_statistics_target, or add CREATE STATISTICS for the multi-column dependency",
  "콜드 버퍼 읽기 (캐시 미스 높음)": "Cold buffer reads (high cache miss)",
  "shared_buffers 또는 메모리 부족. 쿼리가 처음 실행이면 두번째부터 캐시됨. 반복 실행에도 cold면 working set이 buffer를 초과":
    "shared_buffers or memory is short. A first run warms the cache for the next. If repeat runs stay cold, the working set exceeds the buffer",
  "Nested Loop 내부 반복 과다": "Nested Loop inner side ran too many times",
  "Hash Join 또는 Merge Join을 유도 (조인 컬럼에 인덱스 + ANALYZE), 또는 SET enable_nestloop=off로 검증":
    "Steer it to a Hash Join or Merge Join (index the join columns, then ANALYZE), or verify with SET enable_nestloop=off",

  // ── query-lab/query-editor ───────────────────────────────────────────────
  "실행 중...": "Running...",
  "검수 중...": "Reviewing...",
  "AI가 시맨틱을 보존하면서 성능 개선 재작성안을 제안합니다 (plan-only EXPLAIN 비교, 실행 없음)":
    "AI proposes a faster rewrite that preserves semantics (plan-only EXPLAIN comparison, nothing is executed)",
  "분석 중...": "Analyzing...",

  // ── rca/rca-candidate-detail ─────────────────────────────────────────────
  "근거 접기": "Hide basis",
  "점수 근거": "Score basis",
  "최댓값 판정": "Peak qualified",
  "평균 판정": "Average qualified",
  "구간의 초과분을 최댓값 한 개가 전부 설명합니다. 정상적인 버스트나 잘못 기록된 값과 구분할 수 없어 점수를 낮춰 반영했습니다.":
    "A single peak accounts for the whole excess in this window. That cannot be told apart from a normal burst or a bad sample, so the score was reduced.",
  "단일 샘플 (신뢰도 하향)": "Lone sample (score reduced)",
  "점수 계산": "Score derivation",
  측정값: "Measurements",
  "이 신호에 대한 조치": "Action for this signal",
  "채점 기준": "Scoring policy",
  "스키마 관측 범위": "Schema observation scope",
  "기본 가중치": "Base weight",
  "최근성 계수": "Recency factor",
  "급증 배율": "Spike multiplier",
  "심각도 계수": "Severity factor",
  "규모 배율": "Magnitude multiplier",
  계산식: "Formula",
  "판정 근거": "Qualified by",
  "단일 샘플": "Lone sample",
  메트릭: "Metric",
  "구간 평균": "Window avg",
  "평균 비율": "Avg ratio",
  "구간 최댓값": "Window max",
  "최댓값 비율": "Peak ratio",
  "구간 샘플 수": "Window samples",
  "쿼리 해시": "Query hash",
  "호출 수": "Calls",
  "총 실행시간(ms)": "Total time (ms)",
  "평균 실행시간(ms)": "Mean time (ms)",
  스키마: "Schema",
  "스냅샷 시각": "Snapshot time",
  "대기 세션": "Blocked sessions",
  "최대 대기(초)": "Max wait (s)",
  "구간 평균이 기준을 넘었습니다 (지속 상승)":
    "The window average crossed the threshold (sustained rise)",
  "구간 최댓값이 기준을 넘었습니다 (단기 포화)":
    "The window peak crossed the threshold (short saturation)",

  // ── rca/rca-drawer ───────────────────────────────────────────────────────
  "분석 중 오류가 발생했습니다": "The analysis ran into an error",
  "근본 원인 분석": "Root cause analysis",
  "{n} 저장된 분석입니다. 재분석 없이 다시 보는 중. 최신 상태가 필요하면 아래 “다시 실행”을 누르세요.":
    "Saved analysis from {n}, shown without re-running. Press “Run again” below if you need current state.",
  "최근 신호를 상관분석하는 중…": "Correlating recent signals…",
  "다시 실행": "Run again",
  "전체 대화로 이어가기 →": "Continue in full chat →",

  // ── rca/rca-report ───────────────────────────────────────────────────────
  "분석 구간 {n}분": "{n}min analysis window",
  "근거가 불완전합니다": "The evidence is incomplete",
  "리포트를 불러오지 못했습니다": "Could not load the report",
  평가: "Assessment",
  // Shown only when the stored narrative is in the OTHER language. The prose
  // itself is model output, so it is not a key and cannot be translated here;
  // the reader gets told what they are looking at and why instead.
  "이 서술과 권장 조치는 {n}로 생성되었습니다. 요청한 운영자의 콘솔 언어로 생성되며(자동 작업은 배포 기본 언어), 저장된 문장은 번역하지 않습니다.":
    "This narrative and its recommended actions were generated in {n}: the requesting operator's console language, or the deployment default for an automated task. Stored prose is not translated.",
  "근거가 된 관측": "Supporting observations",
  "시각 미기록": "Time not recorded",
  "다음 단계": "Next steps",
  "확인 (읽기 전용)": "Checks (read only)",
  "조치 (변경 작업, 승인 센터를 거칩니다)":
    "Changes (they go through the Approval Center)",
  "분류는 문구를 기준으로 추정합니다. 실행 전에 항목을 직접 확인하세요.":
    "The split is inferred from the wording. Check each item yourself before running it.",
  "자동 수집 신호에서 순위를 매길 후보를 찾지 못했습니다. 위 범위 한계를 함께 보고 수동 점검을 권장합니다.":
    "No candidate could be ranked from the collected signals. Read that together with the coverage limits above, and check manually.",
  "다른 가설 {n}건": "{n} other hypotheses",
  "순위 점수 {n}": "Rank score {n}",
  "1순위 {n}": "leader {n}",
  "분석 상세": "Analysis detail",
  "1순위 순위 점수 {n}": "Leader rank score {n}",
  "검사한 신호 수": "Signals examined",
  "실행 추적": "Execution trace",
  "총 {n}s": "{n}s total",

  // ── reports/report-viewer ────────────────────────────────────────────────
  "{n} 개 리포트": "{n} reports",
  "Fleet 전체": "Whole fleet",
  "왼쪽에서 리포트를 선택하세요": "Pick a report on the left",
  "이 리포트는 HTML 미생성": "No HTML was generated for this report",
  "HTML 파일을 새 탭에서 엽니다": "Opens the HTML file in a new tab",
  "HTML 다운로드": "Download HTML",
  "Fleet 요약": "Fleet summary",
  "클러스터 수": "Clusters",
  "총 경보": "Total alerts",
  "총 슬로우 쿼리": "Total slow queries",
  "주의가 필요한 클러스터 (Top 5)": "Clusters needing attention (top 5)",
  "전체 클러스터": "All clusters",
  경보: "Alerts",
  슬로우: "Slow",
  "스토리지 Δ": "Storage Δ",
  "24시간 요약": "24h summary",
  "1분 단위": "per minute",
  "샘플 수": "Samples",
  "AAS 메트릭": "AAS metric",
  "{a} 누적, {b} calls, mean {c}": "{a} total, {b} calls, mean {c}",
  "{n}회": "{n}x",

  // ── theme-toggle ─────────────────────────────────────────────────────────
  "라이트 모드로 전환": "Switch to light mode",
  "다크 모드로 전환": "Switch to dark mode",

  // "불러오는 중…"; both are real and neither is normalised.
  "불러오는 중...": "Loading...",
  저장: "Save",
  "다시 시도": "Try again",

  // ── app/admin/teams ──────────────────────────────────────────────────────
  "멤버 후보 조회 실패: {n}": "Member candidate fetch failed: {n}",
  "클러스터 목록 조회 실패: {n}": "Cluster list fetch failed: {n}",
  "팀 '{n}'을(를) 삭제하시겠습니까? 해당 팀에 할당된 클러스터는 할당 해제됩니다.":
    "Delete team '{n}'? Clusters assigned to it become unassigned.",
  "'{u}' 사용자를 팀 '{n}'에서 제거하시겠습니까?":
    "Remove user '{u}' from team '{n}'?",
  "클러스터 '{n}'의 팀 할당을 해제하시겠습니까?":
    "Unassign cluster '{n}' from its team?",
  "팀 목록": "Teams",
  이름: "Name",
  "멤버 수": "Members",
  관리: "Manage",
  "{n}명": "{n} members",
  "새 팀 이름": "New team name",
  "팀 만들기": "Create team",
  멤버: "Members",
  "이 팀에 멤버가 없습니다.": "No members in this team.",
  제거: "Remove",
  "멤버 추가…": "Add member…",
  "이 팀에 할당된 클러스터가 없습니다.": "No clusters assigned to this team.",
  "할당 해제": "Unassign",
  "클러스터 할당…": "Assign cluster…",
  "(현재: {n})": "(current: {n})",
  "팀 삭제": "Delete team",

  // ── app/admin/users ──────────────────────────────────────────────────────
  "{u} 사용자의 역할을 '{r}'(으)로 변경하시겠습니까?":
    "Change {u}'s role to '{r}'?",
  사용자: "User",
  변경: "Change",
  "(나)": "(you)",
  "(비활성)": "(disabled)",
  "(암묵: 명시 역할 미지정)": "(implicit: no explicit role)",
  "자신의 역할은 변경할 수 없습니다": "You cannot change your own role",
  "더 불러오기": "Load more",

  // ── shared: the delete button, wrapped at 8 call sites ───────────────────
  삭제: "Delete",

  // ── app/api-docs ─────────────────────────────────────────────────────────
  "스펙 로드 실패": "Spec load failed",
  "스펙 로드 실패: {n}. /openapi.json 이 배포됐는지 확인하세요.":
    "Spec load failed: {n}. Check that /openapi.json is deployed.",
  "Cognito JWT 필요": "Cognito JWT required",
  "공개 (Slack HMAC 또는 health probe)": "Public (Slack HMAC or health probe)",

  // the example must stay a query it can actually parse.
  "쿼리에서 metric을 찾지 못했습니다. 'CPU', 'AAS', 'storage', 'deadlock', 'connection' 같은 단어가 포함돼야 합니다.":
    "No metric found in the query. It needs a word such as 'CPU', 'AAS', 'storage', 'deadlock' or 'connection'.",
  "질문 (자연어)": "Question (natural language)",
  "예: 최근 24시간 동안 CPU 80% 넘은 클러스터":
    "e.g. clusters over 80% CPU in the last 24 hours",
  물어보기: "Ask",
  "매칭 클러스터 {n}개": "{n} matching clusters",
  // The eyebrow over that title, unwrapped while the title beside it was not.
  결과: "Results",
  "저장된 뷰": "Saved views",
  "+ 뷰 저장": "+ Save view",
  "뷰 이름": "View name",
  "이름이 비어있습니다": "The name is empty",
  "같은 이름의 뷰가 이미 있습니다. 덮어쓸까요?":
    "A view with that name exists. Overwrite it?",
  ", 최근 {n}h": ", last {n}h",

  // ── app/context-files ────────────────────────────────────────────────────
  "총 사용량": "Total usage",
  "업로드된 파일 내용은 에이전트가 호출될 때 참조 데이터로 주입됩니다. 명령(command)이 아닌 참고 정보로만 사용됩니다.":
    "Uploaded file content is injected as reference data when the agent runs. It is reference only, never a command.",
  "{n} 삭제": "Delete {n}",
  "형식만 허용, 파일당 최대": "only. Max per file",
  ", 전체 예산": ", total budget",
  "컨텍스트 파일 선택": "Choose a context file",
  "업로드 중…": "Uploading…",
  "파일 선택 후 업로드": "Select a file, then upload",
  ".{n} 형식은 지원하지 않습니다. .md, .txt, .csv 파일만 업로드할 수 있습니다.":
    ".{n} is not supported. Only .md, .txt and .csv can be uploaded.",
  "파일 크기가 {a}입니다. 파일당 최대 {b}까지 업로드할 수 있습니다.":
    "The file is {a}. The per-file limit is {b}.",
  "파일을 읽는 중 오류가 발생했습니다.":
    "Something went wrong while reading the file.",
  '파일에 예약어 "{n}"가 포함되어 있어 업로드할 수 없습니다.':
    'The file contains the reserved word "{n}", so it cannot be uploaded.',
  '"{n}" 파일을 삭제할까요? 에이전트 컨텍스트에서 즉시 제거됩니다.':
    'Delete "{n}"? It leaves the agent context immediately.',
  "컨텍스트 주입 방식": "How context is injected",
  "업로드된 파일은": "An uploaded file is inserted after the",
  "에이전트 시스템 프롬프트": "agent system prompt",
  " 뒤에 참조 섹션으로 삽입됩니다. 파일 내용은":
    " as a reference section. Its content is used only as",
  "명령이 아닌 참조 데이터": "reference data, not commands",
  "로만 사용되며, 에이전트의 판단을 보조하는 용도입니다.":
    ", to support the agent's own judgement.",
  "예: 클러스터별 담당자 매핑, 점검 체크리스트, 내부 SLA 기준, 팀 컨벤션 등을":
    "For example, upload a per-cluster owner map, a check list, internal SLA targets or team conventions as a",
  " 파일로 업로드하면 에이전트가 진단과 권고 시 이를 참조합니다.":
    " file, and the agent consults it when it diagnoses and recommends.",
  "파일당 최대": "Max per file",
  ". 예산 초과 시 업로드가 거부됩니다.":
    ". An upload past the budget is refused.",
  "예산 사용량": "Budget used",
  "등록된 파일": "Uploaded files",
  "파일 업로드": "Upload",
  ".md, .txt, .csv. 파일당 최대 {n}": ".md, .txt, .csv. Max {n} per file",

  // ── app/dashboard ────────────────────────────────────────────────────────
  "임의 시간 범위 지정": "Custom time range",
  "자주 보는 클러스터 + range 조합을 핀으로 저장":
    "Pin a cluster + range you open often",
  "합성 데이터로 채워진 데모 클러스터입니다. 실제 Aurora가 아니라 평가용 24시간 시드 데이터를 보고 있습니다. Clusters 페이지에서 언제든 삭제 가능합니다.":
    "A demo cluster filled with synthetic data. You are looking at 24 hours of seeded evaluation data, not a real Aurora. Delete it any time from the Clusters page.",
  "클러스터 정보": "Cluster info",
  "엔진: {n}": "Engine: {n}",
  스토리지: "Storage",
  "현재 화면 저장": "Save this view",
  "클러스터 선택 후 저장 가능": "Pick a cluster to save a view",
  "이름 (예: prod-write incident, weekend ETL window)":
    "Name (e.g. prod-write incident, weekend ETL window)",
  핀: "Pin",
  "아직 저장된 view 없음, 위 입력란에 이름을 적고 [핀]을 누르세요":
    "No saved view yet. Type a name above and press [Pin]",
  "{n} 범위": "{n} range",
  "이 view 삭제": "Delete this view",
  "시작과 종료 시각을 모두 입력하세요.": "Enter both a start and an end time.",
  "유효한 시간 형식이 아닙니다.": "That is not a valid time format.",
  "종료 시각은 시작 시각보다 늦어야 합니다.":
    "The end time has to be after the start time.",
  "최대 30일 범위까지 조회 가능합니다.": "A range can span at most 30 days.",
  시작: "Start",
  종료: "End",
  "URL에 from/to로 인코딩되어 공유 가능":
    "Encoded in the URL as from/to, so it can be shared",

  // ── app/fleet ────────────────────────────────────────────────────────────
  "EOL 주의": "EOL risk",
  "모든 엔진": "All engines",
  "모든 상태": "All states",
  "available 아님": "Not available",
  "그룹 없음": "No grouping",
  심각도: "Severity",
  "필터 초기화": "Reset filters",
  "{a} / {b} 표시": "Showing {a} / {b}",
  뷰: "Views",
  "뷰 이름…": "View name…",
  "'{n}' 뷰 적용": "Apply view '{n}'",
  "'{n}' 삭제": "Delete '{n}'",
  "필터에 맞는 클러스터가 없습니다.": "No cluster matches the filters.",
  초기화: "Reset",
  "전체 {a}개 중 {b}개 표시": "Showing {b} of {a}",
  "더 보기 (+100)": "Show 100 more",
  "모두 표시": "Show all",

  // ── app/callback (OAuth redirect landing) ────────────────────────────────
  // "로그인 실패", "다시 시도" and "로그인 중…" are reused from the sections
  // below; the callback literal was normalised onto the U+2026 form so there
  // is exactly one "signing in" key.
  "토큰을 받지 못했습니다. 다시 로그인해 주세요.":
    "No tokens were returned. Please sign in again.",

  // ── app/login ────────────────────────────────────────────────────────────
  "로그인 실패": "Sign-in failed",
  "비밀번호가 일치하지 않습니다": "The passwords do not match",
  "비밀번호는 최소 8자 이상이어야 합니다":
    "The password must be at least 8 characters",
  "비밀번호 설정 실패": "Could not set the password",
  "새 비밀번호 설정": "Set a new password",
  "최초 로그인: 임시 비밀번호를 변경하세요":
    "First sign-in: change the temporary password",
  "새 비밀번호": "New password",
  "비밀번호 확인": "Confirm password",
  "설정 중…": "Setting…",
  "비밀번호 설정 후 로그인": "Set password and sign in",
  이메일: "Email",
  비밀번호: "Password",
  "로그인 중…": "Signing in…",
  "비밀번호 찾기": "Forgot password",
  "공개 가입 없음, admin 전용": "No public sign-up, admin only",

  // ── app/forgot + app/reset (password reset) ──────────────────────────────
  복구: "Recovery",
  "비밀번호 재설정": "Reset password",
  "인증 코드를 이메일로 발송합니다": "We will email you a verification code",
  "발송 중…": "Sending…",
  "코드 발송": "Send the code",
  "재설정 코드 발송 실패": "Could not send the reset code",
  "로그인으로 돌아가기": "Back to sign-in",
  "이메일로 받은 인증 코드를 입력하고 새 비밀번호를 설정하세요":
    "Enter the verification code from your email and set a new password",
  "인증 코드": "Verification code",
  "재설정 중…": "Resetting…",
  "재설정 실패": "Could not reset the password",
  "코드 재발송": "Resend the code",

  // ── app/page (home) ──────────────────────────────────────────────────────
  "운영 콘솔": "Operations console",
  "한눈에 보는 Aurora.": "Aurora at a glance.",
  "AI agent + 실시간 메트릭 + DBA 등급 제어를 등록된 모든 클러스터에서.":
    "AI agent, live metrics and DBA-grade control across every registered cluster.",
  "로 command palette 열기.": "opens the command palette.",
  "튜토리얼 다시 보기": "Replay the tutorial",
  튜토리얼: "Tutorial",
  "활성 알림 규칙": "Active alert rules",
  "최근 24h {n}건 발화": "{n} fired in the last 24h",
  "최근 발화 없음": "Nothing fired recently",
  "즉시 확인 필요": "Check this now",
  "이상 없음": "No issues",
  "태그 미활성화": "Tag not activated",
  "태그 기준 사용액": "Spend by tag",
  "등록된 클러스터": "Registered clusters",
  "전체 보기 →": "View all →",
  "빠른 작업": "Quick actions",
  "자주 쓰는 진입점": "The entry points you use most",
  "자연어 분석": "Natural-language analysis",
  "SQL 분석": "SQL analysis",
  "EXPLAIN + index 추천": "EXPLAIN + index recommendations",
  "알림 관리": "Alert management",
  "규칙 + 구독자": "Rules + subscribers",
  "cross-account 지원": "cross-account supported",
  "쓰기 작업 검토": "Review write actions",
  "등록된 클러스터 없음": "No registered clusters",
  "첫 Aurora 클러스터를 등록하면 메트릭 수집이 시작됩니다.":
    "Register your first Aurora cluster and metric collection starts.",

  // ── app/preferences ──────────────────────────────────────────────────────
  "이 기록을 삭제할까요? 이후 Agent는 이 정보를 잊습니다.":
    "Delete this record? The agent forgets it from then on.",
  "{n}개 기록": "{n} records",
  잊기: "Forget",

  // ── app/scaleout ─────────────────────────────────────────────────────────
  "대상 AZ": "Target AZ",

  // ── app/scenarios ────────────────────────────────────────────────────────
  "신호를 주입했습니다. 자동 RCA가 큐에 등록되었습니다 (작업 {n}). 분석은 보통 수십 초 안에 끝납니다.":
    "Signals injected. Auto RCA is queued (task {n}). The analysis usually finishes within tens of seconds.",
  "신호는 주입했지만 자동 RCA를 큐에 넣지 못했습니다. 에이전트 작업 테이블 설정을 확인하세요.":
    "The signals went in, but auto RCA could not be queued. Check the agent task table configuration.",
  "동작 방식": "How it works",
  "시나리오는 캐시된 신호 테이블(metric_snapshots, event_log, blocking_locks, query_stats, schema_snapshots)에 해당 장애가 관측되었을 때와 같은 행을 기록합니다.":
    "A scenario writes into the cached signal tables (metric_snapshots, event_log, blocking_locks, query_stats, schema_snapshots) the same rows the real failure would have produced.",
  "그 다음은 전부 실제 경로입니다. 동일한 결정론적 랭커가 동일한 가중치로 신호를 채점하고, 동일한 모델 호출이 원인 설명과 권장 조치를 생성합니다.":
    "Everything after that is the real path: the same deterministic ranker scores the signals with the same weights, and the same model call writes the cause narrative and the recommended actions.",
  "대상 데이터베이스는 건드리지 않습니다. 주입된 행은 RCA 분석 구간({n}분)에서 벗어나면 자동으로 정리됩니다. 한 번에 하나의 시나리오만 실행됩니다.":
    "The target database is never touched. Injected rows are cleaned up once they fall outside the RCA analysis window ({n} min). Only one scenario runs at a time.",
  "대상 클러스터가 설정되지 않아 실행이 비활성화되어 있습니다. cdk/config/settings.py의 SCENARIO_CLUSTER_ID에 등록된 클러스터를 지정하고 agent 스택을 재배포하세요.":
    "Running is disabled because no target cluster is set. Point SCENARIO_CLUSTER_ID in cdk/config/settings.py at a registered cluster and redeploy the agent stack.",
  "작업으로 이동": "Go to Tasks",
  시나리오: "Scenario",
  "대상 클러스터: {n}": "Target cluster: {n}",
  "랭킹 카테고리 {c}, 기본 가중치 {w}": "Ranking category {c}, base weight {w}",
  실행: "Run",
  "실행 이력": "Run history",
  "RCA 보기": "View RCA",
  "RCA 없음": "No RCA",

  // ── app/settings ─────────────────────────────────────────────────────────
  "마지막 변경:": "Last change:",
  "정기 운영 요약 리포트를 SNS/Slack 구독자에게 자동 발송합니다.":
    "Sends the periodic operations summary to SNS / Slack subscribers automatically.",
  "활성화하면 Tasks 페이지의 scheduled_report 결과가 SNS 토픽에 등록된 이메일과 Slack 구독자에게 자동 발송됩니다. 구독자는 Alerts 페이지에서 추가하세요.":
    "With this on, a scheduled_report result from the Tasks page goes to the emails and Slack subscribers on the SNS topic. Add subscribers on the Alerts page.",
  "이상 감지와 RCA 결과를 외부 티켓 시스템에 자동 등록할 제공자를 지정합니다.":
    "Names the provider that anomaly detections and RCA results are filed to as tickets.",
  "현재 지원하는 값:": "Supported values today:",
  "(비활성).": "(disabled).",
  "등 다른 값을 입력해도 코드에 연동 구현이 없으면 아무 동작도 하지 않습니다. 제공자 연동을 먼저 구현한 뒤 값을 바꾸세요.":
    "or any other value does nothing while the integration is missing from the code. Implement the provider first, then change this value.",
  저장되었습니다: "Saved",

  // ── app/tasks ────────────────────────────────────────────────────────────
  "원인 미상 실패": "Failed, cause unknown",
  "분석 대기 중": "Waiting for analysis",
  "순위를 매길 후보를 찾지 못했습니다": "No candidate to rank",
  "RCA 작업을 시작했습니다. 잠시 후 아래에 결과가 나타납니다.":
    "RCA task started. The result shows up below shortly.",
  "총 작업": "Total tasks",
  성공률: "Success rate",
  "평균 소요": "Average duration",
  "최근 실패 {n}": "{n} recent failures",
  범위: "Scope",
  "예약 리포트는 완료 상태를 RCA와 공유하므로 상태 필터로는 분리되지 않습니다":
    "A scheduled report shares the done state with RCA, so the status filter does not separate the two",
  "모든 클러스터 보기": "Show every cluster",
  "{n}에 대해 RCA를 즉시 실행": "Run RCA on {n} now",
  "실행 중…": "Running…",
  "▶ RCA 실행": "▶ Run RCA",
  "{n} 한 대로 범위를 좁혀 보고 있습니다. 신규 표시는 전체 받은함 기준이며, 이 화면은 방문 기록을 갱신하지 않습니다.":
    "Narrowed to {n} alone. The new badge still counts the whole inbox, and this screen does not move your visit mark.",
  "조회 실패: {n}": "Fetch failed: {n}",
  "더 보기": "Show more",
  "{n}건 표시, 이전 기록이 더 있습니다": "Showing {n}, older records exist",
  "예약 작업": "Scheduled tasks",
  "반복 헬스 다이제스트를 예약합니다. 스케줄러가 주기마다 작업을 자동 등록하고, 결과는 위 목록과 토스트로 도착합니다.":
    "Schedules a recurring health digest. The scheduler queues a task each period, and the result arrives in the list above and as a toast.",
  "위 범위에서 클러스터를 선택하면 예약을 추가할 수 있습니다":
    "Pick a cluster in the scope above to add a schedule",
  "추가 중…": "Adding…",
  "+ 예약 추가": "+ Add schedule",
  "등록된 예약이 없습니다.": "No schedules yet.",
  "최근 {n}": "Last {n}",
  미실행: "Never run",
  "예약 삭제": "Delete schedule",
  "마지막 방문 이후 도착한 리포트":
    "Reports that arrived since your last visit",
  신규: "New",
  "등록 {n}": "Queued {n}",
};

/**
 * The table `translate()` reads. Server-authored prose first, frontend
 * literals second, so a collision resolves to the frontend entry (the one
 * `tools/i18n-check.mjs` can verify against `src/`).
 */
export const EN: Record<string, string> = { ...EN_SERVER, ...EN_UI };
