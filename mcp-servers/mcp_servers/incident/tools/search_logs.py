import boto3
import time
from mcp_servers.shared.cache_client import CacheClient


def search_logs_impl(
    cache: CacheClient,
    cluster_id: str,
    query: str = "fields @timestamp, @message | sort @timestamp desc | limit 50",
    hours: int = 6,
    log_group: str = None,
) -> dict:
    if not log_group:
        log_group = f"/aws/rds/cluster/{cluster_id}/error"

    client = boto3.client("logs")
    start_response = client.start_query(
        logGroupName=log_group,
        startTime=int((time.time() - hours * 3600) * 1000),
        endTime=int(time.time() * 1000),
        queryString=f"/* source=dbops-agent */ {query}",
    )
    query_id = start_response["queryId"]

    for _ in range(30):
        result = client.get_query_results(queryId=query_id)
        if result["status"] == "Complete":
            rows = []
            for r in result.get("results", []):
                row = {f["field"]: f["value"] for f in r}
                rows.append(row)
            return {
                "cluster_id": cluster_id,
                "log_group": log_group,
                "results": rows,
                "count": len(rows),
            }
        time.sleep(1)

    return {"cluster_id": cluster_id, "error": "Query timed out"}
