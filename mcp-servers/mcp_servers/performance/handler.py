import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.performance.tools.compare_periods import compare_periods_impl
from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
from mcp_servers.performance.tools.detect_regressions import detect_regressions_impl
from mcp_servers.performance.tools.explain_plan import explain_plan_impl
from mcp_servers.performance.tools.forecast_capacity import forecast_capacity_impl
from mcp_servers.performance.tools.performance_summary import get_performance_summary_impl
from mcp_servers.performance.tools.pi_metrics import get_pi_metrics_impl
from mcp_servers.performance.tools.recommend_index import recommend_index_impl
from mcp_servers.performance.tools.slow_queries import get_slow_queries_impl
from mcp_servers.performance.tools.top_queries import get_top_queries_impl
from mcp_servers.performance.tools.vacuum_stats import get_vacuum_stats_impl
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import CAPABILITIES, DOCUMENTDB
from mcp_servers.shared.engine_family import engine_family as _engine_family

logger = logging.getLogger(__name__)

cache = CacheClient()

# 쿼리/플랜 툴 → 그 툴이 REQUIRE하는 per-family CAPABILITIES 키.
# operations/handler.py와 같은 POSITIVE + FAIL-CLOSED 게이트다. 게이트가 없던
# 동안 DynamoDB/ElastiCache에 get_top_queries를 물으면 unsupported_engine이
# 아니라 빈 배열이 돌아왔다. DBA에게는 "무거운 쿼리 없음"으로 읽히는 거짓
# 빈 상태. 해석 불가 클러스터(미등록·조회 실패)도 거부한다: 없는 데이터를
# "정상"으로 보고하는 것보다 거부가 안전하다.
_ENGINE_GATED_TOOLS = {
    # query_stats 행을 쓰는 수집기가 있는 패밀리: relational(pg_stat_statements /
    # events_statements_summary_by_digest), rds_instance(direct-TCP),
    # documentdb(profiler 로그, docdb_mongo_collector). dynamodb/elasticache는
    # 생산자가 없다. documentdb 행은 컬럼만 같고 의미가 다르므로
    # _annotate_documentdb가 출처를 붙인다.
    "get_top_queries": "query_stats",
    "get_slow_queries": "query_stats",
    "detect_regressions": "query_stats",
    # explain은 Aurora PG + Aurora MySQL 둘 다 지원하고(툴 안에서 엔진 문자열로
    # 다이얼렉트를 가른다), index_advice는 아직 PG 전용이라 MySQL은 툴 안에서
    # unsupported_engine을 돌려준다. 둘 다 relational만 True인 이유는 같다:
    # rds_instance는 Data API가 없어 EXPLAIN을 보낼 경로 자체가 없다.
    "explain_plan": "explain",
    "recommend_index": "index_advice",
}

_CAP_LABEL = {
    "query_stats": "쿼리 통계가 수집되는 엔진(Aurora, RDS 인스턴스, DocumentDB)",
    "explain": "EXPLAIN 지원 엔진(Aurora)",
    "index_advice": "인덱스 추천 지원 엔진(Aurora)",
}

