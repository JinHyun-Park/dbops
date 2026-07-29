"""The cross-account ManagedBy tag, reported at review time.

The spoke template gates rds:ModifyDBClusterParameterGroup and
rds:ModifyDBParameterGroup on `aws:ResourceTag/ManagedBy=dbops`, and BOTH authorize
against the PARAMETER GROUP rather than the cluster or the instance. An untagged
spoke-account group is therefore denied with AccessDenied AFTER verify_approval has
consumed the DBA's single-use approval.

The property under test is that this is a WARNING and never a refusal. That is not
timidity: this code does not read the spoke role's policy, and that template is one
customers adapt, so an untagged group is not reliably a denial. A refusal would
block writes that work for a deployment that dropped the condition, and blocking a
working capability is worse than the wasted approval it would save. So every
uncertainty produces NO warning, and nothing on the execute path refuses.

Grounding: measured on the standing fixture, the group dbops-demo-mysql84 carries
`dbops-demo=true` and `Application=DBOps`, NOT ManagedBy, which is the untagged
shape used below.
"""

from unittest.mock import MagicMock, patch

import pytest
from mcp_servers.operations.tools import modify_parameter as mp
from mcp_servers.operations.tools import modify_rds_instance_params as mip
from mcp_servers.shared import managed_tag_preflight as mtp

_GRP = "grp-a"
_PARAM = {"ParameterName": "work_mem", "ApplyType": "dynamic", "IsModifiable": True}
_TAGGED = [{"Key": "ManagedBy", "Value": "dbops"}]
# The real fixture's tags: neither of these is ManagedBy.
_UNTAGGED = [{"Key": "dbops-demo", "Value": "true"},
             {"Key": "Application", "Value": "DBOps"}]
_SPOKE_ROW = {"spoke_role_arn": "arn:aws:iam::999999999999:role/dbops-spoke"}


def _aurora_rds(tags, arn="arn:aws:rds:ap-northeast-2:1:cluster-pg:grp-a",
                describe_raises=False):
    r = MagicMock()
    r.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": _GRP}]}
    r.describe_db_cluster_parameters.return_value = {"Parameters": [_PARAM]}
    if describe_raises:
        r.describe_db_cluster_parameter_groups.side_effect = Exception("Throttling")
    else:
        r.describe_db_cluster_parameter_groups.return_value = {
            "DBClusterParameterGroups": [{"DBClusterParameterGroupArn": arn}]}
    r.list_tags_for_resource.return_value = {"TagList": tags}
    return r


def _instance_rds(tags, arn="arn:aws:rds:ap-northeast-2:1:pg:grp-a"):
    r = MagicMock()
    r.describe_db_instances.return_value = {"DBInstances": [{
        "DBInstanceStatus": "available",
        "DBParameterGroups": [{"DBParameterGroupName": _GRP}]}]}
    r.describe_db_parameters.return_value = {"Parameters": [_PARAM]}
    r.describe_db_parameter_groups.return_value = {
        "DBParameterGroups": [{"DBParameterGroupArn": arn}]}
    r.list_tags_for_resource.return_value = {"TagList": tags}
    return r


def _aurora_preview(row, tags, **kw):
    with patch.object(mtp, "lookup_cluster", return_value=row), \
         patch.object(mp, "rds_client_for_cluster",
                      return_value=_aurora_rds(tags, **kw)):
        return mp.modify_parameter_impl(MagicMock(), "clu-1", "work_mem", "8MB")


def _instance_preview(row, tags, **kw):
    with patch.object(mtp, "lookup_cluster", return_value=row), \
         patch.object(mip, "client_for_cluster",
                      return_value=_instance_rds(tags, **kw)):
        return mip.modify_rds_instance_params_impl(
            MagicMock(), "inst-1", "work_mem", "8MB")


def test_an_untagged_group_warns_on_both_tools():
    for preview, action in ((_aurora_preview, "rds:ModifyDBClusterParameterGroup"),
                            (_instance_preview, "rds:ModifyDBParameterGroup")):
        card = preview(_SPOKE_ROW, _UNTAGGED)
        assert card["status"] == "approval_required", card
        warning = card.get("warning") or ""
        assert warning, f"{action}: no warning on an untagged cross-account group"
        assert action in warning, warning
        assert _GRP in warning, warning
        # It must NOT read as a verdict: the spoke policy was never consulted.
        assert "그대로 쓴다면" in warning, "the warning must stay conditional"


