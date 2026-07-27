# 엔진 패리티 프로그램 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> 감사 근거는 `docs/superpowers/specs/2026-07-24-engine-parity-audit.md`. **구현 전에 그 문서의 "적대 검증 교정" 절을 반드시 읽을 것.** 원안 갭 제안 중 여러 건은 원인 진단이 틀렸다.

**Goal:** 엔진별 깊이 차이 중 "그냥 안 만든 것"(감사 기준 약 75%)을 닫고, 그 과정에서 발견된 "틀린 답을 자신 있게 내는" 경로를 먼저 제거한다.

**Architecture:** 기존 스파인(수집 → Aurora PG 캐시 → findings → 승인 게이트 write → 단일 에이전트)은 그대로 두고, 엔진별로 (a) 잘못된 Aurora 가정 제거, (b) 엔진 native 메커니즘 소비, (c) 엔진 중립 디스패치 맵에 누락된 키 추가로 접근한다. 새 인프라는 최소화한다.

**Tech Stack:** Python 3.12 Lambda(MCP servers, data-pipeline, api), Next.js 16 프런트, CDK Python.

## Global Constraints

- 인프라 변경은 전부 CDK. `cdk/config/settings.py`는 사용자 실값이며 절대 덮어쓰지 않는다.
- `api/`는 `mcp_servers`를 임포트할 수 없다(로직 복제 시 병렬 유닛테스트로 패리티를 가드).
- API/툴 응답에 `str(e)` 원문 금지. 정적 사유 + 서버 로그.
- `engine_family.py`는 4개 Python 복사본이 **byte-parity**이며 `frontend/src/lib/engine.ts` 미러가 있다. 한 곳만 바꾸면 parity 테스트가 깨진다.
- 신규 MCP 툴/파라미터는 `cdk/tool_definitions.py` 게이트웨이 스키마와 반드시 parity.
- 신규 승인 액션은 `request_approval` allowlist에 등록.
- 신규 findings check_type은 프런트 `CHECK_LABELS`에 라벨 추가(없으면 대시보드에서 미표시).
- 엔진 게이트는 **positive + fail-closed**(`operations/handler.py:_ENGINE_GATED_TOOLS` 패턴). 해석 불가 클러스터는 거부.
- 커밋에 Claude Co-Authored-By 금지. em-dash 금지.
- `agent/`에서 `py_compile`/`python` 실행 금지(`__pycache__`가 생기면 Runtime 배포 거부). `ast.parse`로 검증.

## 티어 구성

| 티어 | 주제                                                                                     | 상태           |
| ---- | ---------------------------------------------------------------------------------------- | -------------- |
| E-0  | 정확성: 틀린 출력, 규칙 위반, dark 기능 제거                                             | 이 계획의 대상 |
| E-1  | 엔진 중립 디스패치 패리티(용량 ETA, 시즌 베이스라인, RCA 메트릭, findings 윈도우)        | 예정           |
| E-2  | Aurora MySQL 패리티(4 check_type, EXPLAIN 종단, 인덱스 dialect+데이터)                   | 예정           |
| E-3  | rds_instance 패리티(인스턴스 파라미터 경로, 시뮬레이션 게이트 해제, SQL Server DMV 확장) | 예정           |
| E-4  | schema_snapshots 구축(L), ElastiCache 딥리드 영속화                                      | 예정           |

---

## E-0: 정확성 티어

### Task 0: 신규 capability 키 (선행, 단독 실행)

**Files:**

- Modify: `mcp-servers/mcp_servers/shared/engine_family.py`
- Modify: `api/clusters/engine_family.py`
- Modify: `api/dashboard/engine_family.py`
- Modify: `data-pipeline/etl_collector/collectors/engine_family.py`
- Modify: `frontend/src/lib/engine.ts`
- Test: 기존 byte-parity 테스트 확인 후 필요 시 갱신

**Interfaces (Produces):** 아래 4개 capability 키. 이후 모든 태스크가 이걸 소비한다.

| 키                  | relational | rds_instance | documentdb | dynamodb | elasticache |
| ------------------- | ---------- | ------------ | ---------- | -------- | ----------- |
| `query_stats`       | True       | True         | False      | False    | False       |
| `explain`           | True       | False        | False      | False    | False       |
| `index_advice`      | True       | False        | False      | False    | False       |
| `cluster_parameter` | True       | False        | False      | False    | False       |

