"""managed_tag_preflight — tell the DBA at REVIEW time that a cross-account write
is likely to be denied, so the approval is never spent on it.

WHAT IT IS FOR
--------------
`cdk/cross-account/spoke-role-template.yaml` (Sid RDSModifyExisting) authorizes 15
write actions only when the target carries `ManagedBy=dbops`. Two of them,
ModifyDBClusterParameterGroup and ModifyDBParameterGroup, authorize against the
PARAMETER GROUP rather than the cluster or the instance. So a spoke-account group
without that tag is denied with AccessDenied, and that answer arrives only AFTER
verify_approval has consumed the DBA's single-use approval: the approval is burned
and the change does not happen. Measured on the standing fixture: the group
dbops-demo-mysql84 carries `dbops-demo=true` and `Application=DBOps`, NOT
ManagedBy.

IT IS A WARNING, NOT A REFUSAL, AND THAT IS THE WHOLE DESIGN
-----------------------------------------------------------
Refusing would assert what the SPOKE ROLE's policy says, and this code does not
read that policy. The template is a TEMPLATE customers adapt: a deployment that
dropped the tag condition would have its perfectly valid writes blocked by a
refusal here, and blocking a capability that works is worse than the wasted
approval this module exists to prevent. Reporting the tag state is a FACT; predicting
the denial is not.

So it surfaces in the approval_required PREVIEW, where the cost of acting on it is
zero: the DBA reads it before approving and does not spend an approval on a card
that cannot execute. Nothing on the execute path refuses on it.

LIMIT, stated rather than papered over: the warning reaches the DBA only if the
agent relays the preview response (it is in the tool output, so it is in the
transcript, and the write tools put it in their approval_required payload). It is
not a gate, and it is not trying to be one. The IAM condition remains the
enforcement point, and the modify_failed reason still names the parameter-group tag
for the case where the write is attempted anyway.

Every uncertainty produces NO warning: a same-account cluster (where no such
condition exists), a failed describe, a failed list-tags. A warning here means the
tag was READ and is absent or wrong.
"""

import logging

from mcp_servers.shared.cluster_targets import lookup_cluster

logger = logging.getLogger(__name__)

MANAGED_BY_KEY = "ManagedBy"
MANAGED_BY_VALUE = "dbops"


def is_cross_account(cluster_id: str) -> bool:
    """True when writes to this cluster go through a spoke role, i.e. when the
    ResourceTag condition in the spoke template can apply at all. The registry's
    `spoke_role_arn` is the same signal cluster_targets uses to decide whether to
    assume a role, so this cannot disagree with what the write actually does."""
    return bool((lookup_cluster(cluster_id) or {}).get("spoke_role_arn"))


def cluster_parameter_group_tag_warning(rds, cluster_id: str, group_name: str) -> str:
    """"" unless the Aurora CLUSTER parameter group was read and lacks the tag."""
    return _group_tag_warning(
        rds, cluster_id, group_name,
        describe="describe_db_cluster_parameter_groups",
        list_key="DBClusterParameterGroups",
        name_kwarg="DBClusterParameterGroupName",
        arn_key="DBClusterParameterGroupArn",
        label="클러스터 파라미터 그룹",
        action="rds:ModifyDBClusterParameterGroup",
    )


def instance_parameter_group_tag_warning(rds, cluster_id: str, group_name: str) -> str:
    """"" unless the DB INSTANCE parameter group was read and lacks the tag."""
    return _group_tag_warning(
        rds, cluster_id, group_name,
        describe="describe_db_parameter_groups",
        list_key="DBParameterGroups",
        name_kwarg="DBParameterGroupName",
        arn_key="DBParameterGroupArn",
        label="DB 파라미터 그룹",
        action="rds:ModifyDBParameterGroup",
    )


