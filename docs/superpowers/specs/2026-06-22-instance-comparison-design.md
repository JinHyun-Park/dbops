# Instance-vs-Instance Comparison (Compare 확장)

> Version: 1.0
> Date: 2026-06-22
> Status: Draft
> Author: AI-assisted design (Claude Opus 4.8)

## 1. Overview

### 1.1 Purpose

Compare 페이지는 현재 **클러스터 vs 클러스터**(`cluster`)와 **한 클러스터의 두 시점**(`period`)을 비교한다. 운영 환경에서는 한 클러스터에 Read Replica가 여러 대인 경우가 많아, **인스턴스 vs 인스턴스**(writer vs reader, reader vs reader) 비교가 필요하다. 어느 리플리카가 더 핫한지, 특정 리더의 Replica Lag·부하·메모리·IO가 다른 인스턴스 대비 어떤지를 한눈에 본다.

### 1.2 Goals

- Compare에 세 번째 모드 **`instance`** 추가 (`cluster`·`period`는 그대로 유지).
- 한 클러스터의 인스턴스 A vs B를 **풀 메트릭 세트**로 비교 (역할 배지 writer/reader).
- "거의 실시간(≈1분) + 히스토리"를 **단일 경로**로 충족 — DBOps의 캐시-우선 원칙(대시보드는 실시간 AWS 호출 안 함) 준수.
- 기존 cluster-level 데이터 경로·대시보드·알림에 **비파괴**.

### 1.3 Non-Goals (이번 범위 밖)

- **Cross-cluster 인스턴스 비교** — instance 모드는 같은 클러스터 내로 한정. 서로 다른 클러스터 비교는 기존 `cluster` 모드가 커버. (추후 확장 용이.)
- 별도 **라이브 CloudWatch 경로** — 1분 캐시가 near-real-time을 충족하므로 불필요(제품 원칙 위배).
- per-instance 슬로우 쿼리/스키마 비교 — 우선 CloudWatch 메트릭만(쿼리 통계는 클러스터 단위 유지).

## 2. Architecture

### 2.1 데이터 모델 — `metric_snapshots` (기존 테이블, 추가만)

per-instance 메트릭을 기존 `metric_snapshots`에 **나란히 추가**:

- 기존 cluster-level 행: `dimensions = '{}'` (`DBClusterIdentifier` 롤업) — **그대로 유지**.
- 신규 per-instance 행: `dimensions = {"instance":"<DBInstanceIdentifier>","role":"writer|reader"}`.

기존 대시보드/Compare/알림 쿼리는 모두 `dimensions IS NULL OR dimensions::text = '{}'` 로 cluster-level만 필터하므로 **per-instance 행에 영향받지 않는다**(비파괴). instance 비교 쿼리만 `dimensions->>'instance' = :inst` 로 per-instance 행을 읽는다.

약간의 중복(cluster 롤업 ≈ writer)은 감수 — 의미상 cluster 롤업 ≠ 정확히 writer라 별개 값이고, 롤업 유도로 기존 경로를 바꾸는 것보다 위험이 훨씬 작다.

### 2.2 per-instance 메트릭 세트 (풀)

CloudWatch `AWS/RDS` 의 인스턴스 차원(`DBInstanceIdentifier`) 메트릭:

`CPUUtilization`, `AuroraReplicaLag`(리더), `DatabaseConnections`, `FreeableMemory`,
`FreeLocalStorage`, `ReadIOPS`, `WriteIOPS`, `ReadLatency`, `WriteLatency`,
`NetworkReceiveThroughput`, `NetworkTransmitThroughput`, `BufferCacheHitRatio`.

(참고: 현재 cluster-level `cw_collector`는 CPU를 수집하지 않는다 — CPU는 인스턴스 차원에서만 의미 있어 per-instance 세트에 새로 포함. `VolumeBytesUsed`·`Deadlocks`·`EngineUptime`·`ServerlessDatabaseCapacity` 는 클러스터 단위라 per-instance 미수집.)

### 2.3 인스턴스 목록(레지스트리)

ETL meta 수집기가 이미 `describe_db_instances`를 호출하므로, 멤버 목록을 `cluster_meta` 에 저장:

- `cluster_meta` 에 **`instances JSONB` 컬럼 추가**(schema_v18): `[{"id":"...","role":"writer|reader","class":"db.r6g.large"}]`.
- meta 수집기가 매 사이클 멤버를 갱신.

