// Metric glossary — one-line "what it is + why it matters" definitions
// for the metrics DBOps surfaces. Keyed by the canonical metric_type
// string the backend uses (matches ETL collector metric_type values +
// health-score signal keys).
//
// Korean-translation rule (from project convention): DBA-known jargon
// (AAS, Replica Lag, IOPS) stays English; the explanation is Korean.
// These render as hover hints next to metric labels for onboarding —
// a senior DBA ignores them, a new one gets the gist without leaving
// the page.

export interface MetricDef {
  /** Short human label (matches what's shown in the UI). */
  label: string;
  /** One-line definition — what the number measures. */
  what: string;
  /** Why a DBA cares — what a high/abnormal value implies. */
  why: string;
  /** Optional unit hint shown in the tooltip footer. */
  unit?: string;
}

export const METRIC_GLOSSARY: Record<string, MetricDef> = {
  cpu: {
    label: "CPU",
    what: "인스턴스 CPU 사용률 (%).",
    why: "지속적으로 높으면 쿼리 비효율 또는 인스턴스 under-provisioning. 70% 경고, 90% 위험.",
    unit: "%",
  },
  aas: {
    label: "Load (AAS)",
    what: "Average Active Sessions — 평균적으로 동시에 일하고 있던 세션 수.",
    why: "vCPU 수를 넘으면 세션이 CPU/IO/lock을 기다리며 대기 중이라는 신호. Performance Insights의 핵심 지표.",
    unit: "sessions",
  },
  connections: {
    label: "Connections",
    what: "활성 + 유휴 DB 커넥션 총수.",
    why: "max_connections에 근접하면 신규 연결이 거부됨. 급증은 connection pool 누수 또는 트래픽 폭주.",
  },
  // Canonical total-connections metric (CloudWatch DatabaseConnections),
  // collected for every cluster — same meaning as "connections", reliably
  // populated even when Performance Insights is off.
  db_connections: {
    label: "Connections",
    what: "활성 + 유휴 DB 커넥션 총수 (CloudWatch DatabaseConnections).",
    why: "max_connections에 근접하면 신규 연결이 거부됨. 급증은 connection pool 누수 또는 트래픽 폭주.",
  },
  conn_active: {
    label: "Active connections",
    what: "현재 쿼리를 실행 중인 (idle 아닌) 커넥션 수.",
    why: "active가 높고 AAS도 높으면 실제 작업 부하. active는 낮은데 total이 높으면 idle 연결 누적.",
  },
  replica_lag_ms: {
    label: "Replica Lag",
    what: "Reader 인스턴스가 Writer를 따라잡지 못한 지연 (ms).",
    why: "읽기 복제본에서 stale 데이터를 반환할 수 있는 시간. 쓰기 폭주나 reader 과부하 시 증가.",
    unit: "ms",
  },
  deadlocks: {
    label: "Deadlocks/min",
    what: "분당 감지된 데드락 수.",
    why: "0이 정상. 0보다 크면 트랜잭션이 서로의 lock을 순환 대기 → 한쪽이 강제 abort. 애플리케이션 lock 순서 문제.",
    unit: "/min",
  },
  read_iops: {
    label: "Read IOPS",
    what: "초당 읽기 I/O 연산 수.",
    why: "buffer cache miss로 디스크를 때리는 정도. 갑작스러운 증가는 대용량 스캔 또는 cache eviction.",
    unit: "IOPS",
  },
  write_iops: {
    label: "Write IOPS",
    what: "초당 쓰기 I/O 연산 수.",
    why: "WAL flush + dirty page write 부하. checkpoint, 대량 INSERT/UPDATE, vacuum 시 급증.",
    unit: "IOPS",
  },
  storage_bytes: {
    label: "Storage",
    what: "클러스터 볼륨 사용량 (bytes).",
    why: "Aurora는 자동 확장되지만, 증가 속도가 급격하면 bloat / 미정리 데이터 / 로그 누적 의심.",
    unit: "bytes",
  },
  mem_free: {
    label: "Free Memory",
    what: "인스턴스 가용 메모리 (bytes).",
    why: "지속적으로 낮으면 buffer cache 압박 → 디스크 I/O 증가 → 성능 저하. swap 발생 직전 신호.",
    unit: "bytes",
  },
  cache_hit: {
    label: "Cache Hit",
    what: "buffer cache에서 처리된 블록 읽기 비율.",
    why: "PostgreSQL은 보통 99%+ 가 정상. 떨어지면 working set이 메모리를 초과했다는 의미.",
  },
  tup_returned: {
    label: "Tuples Returned",
    what: "초당 스캔되어 반환된 row 수.",
    why: "tup_fetched 대비 과도하게 높으면 인덱스 없이 풀스캔하는 쿼리가 많다는 신호.",
  },
  xact_commit: {
    label: "Commits",
    what: "초당 커밋된 트랜잭션 수.",
    why: "처리량(throughput)의 직접 지표. rollback 비율이 높이 동반되면 애플리케이션 오류 의심.",
  },

  // ── RDS instance (비-Aurora MySQL / SQL Server) health-score 시그널 ──
  free_storage_bytes: {
    label: "Free Storage",
    what: "인스턴스 스토리지 잔여 용량 (CloudWatch FreeStorageSpace).",
    why: "Aurora와 달리 RDS 인스턴스 볼륨은 자동 확장되지 않는다(storage autoscaling 미설정 시). 소진되면 STORAGE_FULL로 쓰기가 멈추므로 낮을수록 위험.",
    unit: "bytes",
  },
  read_latency: {
    label: "Read Latency",
    what: "읽기 I/O 1건당 평균 소요 시간 (CloudWatch ReadLatency, 수집은 초 단위).",
    why: "스토리지 포화·IOPS 한계 신호. 20ms를 넘어 지속되면 쿼리 응답 시간이 그대로 늘어난다.",
    unit: "ms",
  },
  write_latency: {
    label: "Write Latency",
    what: "쓰기 I/O 1건당 평균 소요 시간 (CloudWatch WriteLatency, 수집은 초 단위).",
    why: "커밋 지연의 직접 원인. gp2 버스트 소진, 프로비저닝 IOPS 부족, 대량 쓰기 시 상승.",
    unit: "ms",
  },

  // ── ElastiCache (Redis/Valkey/Memcached) health-score 시그널 ──
  engine_cpu: {
    label: "Engine CPU",
    what: "Redis/Valkey 엔진 스레드의 CPU 사용률 (EngineCPUUtilization).",
    why: "명령 처리는 단일 스레드다. 노드 전체 CPU가 여유로워도 이 값이 높으면 이미 포화. 스케일업/샤딩 판단의 실제 기준.",
    unit: "%",
  },
  cache_cpu: {
    label: "CPU",
    what: "캐시 노드 전체 CPU 사용률 (CPUUtilization).",
    why: "멀티스레드인 Memcached에서는 이 값이 주 포화 지표. Redis에서는 복제·스냅샷 같은 백그라운드 작업까지 포함한다.",
    unit: "%",
  },
  memory_usage_pct: {
    label: "Memory Usage",
    what: "maxmemory 대비 사용 중 메모리 비율 (DatabaseMemoryUsagePercentage).",
    why: "100%에 가까우면 eviction 정책이 키를 버리기 시작하고, noeviction이면 쓰기가 거부된다.",
    unit: "%",
  },
  evictions: {
    label: "Evictions/min",
    what: "메모리 확보를 위해 삭제된 키 수.",
    why: "0이 정상. 계속 발생하면 working set이 노드 메모리를 초과한 상태다. 스케일업 또는 TTL·키 정리가 필요하다.",
    unit: "/min",
  },
  curr_connections: {
    label: "Connections",
    what: "캐시 노드의 현재 클라이언트 커넥션 수 (CurrConnections).",
    why: "maxclients(기본 65000)에 근접하면 신규 연결이 거부된다. 급증은 커넥션 풀 누수 신호.",
  },
  replication_lag: {
    label: "Replication Lag",
    what: "replica가 primary를 따라잡지 못한 지연.",
    why: "replica 읽기에서 stale 값을 반환할 수 있는 시간. 쓰기 폭주·네트워크 지연·대형 키 복제 시 증가.",
    unit: "s",
  },
  swap_usage: {
    label: "Swap",
    what: "노드가 사용 중인 swap 크기 (SwapUsage).",
    why: "AWS 권장은 50MB 미만. 그 이상은 메모리가 부족해 디스크를 쓰는 상태로, 레이턴시가 급격히 나빠진다.",
    unit: "MB",
  },

  // ── DocumentDB health-score 시그널 ──
  cpu_utilization: {
    label: "CPU",
    what: "인스턴스 CPU 사용률 (CloudWatch CPUUtilization).",
    why: "지속적으로 높으면 쿼리 비효율 또는 인스턴스 under-provisioning. 70% 경고, 90% 위험.",
    unit: "%",
  },
  buffer_cache_hit: {
    label: "Buffer Cache Hit",
    what: "요청 블록을 버퍼 캐시에서 처리한 비율.",
    why: "DocumentDB는 보통 95%+ 가 정상. 떨어지면 working set이 인스턴스 메모리를 초과했다는 의미다. 인덱스 추가나 스케일업을 검토한다.",
    unit: "%",
  },
  cursors_timed_out: {
    label: "Cursor Timeouts",
    what: "타임아웃으로 정리된 커서 수.",
    why: "0이 정상. 증가하면 애플리케이션이 커서를 끝까지 읽지 않고 방치해 서버 리소스를 잡고 있다는 신호.",
  },

  // ── DynamoDB health-score 시그널 ──
  read_throttle_events: {
    label: "Read Throttles",
    what: "프로비저닝된 읽기 용량을 초과해 스로틀된 이벤트 수.",
    why: "0이 정상. 발생하면 읽기가 지연·거부된다. 파티션 편중(hot key)이거나 RCU가 부족한 상태.",
  },
  write_throttle_events: {
    label: "Write Throttles",
    what: "프로비저닝된 쓰기 용량을 초과해 스로틀된 이벤트 수.",
    why: "0이 정상. 발생하면 쓰기가 실패한다(재시도 필요). WCU 증설 또는 On-Demand 전환 검토.",
  },
  throttled_requests: {
    label: "Throttled Requests",
    what: "용량 초과로 거부된 요청 수 (ProvisionedThroughputExceeded).",
    why: "테이블·인덱스 단위 스로틀의 총량. 지속되면 애플리케이션에 그대로 에러로 노출된다.",
  },
  latency_ms_getitem: {
    label: "GetItem Latency",
    what: "GetItem 요청의 평균 응답 시간 (SuccessfulRequestLatency).",
    why: "단일 항목 조회는 보통 한 자리 ms. 상승하면 항목 크기 증가나 스로틀 재시도를 의심.",
    unit: "ms",
  },
  latency_ms_query: {
    label: "Query Latency",
    what: "Query 요청의 평균 응답 시간 (SuccessfulRequestLatency).",
    why: "스캔 범위가 넓거나 필터로 대량 항목을 버리면 상승한다. 키 설계·GSI 재검토 신호.",
    unit: "ms",
  },

  // ── SQL Server 엔진 내부 (sys.dm_os_performance_counters, rds_instance) ──
  // 전부 파생값 또는 순간값이다. 누적 카운터(cntr_type 272696576)는 수집하지
  // 않으므로 이 시리즈에는 단조 증가하는 값이 없다.
  mssql_buffer_cache_hit_ratio: {
    label: "Buffer Cache Hit Ratio",
    what: "요청 페이지를 디스크 없이 버퍼 풀에서 처리한 비율. Buffer cache hit ratio를 짝 base 카운터로 나눈 값이다(원시 cntr_value는 비율이 아니다).",
    why: "정상 워크로드에서는 95% 이상. 낮으면 working set이 버퍼 풀을 넘어 디스크를 읽고 있다는 뜻이라 max server memory 또는 인스턴스 메모리를 함께 본다. 단독으로는 신뢰도가 낮아 Page Life Expectancy와 같이 읽어야 한다.",
    unit: "%",
  },
  mssql_page_life_expectancy_sec: {
    label: "Page Life Expectancy",
    what: "데이터 페이지가 버퍼 풀에 머무는 기대 시간(초).",
    why: "메모리 압박의 가장 직접적인 신호다. 급격히 떨어지면 버퍼 풀이 계속 밀려나며 재읽기가 일어나고 있다는 뜻. 절대 임계치는 메모리 크기에 따라 달라지므로 이 클러스터의 평소 추세와 비교한다.",
    unit: "s",
  },
  mssql_server_memory_used_pct: {
    label: "Total / Target Server Memory",
    what: "SQL Server가 지금 확보한 메모리(Total Server Memory)를 확보 목표(Target Server Memory)로 나눈 비율. 건강도 점수가 아니라 버퍼 풀 확보가 어디까지 진행됐는지를 나타낸다.",
    why: "낮은 값이 곧 문제는 아니다. 수요가 없으면 SQL Server는 Target까지 올릴 이유가 없어 Total이 Target 밑에 오래 머무는 것이 정상 정상상태다(실측: 유휴 상태의 dbops-demo-mssql에서 37~43%, 같은 시점 Page Life Expectancy 26,429초·Memory Grants Pending 0·Processes Blocked 0). 100%에 붙어 있으면 목표만큼 다 확보한 상태로, 더 필요하면 max server memory 상한을 본다. 이 지표만으로는 유휴와 메모리 압박을 구분할 수 없으므로, 압박 여부는 Page Life Expectancy와 Memory Grants Pending으로 판단한다.",
    unit: "%",
  },
  mssql_processes_blocked: {
    label: "Processes Blocked",
    what: "지금 다른 세션의 락을 기다리며 차단된 프로세스 수.",
    why: "0이 정상. 0이 아닌 값이 지속되면 블로킹 체인이 있다는 뜻으로, blocked process threshold를 설정해 블로킹 리포트를 남기고 원인 트랜잭션을 찾는다.",
  },
  mssql_memory_grants_pending: {
    label: "Memory Grants Pending",
    what: "쿼리 실행에 필요한 메모리 그랜트를 받지 못해 대기 중인 쿼리 수.",
    why: "0이 정상. 0보다 크면 정렬·해시 조인이 메모리를 못 받아 대기 중이라는 뜻으로, 메모리 부족이 이미 쿼리 지연으로 나타나고 있는 상태다.",
  },
};

/** Lookup with graceful fallback — unknown metric returns null so
 *  callers can simply skip the hint rather than render an empty one. */
export function metricDef(metric: string): MetricDef | null {
  return METRIC_GLOSSARY[metric] ?? null;
}
