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
    return (
        f"주의: 이 클러스터는 다른 AWS 계정(spoke)에 있고, {label} "
        f"'{group_name}'에는 {MANAGED_BY_KEY}={MANAGED_BY_VALUE} 태그가 없습니다. "
        f"spoke role이 기본 템플릿을 그대로 쓴다면 {action} 권한이 이 태그를 요구하므로 "
        f"변경이 AccessDenied로 거부되고, 그 거부는 승인이 소모된 뒤에 발생합니다. "
        f"승인하기 전에 그룹(클러스터가 아니라 그룹 자체)에 태그를 붙이거나, "
        f"spoke role 정책에서 이 조건을 쓰지 않는지 확인하세요."
    )


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
