
from mcp_servers.shared.cache_client import CacheClient

_SQL = """
SELECT check_type, severity, subject, value_str, threshold_str, recommendation, details
FROM cluster_health_findings
WHERE cluster_id = :cid
  AND snapshot_time = (
      SELECT MAX(snapshot_time)
      FROM cluster_health_findings
      WHERE cluster_id = :cid
  )
ORDER BY
    CASE severity
        WHEN 'critical' THEN 0
        WHEN 'warning'  THEN 1
        ELSE 2
    END,
    check_type
"""

_NON_RELATIONAL_ENGINES = {"dynamodb", "docdb", "documentdb"}


def get_maintenance_findings_impl(cache: CacheClient, cluster_id: str) -> dict:
    result = cache.execute(_SQL, {"cid": cluster_id})

    counts = {"critical": 0, "warning": 0, "info": 0}
    findings = []

    for row in result.rows:
        sev = row.get("severity", "info")
        if sev in counts:
            counts[sev] += 1
        else:
            counts["info"] += 1

        findings.append({
            "check_type": row.get("check_type"),
            "severity": sev,
            "subject": row.get("subject"),
            "value_str": row.get("value_str"),
            "threshold_str": row.get("threshold_str"),
            "recommendation": row.get("recommendation"),
        })

    return {
        "cluster_id": cluster_id,
        "findings": findings,
        "counts": counts,
    }
