import logging
import time

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

logger = logging.getLogger(__name__)

# log_group is an AGENT-SUPPLIED parameter, so it is the one input here that can
# point the Insights query anywhere. Restrict it to the DB log-group families
# DBOps is meant to read; anything else (Lambda, application, CloudTrail, another
# team's groups) is refused before the AWS call. The incident Lambda's IAM is
# scoped to the same three prefixes in cdk/stacks/agent_stack.py, so this is
# defense in depth rather than the only control, and it turns what would be an
# opaque AccessDenied into an explicit, actionable refusal.
#   /aws/rds/cluster/*  Aurora error/slowquery/general/audit
#   /aws/rds/instance/* standalone RDS MySQL / SQL Server
#   /aws/docdb/*        DocumentDB profiler + audit
ALLOWED_LOG_GROUP_PREFIXES = (
    "/aws/rds/cluster/",
    "/aws/rds/instance/",
    "/aws/docdb/",
)


def search_logs_impl(
    cache: CacheClient,
    cluster_id: str,
    query: str = "fields @timestamp, @message | sort @timestamp desc | limit 50",
    hours: int = 6,
    log_group: str = None,
) -> dict:
    if not log_group:
        log_group = f"/aws/rds/cluster/{cluster_id}/error"
    if not str(log_group).startswith(ALLOWED_LOG_GROUP_PREFIXES):
        logger.warning(
            "search_logs refused out-of-scope log group for %s: %r", cluster_id, log_group
        )
        return {
            "cluster_id": cluster_id,
            "log_group": log_group,
            "status": "log_group_not_allowed",
            "reason": (
                "DBOps는 데이터베이스 로그 그룹만 조회합니다. log_group은 "
                + ", ".join(ALLOWED_LOG_GROUP_PREFIXES)
                + " 중 하나로 시작해야 합니다."
            ),
            "results": [],
            "count": 0,
        }

    # Cross-account-aware: the RDS log group lives in the cluster's own account,
    # so target it via the spoke role when registered (local otherwise).
    client = client_for_cluster(cluster_id, "logs")
    try:
        start_response = client.start_query(
            logGroupName=log_group,
            startTime=int((time.time() - hours * 3600) * 1000),
            endTime=int(time.time() * 1000),
            # NO `/* source=dbops-agent */` prefix here. That marker is the audit
            # convention for SQL sent to a TARGET DATABASE; CloudWatch Logs
            # Insights is not SQL and rejects the comment outright with
            # MalformedQueryException, before it even resolves the log group. It
            # made every search_logs call fail for every cluster and engine
            # (found by live verification 2026-07-24, the failure was masked as a
            # generic tool error). Insights calls are attributable through
            # CloudTrail, so nothing is lost by dropping it.
            queryString=query,
        )
    except client.exceptions.ResourceNotFoundException:
        # A missing group almost always means log exports are simply not enabled
        # for this cluster, which is an operator action, not an internal error.
        logger.info("log group not found for %s: %s", cluster_id, log_group)
        return {
            "cluster_id": cluster_id,
            "log_group": log_group,
            "status": "log_group_not_found",
            "reason": (
                f"로그 그룹 {log_group}이 없습니다. 이 클러스터에서 해당 로그 "
                "내보내기가 켜져 있지 않거나(RDS/DocumentDB의 CloudWatch Logs "
                "export 설정), 아직 첫 레코드가 생기지 않았습니다."
            ),
            "results": [],
            "count": 0,
        }
    except client.exceptions.MalformedQueryException:
        # Do NOT log the query text. An incident-investigation query routinely
        # carries the value being hunted (`filter @message like /user@example.com/`,
        # an account id, a token seen in a log line), so echoing it would copy
        # that content into a second, longer-lived log group. The caller already
        # knows its own query and the response says what is wrong with it, so the
        # text adds nothing here: log only that it happened, plus the length,
        # which is enough to tell a truncated query from a wrong-dialect one.
        logger.warning(
            "malformed Insights query for %s (length %d)", cluster_id, len(str(query))
        )
        return {
            "cluster_id": cluster_id,
            "log_group": log_group,
            "status": "malformed_query",
            "reason": (
                "CloudWatch Logs Insights 문법 오류입니다. Insights는 SQL이 아니며 "
                "`fields ... | filter ... | sort ... | limit N` 형태의 파이프 "
                "구문을 씁니다(SQL 주석 /* */ 사용 불가)."
            ),
            "results": [],
            "count": 0,
        }
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
