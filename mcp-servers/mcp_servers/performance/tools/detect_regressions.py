from mcp_servers.shared.cache_client import CacheClient


def detect_regressions_impl(
    cache: CacheClient,
    cluster_id: str,
    change_point: str,
    hours_before: int = 24,
    hours_after: int = 24,
    min_change_pct: float = 50.0,
) -> dict:
    # There is deliberately NO minimum-calls filter here (the ETL
    # query_regression collector's MIN_CALLS is a different, findings-side
    # mechanism). Without one, `min_change_pct` can fire on a query whose whole
    # sample is a couple of executions, so the call counters ride along in the
    # result and `methodology` says how to read them. `calls` is a CUMULATIVE
    # counter in every writer of this table (pg_stat_statements, the MySQL/SQL
    # Server digest collectors, and the DocumentDB profiler accumulator), so
    # MAX(calls) per side is the running total at the END of that side's window
    # and after_calls - before_calls is what actually ran in between.
    #
    # BOTH casts around ROUND are load-bearing, do not "simplify" either away:
    #   * query_stats.mean_time_ms is DOUBLE PRECISION, so AVG() of it is double
    #     precision, and PostgreSQL has NO round(double precision, integer) -- only
    #     round(numeric, integer) and the 1-arg round(double precision). Without
    #     ::numeric the whole statement fails to parse (SQLSTATE 42883) and this
    #     tool returned tool_error on EVERY call, for every engine family.
    #   * ::double precision back on the outside keeps change_pct a NUMBER in the
    #     payload. The RDS Data API sends numeric/decimal as stringValue, so
    #     stopping at ::numeric would silently turn change_pct into "77.5" while
    #     every other change_pct in this repo is a float.
    sql = """
        WITH before_period AS (
            SELECT query_hash, query_text, AVG(mean_time_ms) as before_mean_ms,
                   MAX(calls) as before_calls
            FROM query_stats
            WHERE cluster_id = :cluster_id
              AND snapshot_time >= :change_point::timestamptz - (:hours_before || ' hours')::interval
              AND snapshot_time < :change_point::timestamptz
            GROUP BY query_hash, query_text
        ),
        after_period AS (
            SELECT query_hash, AVG(mean_time_ms) as after_mean_ms,
                   MAX(calls) as after_calls
            FROM query_stats
            WHERE cluster_id = :cluster_id
              AND snapshot_time >= :change_point::timestamptz
              AND snapshot_time < :change_point::timestamptz + (:hours_after || ' hours')::interval
            GROUP BY query_hash
        )
        SELECT b.query_hash, b.query_text, b.before_mean_ms, a.after_mean_ms,
               b.before_calls, a.after_calls,
               ROUND((((a.after_mean_ms - b.before_mean_ms) / NULLIF(b.before_mean_ms, 0)) * 100)::numeric, 1)::double precision as change_pct
        FROM before_period b JOIN after_period a ON b.query_hash = a.query_hash
        WHERE a.after_mean_ms > b.before_mean_ms * (1 + :min_change_pct / 100.0)
        ORDER BY change_pct DESC LIMIT 20
    """
    params = {
        "cluster_id": cluster_id,
        "change_point": change_point,
        "hours_before": hours_before,
        "hours_after": hours_after,
        "min_change_pct": min_change_pct,
    }
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "change_point": change_point,
        "regressions": result.rows,
        "count": result.row_count,
        "methodology": (
            "mean_time_ms는 각 스냅샷 시점까지의 누적 평균이므로 두 구간 평균의 비교는 "
            "구간 내 평균이 아니라 '그 시점까지의 생애 평균'끼리의 비교입니다. 최소 호출 "
            "수 필터가 없으니 change_pct를 인용하기 전에 before_calls/after_calls "
            "(누적 호출 수, 차이가 after 구간 실행 횟수)로 표본 크기를 확인하세요. "
            "before 구간에 행이 없는 쿼리는 JOIN에서 빠지므로 결과가 비어 있는 것이 "
            "리그레션이 없다는 뜻은 아닙니다."
        ),
    }
