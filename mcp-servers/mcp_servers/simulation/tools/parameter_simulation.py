"""simulate_parameter_change — simulate a parameter change from the cluster's
REAL parameter metadata.

Thin wrapper: this tool owns the cross-account AWS describe glue (resolve the
cluster's parameter group, paginate its parameters) and delegates all the
derivation (dynamic vs static, validation, recommendation) + the graceful
fallback to the shared :mod:`parameter_estimator`, which the REST mirror also
uses so the two can't drift.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster
from mcp_servers.shared.parameter_estimator import (
    build_live_result,
    describe_all_parameters,
    static_fallback,
)


def simulate_parameter_change_impl(cache: CacheClient, cluster_id: str, parameter_name: str, new_value: str) -> dict:
    # Resolve the cluster's parameter group via the cross-account-aware client.
    # Any failure (assume-role denied, unregistered, RDS unreachable) degrades to
    # the static fallback rather than surfacing an exception to the agent.
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception as e:
        return static_fallback(cluster_id, parameter_name, new_value, f"live describe unavailable: {e}")

    pg_name = cluster.get("DBClusterParameterGroup") or ""
    if not pg_name:
        return static_fallback(cluster_id, parameter_name, new_value, "no parameter group on cluster")
    # AWS-managed default.* groups can't be modified and their values aren't ours
    # to read meaningfully — treat like the live path is unavailable.
    if pg_name.startswith("default."):
        return static_fallback(cluster_id, parameter_name, new_value, "AWS-default parameter group")

    try:
        params = describe_all_parameters(rds, pg_name)
    except Exception as e:
        return static_fallback(cluster_id, parameter_name, new_value, f"live describe unavailable: {e}")

    row = next((p for p in params if p.get("ParameterName") == parameter_name), None)
    if row is None:
        return static_fallback(cluster_id, parameter_name, new_value, "parameter not found in group")

    return build_live_result(cluster_id, parameter_name, new_value, row, pg_name)
