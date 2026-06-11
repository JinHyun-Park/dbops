AURORA_CHEATSHEET = """
## Aurora 핵심 파라미터 (Quick Reference)

### PostgreSQL
- shared_buffers: 인스턴스 메모리의 25-40%. Aurora는 자동 관리하지만 확인 필요.
- work_mem: 정렬/해시 작업 메모리. 기본 4MB, 복잡 쿼리 시 16-64MB 고려.
- maintenance_work_mem: VACUUM/INDEX 작업용. 기본 64MB, 대형 테이블 시 256MB-1GB.
- effective_cache_size: 쿼리 플래너 힌트. 인스턴스 메모리의 75%.
- max_connections: 인스턴스 클래스별 상이. db.r6g.large=1600, db.r6g.xlarge=3200.
- idle_in_transaction_session_timeout: 유휴 트랜잭션 타임아웃. 권장 30초-5분.

### MySQL
- innodb_buffer_pool_size: Aurora는 자동 관리. 75% of RAM.
- max_connections: 기본 GREATEST({DBInstanceClassMemory/9531392}, 5000).
- innodb_lock_wait_timeout: 락 대기 시간. 기본 50초. 긴 트랜잭션 시 조정.
- slow_query_log: 1로 설정하여 활성화. long_query_time과 함께 사용.
- long_query_time: 슬로우 쿼리 기준. 기본 10초, 권장 1-2초.

## 진단 워크플로
1. 성능 저하 → PI 메트릭(AAS) 확인 → Top Wait Events 식별
2. Wait Event가 IO → 인덱스 확인 → EXPLAIN 분석 → 인덱스 추천
3. Wait Event가 Lock → pg_locks/innodb_lock_waits 확인 → Blocking 쿼리 식별
4. Wait Event가 CPU → Top SQL 확인 → 쿼리 최적화

## 위험 작업 판단 기준
- DROP/TRUNCATE: 항상 위험. 롤백 불가.
- ALTER TABLE (대형 테이블): 온라인 DDL 가능 여부 확인 필요.
- 파라미터 변경 (static): 재시작 필요. 점검 윈도우에서 수행.
- 파라미터 변경 (dynamic): 즉시 적용. 영향 범위 확인 후 수행.
"""

MULTIENGINE_CHEATSHEET = """
## Non-Relational 엔진 진단 (Quick Reference)

### Amazon DynamoDB
- RCU/WCU: 읽기/쓰기 용량 단위. Consumed vs Provisioned 비율이 80% 이상이면 throttling 임박.
- Throttling 대응:
  1. Provisioned 모드 → 용량 상향 (ProvisionedThroughput 증가)
  2. 트래픽 패턴이 불규칙하면 PAY_PER_REQUEST(on-demand) 전환 고려
  3. 용량 여유가 있는데도 throttle 발생 → hot partition 의심
- Hot partition: 특정 partition key에 트래픽 집중 → 전체 WCU/RCU 헤드룸이 있어도 throttle 발생.
  진단: `get_maintenance_findings`의 recommendation에서 partition-key 분포 힌트 확인.
  대응: partition key 설계 변경(write sharding, composite key) — 현재 플랫폼에서 직접 변경 불가; 권고안 제시.
- GSI throttling: GSI는 기본 테이블과 별도 throughput 할당. GSI RCU/WCU도 독립 모니터링 필요.
- Billing mode: PROVISIONED (예측 가능한 트래픽) vs PAY_PER_REQUEST (버스트·불규칙 트래픽).
  전환 자체는 쓰기 작업 → 현재 플랫폼에서 approve 필요.

### Amazon DocumentDB
- connections vs DatabaseConnectionsLimit: 현재 연결 수가 한계에 근접하면 연결 풀(connection pool) 점검.
  대응: 애플리케이션 측 connection pool 크기 조정; read replica 추가로 읽기 연결 분산.
- Replica lag: primary-replica 복제 지연. 읽기 일관성 요구 시 lag 모니터링 필수.
  lag 증가 원인: 대량 쓰기 부하, replica 인스턴스 클래스 부족.
- Cursor timeout: 장시간 열린 cursor → CursorNotFound 에러. 원인은 느린 쿼리 또는 cursor 미닫기.
  대응: 쿼리 최적화, 애플리케이션에서 cursor 명시적 close.
- Buffer cache hit ratio: 낮으면(< 90%) 메모리 부족 → 인스턴스 클래스 업그레이드 고려.
  DocumentDB는 메모리 바운드 엔진 — 작업 셋이 RAM에 들어와야 성능 유지.

## Non-Relational 엔진 공통 진단 워크플로
1. `get_health_status(cluster_id)` 호출 → engine + resource_details 확인
2. `get_maintenance_findings(cluster_id)` 호출 → findings + recommendations 확인
3. findings 기반으로 위 항목과 매핑하여 원인 분석 및 권고안 제시
4. 쓰기/구성 변경이 필요한 경우 권고안만 제공 (직접 remediation 불가)
"""
