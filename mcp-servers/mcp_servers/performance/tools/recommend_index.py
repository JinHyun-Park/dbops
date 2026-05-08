from mcp_servers.shared.cache_client import CacheClient


def recommend_index_impl(cache: CacheClient, cluster_id: str, min_seq_scan_ratio: float = 0.5) -> dict:
    sql = """
        SELECT q.query_hash, q.query_text, q.total_time_ms, q.calls,
               COALESCE(i.idx_scan, 0) as index_scans,
               COALESCE(q.shared_blks_read, 0) as blocks_read
        FROM query_stats q
        LEFT JOIN index_usage i ON q.cluster_id = i.cluster_id
        WHERE q.cluster_id = :cluster_id
          AND q.snapshot_time > NOW() - INTERVAL '24 hours'
          AND q.shared_blks_read > q.shared_blks_hit * :ratio
        ORDER BY q.total_time_ms DESC
        LIMIT 10
    """
    params = {"cluster_id": cluster_id, "ratio": min_seq_scan_ratio}
    result = cache.execute(sql, params)

    recommendations = []
    for row in result.rows:
        recommendations.append({
            "query_hash": row.get("query_hash"),
            "query_text": row.get("query_text", "")[:200],
            "total_time_ms": row.get("total_time_ms"),
            "calls": row.get("calls"),
            "reason": "High sequential scan ratio — missing or inefficient index",
        })

    return {"cluster_id": cluster_id, "recommendations": recommendations, "count": len(recommendations)}
