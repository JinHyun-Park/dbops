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
};

/** Lookup with graceful fallback — unknown metric returns null so
 *  callers can simply skip the hint rather than render an empty one. */
export function metricDef(metric: string): MetricDef | null {
  return METRIC_GLOSSARY[metric] ?? null;
}
