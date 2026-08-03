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


def table_name_for_cluster(cluster_id: str) -> str:
    """Real resource name (e.g. the DynamoDB table name) for a `ddb-*` registry
    slug. The slug is a one-way hash of account+region+name, so the real name is
    NOT recoverable from it — it lives on the registry row as `resource_name`
    (the SAME row client_for_cluster reads), NOT in the Aurora cluster_meta cache.
    Falls back to cluster_id so a direct, non-slug id still works."""
    return lookup_cluster(cluster_id).get("resource_name") or cluster_id


def rds_client_for_cluster(cluster_id: str):
    """RDS control-plane client targeting the cluster's account+region.

    Resolves `region` + `spoke_role_arn` from the clusters registry. If the
    cluster isn't registered (or the table is unset), falls back to a local
    client so legacy single-account deploys keep working."""
    return client_for_cluster(cluster_id, "rds")


# The note appended when a control-plane call fails against the built-in demo row.
# Kept here rather than duplicated per tool so the wording stays consistent.
DEMO_CLUSTER_NOTE = (
    "이 클러스터는 DBOps 내장 샘플(데모) 행입니다. 캐시에 미리 채워진 데이터로 "
    "대시보드와 분석 기능을 체험하기 위한 것이며, 실제 AWS 리소스가 없습니다. "
    "따라서 실 리소스를 조회·변경하는 작업은 이 클러스터에서 동작하지 않습니다. "
    "등록이 잘못된 것이 아니므로 다시 등록할 필요는 없습니다. 실제 동작을 보려면 "
    "/clusters에서 운영 클러스터를 등록하세요."
)


def demo_cluster_note(cluster_id: str) -> str:
    """``DEMO_CLUSTER_NOTE`` when `cluster_id` is the built-in sample row, else "".

    The registry has carried `is_demo` since the sample-seeding path was added, but
    only the frontend read it. Control-plane failures against the demo row therefore
    surfaced as "cluster_id를 확인하세요" and "register it via /clusters first"
    (measured 2026-08-02), which is the opposite of the truth: the row IS registered,
    and it deliberately has no live AWS resources. A first-run user following that
    advice re-seeds the sample and gets the same message again.
    """
    try:
        return DEMO_CLUSTER_NOTE if lookup_cluster(cluster_id).get("is_demo") else ""
    except Exception:  # pragma: no cover - lookup_cluster already swallows its own
        return ""