def aurora_cluster_tag_warning(rds, cluster_id, *, action) -> str:
    """"" unless the Aurora/DocumentDB CLUSTER lacks the tag. Gated actions:
    rds:ModifyDBCluster (also the DocumentDB write path, which uses the rds
    namespace)."""
    return _resolved_tag_warning(
        rds, "describe_db_clusters", {"DBClusterIdentifier": cluster_id},
        "DBClusters", "DBClusterArn", "list_tags_for_resource",
        cluster_id=cluster_id, label="클러스터", action=action, what=cluster_id)


def rds_instance_tag_warning(rds, cluster_id, instance_id, *, action) -> str:
    """"" unless the DB INSTANCE lacks the tag. rds:DeleteDBInstance authorizes
    against the instance, NOT the cluster it belongs to."""
    if not instance_id:
        return ""
    return _resolved_tag_warning(
        rds, "describe_db_instances", {"DBInstanceIdentifier": instance_id},
        "DBInstances", "DBInstanceArn", "list_tags_for_resource",
        cluster_id=cluster_id, label="DB 인스턴스", action=action, what=instance_id)


def cluster_endpoint_tag_warning(rds, cluster_id, endpoint_id, *, action) -> str:
    """"" unless the CUSTOM ENDPOINT lacks the tag. rds:ModifyDBClusterEndpoint
    and rds:DeleteDBClusterEndpoint authorize against the endpoint itself."""
    if not endpoint_id:
        return ""
    return _resolved_tag_warning(
        rds, "describe_db_cluster_endpoints",
        {"DBClusterEndpointIdentifier": endpoint_id},
        "DBClusterEndpoints", "DBClusterEndpointArn", "list_tags_for_resource",
        cluster_id=cluster_id, label="커스텀 엔드포인트", action=action,
        what=endpoint_id)


def elasticache_group_tag_warning(ec, cluster_id, group_id, *, action) -> str:
    """"" unless the REPLICATION GROUP lacks the tag. Gated actions:
    elasticache:ModifyReplicationGroup, elasticache:TestFailover."""
    if not group_id:
        return ""
    return _resolved_tag_warning(
        ec, "describe_replication_groups", {"ReplicationGroupId": group_id},
        "ReplicationGroups", "ARN", "list_tags_for_resource",
        cluster_id=cluster_id, label="replication group", action=action,
        what=group_id)


def elasticache_cache_cluster_tag_warning(ec, cluster_id, cache_cluster_id, *,
                                          action) -> str:
    """"" unless the CACHE CLUSTER lacks the tag.

    elasticache:RebootCacheCluster authorizes against the individual cache
    cluster (the node), NOT the replication group that contains it, so reading
    the group's tags here would answer a different question than the one IAM
    asks.
    """
    if not cache_cluster_id:
        return ""
    return _resolved_tag_warning(
        ec, "describe_cache_clusters", {"CacheClusterId": cache_cluster_id},
        "CacheClusters", "ARN", "list_tags_for_resource",
        cluster_id=cluster_id, label="캐시 클러스터", action=action,
        what=cache_cluster_id)


def dynamodb_table_tag_warning(ddb, cluster_id, table, *, action) -> str:
    """"" unless the TABLE lacks the tag. Gated actions: dynamodb:UpdateTable,
    dynamodb:UpdateContinuousBackups, dynamodb:UpdateTimeToLive.

    describe_table returns a single `Table` DICT (not a list), and the tag read is
    list_tags_of_resource(ResourceArn=...) returning `Tags`. Both differ from the
    rds/elasticache shape, which is why this lives here instead of at each call
    site.
    """
    if not table:
        return ""
    return _resolved_tag_warning(
        ddb, "describe_table", {"TableName": table},
        "Table", "TableArn", "list_tags_of_resource",
        cluster_id=cluster_id, label="DynamoDB 테이블", action=action,
        what=table, arn_kwarg="ResourceArn")


