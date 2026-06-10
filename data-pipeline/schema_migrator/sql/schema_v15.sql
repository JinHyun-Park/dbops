-- v15: Data API(HttpEndpoint) 가용성을 cluster_meta에 기록.
--
-- 라이브 SQL 수집(pg_stat_activity, 테이블 통계, Top Queries, 설정 스냅샷)과
-- 에이전트 execute_sql은 전부 RDS Data API를 경유한다. HttpEndpoint가 꺼진
-- 클러스터(특히 프로비저닝)는 이 경로가 구조적으로 막혀 있는데, 등록 검증은
-- 컨트롤 플레인(DescribeDBClusters)만 보기 때문에 "ok"로 통과한다 — 그 결과
-- 대시보드의 라이브 SQL 패널이 영원히 "수집 대기" 상태로 남는다.
-- 수집기가 매 사이클 HttpEndpointEnabled를 기록하고, 프런트가 false일 때
-- 조치 안내 배너를 띄우는 데 쓴다. NULL = 아직 미수집(구버전 행).

ALTER TABLE cluster_meta ADD COLUMN IF NOT EXISTS http_endpoint_enabled BOOLEAN;