Compare가 클러스터 선택 시 이 목록으로 인스턴스 A/B 드롭다운을 채운다(역할 배지·클래스 표시).

### 2.4 데이터 흐름

```
[etl_collector] 1분 주기
  ├─ describe_db_instances → cluster_meta.instances 갱신 (id/role/class)
  └─ cw_collector(per-instance): 멤버 순회 × 풀세트, DBInstanceIdentifier 차원
       → metric_snapshots (dimensions={instance,role})
                         │  (cluster-level 행은 기존대로 dimensions={})
                         ▼
[GET /api/dashboard/{cluster}/instances]      → 인스턴스 목록(id/role/class)
[GET .../batch-timeseries?instance=<id>&...]  → per-instance 시계열(dimensions 필터)
                         │
                         ▼
[Compare 페이지 instance 모드] 클러스터 1개 → 인스턴스 A/B → 2×3 풀세트 차트
```

## 3. Components

### 3.1 Backend / ETL (data 스택)

- `schema_migrator/sql/schema_v18.sql` — `ALTER TABLE cluster_meta ADD COLUMN IF NOT EXISTS instances JSONB`.
- `etl_collector/collectors/meta_collector` — 멤버 목록(id/role/class)을 `cluster_meta.instances` 에 저장.
- `etl_collector/collectors/cw_collector.py` — per-instance 수집 추가: 클러스터 멤버를 순회하며 풀세트를 `DBInstanceIdentifier` 차원으로 조회, `dimensions={instance,role}` 로 INSERT. 기존 cluster-level 수집 로직은 그대로. (인스턴스×메트릭 호출 수가 fleet 스케일에서 커지면 `get_metric_data` 배치로 전환 — 우선은 기존 `get_metric_statistics` 루프 패턴 유지.)

### 3.2 API (agent 스택)

- `GET /api/dashboard/{cluster_id}/instances` — `cluster_meta.instances` 반환(없으면 빈 배열). 라우트 개별 등록(add_routes).
- `batch-timeseries` 핸들러에 **선택적 `instance` 쿼리 파라미터** — 있으면 `WHERE ... AND dimensions->>'instance' = :inst`, 없으면 기존 cluster-level(`dimensions={}`) 동작 유지.

### 3.3 Frontend

- `lib/api-client.ts` — `fetchClusterInstances(clusterId)`; `fetchBatchTimeseries` 에 `instance?` 옵션 추가.
- `app/compare/page.tsx` — `Mode` 에 `"instance"` 추가. instance 모드 UI: 클러스터 1개 picker → 그 클러스터 인스턴스 A/B picker(역할 배지·클래스) → 기존 차트 그리드를 per-instance 풀세트로 렌더(기존 시리즈/색상/Expandable 재사용). cluster·period 모드 분기는 불변.

## 4. Safety / Cost

- 모든 경로 **읽기 전용**(write 없음).
- 저장량은 클러스터당 인스턴스 수에 비례 증가 — 기존 `metric_snapshots` 시간 파티션 + 보존정책으로 상한. per-instance 모니터링 도구가 으레 저장하는 수준.
- 기존 cluster-level 쿼리·대시보드·알림 **비파괴**(dimensions 필터로 분리).
- `instance` 파라미터/`cluster_id` 는 인증 authorizer 하위 라우트.

## 5. Increments (구현 순서)

1. **수집**: schema_v18(`instances` 컬럼) + meta 수집기 멤버 저장 + cw_collector per-instance. → data 배포, dev에서 metric_snapshots에 per-instance 행 + cluster_meta.instances 확인.
2. **API**: `/instances` 엔드포인트 + batch-timeseries `instance` 필터. → 유닛 + 라이브 조회 검증.
3. **프런트**: Compare `instance` 모드(클러스터→인스턴스 A/B→차트). → 빌드·배포·종단 검증(W/R 클러스터에서 두 인스턴스 비교).

## 6. Test Strategy

- cw_collector per-instance 유닛: 멤버 순회·`DBInstanceIdentifier` 차원·`dimensions={instance,role}` INSERT, cluster-level 경로 불변.
- meta 수집기 유닛: `instances` 페이로드(id/role/class).
- API 유닛: `/instances` 응답, batch-timeseries `instance` 필터(미지정 시 기존 동작 유지) — 핸들러↔스키마 parity.
- 기존 dashboard/Compare 회귀: per-instance 행 추가 후에도 cluster-level 시리즈 불변.
- 종단: dev W/R(또는 다중 리더) 클러스터에서 instance 모드 A/B 비교 렌더.
