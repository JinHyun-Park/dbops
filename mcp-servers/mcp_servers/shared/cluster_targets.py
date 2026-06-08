"""cluster_targets — resolve the right AWS account+region for a cluster's
control-plane (RDS) operations.

The agent write tools (`modify_parameter`, `modify_scaling`,
`manage_maintenance`, `create_snapshot`, `restore_cluster`) call RDS control
APIs by cluster IDENTIFIER. A plain `boto3.client("rds")` runs in the runtime
Lambda's own account+region, so for a fleet registered via hub-spoke role
chaining it would either fail (the cluster lives in a spoke account) or — worse
— hit a same-named cluster in the hub account/region.

This module centralizes target resolution the same way `api/clusters`
(`_session_for`) does for the REST path: look up the cluster's `region` and
`spoke_role_arn` in the DynamoDB clusters registry, assume the spoke role when
present, and return an RDS client scoped to that account+region. Clusters with
no `spoke_role_arn` (single-account deploys) transparently use a local session.
"""

import os
from datetime import datetime, timezone

import boto3

_CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE", "")


def lookup_cluster(cluster_id: str) -> dict:
    """Return the clusters-registry row for `cluster_id`, or {} if not found
    or the table isn't configured."""
    if not cluster_id or not _CLUSTERS_TABLE_NAME:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(_CLUSTERS_TABLE_NAME)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:  # pragma: no cover - defensive
        print(f"[cluster_targets] lookup failed for {cluster_id}: {e}")
        return {}


def session_for(region: str = "", role_arn: str = "") -> boto3.session.Session:
    """A boto3 Session for the target account+region. With `role_arn`, assume
    it (hub-spoke chaining) so every client spawned from the session runs as
    the spoke role. Mirrors api/clusters `_session_for`."""
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"dbops-mcp-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def client_for_cluster(cluster_id: str, service: str):
    """Any AWS client (rds, logs, cloudwatch, …) targeting the cluster's
    account+region. Resolves `region` + `spoke_role_arn` from the registry and
    assumes the spoke role when present; falls back to a local client for
    single-account deploys."""
    row = lookup_cluster(cluster_id)
    return session_for(row.get("region", ""), row.get("spoke_role_arn", "")).client(service)


def rds_client_for_cluster(cluster_id: str):
    """RDS control-plane client targeting the cluster's account+region.

    Resolves `region` + `spoke_role_arn` from the clusters registry. If the
    cluster isn't registered (or the table is unset), falls back to a local
    client so legacy single-account deploys keep working."""
    return client_for_cluster(cluster_id, "rds")