근거: `query_stats` 테이블에 행을 쓰는 패밀리는 relational(pg_stat_statements / events_statements_summary_by_digest)과 rds_instance(직접 TCP 수집기)뿐이다. `explain`/`index_advice`는 현재 PG 전용 구현이라 relational만 True로 두고, E-2에서 MySQL을 켠다. `cluster_parameter`는 Aurora 클러스터 파라미터 그룹 전용이며 rds_instance는 인스턴스 파라미터 그룹이라 E-3에서 별도 키로 다룬다.

- [ ] 4개 Python 복사본에 동일 문자열로 키 추가(byte-parity 유지)
- [ ] `engine.ts` 미러 갱신
- [ ] parity 테스트 실행: `python3 -m pytest tests/unit -k engine_family -q`

### Task 1: performance 서버 엔진 게이트 + forecast 스토리지 메트릭 정정

**Files:**

- Modify: `mcp-servers/mcp_servers/performance/handler.py`
- Modify: `mcp-servers/mcp_servers/performance/tools/forecast_capacity.py`
- Modify: `mcp-servers/schemas/performance.json`
- Test: `tests/unit/test_performance_*.py`

**문제 1, 게이트 부재:** `performance/handler.py`에는 엔진 게이트가 전혀 없다(`lambda_handler`가 곧바로 impl 호출). 그래서 DynamoDB/ElastiCache에 `get_top_queries`/`get_slow_queries`를 물으면 `unsupported_engine`이 아니라 **빈 배열**이 돌아온다(거짓 빈 상태). `explain_plan`은 PG 문법을 무조건 만들어 Aurora MySQL에서 syntax error, rds_instance에서는 `cache_client.execute_on_target`이 `cluster_arn`/`secret_arn`을 요구해 "cluster not registered or unreachable"을 반환한다(등록된 클러스터인데도).

**문제 2, forecast 기본 메트릭이 죽어 있음:** `_resolve_limit`은 `storage_gb`에 Aurora 128TiB를 반환하지만, **어떤 수집기도 `metric_type='storage_gb'`를 쓰지 않는다.** 실제 수집값은 Aurora/DocDB `storage_bytes`(증가), rds_instance `free_storage_bytes`(감소)다. 따라서 기본 경로가 전 엔진에서 표본 0개다. 정답 계산식은 이미 `data-pipeline/etl_collector/collectors/capacity_forecast.py`(285~346행)에 있으니 그 로직을 따른다.

- [ ] `operations/handler.py:827-842`의 positive fail-closed 게이트 패턴을 `performance/handler.py`에 이식
- [ ] 매핑: `get_top_queries`/`get_slow_queries`/`detect_regressions` → `query_stats`, `explain_plan` → `explain`, `recommend_index` → `index_advice`
- [ ] `forecast_capacity`: 메트릭 이름을 실제 수집 값으로 교체. Aurora/DocDB는 `storage_bytes` 증가분을 볼륨 상한과 비교, rds_instance는 `free_storage_bytes`가 0으로 감소하는 소진 ETA. 상한은 `cluster_meta.resource_details.allocated_storage_gb`(rds_instance) 사용. 값이 없으면 `grounded=False`로 표기하고 단정하지 않는다.
- [ ] `performance.json`과 `handler.py`의 metric enum을 실제 지원 값으로 정정
- [ ] `cdk/tool_definitions.py`와 파라미터 parity 확인
- [ ] 유닛테스트: 게이트가 각 비지원 패밀리에 `unsupported_engine`을 반환하는지, forecast가 두 스토리지 형태(증가/감소)를 각각 올바른 방향으로 예측하는지

### Task 2: operations 정확성 (modify_parameter 게이트 + str(e) 제거 + DocumentDB 프로파일러 재구현)

**Files:**

- Modify: `mcp-servers/mcp_servers/operations/tools/modify_parameter.py`
- Modify: `mcp-servers/mcp_servers/operations/handler.py`
- Modify: `mcp-servers/mcp_servers/operations/tools/set_docdb_profiler.py`
- Modify: `cdk/tool_definitions.py` (파라미터 변경 시)
- Test: `tests/unit/test_operations_*.py`

**문제 1:** `modify_parameter.py:36`이 `rds.describe_db_clusters`를 호출하는데 `_ENGINE_GATED_TOOLS`에 없다. rds_instance/DocumentDB/ElastiCache에서 실패하며 **39행과 65행이 `str(e)`를 응답에 넣는다**(프로젝트 규칙 위반).

**문제 2, 구조적 오류:** `set_docdb_profiler`는 `db.command("profile", level, slowms=)`로 프로파일러를 켠다. **관리형 Amazon DocumentDB는 이 방식을 지원하지 않는다.** AWS 문서(`docs.aws.amazon.com/documentdb/latest/devguide/profiling.html`) 기준 올바른 절차는 3단계다.