def _resolved_tag_warning(client, describe_name, describe_kwargs, container, arn_key,
                          list_tags_name, *, cluster_id, label, action, what,
                          arn_kwarg="ResourceName") -> str:
    """Resolve the GATED resource's ARN with one describe, then read its tags.

    The two API methods arrive as NAMES, not as bound methods, and are resolved
    with getattr INSIDE the try below. That is deliberate: passing
    `client.list_tags_for_resource` would evaluate the attribute at the call
    site, before the cross-account gate, so a client that lacks the method
    (a wrong-service client, a stub) would raise AttributeError out of a
    fail-open preflight. Resolving late keeps "every uncertainty produces no
    warning" true for the client itself, not just for the API responses.

    Why resolve instead of taking an ARN the caller already has: whether a tool
    holds the right ARN at PREVIEW time depends on its control flow, not on
    whether the file contains a describe. manage_maintenance is the example that
    proves it: its describe_db_clusters sits inside the `action == "describe"`
    branch, which returns before the modify branch is ever reached, so an
    in-hand ARN there would have been None. Resolving here removes that whole
    class of mistake, and the cost is one describe on a cold preview path.

    The cross-account gate runs FIRST so a same-account cluster (the common case)
    makes no extra call at all. Fail-open throughout.

    Three call sites (remove_reader_instance, prewarm_reader, request_approval)
    check is_cross_account THEMSELVES before this is reached, because their
    preview is documented and tested to resolve no AWS client, and the client has
    to be built before a resolver can be called. So on a cross-account preview
    from those three the registry is read twice: one extra DynamoDB GetItem on a
    cold, once-per-approval path, in exchange for keeping the gate at the point
    that actually decides whether to touch AWS. Note for tests: patching only ONE
    of the two `is_cross_account` references (the tool binds it by name at import;
    this module reads its own attribute) leaves the other consulting the real
    registry, which silently produces no warning.
    """
    if not is_cross_account(cluster_id):
        return ""
    try:
        item = getattr(client, describe_name)(**describe_kwargs).get(container)
        # rds/elasticache describes return a LIST; dynamodb's describe_table
        # returns a single dict under "Table".
        if isinstance(item, list):
            item = item[0] if item else None
        arn = item.get(arn_key) or "" if isinstance(item, dict) else ""
        list_tags = getattr(client, list_tags_name)
    except Exception:
        logger.warning("ManagedBy tag lookup unavailable for %s (%s)",
                       cluster_id, what, exc_info=True)
        return ""
    return _warn_if_untagged(list_tags, arn, cluster_id=cluster_id, label=label,
                             action=action, arn_kwarg=arn_kwarg)


def resource_tag_warning(list_tags, arn, cluster_id, *, label, action,
                         arn_kwarg="ResourceName") -> str:
    """"" unless `arn` was read and provably lacks ManagedBy=dbops.

    For the write actions whose target ARN is already in a describe response the
    tool makes anyway, so this costs ONE extra list-tags call and no extra
    describe. `list_tags` is the bound API, and `arn_kwarg` is its parameter name,
    which is NOT uniform across services:

      rds          rds.list_tags_for_resource(ResourceName=arn)
      elasticache  ec.list_tags_for_resource(ResourceName=arn)
      dynamodb     ddb.list_tags_of_resource(ResourceArn=arn)   <- different verb
                                                                   AND kwarg

    Same fail-open contract as everything else here: no ARN, a same-account
    cluster, or a failed call produces NO warning.
    """
    if not is_cross_account(cluster_id):
        return ""
    return _warn_if_untagged(list_tags, arn, cluster_id=cluster_id, label=label,
                             action=action, arn_kwarg=arn_kwarg)


