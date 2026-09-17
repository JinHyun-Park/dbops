/**
 * English for prose the BACKEND wrote, keyed by the Korean source literal.
 *
 * Same table, same lookup, same fallback as `en.ts`: `translate()` returns
 * `EN[ko] ?? ko`, and `en.ts` spreads this map in. The split is only about
 * which gate can check it.
 *
 * WHY A SEPARATE FILE. `tools/i18n-check.mjs` asserts that every key in
 * `en.ts` still appears somewhere under `src/`. That is the right check for a
 * frontend literal and the wrong one here: these keys are Python literals in
 * `api/`, `data-pipeline/` and `mcp-servers/`, so none of them appears in
 * `src/` and all of them would read as orphans. The mirror-image check lives
 * where the strings do, in `tests/unit/test_en_server_keys.py`, which fails if
 * a key here no longer appears verbatim in the backend source.
 *
 * ONLY FIXED LITERALS BELONG HERE. The lookup is exact equality, so a string
 * the producer composes at runtime (`f"... {n} ..."`) can never match. Those
 * stay Korean at the render site, which is the intended fallback: visible,
 * never blank. Adding an interpolated string here would be a silent no-op.
 *
 * NOT IN HERE, on purpose:
 *   - MODEL prose (the RCA narrative, recommendations, the report digest). It
 *     is generated in the task's own locale, not translated. Running a lookup
 *     over it would miss and return it unchanged.
 *   - MCP tool `reason` / `message` / `note` read by the agent. The chat UI
 *     renders tool name and status, never raw tool JSON, so no reader sees
 *     them and the agent restates them in the reader's language.
 *   - approval `cli_preview`. Every one of the 29 is interpolated, and the
 *     payload is stored and hash-bound, so it is left alone deliberately.
 *   - Korean used as a MATCHER rather than displayed
 *     (`outcome_evaluator/remediation_classify.py` needles, the SQL Server
 *     `unit` strings `settings-panel.tsx` switches on). Changing those changes
 *     behaviour.
 *
 * HEDGES SURVIVE. 으로 보입니다 / 가능성 / 의심됩니다 / 추정 / 권장합니다 keep their
 * force on this side too: "appears", "may", "suggests", "estimated",
 * "recommended", never a bare assertion and never an imperative that the
 * Korean did not make. A ranking is an investigation priority, not a cause.
 */