# documentdb의 query_stats 행은 엔진 카운터가 아니다: docdb_mongo_collector가
# profiler 로그 창을 누적해 넣은 것이라(ee0a63c/ff48098) 컬럼은 같고 의미가
# 다르다. 라벨 없이 내보내면 "느린 op만의 평균"이 "그 op의 평균 응답시간"으로
# 인용된다. impl이 아니라 핸들러에 두는 이유: 패밀리는 게이트가 이미 해석했고,
# impl 세 개는 패밀리를 모르는 캐시 리더다.
_DOCDB_QUERY_STATS_NOTE = {
    "origin": "documentdb profiler log (docdb_mongo_collector), not an engine counter",
    "query_text": (
        "SQL이 아니라 MongoDB op shape입니다 (op + namespace + filter/pipeline 키). "
        "EXPLAIN이나 인덱스 추천 도구의 입력으로 쓸 수 없습니다."
    ),
    "counters": (
        "calls / total_time_ms / rows_returned 는 profiler_threshold_ms를 넘긴 op만 "
        "누적한 값입니다. 그 op의 전체 호출 수나 전체 실행시간이 아닙니다."
    ),
    "mean_time_ms": (
        "느린 op만의 평균이고, 그 위에 수집 시작 이후를 통째로 누적한 생애 평균입니다"
        "(collector가 창마다 누적 total_time_ms / 누적 calls로 다시 계산합니다). "
        "항상 profiler_threshold_ms 이상이며, 그 op의 평균 응답시간으로도 '이번 창의 "
        "평균'으로도 인용하지 마세요. 최근 구간이 실제로 얼마나 느린지는 연속한 두 행의 "
        "total_time_ms 차이를 calls 차이로 나눈 값, 즉 구간 평균으로만 말할 수 있습니다."
    ),
    # 행 하나 = op shape 하나가 아니다. 이걸 밝히지 않으면 top-N / slow-N의 답이
    # "느린 shape N개"로 읽히는데, 실제로는 같은 shape의 연속 스냅샷일 수 있다.
    # ponytail: query_hash별 최신 행만 남기는 dedup이 다음 단계다. 오늘 넣지 않는
    # 이유는 query_stats가 relational에서도 같은 스냅샷 구조라서(pg_stat_statements)
    # dedup은 이 패밀리만의 수정이 아니라 세 툴 전체의 동작 변경이기 때문이다.
    "rows": (
        "행 하나는 op shape 하나가 아니라 수집 창 하나의 스냅샷입니다. 같은 shape이 창마다 "
        "한 행씩 쌓이므로 top-N / slow-N 결과가 서로 다른 shape N개라는 보장이 없습니다"
        "(같은 query_hash의 연속 스냅샷일 수 있습니다). shape 단위로 보려면 query_hash로 "
        "그룹핑해서 shape마다 최신 행만 쓰고, 서로 다른 query_hash가 몇 개인지 세어 보세요."
    ),
    "coverage": (
        "임계값을 넘긴 op이 없던 구간에는 행이 아예 없습니다. 행이 없다는 것은 "
        "'느린 op이 없다'는 근거가 아닙니다 (profiler OFF, sampling, 로그 읽기 실패 "
        "모두 같은 모양입니다). docdb_mongo_profiler_off / "
        "docdb_mongo_profiler_read_failed finding을 함께 확인하세요."
    ),
}

# get_slow_queries가 relational 기준으로 붙이는 source 문구는 이 패밀리에서
# 사실과 반대다("슬로우 쿼리 로그가 아니다" → documentdb는 정확히 그것이다).
# threshold_ms 기본값(1000)은 이 패밀리에서 사실상 필터다: DocumentDB profiler의
# 기본 profiler_threshold_ms는 100ms(DOC_DEFAULT_THRESHOLD_MS)라서 profiler가 기록한
# 느린 op 대부분이 기본값에 걸려 빠진다. 게다가 비교 대상 mean_time_ms는 생애 평균이라
# 지금 느려진 shape도 임계값 밑에 남아 있을 수 있다.
_DOCDB_SLOW_QUERIES_SOURCE = (
    "documentdb profiler log에서 누적한 query_stats.mean_time_ms "
    "(profiler_threshold_ms를 넘긴 op만의 생애 평균). threshold_ms를 이 클러스터의 "
    "profiler_threshold_ms에 맞춰 지정하세요. 기본값 1000ms는 DocumentDB profiler 기본 "
    "임계값(100ms)의 10배라서, 기본값으로 호출하면 profiler가 실제로 기록한 느린 op이 "
    "대부분 빠지고 그 결과를 '느린 op이 없다'로 읽게 됩니다."
)