def _warn_if_untagged(list_tags, arn, *, cluster_id, label, action,
                      arn_kwarg="ResourceName") -> str:
    """The tag read itself, with NO cross-account gate: every caller has already
    gated (the resolvers gate before spending a describe). Split out so a
    resolved-ARN path costs one registry lookup instead of two."""
    if not arn:
        return ""
    try:
        resp = list_tags(**{arn_kwarg: arn})
    except Exception:
        logger.warning("ManagedBy tag lookup unavailable for %s (%s)",
                       cluster_id, arn, exc_info=True)
        return ""
    # rds/elasticache return TagList; dynamodb returns Tags.
    tags = resp.get("TagList")
    if tags is None:
        tags = resp.get("Tags") or []
    if tag_is_present(tags):
        return ""
    return _warning_text(label, arn.rsplit(":", 1)[-1] or arn, action)


def _warning_text(label, name, action) -> str:
    return (
        f"주의: 이 클러스터는 다른 AWS 계정(spoke)에 있고, {label} "
        f"'{name}'에는 {MANAGED_BY_KEY}={MANAGED_BY_VALUE} 태그가 없습니다. "
        f"spoke role이 기본 템플릿을 그대로 쓴다면 {action} 권한이 이 태그를 요구하므로 "
        f"변경이 AccessDenied로 거부되고, 그 거부는 승인이 소모된 뒤에 발생합니다. "
        # The old wording said only "태그를 붙이거나", which reads as something the
        # operator can do through DBOps. They cannot: the spoke role's single
        # tag-write statement (RDSTagOnCreate) is key-restricted to dbops:created-by
        # and dbops:type, so ManagedBy is unreachable from this side BY DESIGN.
        # Measured 2026-08-03 by assuming the real spoke role and trying:
        # AccessDenied on rds:AddTagsToResource. Naming where the tag has to be
        # applied is the difference between an actionable warning and a dead end.
        f"태그는 DBOps에서 붙일 수 없습니다(spoke role의 태그 권한은 dbops: 접두 키로 "
        f"제한되어 있고, 이는 역할이 스스로 write 권한을 열지 못하게 하는 의도된 "
        f"제약입니다). 대상 계정에서 직접 태그를 붙이거나, spoke role 정책이 이 조건을 "
        f"쓰지 않는지 확인하세요."
    )


def _group_tag_warning(rds, cluster_id, group_name, *, describe, list_key,
                       name_kwarg, arn_key, label, action) -> str:
    if not group_name or not is_cross_account(cluster_id):
        return ""
    try:
        groups = getattr(rds, describe)(**{name_kwarg: group_name}).get(list_key) or []
        arn = (groups[0].get(arn_key) or "") if groups else ""
        if not arn:
            # The group exists (the caller just read its parameters) but its ARN is
            # not in hand, so the tag cannot be read. Claim nothing.
            return ""
        tags = rds.list_tags_for_resource(ResourceName=arn).get("TagList") or []
    except Exception:
        # COULD NOT ASK. Never a warning: a warning asserts the tag is absent, and
        # a failed lookup does not know that.
        logger.warning("ManagedBy tag lookup unavailable for %s (%s=%s)",
                       cluster_id, name_kwarg, group_name, exc_info=True)
        return ""
    if tag_is_present(tags):
        return ""
    # The parameter-group wording says "the GROUP, not the cluster" on purpose:
    # that is the part operators get wrong, because the action reads as if it
    # authorizes against the cluster.
    return _warning_text(label + "(클러스터가 아니라 그룹 자체)", group_name, action)


def tag_is_present(tags) -> bool:
    """Does this TagList carry ManagedBy=dbops?

    The KEY is matched case-insensitively (IAM's `aws:ResourceTag/<key>` condition
    key is), the VALUE exactly (`StringEquals` on the value is case-sensitive, so a
    group tagged `ManagedBy=DBOps` is still denied). Matching the way IAM matches
    keeps this predicate's answer equal to the one that decides the write.
    """
    for t in tags or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("Key") or "").lower() == MANAGED_BY_KEY.lower():
            return str(t.get("Value") or "") == MANAGED_BY_VALUE
    return False