export const EN_SERVER: Record<string, string> = {
  // ── api/dashboard/handler.py: panel errors and refusals ──────────────────
  "스키마 그래프 쿼리 실행에 실패했습니다. 잠시 후 다시 시도해주세요.":
    "The schema graph query failed to run. Please try again shortly.",
  "중복 인덱스 분석 쿼리 실행에 실패했습니다. 잠시 후 다시 시도해주세요.":
    "The redundant-index analysis query failed to run. Please try again shortly.",
  "인덱스 조회 쿼리 실행에 실패했습니다. 잠시 후 다시 시도해주세요.":
    "The index query failed to run. Please try again shortly.",
  "라이브 top은 Aurora PostgreSQL 전용입니다":
    "Live top is Aurora PostgreSQL only",
  "대상 클러스터에 RDS Data API가 없어 라이브 조회가 불가합니다 (활성화 필요)":
    "The target cluster has no RDS Data API, so a live read is not possible (it has to be enabled)",
  "pg_buffercache 확장을 사용할 수 없습니다":
    "The pg_buffercache extension is not available",
  "로그 검색을 시작하지 못했습니다. 로그 내보내기 설정과 권한을 확인해주세요.":
    "The log search could not be started. Check the log-export settings and the permissions.",
  "이 DocumentDB 클러스터의 토폴로지를 조회할 수 없습니다. 등록되지 않았거나 접근 권한이 없습니다.":
    "The topology of this DocumentDB cluster cannot be read. It is either not registered or not accessible.",
  "토폴로지 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.":
    "The topology could not be loaded. Please try again shortly.",
  "이 클러스터의 복제 토폴로지를 조회할 수 없습니다. 데모(합성) 클러스터이거나 실제 Aurora로 등록되지 않았습니다. 등록된 클러스터를 선택하면 writer/reader 구성과 Replica Lag이 표시됩니다.":
    "The replication topology of this cluster cannot be read. It is either a demo (synthetic) cluster or it is not registered as a real Aurora cluster. Pick a registered cluster and the writer/reader layout and Replica Lag appear.",
  "이 DocumentDB 클러스터의 백업 정보를 조회할 수 없습니다. 등록되지 않았거나 접근 권한이 없습니다.":
    "The backup information for this DocumentDB cluster cannot be read. It is either not registered or not accessible.",
  "이 DynamoDB 테이블의 백업 정보를 조회할 수 없습니다. 등록되지 않았거나 접근 권한이 없습니다.":
    "The backup information for this DynamoDB table cannot be read. It is either not registered or not accessible.",
  "백업 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.":
    "The backup information could not be loaded. Please try again shortly.",
  "이 클러스터의 실시간 백업 정보를 조회할 수 없습니다. 데모(합성) 클러스터이거나 실제 Aurora로 등록되지 않았습니다. 등록된 클러스터를 선택하면 스냅샷과 PITR 윈도우가 표시됩니다.":
    "Live backup information for this cluster cannot be read. It is either a demo (synthetic) cluster or it is not registered as a real Aurora cluster. Pick a registered cluster and the snapshots and the PITR window appear.",
  "이 클러스터의 엔드포인트 정보를 조회할 수 없습니다. 데모(합성) 클러스터이거나 실제 Aurora로 등록되지 않았습니다.":
    "The endpoints of this cluster cannot be read. It is either a demo (synthetic) cluster or it is not registered as a real Aurora cluster.",
  "엔드포인트 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.":
    "The endpoints could not be loaded. Please try again shortly.",
  "이 DocumentDB 클러스터의 구성 정보를 조회할 수 없습니다. 등록되지 않았거나 접근 권한이 없습니다.":
    "The configuration of this DocumentDB cluster cannot be read. It is either not registered or not accessible.",
  "이 DynamoDB 테이블의 구성 정보를 조회할 수 없습니다. 등록되지 않았거나 접근 권한이 없습니다.":
    "The configuration of this DynamoDB table cannot be read. It is either not registered or not accessible.",
  "구성 정보를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.":
    "The configuration could not be loaded. Please try again shortly.",
  "이 ElastiCache 클러스터의 구성 정보를 조회할 수 없습니다. 등록되지 않았거나 접근 권한이 없습니다.":
    "The configuration of this ElastiCache cluster cannot be read. It is either not registered or not accessible.",

  // ── api/dashboard/handler.py: timeline and schema-changes verdict notes ──
  // Every one of these says what was NOT judged. The English keeps that: the
  // absence of a verdict is never reported as the absence of a change.
  "이 cluster의 schema 스냅샷이 아직 없어 schema_change 신호는 판정 대상이 아니었습니다. 이 구간에 DDL이 없었다는 뜻이 아닙니다.":
    "This cluster has no schema snapshot yet, so the schema_change signal was not something that could be judged. That does not mean there was no DDL in this window.",
  "[기록, 판정 아님]": "[recorded, not a verdict]",
  "이 cluster의 엔진은 스키마 스냅샷 판정 대상이 아닙니다. 이 항목은 과거에 기록된 이력이며 현재 상태에 대한 판정이 아닙니다. 위 배너의 설명을 확인하세요.":
    "This cluster's engine is not one that schema snapshots can judge. This entry is history that was recorded earlier, not a verdict about the current state. See the banner above.",
  "이 클러스터의 스키마 이력이 아직 없습니다 (schema_snapshots와 table_stats 모두 이 클러스터 행이 없음). 변경이 없다는 뜻이 아닙니다.":
    "There is no schema history for this cluster yet (neither schema_snapshots nor table_stats has a row for it). That does not mean nothing changed.",
  "수집된 이력이 요청 구간을 걸치지 않아 비교할 기준점이 없습니다. 변경이 없다는 뜻이 아닙니다. 구간을 늘리거나 수집이 쌓일 때까지 기다려야 합니다.":
    "The collected history does not span the requested window, so there is no baseline to compare against. That does not mean nothing changed. Widen the window, or wait for more collection.",
  "테이블 생성/삭제(DDL)는 schema_snapshots로만 판정하는데 이 클러스터의 스냅샷이 아직 없습니다. DDL 변경이 없다는 뜻이 아닙니다. 다음 ETL 주기에 최초 baseline 스냅샷이 기록되고, 그 다음 변경 시점부터 생성/삭제가 표시됩니다.":
    "Table creation and deletion (DDL) is judged from schema_snapshots alone, and this cluster has no snapshot yet. That does not mean there was no DDL change. The next ETL cycle records the first baseline snapshot, and creations and deletions appear from the next change onward.",
  "baseline 스냅샷만 있어 DDL 비교 대상이 없습니다 (판정에는 스냅샷 2개가 필요합니다). 다음 스키마 변경이 감지되면 그 시점의 스냅샷과 비교됩니다.":
    "There is only a baseline snapshot, so there is nothing to compare DDL against (a verdict needs two snapshots). Once the next schema change is detected, it is compared against the snapshot taken then.",
  "이 스키마의 스냅샷이 모두 요청 구간보다 오래되어, 구간 안에서 비교할 스냅샷 쌍이 없습니다 (가장 최근 스냅샷조차 구간 시작보다 이전). DDL 변경이 없다는 뜻이 아닙니다. 구간을 늘리면 그 이전 이력끼리 비교할 수 있습니다.":
    "Every snapshot of this schema is older than the requested window, so there is no pair to compare inside it (even the most recent snapshot predates the window start). That does not mean there was no DDL change. Widen the window and the older history can be compared against itself.",
  "저장된 스냅샷이 모두 schema_v27 이전 기록이라 어떤 카탈로그를 읽은 것인지 알 수 없어 테이블 생성/삭제(DDL)를 비교하지 못했습니다. DDL 변경이 없다는 뜻이 아닙니다. 다음 ETL 주기에 각 스키마가 baseline으로 다시 기록되고, 그 다음 변경 시점부터 비교됩니다.":
    "Every stored snapshot predates schema_v27, so which catalog was read is unknown and table creation and deletion (DDL) could not be compared. That does not mean there was no DDL change. The next ETL cycle records each schema as a baseline again, and comparison resumes from the next change onward.",
  "schema_snapshots를 조회할 수 없어 이번 응답에서는 테이블 생성/삭제(DDL)를 판정하지 못했습니다 (schema_v26 마이그레이션 적용 여부를 확인하세요). DDL 변경이 없다는 뜻이 아닙니다.":
    "schema_snapshots could not be read, so this response carries no verdict on table creation and deletion (DDL) (check whether the schema_v26 migration has been applied). That does not mean there was no DDL change.",
  "table_stats에 이 클러스터의 수집 기록이 없어 위 판정이 언제 기준인지 확인할 수 없습니다. 표시된 내용은 마지막으로 기록된 스냅샷 기준입니다.":
    "table_stats holds no collection record for this cluster, so there is no way to tell what moment the verdict above is current as of. What is shown is as of the last snapshot recorded.",
  "행 수 증감은 table_stats 기준이며, 이 수집기는 매 주기 상위 100개 테이블만 기록합니다. 상위 100개 밖의 테이블은 증감 판정 대상이 아닙니다.":
    "Row-count movement comes from table_stats, and that collector records only the top 100 tables each cycle. A table outside the top 100 is not something this can judge.",
  "두 신호 중 일부만 판정됐습니다. 아래 칩에서 판정되지 않은 신호를 확인하세요. 판정되지 않은 쪽은 변경이 없다는 뜻이 아닙니다.":
    "Only some of the two signals were judged. The chips below show which one was not. An unjudged signal does not mean nothing changed.",

  // ── api/dashboard/handler.py: capacity forecast limits and refusals ──────
  "max_connections 미상, 기본값 가정":
    "max_connections unknown, assuming the default",
  "인스턴스 vCPU 미상(서버리스/미등록), 기본값 가정":
    "Instance vCPU unknown (serverless or unregistered), assuming the default",
  "온디맨드(프로비저닝 용량 없음), 근거 있는 천장 없음":
    "On-demand (no provisioned capacity), so there is no grounded ceiling",
  "여유 스토리지 소진(0 bytes)": "Free storage exhausted (0 bytes)",
  "클러스터 볼륨 상한 128 TiB": "Cluster volume limit 128 TiB",
  "메모리 사용률 상한 100%": "Memory utilization limit 100%",
  "클러스터 레지스트리를 조회할 수 없어 엔진을 확인하지 못했습니다.":
    "The cluster registry could not be read, so the engine could not be confirmed.",
  "cluster_meta에 이 클러스터가 없습니다(미등록이거나 첫 메트릭 수집 전). 등록 및 수집 상태를 확인한 뒤 다시 예측하세요.":
    "cluster_meta has no row for this cluster (it is unregistered, or the first metric collection has not run). Check the registration and collection state, then forecast again.",
  "eviction이 발생 중입니다. eviction 정책(LRU/TTL)이 걸린 캐시는 설계상 메모리 상한 근처에서 동작하므로 '100% 도달까지 며칠'은 의미가 없습니다. 정확한 신호는 eviction 양과 hit rate이며 Maintenance Health의 elasticache_evictions_spike, elasticache_memory_pressure finding이 이를 임계로 관리합니다.":
    'Evictions are happening. A cache with an eviction policy (LRU/TTL) is designed to run near its memory limit, so "days until 100%" means nothing here. The signals that do mean something are the eviction volume and the hit rate, and the elasticache_evictions_spike and elasticache_memory_pressure findings in Maintenance Health hold those to thresholds.',

  // ── shared/schema_diff_util.py (4 byte-identical copies) ─────────────────
  "dropped 목록은 수집 계정이 그 시점에 볼 수 있었던 카탈로그를 기준으로 계산됩니다. PostgreSQL 카탈로그(pg_namespace/pg_class)는 권한으로 필터링되지 않으므로 이 목록은 실제 DDL을 반영합니다. MySQL 계열은 information_schema가 권한 필터링되어 REVOKE와 DROP을 구분할 수 없기 때문에 스키마 스냅샷 수집 대상이 아닙니다.":
    "The dropped list is computed against the catalog the collecting account could see at that moment. The PostgreSQL catalog (pg_namespace/pg_class) is not permission-filtered, so this list does reflect real DDL. MySQL-family engines are not collected for schema snapshots at all, because their information_schema IS permission-filtered and a REVOKE cannot be told apart from a DROP.",
  "스키마 스냅샷(테이블 생성/삭제 판정)은 PostgreSQL 카탈로그(pg_namespace/pg_class)를 읽는 cluster에서만 수집합니다. 이 카탈로그는 권한으로 필터링되지 않아 '읽기 결과에 없으면 실제로 없다'가 성립하기 때문입니다. 이 cluster의 엔진은 그 전제가 확인된 대상이 아니어서 스냅샷을 수집하지 않으며, 따라서 이 cluster에 대해서는 '변경 없음'도 '변경 있음'도 말할 수 없습니다. 엔진별 근거는 snapshot_dialect_supported()에 기록되어 있습니다.":
    'Schema snapshots (the table creation/deletion verdict) are collected only on a cluster whose PostgreSQL catalog (pg_namespace/pg_class) can be read, because that catalog is not permission-filtered, which is what makes "absent from the read means actually absent" hold. This cluster\'s engine is not one where that premise has been confirmed, so no snapshot is collected, and therefore neither "nothing changed" nor "something changed" can be said about it. The per-engine grounds are recorded in snapshot_dialect_supported().',
  "스키마 관측에 필요한 정보를 조회할 수 없어(캐시 DB에 schema_v27 미적용이거나 이 cluster의 cluster_meta 행이 아직 없음) 각 스키마가 현재도 존재하는지는 확인하지 못했습니다.":
    "The information the schema observation needs could not be read (either schema_v27 is not applied to the cache DB, or this cluster has no cluster_meta row yet), so whether each schema still exists was not confirmed.",
  "저장된 스냅샷이 모두 schema_v27 이전 기록이라 어떤 카탈로그를 읽은 것인지 알 수 없어 비교 대상으로 쓸 수 없습니다. 다음 수집 주기에 각 스키마가 baseline으로 다시 기록됩니다.":
    "Every stored snapshot predates schema_v27, so which catalog was read is unknown and none of them can be used for comparison. The next collection cycle records each schema as a baseline again.",

  // ── api/clusters/handler.py: cross-account registration failures ─────────
  "권한이 부족합니다. 스포크 역할의 신뢰 정책과 연결된 권한을 확인하세요.":
    "Not enough permission. Check the spoke role's trust policy and the policies attached to it.",
  "자격 증명이 유효하지 않습니다. 스포크 역할 ARN을 확인하세요.":
    "The credentials are not valid. Check the spoke role ARN.",
  "자격 증명이 만료되었습니다. 잠시 후 다시 시도하세요.":
    "The credentials have expired. Please try again shortly.",
  "해당 계정/리전에서 클러스터를 찾을 수 없습니다. 식별자와 리전을 확인하세요.":
    "No such cluster in that account and region. Check the identifier and the region.",
  "해당 계정/리전에서 DB 인스턴스를 찾을 수 없습니다. 식별자와 리전을 확인하세요.":
    "No such DB instance in that account and region. Check the identifier and the region.",
  "해당 계정/리전에서 replication group을 찾을 수 없습니다. 이름과 리전을 확인하세요.":
    "No such replication group in that account and region. Check the name and the region.",
  "해당 계정/리전에서 cache cluster를 찾을 수 없습니다. 이름과 리전을 확인하세요.":
    "No such cache cluster in that account and region. Check the name and the region.",
  "해당 계정/리전에서 리소스를 찾을 수 없습니다. 이름과 리전을 확인하세요.":
    "No such resource in that account and region. Check the name and the region.",
  "해당 계정/리전에서 replication group도 cache cluster도 찾을 수 없습니다. 이름과 리전을 확인하세요.":
    "Neither a replication group nor a cache cluster with that name exists in that account and region. Check the name and the region.",
  "AWS API 호출이 제한되었습니다. 잠시 후 다시 시도하세요.":
    "The AWS API call was throttled. Please try again shortly.",
  "요청 값이 올바르지 않습니다. 식별자 형식을 확인하세요.":
    "The request values are not valid. Check the identifier format.",
  "AWS 엔드포인트에 연결할 수 없습니다. 리전 값을 확인하세요.":
    "The AWS endpoint could not be reached. Check the region value.",

  // ── api/cost/handler.py ──────────────────────────────────────────────────
  "클러스터별 비용 분리를 사용할 수 없습니다. AWS Billing 콘솔에서 cost-allocation 태그(예: 'dbops:cluster')를 활성화하고 Aurora 클러스터에 적용하면 약 24시간 내에 Cost Explorer가 클러스터 단위로 비용을 분리합니다. 리소스 수준 CE 데이터는 추가 비용이 발생해 사용하지 않으며, 과거 비용은 소급 반영되지 않습니다.":
    "Per-cluster cost separation is not available. Activate a cost-allocation tag (for example 'dbops:cluster') in the AWS Billing console and apply it to the Aurora clusters, and within roughly 24 hours Cost Explorer separates cost per cluster. Resource-level CE data is not used because it costs extra, and past cost is never backfilled.",
  "Cost Explorer가 RDS 데이터를 반환하지 않았습니다. 이 계정에서 Cost Explorer가 활성화되어 있는지 확인 후 24시간 뒤 다시 확인하세요.":
    "Cost Explorer returned no RDS data. Check that Cost Explorer is enabled in this account, then look again after 24 hours.",
  "이 기간에 기록된 RDS/Aurora 비용이 없습니다. Aurora를 운영 중이라면 Cost Explorer 활성화 여부를 확인하세요 (반영까지 약 24시간 지연).":
    "No RDS/Aurora cost is recorded for this period. If Aurora is running, check whether Cost Explorer is enabled (it lags by roughly 24 hours).",
  "클러스터별 비용 분리를 사용할 수 없습니다. AWS Billing 콘솔에서 cost-allocation 태그를 활성화하고 ElastiCache 클러스터에 적용하면 약 24시간 내에 Cost Explorer가 클러스터 단위로 비용을 분리합니다. 과거 비용은 소급 반영되지 않습니다.":
    "Per-cluster cost separation is not available. Activate a cost-allocation tag in the AWS Billing console and apply it to the ElastiCache clusters, and within roughly 24 hours Cost Explorer separates cost per cluster. Past cost is never backfilled.",
  "이 기간에 기록된 ElastiCache 비용이 없습니다. ElastiCache를 운영 중이라면 Cost Explorer 활성화 여부를 확인하세요 (반영까지 약 24시간 지연).":
    "No ElastiCache cost is recorded for this period. If ElastiCache is running, check whether Cost Explorer is enabled (it lags by roughly 24 hours).",
  "클러스터 레지스트리 조회 실패로 커밋 할인 현황을 표시할 수 없습니다.":
    "The cluster registry could not be read, so the commitment-discount position cannot be shown.",
  "등록된 Aurora/RDS 클러스터가 없어 조회할 계정이 없습니다.":
    "No Aurora/RDS cluster is registered, so there is no account to query.",
  "커버/초과/미사용 추정치는 계정/리전/클래스별 실행 중인 인스턴스 수와 보유 RI 수량을 비교한 근사치입니다. RI 실효 절감은 계약 조건(선결제, 기간)에 따라 달라집니다. CE 커버리지는 허브 계정 기준입니다.":
    "The covered, over and unused figures are approximations, comparing the number of running instances per account, region and class against the RI quantity held. Effective RI savings depend on the contract terms (upfront payment, term length). CE coverage is as seen from the hub account.",
  "Application 태그가 cost allocation tag로 활성화되지 않았습니다. AWS Billing 콘솔에서 활성화하면 ~24시간 후부터 집계됩니다.":
    "The Application tag is not activated as a cost allocation tag. Activate it in the AWS Billing console and aggregation starts roughly 24 hours later.",
  "이 기간에 Application=DBOps 태그가 붙은 비용이 없습니다. 배포 직후라면 Cost Explorer 반영(~24h)을 기다려 주세요.":
    "No cost carrying the Application=DBOps tag is recorded for this period. Right after a deployment, wait for Cost Explorer to catch up (roughly 24h).",
  "Application=DBOps 태그 기준: 모니터링 대상 고객 클러스터는 태그가 없어 제외됩니다. RDS 항목은 DBOps 캐시 DB(+CDK 샘플 클러스터)이며, Bedrock 항목은 Bedrock 탭과 동일한 비용입니다.":
    "Scoped to the Application=DBOps tag: the customer clusters being monitored carry no such tag and are therefore excluded. The RDS line is the DBOps cache DB (plus the CDK sample cluster), and the Bedrock line is the same cost as the Bedrock tab.",
  "CloudWatch 토큰 메트릭 목록 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요).":
    "Listing the CloudWatch token metrics failed (see the server log for the cause).",
  "Bedrock 토큰 메트릭 없음: 아직 모델 호출 기록이 없거나 메트릭 전파 전입니다.":
    "No Bedrock token metric: either no model call has been recorded yet, or the metric has not propagated.",
  "CloudWatch 토큰 사용량 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요).":
    "Reading the CloudWatch token usage failed (see the server log for the cause).",
  "계정 전체 Bedrock 토큰 사용량(모델별): CloudWatch 메트릭은 태그 필터 불가.":
    "Account-wide Bedrock token usage, per model: CloudWatch metrics cannot be filtered by tag.",

  // ── api/approvals/handler.py ─────────────────────────────────────────────
  "operations 호출에 실패했습니다": "The operations call failed",
  "operations Lambda가 오류를 반환했습니다":
    "The operations Lambda returned an error",
  "operations 응답을 해석할 수 없습니다":
    "The operations response could not be parsed",
  "자동 예열만 취소되었습니다. 생성된 리더 인스턴스는 유지됩니다. 필요하면 스케일 인으로 별도 제거하세요.":
    "Only the automatic prewarm was cancelled. The reader instances that were created stay. Remove them separately with a scale-in if you want them gone.",
  "런타임 boto3가 EnableHttpEndpoint API를 지원하지 않습니다. 런타임 업그레이드 필요":
    "The runtime's boto3 does not support the EnableHttpEndpoint API. The runtime has to be upgraded",
  "전파까지 보통 1~2분, VPC(NAT) 경유 호출자는 더 걸릴 수 있습니다. 다음 수집 사이클에 대시보드 경고가 사라집니다.":
    "Propagation usually takes one to two minutes, and a caller going through the VPC (NAT) may take longer. The dashboard warning clears on the next collection cycle.",
  "승인 요청 생성에 실패했습니다": "Creating the approval request failed",
  "static_members와 excluded_members는 상호 배타적입니다. 하나만 지정하세요":
    "static_members and excluded_members are mutually exclusive. Specify only one",
  "endpoint_type은 READER 또는 ANY 여야 합니다":
    "endpoint_type has to be READER or ANY",
  "static_members 또는 excluded_members 중 하나는 지정해야 합니다":
    "One of static_members or excluded_members has to be specified",
  "승인 요청은 생성됐지만 자동 실행 표식 기록에 실패했습니다. 승인해도 자동 실행되지 않을 수 있습니다. 관리자에게 문의하세요.":
    "The approval request was created, but recording the auto-execute marker failed. Approving it may not run it automatically. Contact an administrator.",
  "승인 요청이 생성되었습니다. 승인 센터에서 검토하고 승인하면 실행됩니다.":
    "The approval request has been created. Review it in the Approval Center, and it runs once approved.",
  "계획 생성에 실패했습니다": "Building the plan failed",
  "승인자를 식별할 수 없습니다.": "The approver could not be identified.",
  "자기 요청은 승인할 수 없습니다. 다른 승인자가 처리해야 합니다.":
    "You cannot approve your own request. Another approver has to handle it.",
  "이 작업은 지정된 승인자만 승인할 수 있습니다.":
    "Only a designated approver can approve this action.",

  // ── api/tasks, api/scheduled_tasks, api/chat_sessions, api/reports ───────
  "작업 통계를 집계하지 못했습니다. 잠시 후 다시 시도하세요.":
    "The task statistics could not be aggregated. Please try again shortly.",
  "작업을 불러오지 못했습니다. 잠시 후 다시 시도하세요.":
    "The task could not be loaded. Please try again shortly.",
  "작업 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.":
    "The task list could not be loaded. Please try again shortly.",
  "작업 생성에 실패했습니다. 잠시 후 다시 시도하세요.":
    "Creating the task failed. Please try again shortly.",
  "예약 작업 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.":
    "The scheduled-task list could not be loaded. Please try again shortly.",
  "예약 작업 생성에 실패했습니다. 잠시 후 다시 시도하세요.":
    "Creating the scheduled task failed. Please try again shortly.",
  "삭제할 예약 작업을 조회하지 못했습니다. 잠시 후 다시 시도하세요.":
    "The scheduled task to delete could not be read. Please try again shortly.",
  "예약 작업 삭제에 실패했습니다. 잠시 후 다시 시도하세요.":
    "Deleting the scheduled task failed. Please try again shortly.",
  "대화 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.":
    "The conversation list could not be loaded. Please try again shortly.",
  "대화를 불러오지 못했습니다. 잠시 후 다시 시도하세요.":
    "The conversation could not be loaded. Please try again shortly.",
  "대화 소유자 확인에 실패했습니다. 잠시 후 다시 시도하세요.":
    "Confirming the conversation's owner failed. Please try again shortly.",
  "대화 저장에 실패했습니다. 잠시 후 다시 시도하세요.":
    "Saving the conversation failed. Please try again shortly.",
  "삭제할 대화를 조회하지 못했습니다. 잠시 후 다시 시도하세요.":
    "The conversation to delete could not be read. Please try again shortly.",
  "대화 삭제에 실패했습니다. 잠시 후 다시 시도하세요.":
    "Deleting the conversation failed. Please try again shortly.",
  "리포트를 찾을 수 없습니다.": "No such report.",
  "이 리포트는 HTML 생성 이전에 만들어졌습니다.":
    "This report was produced before HTML generation existed.",
  "HTML 리포트 파일이 아직 생성되지 않았습니다.":
    "The HTML report file has not been generated yet.",
  "HTML 리포트를 불러오지 못했습니다.": "The HTML report could not be loaded.",
  "리포트를 불러오지 못했습니다.": "The report could not be loaded.",

  // ── api/explain, api/config, api/alerts, api/onboarding ──────────────────
  "데이터베이스가 이 문장을 거부했습니다. SQL 구문과 참조한 테이블/컬럼 이름을 확인하세요 (자세한 오류는 서버 로그에 기록됩니다).":
    "The database rejected this statement. Check the SQL syntax and the table and column names it references (the full error is recorded in the server log).",
  "EXPLAIN 실행에 실패했습니다. 클러스터 연결/권한 문제일 수 있으니 잠시 후 다시 시도하고, 계속되면 서버 로그를 확인하세요.":
    "Running EXPLAIN failed. It may be a cluster connection or permission problem, so try again shortly, and check the server log if it keeps happening.",
  // api/config emits these unprefixed, with the key name inside the literal:
  // lookup is exact equality, so a body composed as `${key}: ${hint}` could
  // never match a key for the bare hint.
  "TICKETING_PROVIDER: [a-z0-9_-] 문자 1~32자여야 합니다.":
    "TICKETING_PROVIDER: has to be 1 to 32 characters from [a-z0-9_-].",
  "REPORT_DELIVERY_ENABLED: true 또는 false여야 합니다.":
    "REPORT_DELIVERY_ENABLED: has to be true or false.",
  "DEFAULT_LOCALE: ko 또는 en이어야 합니다.":
    "DEFAULT_LOCALE: has to be ko or en.",
  "허브 계정 ID를 확인하지 못했습니다. 잠시 후 다시 시도하세요.":
    "The hub account ID could not be confirmed. Please try again shortly.",
  // One string, three producers: api/dashboard, api/alerts, api/saved_queries.
  "이 클러스터에 대한 접근 권한이 없습니다.":
    "You do not have access to this cluster.",

  // ── api/scenarios/handler.py: the injectable scenario catalog ────────────
  "스키마 변경 후 성능 저하": "Slowdown after a schema change",
  "마이그레이션이 orders 테이블의 컬럼 구성을 바꾼 직후 지연이 증가한 상황":
    "Latency rises right after a migration changes the column layout of the orders table",
  "라이터 페일오버": "Writer failover",
  "라이터 인스턴스가 교체되며 연결이 끊기고 캐시가 비워진 상황":
    "The writer instance is replaced, connections drop and the cache is emptied",
  "배치 트랜잭션이 orders 행 잠금을 길게 유지해 애플리케이션 쿼리가 대기하는 상황":
    "A batch transaction holds row locks on orders for a long time and application queries wait behind it",
  "CPU 포화": "CPU saturation",
  "CPU 사용률이 베이스라인 대비 8배로 올라 20분간 유지된 상황":
    "CPU utilization climbs to 8x the baseline and stays there for 20 minutes",
  "커넥션 고갈": "Connection exhaustion",
  "커넥션 수가 max_connections 근처까지 치솟아 신규 연결이 거부되는 상황":
    "The connection count spikes near max_connections and new connections are refused",
  "슬로우 쿼리 급증": "Slow-query surge",
  "인덱스를 타지 못하는 조인이 상위 쿼리를 점유하며 총 실행시간이 급증한 상황":
    "A join that cannot use an index takes over the top queries and total execution time surges",

  // ── api/simulation/handler.py: upgrade plan steps ────────────────────────
  "사전 체크": "Pre-checks",
  "클러스터 상태 확인, 진행 중인 유지보수 / 백업 윈도우 충돌 여부 확인":
    "Check the cluster state, and whether it collides with maintenance in progress or the backup window",
  "백업 확인": "Verify the backup",
  "최신 자동 백업 존재 확인, 필요시 수동 스냅샷 생성":
    "Confirm a recent automated backup exists, and take a manual snapshot if needed",
  "파라미터 호환성": "Parameter compatibility",
  "파라미터 그룹 패밀리 마이그레이션": "Parameter group family migration",
  "확장(extension)/비호환 기능 호환성 점검":
    "Check extension and incompatible-feature compatibility",
  "설치된 extension, deprecated 기능, 예약어/타입 변경 등 메이저 비호환 항목 점검":
    "Check the major incompatibilities: installed extensions, deprecated features, reserved-word and type changes",
  "pg_upgrade 사전 점검": "pg_upgrade pre-check",
  "pg_upgrade --check로 사전 호환성 검증, 비호환 객체 식별":
    "Verify compatibility ahead of time with pg_upgrade --check, and identify incompatible objects",
  "애플리케이션 준비": "Application readiness",
  "커넥션 재시도 로직 / 백오프 / read-only fallback 확인":
    "Check the connection retry logic, the backoff and the read-only fallback",
  "Blue/Green 배포 생성": "Create the Blue/Green deployment",
  "Green 환경 검증": "Validate the Green environment",
  "Green 환경에서 핵심 read/write 쿼리 실행, 응답 시간 비교":
    "Run the core read and write queries against Green and compare response times",
  "리더 복제 검증": "Validate reader replication",
  "전환 (Switchover)": "Cut over (switchover)",
  "트래픽을 Green으로 전환 (~30초 다운타임)":
    "Move traffic to Green (roughly 30s of downtime)",
  검증: "Validate",
  "애플리케이션 정상 동작 확인, 메트릭 모니터링":
    "Confirm the application behaves normally, and watch the metrics",
  정리: "Clean up",
  "롤백 불필요 시 Blue 환경 삭제":
    "Delete the Blue environment once no rollback is needed",
  "Blue 환경이 유지되므로 전환 취소(switchover-rollback)로 즉시 복귀 가능":
    "The Blue environment is kept, so a switchover-rollback returns immediately",
  "클러스터 클론 생성": "Create the cluster clone",
  "클론 업그레이드": "Upgrade the clone",
  "클론 검증": "Validate the clone",
  "클론에서 핵심 쿼리/성능 검증, 비호환 여부 확인":
    "Validate the core queries and the performance on the clone, and check for incompatibilities",
  "리더 검증": "Validate the readers",
  "엔드포인트 전환": "Switch the endpoint",
  "애플리케이션을 클론 클러스터 엔드포인트로 전환 (DNS/설정)":
    "Point the application at the clone cluster's endpoint (DNS or configuration)",
  "원본 클러스터가 유지되므로 DNS 전환으로 롤백":
    "The original cluster is kept, so rolling back is a DNS switch",
  "In-place 업그레이드 실행": "Run the in-place upgrade",
  "업그레이드 완료까지 클러스터 status=upgrading 모니터링":
    "Watch for cluster status=upgrading until the upgrade completes",
  "리더 업그레이드 검증": "Validate the reader upgrade",
  "버전 확인, 애플리케이션 정상 동작 확인":
    "Confirm the version, and confirm the application behaves normally",
  "스냅샷 복원으로만 롤백 가능. 시간 소요. 적용 전 스냅샷 필수.":
    "A snapshot restore is the only rollback. It takes time. A snapshot before applying is mandatory.",

  // ── api/simulation/handler.py: right-sizing and live-read refusals ───────
  "라이브 클러스터 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요)":
    "The live cluster read failed (see the server log for the cause)",
  "describe_db_clusters가 해당 cluster_id를 반환하지 않았습니다":
    "describe_db_clusters did not return that cluster_id",
  "CloudWatch ServerlessDatabaseCapacity 데이터포인트 없음":
    "No CloudWatch ServerlessDatabaseCapacity datapoints",
  "CloudWatch 14일 평균 ACU": "CloudWatch 14-day average ACU",
  "관측 ACU 조회 실패 (자세한 원인은 서버 로그를 확인하세요)":
    "Reading the observed ACU failed (see the server log for the cause)",
  "해당 이름의 replication group을 찾을 수 없습니다":
    "No replication group with that name",
  "일부 노드 단가를 AWS Price List API에서 확인하지 못해 부분 추정입니다.":
    "Some node prices could not be confirmed against the AWS Price List API, so this is partly an estimate.",
  "SQL Server Express: 라이선스 비용 $0 (License Included 요율에 반영)":
    "SQL Server Express: $0 license cost (already in the License Included rate)",
  "SQL Server 라이선스는 License Included 인스턴스 요율에 포함되어 가격에 반영됨":
    "The SQL Server license is part of the License Included instance rate, so it is already in the price",
  "cluster_id가 필요합니다": "cluster_id is required",
  "cluster_meta를 찾지 못했습니다": "cluster_meta was not found",
  "인스턴스 라이트사이징은 RDS MySQL/SQL Server 인스턴스에서만 지원됩니다.":
    "Instance right-sizing is supported only on RDS MySQL and SQL Server instances.",
  "우측 사이징에 필요한 CloudWatch 지표가 아직 충분히 수집되지 않았습니다.":
    "Not enough CloudWatch metrics have been collected yet for right-sizing.",
  "요청한 인스턴스 클래스로 비용 비교":
    "Cost comparison against the requested instance class",
  "이미 최소 클래스: 축소 불가":
    "Already the smallest class, so there is nothing to shrink to",

  // ── api/simulation/upgrade_estimator.py (parity twin under shared/) ──────
  "추정치는 객체(테이블) 수, 메이저 버전 점프, 리더 수 기반 휴리스틱입니다. 실제 시간은 워크로드(MySQL undo/history list length, blue/green 복제 catch-up 시 쓰기량)에 따라 달라집니다. 정확한 수치가 필요하면 fast clone으로 동일 클러스터를 복제해 시험 업그레이드를 1회 측정하는 것이 AWS 권장 방식입니다.":
    "The estimate is a heuristic over the object (table) count, the major-version jump and the reader count. The real duration depends on the workload (MySQL undo/history list length, and the write volume during blue/green replication catch-up). If you need an accurate number, the AWS-recommended way is to fast-clone the same cluster and measure one trial upgrade.",
  "~1분 미만 (switchover)": "under ~1 min (switchover)",
  "blue/green: green 환경 동기화는 백그라운드(프로덕션 영향 없음), 다운타임은 switchover(<1분, 가드레일 강제)만, green 복제 catch-up은 쓰기량에 비례":
    "blue/green: syncing the green environment runs in the background (no production impact), the only downtime is the switchover (<1 min, enforced by guardrails), and green replication catch-up scales with the write volume",
  "~1-2분 (엔드포인트 전환)": "~1-2 min (endpoint switch)",
  "clone: fast clone(copy-on-write)으로 즉시 생성, 원본 무영향; 다운타임은 애플리케이션 엔드포인트 전환만":
    "clone: created immediately as a fast clone (copy-on-write) with no effect on the original; the only downtime is switching the application endpoint",
  "in-place: writer가 업그레이드 컴퓨트 창 동안 오프라인":
    "in-place: the writer is offline for the upgrade compute window",
  "메이저 업그레이드는 in-place 시 다운타임이 길고 비호환 위험이 커 blue/green 무중단 전환을 권장합니다.":
    "For a major upgrade, in-place means long downtime and a higher incompatibility risk, so a blue/green zero-downtime cutover is recommended.",
  "객체 수 미상 (table_stats 미수집): 기본값으로 추정, 신뢰도 낮음":
    "Object count unknown (table_stats not collected): estimated from the default, low confidence",
  "마이너 업그레이드는 객체 수 무관":
    "A minor upgrade does not depend on the object count",

  // ── api/simulation/ddl_estimator.py (parity twin under shared/) ──────────
  "Serverless v2: ACU 범위가 아직 수집되지 않아 중간 처리량을 가정했습니다(실제 처리량은 ACU에 비례)":
    "Serverless v2: the ACU range has not been collected yet, so a mid-range throughput was assumed (real throughput scales with ACU)",
  "인스턴스 클래스 미상 토큰: large(×1.0) 기준 가정":
    "Unrecognized instance-class token: assuming large (x1.0)",
  "인스턴스 클래스 미상: large(×1.0) 기준 가정":
    "Instance class unknown: assuming large (x1.0)",
  "테이블 크기 미상(table_stats 미수집): 최소값 적용":
    "Table size unknown (table_stats not collected): the minimum is applied",
  "메타데이터 전용: 테이블 크기나 인스턴스와 무관, 거의 즉시 완료":
    "Metadata only: independent of table size and instance, and finishes almost immediately",
  "쓰기를 막지 않지만 테이블 전체를 재작성합니다. 소요 시간이 길고 테이블 크기만큼 추가 디스크가 필요하므로, 여유 공간과 복제 지연을 함께 확인하세요":
    "It does not block writes, but it rewrites the whole table. It takes a long time and needs as much extra disk as the table itself, so check the free space and the replication lag together",
  "온라인 DDL 가능, 서비스 영향 최소":
    "Online DDL is possible, with minimal service impact",
  "pg_repack/BG 마이그레이션": "pg_repack or a background migration",
  "쓰기 차단/배타적 락, 점검 윈도우에서 수행 권장":
    "Blocks writes and takes an exclusive lock, so running it in a maintenance window is recommended",
  "InnoDB에서 ADD COLUMN은 DEFAULT가 있어도 ALGORITHM=INSTANT로 처리되며, GENERATED ... STORED 만 테이블 재작성을 유발합니다. ":
    "On InnoDB, ADD COLUMN is handled as ALGORITHM=INSTANT even with a DEFAULT, and only GENERATED ... STORED forces a table rewrite. ",
  "ADD COLUMN은 상수/무default일 때만 메타데이터 변경이며 volatile default는 재작성을 유발합니다. ":
    "ADD COLUMN is a metadata-only change only with a constant default or none; a volatile default forces a rewrite. ",
  "메타데이터 전용 작업으로 테이블 크기와 무관하게 거의 즉시 완료됩니다.":
    "A metadata-only operation, so it finishes almost immediately regardless of table size.",

  // ── api/simulation/parameter_estimator.py (parity twin under shared/) ────
  "재시작 필요: 점검 윈도우에서 수행 권장":
    "A restart is required, so running it in a maintenance window is recommended",
  "즉시 적용 가능": "Can be applied immediately",
  "이 파라미터는 수정할 수 없습니다 (IsModifiable=false).":
    "This parameter cannot be modified (IsModifiable=false).",
  "엔진 기본값 사용 중 (파라미터 그룹에 명시값 없음)":
    "Using the engine default (no explicit value in the parameter group)",

  // ── api/simulation/dynamodb_cost.py (parity twin under shared/) ──────────
  "글로벌 테이블(멀티 리전 복제)은 용량 단가 모델이 달라 비용 비교를 지원하지 않습니다.":
    "A global table (multi-region replication) prices capacity differently, so cost comparison is not supported for it.",
  "이 시뮬레이터는 STANDARD 테이블 클래스이며 글로벌 테이블(멀티 리전 복제)이 아닌 DynamoDB 테이블만 지원합니다.":
    "This simulator supports only DynamoDB tables on the STANDARD table class that are not global tables (multi-region replication).",
  "On-Demand: 1 consumed RCU ≈ 1 RRU(≤4KB strongly-consistent read), 1 consumed WCU ≈ 1 WRU로 근사합니다.":
    "On-Demand: approximated as 1 consumed RCU ≈ 1 RRU (a ≤4KB strongly-consistent read) and 1 consumed WCU ≈ 1 WRU.",
  "RCU/WCU(capacity)만 비교합니다. storage, backup, stream, global-table replication, free-tier는 제외합니다.":
    "Only RCU/WCU (capacity) is compared. Storage, backup, streams, global-table replication and the free tier are excluded.",
  "일부 단가를 AWS Price List API에서 확인하지 못해 해당 비용은 생략했습니다(fallback).":
    "Some prices could not be confirmed against the AWS Price List API, so those costs are omitted (fallback).",

  // ── mcp-servers: task_worker trace, incident tools ───────────────────────
  진단: "Diagnose",
  "서술 생성": "Write the narrative",
  // A ko-locale task is readable on an en console: the narrative itself is
  // frozen Korean prose, but this LABEL is a fixed literal, so translating it
  // keeps the fact (the prose is Korean) legible.
  "한국어 narrative+권장조치": "Korean narrative + recommendations",
  "모델 미설정/실패, 스킵": "No model configured or the call failed, skipped",
  "신호 감지": "Signal detected",
  "자동 수집 신호에서 뚜렷한 원인 미발견, 수동 점검 권장":
    "No clear cause found in the automatically collected signals, so a manual check is recommended",
  헬스: "Health",
  "헬스 다이제스트": "Health digest",
  "작업 실행 중 오류가 발생했습니다. 자세한 원인은 서버 로그(CloudWatch)를 확인하세요.":
    "An error occurred while the task was running. See the server log (CloudWatch) for the cause.",
  "각 candidate의 score = base_weight(카테고리 prior) × recency(앵커 근접) × 카테고리 인자(event=severity, blocking=block 지속, spike=배수). 자세한 분해는 candidate.score_breakdown 참고. 우선순위(prior)는 휴리스틱입니다.":
    "Each candidate's score = base_weight (the category prior) x recency (closeness to the anchor) x a category factor (event=severity, blocking=block duration, spike=multiple). See candidate.score_breakdown for the full derivation. The priors are heuristics.",
  "cluster_meta에 status가 없습니다 (등록 직후이거나 수집 전일 수 있습니다).":
    "cluster_meta carries no status (it may have just been registered, or collection may not have run).",

  // ── data-pipeline collectors: findings prose ─────────────────────────────
  "un-purge된 행 버전이 많습니다. 장기 트랜잭션이 purge를 막고 있을 수 있습니다. 오래된 트랜잭션(information_schema.innodb_trx)을 확인/종료하세요.":
    "There are many un-purged row versions. A long-running transaction may be holding purge back. Check the old transactions (information_schema.innodb_trx) and end them.",
  커넥션: "Connections",
  "storage_type이 gp2입니다. gp3로 전환하면 마이그레이션 없이 ModifyDBInstance로 즉시 적용되고, 동일 용량에서 더 저렴하며 baseline 3000 IOPS를 확보합니다. 마이그레이션보다 먼저 검토할 항목입니다.":
    "storage_type is gp2. Moving to gp3 applies immediately through ModifyDBInstance with no migration, costs less at the same capacity and gets you a 3000 IOPS baseline. It is worth looking at before any migration.",
  "avg < 30% & p95 < 60% → 다운사이즈 검토":
    "avg < 30% and p95 < 60%, so consider downsizing",
  "슬로우 쿼리 로그가 꺼져 있어 느린 쿼리가 CloudWatch Logs로 나가지 않습니다. 인시던트 조사에서 search_logs로 슬로우 쿼리를 확인할 수 없고, 사후에 소급해 볼 수도 없습니다. slow_query_log를 ON으로 두고 long_query_time을 워크로드에 맞게(보통 1초) 낮추세요.":
    "The slow query log is off, so slow queries never reach CloudWatch Logs. An incident investigation cannot see them through search_logs, and they cannot be recovered after the fact either. Turn slow_query_log ON and lower long_query_time to suit the workload (usually 1 second).",
  "per-connection 버퍼 × max_connections":
    "per-connection buffers x max_connections",
  "shared-buffers 캐시 히트율": "shared_buffers cache hit rate",
  "shared_buffers 대비 작업셋이 커 디스크에서 읽고 있습니다. work_mem/shared_buffers 또는 인스턴스 메모리를 검토하세요.":
    "The working set has outgrown shared_buffers and reads are going to disk. Consider work_mem, shared_buffers or the instance memory.",
  "롤백 비율이 높습니다. 애플리케이션 오류/데드락/제약 위반을 확인하세요.":
    "The rollback ratio is high. Check for application errors, deadlocks and constraint violations.",
  "강제(req) 체크포인트가 잦습니다. WAL이 max_wal_size에 자주 도달한다는 신호입니다. max_wal_size 상향을 검토하세요.":
    "Forced (requested) checkpoints are frequent, which signals WAL hitting max_wal_size often. Consider raising max_wal_size.",
  "쿼리별 지연 집계가 슬로우 쿼리 패널과 AI 분석의 원천입니다.":
    "Per-query latency aggregates are what the slow-query panel and the AI analysis read.",
  "느린 쿼리의 EXPLAIN을 자동 수집합니다. 사후 분석에 필수입니다.":
    "Captures EXPLAIN for slow queries automatically. Indispensable for a post-mortem.",
  "크기 기반 추정 대신 정밀한 bloat 측정을 제공합니다.":
    "Gives a precise bloat measurement instead of a size-based estimate.",
  "배타 락 없이 동작하는 VACUUM FULL 대안입니다.":
    "A VACUUM FULL alternative that takes no exclusive lock.",
  "통계가 부정확할 때 플래너 선택을 강제할 수 있습니다.":
    "Lets you override the planner's choice when the statistics mislead it.",
  "외부 스케줄러 없이 VACUUM/ANALYZE 작업을 예약합니다.":
    "Schedules VACUUM and ANALYZE jobs without an external scheduler.",
  "핫 테이블에 즉시 VACUUM FREEZE를 실행하세요. wraparound 위험이 임박했습니다.":
    "Run VACUUM FREEZE on the hot tables now. The wraparound risk is imminent.",
  "다음 점검 윈도우에 수동 VACUUM FREEZE 실행을 예약하세요.":
    "Schedule a manual VACUUM FREEZE for the next maintenance window.",
  "쿼리 구간 평균 실행시간이 기준 대비 크게 느려졌습니다. 플랜 리그레션 또는 데이터 증가가 의심됩니다. Query Lab에서 EXPLAIN으로 현재 플랜을 확인하고, 필요하면 인덱스/통계(ANALYZE)를 점검하세요.":
    "The query's window-average execution time has slowed considerably against its baseline. A plan regression or data growth is the suspicion. Check the current plan with EXPLAIN in Query Lab, and look at the indexes and the statistics (ANALYZE) if needed.",
  "profiler=enabled, CloudWatch Logs 내보내기 OFF":
    "profiler=enabled, CloudWatch Logs export OFF",
  "profiler 파라미터는 켜져 있지만 클러스터가 profiler 로그를 CloudWatch Logs로 내보내지 않습니다. 이 단계가 빠지면 프로파일러 출력이 어디에도 전달되지 않습니다":
    "The profiler parameter is on, but the cluster does not export profiler logs to CloudWatch Logs. Without that step the profiler output goes nowhere",
  "느린 op 가시성: profiler=enabled + profiler 로그 내보내기 + sampling_rate > 0":
    "Slow-op visibility: profiler=enabled, plus the profiler log export, plus sampling_rate > 0",
};
