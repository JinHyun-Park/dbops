"""simulate_parameter_change — simulate a parameter change from the cluster's
REAL parameter metadata.

Thin wrapper: this tool owns the cross-account AWS describe glue (resolve the
cluster's parameter group, paginate its parameters) and delegates all the
derivation (dynamic vs static, validation, recommendation) + the graceful
fallback to the shared :mod:`parameter_estimator`, which the REST mirror also
uses so the two can't drift.

The fallback `reason` becomes the response's ``data_source`` label, so it stays
a STATIC string: an RDS describe error carries the hub account id, the platform
role name and the parameter-group ARN, and must only reach the module logger.
"""

import logging

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster
from mcp_servers.shared.parameter_estimator import (
    build_live_result,
    describe_all_parameters,
    static_fallback,
)

logger = logging.getLogger(__name__)


def _cluster_engine(cache: CacheClient, cluster_id: str) -> str:
    """The engine string from cluster_meta, or "" when unreadable.

    Read from the CACHE rather than from the live describe below, because the
    fallback is reached precisely when that describe failed, and the engine is what
    decides whether a catalog entry applies at all.
    """
    try:
        res = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception:
        return ""
    rows = getattr(res, "rows", res)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("engine") or "")
    return ""


def simulate_parameter_change_impl(cache: CacheClient, cluster_id: str, parameter_name: str, new_value: str) -> dict:
    # The engine decides whether a fallback catalog entry applies: the catalog holds
    # both PG and MySQL parameters, and `work_mem` on Aurora MySQL was being reported
    # as known and safe to apply immediately.
    engine = _cluster_engine(cache, cluster_id)
    # Resolve the cluster's parameter group via the cross-account-aware client.
    # Any failure (assume-role denied, unregistered, RDS unreachable) degrades to
    # the static fallback rather than surfacing an exception to the agent.
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception:
        logger.warning("describe_db_clusters failed for %s", cluster_id, exc_info=True)
        return static_fallback(cluster_id, parameter_name, new_value, "live describe unavailable", engine)

    pg_name = cluster.get("DBClusterParameterGroup") or ""
    if not pg_name:
        return static_fallback(cluster_id, parameter_name, new_value, "no parameter group on cluster", engine)
    # AWS-managed default.* groups can't be modified and their values aren't ours
    # to read meaningfully — treat like the live path is unavailable.
    if pg_name.startswith("default."):
        return static_fallback(cluster_id, parameter_name, new_value, "AWS-default parameter group", engine)

    try:
        params = describe_all_parameters(rds, pg_name)
    except Exception:
        logger.warning(
            "describe_all_parameters failed for %s (group=%s)",
            cluster_id, pg_name, exc_info=True,
        )
        return static_fallback(cluster_id, parameter_name, new_value, "live describe unavailable", engine)

    row = next((p for p in params if p.get("ParameterName") == parameter_name), None)
    if row is None:
        return static_fallback(cluster_id, parameter_name, new_value, "parameter not found in group", engine)

    return build_live_result(cluster_id, parameter_name, new_value, row, pg_name)