# detect_regressions는 before/after를 query_hash로 INNER JOIN한다. relational에서
# before 쪽 행이 없다는 것은 "그때는 없던 쿼리"지만, documentdb에서는 "그때는
# 느리지 않았던 op", 즉 찾고 있던 바로 그 리그레션이다. 그 행은 결과에서 빠지므로
# 빈 결과를 "리그레션 없음"으로 읽으면 안 된다.
# ponytail: after 구간에만 나타난 shape을 따로 세어 돌려주는 게 다음 단계다.
# 오탐이 아니라 누락이 실제로 문제가 될 때 추가할 것.
_DOCDB_REGRESSION_CAVEAT = (
    "이 패밀리에서는 두 가지를 먼저 확인하세요. (1) 변경 시점 전에 느리지 않았던 op은 "
    "before 쪽 행이 없어 결과에서 빠집니다. regressions가 비어 있는 것은 '리그레션이 "
    "없다'는 근거가 아니며, 같은 창의 get_top_queries와 docdb_mongo_slow_ops finding을 "
    "함께 보세요. (2) 두 구간 사이에 profiler_threshold_ms가 바뀌었다면 변화량은 "
    "워크로드가 아니라 계측 변경일 수 있습니다. before_calls / after_calls로 표본 "
    "크기를 먼저 확인하세요."
)


def _annotate_documentdb(tool_name, fam, result):
    """documentdb의 query_stats 결과에 출처 라벨을 붙인다. 다른 패밀리와 다른 값을
    돌려주지는 않는다: 같은 숫자에 그 숫자가 무엇인지를 덧붙일 뿐이다."""
    if fam != DOCUMENTDB or not isinstance(result, dict):
        return result
    if _ENGINE_GATED_TOOLS.get(tool_name) != "query_stats":
        return result
    note = dict(_DOCDB_QUERY_STATS_NOTE)
    if tool_name == "get_slow_queries":
        result["source"] = _DOCDB_SLOW_QUERIES_SOURCE
    elif tool_name == "detect_regressions":
        note["caveat"] = _DOCDB_REGRESSION_CAVEAT
    result["data_source"] = note
    return result