def test_a_tagged_group_is_silent():
    for preview in (_aurora_preview, _instance_preview):
        card = preview(_SPOKE_ROW, _TAGGED)
        assert card["status"] == "approval_required"
        assert not card.get("warning"), card


def test_a_same_account_cluster_is_never_warned_about():
    """No spoke role means no ResourceTag condition exists, so an untagged group is
    not a problem at all. Warning here would be noise on every single-account
    deployment, which is most of them."""
    for preview in (_aurora_preview, _instance_preview):
        card = preview({}, _UNTAGGED)
        assert not card.get("warning"), card


def test_a_failed_lookup_claims_nothing():
    """A warning asserts the tag is absent. A describe that raised, or a response
    with no ARN, does not know that."""
    assert not _aurora_preview(_SPOKE_ROW, _UNTAGGED, describe_raises=True).get("warning")
    assert not _aurora_preview(_SPOKE_ROW, _UNTAGGED, arn="").get("warning")


def test_the_value_is_case_sensitive_and_the_key_is_not():
    """Matched the way IAM matches, so this predicate's answer equals the one that
    decides the write: `aws:ResourceTag/<key>` is case-insensitive on the KEY, while
    StringEquals on the VALUE is case-sensitive. A group tagged ManagedBy=DBOps IS
    denied, so it must still warn."""
    assert mtp.tag_is_present([{"Key": "MANAGEDBY", "Value": "dbops"}]) is True
    assert mtp.tag_is_present([{"Key": "managedby", "Value": "dbops"}]) is True
    assert mtp.tag_is_present([{"Key": "ManagedBy", "Value": "DBOps"}]) is False
    assert mtp.tag_is_present([{"Key": "ManagedBy", "Value": ""}]) is False
    assert mtp.tag_is_present([]) is False
    assert mtp.tag_is_present(None) is False
    # A TagList carrying junk entries must not raise.
    assert mtp.tag_is_present(["nonsense", None, {"Key": "ManagedBy", "Value": "dbops"}]) is True


def test_the_execute_path_never_refuses_on_the_tag():
    """The whole design: an untagged group must still reach the write, because this
    module does not know the spoke policy requires the tag. If this ever starts
    refusing, a deployment that dropped the condition loses parameter tuning."""
    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(mp, "rds_client_for_cluster",
                      return_value=_aurora_rds(_UNTAGGED)), \
         patch.object(mp, "verify_approval", return_value={"ok": True}):
        aurora = mp.modify_parameter_impl(
            MagicMock(), "clu-1", "work_mem", "8MB", parameter_group=_GRP,
            approved=True, approval_id="a1")
    assert aurora["status"] == "modified", aurora

    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(mip, "client_for_cluster",
                      return_value=_instance_rds(_UNTAGGED)), \
         patch.object(mip, "verify_approval", return_value={"ok": True}):
        inst = mip.modify_rds_instance_params_impl(
            MagicMock(), "inst-1", "work_mem", "8MB", parameter_group=_GRP,
            approved=True, approval_id="a1")
    assert inst["status"] == "modified", inst


def test_the_tag_lookup_does_not_run_on_the_execute_path_at_all():
    """It is a review-time report, so the execute leg should not pay for it either.
    Asserted on the CALLS, because "it does not refuse" would also hold if it ran
    and happened to pass."""
    rds = _aurora_rds(_UNTAGGED)
    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(mp, "rds_client_for_cluster", return_value=rds), \
         patch.object(mp, "verify_approval", return_value={"ok": True}):
        mp.modify_parameter_impl(MagicMock(), "clu-1", "work_mem", "8MB",
                                 parameter_group=_GRP, approved=True,
                                 approval_id="a1")
    rds.list_tags_for_resource.assert_not_called()


# ---------------------------------------------------------------------------
# The generic resource form: actions whose target ARN is already in a describe
# the tool makes anyway, so the tag costs one list-tags call and no extra
# describe. Wired for the two rds_instance writes; the remaining tag-gated
# actions use the same helper (see BACKLOG).
# ---------------------------------------------------------------------------

from mcp_servers.operations.tools import modify_rds_instance_class as mic  # noqa: E402
from mcp_servers.operations.tools import reboot_rds_instance as reb  # noqa: E402

_INST_ARN = "arn:aws:rds:ap-northeast-2:999999999999:db:inst-1"


def _instance_client(tags, cls="db.t4g.micro"):
    r = MagicMock()
    r.describe_db_instances.return_value = {"DBInstances": [{
        "DBInstanceStatus": "available",
        "DBInstanceClass": cls,
        "DBInstanceArn": _INST_ARN,
    }]}
    r.list_tags_for_resource.return_value = {"TagList": tags}
    return r


