import logging
import time

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster
from mcp_servers.shared.engine_family import RDS_INSTANCE, engine_family

logger = logging.getLogger(__name__)

# The DB log-group families DBOps reads. These are FAMILIES, not the allowlist:
# the check below binds the group to the requested cluster inside one of them.
# The incident Lambda's IAM is scoped to these same three prefixes
# (cdk/stacks/agent_stack.py), but a prefix grant cannot tell one team's cluster
# from another's inside it, which is exactly what the per-cluster binding adds.
#   /aws/rds/cluster/*  Aurora error/slowquery/general/audit
#   /aws/rds/instance/* standalone RDS MySQL / SQL Server
#   /aws/docdb/*        DocumentDB profiler + audit
ALLOWED_LOG_GROUP_PREFIXES = (
    "/aws/rds/cluster/",
    "/aws/rds/instance/",
    "/aws/docdb/",
)


def permitted_log_group_prefixes(cluster_id: str) -> tuple:
    """The log-group prefixes the requested cluster's own logs live under.

    log_group is an AGENT-SUPPLIED parameter, so it is the one input here that
    steers WHERE the Insights query reads. Bounding it to a FAMILY was not
    enough: agent/tool_gate.py's ClusterVisibilityGate inspects only
    args["cluster_id"], and the hub IAM grant covers the whole
    /aws/rds/cluster/* prefix, so a caller scoped to team A could pass a
    cluster_id it is allowed to see PLUS team B's log group and read another
    team's database logs. Both controls above the tool are cluster-blind, so the
    binding has to happen here: the group must belong to the cluster the caller
    was actually authorized for, the way api/dashboard/handler.py::_log_insights
    builds its group from cluster_id rather than accepting one.

    An unusable cluster_id yields ``()``, and ``startswith(())`` is always False,
    so an unresolvable cluster refuses every group instead of widening back to a
    family prefix.
    """
    cid = str(cluster_id or "").strip()
    if not cid or "/" in cid:
        return ()
    return tuple(f"{family}{cid}/" for family in ALLOWED_LOG_GROUP_PREFIXES)


def search_logs_impl(
    cache: CacheClient,
    cluster_id: str,
    query: str = "fields @timestamp, @message | sort @timestamp desc | limit 50",
    hours: int = 6,
    log_group: str = None,
) -> dict:
    if not log_group:
        # A standalone RDS DB instance publishes to /aws/rds/instance/<id>/...;
        # the Aurora /aws/rds/cluster/ path NEVER exists for it, so the default
        # used to guarantee a "log group not found" on every default call for
        # this family. engine_of() returns "" on any lookup failure and
        # engine_family("") is relational, so an unresolvable cluster keeps the
        # historical Aurora default rather than guessing the instance path.
        fam = engine_family(cache.engine_of(cluster_id))
        prefix = "/aws/rds/instance/" if fam == RDS_INSTANCE else "/aws/rds/cluster/"
        log_group = f"{prefix}{cluster_id}/error"
    permitted = permitted_log_group_prefixes(cluster_id)
    if not str(log_group).startswith(permitted):
        logger.warning(
            "search_logs refused out-of-scope log group for %s: %r", cluster_id, log_group
        )
        return {
            "cluster_id": cluster_id,
            "log_group": log_group,
            "status": "log_group_not_allowed",
            "reason": (
                "DBOps는 요청한 클러스터의 데이터베이스 로그 그룹만 조회합니다. log_group은 "
                + (", ".join(permitted) or "해당 클러스터의 DB 로그 그룹 경로")
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