TOOLS = {
    "get_top_queries": {
        "impl": get_top_queries_impl,
        "description": "Get top-N queries from Aurora PG Cache sorted by total time, calls, or mean time",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sort_by": {"type": "string", "enum": ["total_time", "calls", "mean_time", "rows"], "default": "total_time"},
                "limit": {"type": "integer", "default": 10},
                "start_time": {"type": "string", "description": "ISO 8601 start time"},
                "end_time": {"type": "string", "description": "ISO 8601 end time"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_pi_metrics": {
        "impl": get_pi_metrics_impl,
        "description": "Get Performance Insights metrics (AAS, wait events, counter metrics) from cache",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "metric_type": {"type": "string", "enum": ["aas", "cpu", "connections", "iops", "wait_events"], "default": "aas"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_slow_queries": {
        "impl": get_slow_queries_impl,
        "description": "Get slow queries exceeding threshold from cache",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "threshold_ms": {"type": "number", "default": 1000.0},
                "limit": {"type": "integer", "default": 20},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "compare_periods": {
        "impl": compare_periods_impl,
        "description": "Compare metrics between two time periods for trend analysis",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "period_a_start": {"type": "string"},
                "period_a_end": {"type": "string"},
                "period_b_start": {"type": "string"},
                "period_b_end": {"type": "string"},
                "metric_type": {"type": "string", "default": "aas"},
            },
            "required": ["cluster_id", "period_a_start", "period_a_end", "period_b_start", "period_b_end"],
        },
    },
    "detect_anomalies": {
        "impl": detect_anomalies_impl,
        "description": "Detect anomalous metrics using z-score analysis against a 7-day baseline",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "hours": {"type": "integer", "default": 4, "description": "Recent window in hours to compare against baseline"},
                "threshold": {"type": "number", "default": 2.0, "description": "Z-score threshold for anomaly detection"},
            },
            "required": ["cluster_id"],
        },
    },
    "detect_regressions": {
        "impl": detect_regressions_impl,
        "description": "Detect query performance regressions around a change point (deploy, config change)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "change_point": {"type": "string", "description": "ISO 8601 timestamp of the change event"},
                "hours_before": {"type": "integer", "default": 24, "description": "Hours before change point for baseline"},
                "hours_after": {"type": "integer", "default": 24, "description": "Hours after change point to analyze"},
                "min_change_pct": {"type": "number", "default": 50.0, "description": "Minimum % increase to flag as regression"},
            },
            "required": ["cluster_id", "change_point"],
        },
    },
    "forecast_capacity": {
        "impl": forecast_capacity_impl,
        "description": (
            "Forecast when a metric will reach its limit using linear regression over the "
            "metric series that is actually collected for the cluster's engine: storage "
            "(Aurora/DocumentDB volume growth toward the 128 TiB ceiling, standalone RDS "
            "free-space shrinking toward exhaustion), connections (DatabaseConnections vs "
            "max_connections from cluster_meta / cluster_settings / the DocumentDB "
            "DatabaseConnectionsLimit datapoint), aas (vs instance vCPU, or "
            "serverlessv2_max_acu converted to vCPU on Serverless v2), read_capacity / "
            "write_capacity (DynamoDB consumed RCU/WCU per minute vs the latest provisioned "
            "rate x 60; on-demand tables have no provisioned row, so grounded=false and no "
            "date), memory (ElastiCache Redis/Valkey DatabaseMemoryUsagePercentage vs 100; "
            "Memcached does not publish it and is refused). Each metric is "
            "per-engine-family: status=unsupported_metric when the engine collects no such "
            "series. status is always present (ok | limit_reached | evicting | no_data | "
            "unsupported_metric | unknown_metric | unknown_cluster); already at/past the "
            "limit is status=limit_reached with days_until_limit=0 and "
            "approaching_limit=true. direction is up or down and usage_pct is precomputed, "
            "so never divide by limit (it is legitimately 0 in the down mode). "
            "status=evicting means an LRU/TTL cache is already recycling memory so a "
            "days-to-100% number would be meaningless: it is not an all-clear, the accurate "
            "signal is the elasticache eviction findings. Returns no date when the limit "
            "cannot be grounded in the cluster's real config."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "metric": {"type": "string",
                           "enum": ["storage", "connections", "aas", "read_capacity",
                                    "write_capacity", "memory"],
                           "default": "storage"},
                "days_lookback": {"type": "integer", "default": 30, "description": "Days of historical data for trend calculation"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_performance_summary": {
        "impl": get_performance_summary_impl,
        "description": "Get a high-level performance summary with key KPIs (AAS, slow queries, connections)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "hours": {"type": "integer", "default": 24, "description": "Time window in hours"},
            },
            "required": ["cluster_id"],
        },
    },
    "recommend_index": {
        "impl": recommend_index_impl,
        "description": (
            "(PostgreSQL only) Emit concrete CREATE INDEX CONCURRENTLY DDL by parsing the heavy "
            "queries in the cache: derives the driving table and composite column order (WHERE "
            "equality, then JOIN keys, then ORDER BY) from query_text, corroborated by "
            "table_stats seq_scan/idx_scan when available. Read-only advice — never executed; "
            "validate with EXPLAIN and create via the approval flow. MySQL returns "
            "status=unsupported_engine: the candidate filter needs shared_blks_read/hit, which "
            "the MySQL collector does not populate, so an empty answer would be 'not measured', "
            "never 'no index needed'. Use explain_plan on MySQL instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "min_seq_scan_ratio": {"type": "number", "default": 0.5},
            },
            "required": ["cluster_id"],
        },
    },
    "get_vacuum_stats": {
        "impl": get_vacuum_stats_impl,
        "description": (
            "Per-table maintenance stats. PostgreSQL: dead tuples, bloat ratio, last "
            "autovacuum/analyze. MySQL: the same cache rows relabelled in InnoDB terms — "
            "free_rows_est and fragmentation_pct from information_schema.tables.DATA_FREE "
            "(reclaimable free space expressed in ROWS, not bytes), no last_vacuum, and the "
            "remedy is OPTIMIZE TABLE. InnoDB has neither dead tuples nor VACUUM, so check "
            "the `engine` and `source` fields before quoting any number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "explain_plan": {
        "impl": explain_plan_impl,
        "description": (
            "Run EXPLAIN on a SELECT against the target cluster and return a structured plan "
            "analysis: summary, findings, and the most expensive nodes. Aurora PostgreSQL: "
            "seq scans, bad row estimates, disk spills, nested loops; analyze=false (default) "
            "only plans the query (safe/instant), analyze=true ACTUALLY RUNS the SELECT for "
            "real timings and row counts. Aurora MySQL: EXPLAIN FORMAT=JSON, reporting full "
            "table scans (access_type=ALL), filter selectivity, filesort, internal temporary "
            "tables and query_cost, plus an unavailable_analysis field naming the analyses "
            "MySQL cannot produce; analyze=true is refused on MySQL because its EXPLAIN "
            "ANALYZE returns no JSON to parse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sql": {"type": "string", "description": "SELECT or WITH...SELECT statement to explain"},
                "analyze": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, run EXPLAIN ANALYZE (executes the query) for real timings",
                },
            },
            "required": ["cluster_id", "sql"],
        },
    },
}