_INSTANCE_TOOLS = (
    ("reboot", reb, reb.reboot_rds_instance_impl, {}, "rds:RebootDBInstance"),
    ("modify_class", mic, mic.modify_rds_instance_class_impl,
     {"target_class": "db.t4g.small"}, "rds:ModifyDBInstance"),
)


@pytest.mark.parametrize("name,mod,impl,kwargs,action", _INSTANCE_TOOLS)
def test_an_untagged_cross_account_instance_warns(name, mod, impl, kwargs, action):
    client = _instance_client(_UNTAGGED)
    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(mod, "client_for_cluster", return_value=client):
        card = impl(MagicMock(), "inst-1", **kwargs)
    assert card["status"] == "approval_required", (name, card)
    warning = card.get("warning") or ""
    assert warning, f"{name}: no warning on an untagged cross-account instance"
    assert action in warning, warning
    assert "그대로 쓴다면" in warning, "the warning must stay conditional"


@pytest.mark.parametrize("name,mod,impl,kwargs,action", _INSTANCE_TOOLS)
def test_a_tagged_or_same_account_instance_is_silent(name, mod, impl, kwargs, action):
    for row, tags in ((_SPOKE_ROW, _TAGGED), ({}, _UNTAGGED)):
        client = _instance_client(tags)
        with patch.object(mtp, "lookup_cluster", return_value=row), \
             patch.object(mod, "client_for_cluster", return_value=client):
            card = impl(MagicMock(), "inst-1", **kwargs)
        assert not card.get("warning"), (name, row, card)


@pytest.mark.parametrize("name,mod,impl,kwargs,action", _INSTANCE_TOOLS)
def test_the_execute_path_neither_refuses_nor_pays_for_the_tag(name, mod, impl, kwargs, action):
    """It is a review-time report. Asserted on the CALL, because "it did not
    refuse" would also hold if it ran and happened to pass."""
    client = _instance_client(_UNTAGGED)
    extra = dict(kwargs)
    if "target_class" in extra:
        extra["current_class"] = "db.t4g.micro"
    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(mod, "client_for_cluster", return_value=client), \
         patch.object(mod, "verify_approval", return_value={"ok": True}):
        resp = impl(MagicMock(), "inst-1", approved=True, approval_id="a1", **extra)
    assert resp["status"] in ("rebooting", "modifying"), (name, resp)
    client.list_tags_for_resource.assert_not_called()


def test_a_failed_list_tags_on_the_resource_form_claims_nothing():
    client = _instance_client(_UNTAGGED)
    client.list_tags_for_resource.side_effect = Exception("Throttling")
    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(reb, "client_for_cluster", return_value=client):
        card = reb.reboot_rds_instance_impl(MagicMock(), "inst-1")
    assert not card.get("warning")


def test_a_missing_arn_claims_nothing():
    """Not every describe response carries the ARN in every API version; absence
    is not evidence the tag is missing."""
    client = _instance_client(_UNTAGGED)
    client.describe_db_instances.return_value = {"DBInstances": [
        {"DBInstanceStatus": "available", "DBInstanceClass": "db.t4g.micro"}]}
    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW), \
         patch.object(reb, "client_for_cluster", return_value=client):
        card = reb.reboot_rds_instance_impl(MagicMock(), "inst-1")
    assert not card.get("warning")
    client.list_tags_for_resource.assert_not_called()


def test_the_dynamodb_shape_is_handled_even_though_its_api_differs():
    """dynamodb uses list_tags_OF_resource(ResourceArn=...) and returns `Tags`,
    while rds/elasticache use list_tags_FOR_resource(ResourceName=...) returning
    `TagList`. Pinned so the next wiring cannot assume they are the same."""
    called = {}

    def ddb_list_tags(ResourceArn=None):
        called["arn"] = ResourceArn
        return {"Tags": [{"Key": "Application", "Value": "DBOps"}]}

    with patch.object(mtp, "lookup_cluster", return_value=_SPOKE_ROW):
        w = mtp.resource_tag_warning(
            ddb_list_tags, "arn:aws:dynamodb:ap-northeast-2:9:table/t1", "t1",
            label="DynamoDB 테이블", action="dynamodb:UpdateTable",
            arn_kwarg="ResourceArn")
    assert w, "the dynamodb Tags key was not read"
    assert called["arn"].endswith("table/t1")
