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