def _extract_tool_name(context):
    cc = getattr(context, "client_context", None)
    if not cc:
        return None
    custom = getattr(cc, "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName") or custom.get("tool_name")
    if not raw:
        return None
    delim = "___"
    return raw.split(delim, 1)[1] if delim in raw else raw


def _resolve_family(cluster_id):
    """cluster_meta에서 엔진 패밀리를 해석한다. cluster_id가 없거나, 행이
    없거나, 조회가 실패하면 None. 게이트에서 None은 FAIL-CLOSED(거부)다."""
    if not cluster_id:
        return None
    try:
        rows = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception as e:
        print(f"[performance] family lookup failed for {cluster_id}: {e}")
        return None
    rows = getattr(rows, "rows", rows)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return _engine_family(rows[0].get("engine"))
    return None


def lambda_handler(event, context):
    tool_name = _extract_tool_name(context)
    method = event.get("method") if isinstance(event, dict) else None
    print(f"INVOKE tool={tool_name} method={method}")

    if method == "tools/list" or event.get("operation") == "list_tools":
        return {
            "tools": [
                {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
                for n, t in TOOLS.items()
            ]
        }

    if tool_name and tool_name in TOOLS:
        # POSITIVE, FAIL-CLOSED 엔진 게이트. 지원 capability가 없는 패밀리(그리고
        # 해석 불가 클러스터)는 impl에 닿기 전에 unsupported_engine으로 거부한다.
        cap_key = _ENGINE_GATED_TOOLS.get(tool_name)
        fam = None
        if cap_key:
            cluster_id = (event or {}).get("cluster_id") if isinstance(event, dict) else None
            fam = _resolve_family(cluster_id)
            if not CAPABILITIES.get(fam, {}).get(cap_key, False):
                engine_label = _CAP_LABEL.get(cap_key, cap_key)
                return {"content": [{"type": "text", "text": json.dumps({
                    "status": "unsupported_engine",
                    "engine_family": fam,
                    "cluster_id": cluster_id,
                    "reason": (
                        "클러스터 엔진을 확인할 수 없습니다. 등록되지 않은 클러스터이거나 "
                        "첫 메트릭 수집 전일 수 있습니다. 클러스터 등록·수집 상태를 확인한 뒤 "
                        "다시 시도하세요."
                        if fam is None
                        else f"{tool_name}는 {engine_label} 전용입니다 (현재 엔진: {fam})."
                    ),
                })}]}
        try:
            result = TOOLS[tool_name]["impl"](cache, **(event or {}))
            result = _annotate_documentdb(tool_name, fam, result)
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except Exception:
            # 예외 텍스트는 응답에 절대 넣지 않는다(SQL·ARN·내부 경로 누출).
            # 진단 정보는 CloudWatch 로그로만 보낸다.
            logger.exception("TOOL ERROR (%s)", tool_name)
            return {"content": [{"type": "text", "text": json.dumps({
                "status": "tool_error",
                "tool": tool_name,
                "reason": (
                    "도구 실행 중 내부 오류가 발생했습니다. 결과가 없으므로 이 호출로는 "
                    "아무것도 단정할 수 없습니다. 잠시 후 다시 시도하거나 다른 도구로 확인하세요."
                ),
            })}]}

    print(f"NO MATCH tool_name={tool_name} method={method}")
    return {"error": f"Unknown tool: {tool_name}"}