1. 커스텀 클러스터 파라미터 그룹에서 `profiler`(enabled/disabled), `profiler_threshold_ms`(50~INT_MAX), `profiler_sampling_rate`(0.0~1.0) 수정
2. 클러스터가 그 파라미터 그룹을 쓰도록 변경
3. `profiler` 로그를 CloudWatch Logs로 내보내기 활성화(로그 그룹 `/aws/docdb/profiler`)

또한 프로파일러 출력은 `system.profile` 컬렉션이 아니라 CloudWatch Logs로 간다. `system.*` 컬렉션은 DocumentDB에서 미지원이다.

- [ ] `modify_parameter`를 `cluster_parameter` capability로 positive 게이트
- [ ] `str(e)` 두 곳 제거: 정적 사유 문자열 + `logger` 기록
- [ ] `set_docdb_profiler`를 boto3 기반으로 재구현: `docdb`(또는 `rds`) `modify_db_cluster_parameter_group` + `modify_db_cluster(EnableCloudwatchLogsExports=['profiler'])`. Mongo write 자격증명이 아니라 IAM 권한으로 동작하므로 `mongo_write_secret_arn` 의존을 제거한다.
- [ ] 기본 파라미터 그룹은 수정 불가이므로, 커스텀 그룹이 아닐 때는 그 사실을 명시해 거부(자동 생성은 이 태스크 범위 밖)
- [ ] 승인 게이트 유지: `verify_approval` 소비 흐름과 payload 바인딩 불변
- [ ] `cdk/tool_definitions.py` 스키마 parity
- [ ] 유닛테스트: 비관계형에서 `unsupported_engine`, 응답에 예외 원문 없음, 프로파일러가 파라미터 그룹 API를 호출하는지(Mongo 명령 미호출)

### Task 3: DocumentDB mongo 자격증명 배선

**Files:**

- Modify: `api/clusters/handler.py`
- Test: `tests/unit/test_api_clusters*.py`

**문제:** `data-pipeline/docdb_mongo_collector/handler.py:362`는 `mongo_secret_arn`을, `create_docdb_index.py:67`은 `mongo_write_secret_arn`을 읽는다. 그런데 `_register_docdb`는 두 필드를 쓰지 않고, `PATCH /clusters/{id}/meta`(978행)는 `db_secret_arn`/`db_write_secret_arn`만 화이트리스트한다. **결과적으로 제품 어디에서도 이 값을 채울 수 없어** DocumentDB 딥리드 수집기가 모든 클러스터를 skip하고 Mongo write 툴은 항상 "자격증명 없음"을 반환한다.

- [ ] `_register_docdb`가 `mongo_secret_arn`/`mongo_write_secret_arn`을 받아 저장(미지정 시 빈 문자열)
- [ ] `PATCH /meta` 화이트리스트에 두 필드 추가(다른 필드와 동일한 검증 + admin 게이트 유지)
- [ ] `ConditionExpression=attribute_exists`로 삭제 레이스 phantom 방지(기존 패턴 준수)
- [ ] 유닛테스트: 등록/PATCH 후 projection에 필드가 실제로 포함되는지(mock의 silent no-op 누출 주의)

### Task 4: HealthScore 신호셋 (rds_instance, ElastiCache)

**Files:**

- Modify: `frontend/src/components/dashboard/health-score.tsx`
- Test: 기존 프런트 테스트 확인

**문제:** `signalsForEngine`(145~149행)은 documentdb/dynamodb만 분기하고 나머지를 `SIGNALS_RELATIONAL`로 폴백한다. 그래서 rds_instance는 `replica_lag_ms`/`deadlocks`/`buffer_cache_hit`가 영구 공백이고(Aurora 전용 메트릭), ElastiCache는 자기 7개 metric_type을 쓰는데 관계형 신호로 채점된다.

- [ ] `SIGNALS_RDS_INSTANCE` 추가: 실제 수집되는 값만(cpu, db_connections, free_storage_bytes, read/write latency). Aurora 전용(replica_lag_ms, deadlocks, buffer_cache_hit) 제외
- [ ] `SIGNALS_ELASTICACHE` 추가: `cache_cpu`/`engine_cpu`/`memory_usage_pct`/`evictions`/`curr_connections`/`cache_hit_rate` 등 실제 metric_type 사용
- [ ] `signalsForEngine`에 두 분기 추가
- [ ] 실제 수집되는 metric_type 이름을 수집기 소스에서 확인(추측 금지)
- [ ] 프런트 빌드: `cd frontend && npm run build`

---

## 진행 원장

`.superpowers/sdd/progress.md`에 태스크 완료를 한 줄씩 append 한다.
