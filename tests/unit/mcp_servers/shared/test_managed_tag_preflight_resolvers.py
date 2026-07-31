"""The ARN-resolving tag-preflight helpers, plus a coverage guard.

Two separate jobs here:

1. Pin each resolver's AWS API SHAPE. This is where the real bugs live, because
   the shapes are NOT uniform: describe_table returns a single dict under
   "Table" while every rds/elasticache describe returns a LIST, and dynamodb's
   tag read is list_tags_of_resource(ResourceArn=) -> "Tags" while rds and
   elasticache use list_tags_for_resource(ResourceName=) -> "TagList". A fake
   that accepts anything would pass regardless, so every fake here ASSERTS the
   kwarg it was called with and returns only its own service's key.

2. Assert that every tag-gated action in the spoke template has a wired tool.
   The count in a prose note drifted (a BACKLOG entry said "6 remaining" when
   the template gates 15 actions across three statements); this test reads the
   template so the number cannot be wrong again.
"""

import json
import pathlib
import re

import pytest
import yaml
from mcp_servers.shared import managed_tag_preflight as mtp

TAGGED = [{"Key": "ManagedBy", "Value": "dbops"}]
UNTAGGED = [{"Key": "Application", "Value": "DBOps"}]


@pytest.fixture(autouse=True)
def _cross_account(monkeypatch):
    """Default every test to a cross-account cluster; the gate itself is tested
    explicitly below."""
    monkeypatch.setattr(mtp, "is_cross_account", lambda cid: True)


class _RdsLike:
    """rds/elasticache shape: describes return a LIST, tags come back as TagList
    from list_tags_for_resource(ResourceName=...)."""

    def __init__(self, container, arn_key, arn, tags, describe_name):
        self._c, self._k, self._arn, self._tags = container, arn_key, arn, tags
        self.describe_calls, self.tag_calls = [], []
        setattr(self, describe_name, self._describe)

    def _describe(self, **kw):
        self.describe_calls.append(kw)
        return {self._c: [{self._k: self._arn}]}

    def list_tags_for_resource(self, **kw):
        assert set(kw) == {"ResourceName"}, f"rds/elasticache kwarg wrong: {kw}"
        self.tag_calls.append(kw)
        return {"TagList": self._tags}


class _Ddb:
    """dynamodb shape: describe_table returns a single DICT, and the tag read is
    a different verb AND a different kwarg, returning "Tags"."""

    def __init__(self, arn, tags):
        self._arn, self._tags = arn, tags
        self.tag_calls = []

    def describe_table(self, **kw):
        assert set(kw) == {"TableName"}, kw
        return {"Table": {"TableArn": self._arn}}

    def list_tags_of_resource(self, **kw):
        assert set(kw) == {"ResourceArn"}, f"dynamodb needs ResourceArn: {kw}"
        self.tag_calls.append(kw)
        return {"Tags": self._tags}


# --------------------------------------------------------------------------
# per-resolver: the right describe, the right ARN, the right tag call
# --------------------------------------------------------------------------

def test_aurora_cluster_resolves_cluster_arn_and_warns_when_untagged():
    rds = _RdsLike("DBClusters", "DBClusterArn", "arn:aws:rds:::cluster:c1",
                   UNTAGGED, "describe_db_clusters")
    w = mtp.aurora_cluster_tag_warning(rds, "c1", action="rds:ModifyDBCluster")
    assert "rds:ModifyDBCluster" in w and "ManagedBy=dbops" in w
    assert rds.describe_calls == [{"DBClusterIdentifier": "c1"}]
    assert rds.tag_calls == [{"ResourceName": "arn:aws:rds:::cluster:c1"}]


def test_aurora_cluster_silent_when_tagged():
    rds = _RdsLike("DBClusters", "DBClusterArn", "arn:x", TAGGED,
                   "describe_db_clusters")
    assert mtp.aurora_cluster_tag_warning(rds, "c1", action="a") == ""


def test_rds_instance_describes_the_INSTANCE_not_the_cluster():
    """DeleteDBInstance authorizes against the instance, so reading the cluster's
    tags would answer a different question than IAM asks."""
    rds = _RdsLike("DBInstances", "DBInstanceArn", "arn:aws:rds:::db:i1",
                   UNTAGGED, "describe_db_instances")
    w = mtp.rds_instance_tag_warning(rds, "c1", "i1", action="rds:DeleteDBInstance")
    assert "DB 인스턴스" in w
    assert rds.describe_calls == [{"DBInstanceIdentifier": "i1"}]


def test_cluster_endpoint_describes_the_ENDPOINT():
    rds = _RdsLike("DBClusterEndpoints", "DBClusterEndpointArn", "arn:aws:rds:::cluster-endpoint:e1",
                   UNTAGGED, "describe_db_cluster_endpoints")
    w = mtp.cluster_endpoint_tag_warning(rds, "c1", "e1",
                                        action="rds:ModifyDBClusterEndpoint")
    assert "커스텀 엔드포인트" in w
    assert rds.describe_calls == [{"DBClusterEndpointIdentifier": "e1"}]


def test_elasticache_group_uses_the_bare_ARN_key():
    """ElastiCache calls the field "ARN", not "ReplicationGroupArn"."""
    ec = _RdsLike("ReplicationGroups", "ARN", "arn:aws:elasticache:::replicationgroup:g1",
                  UNTAGGED, "describe_replication_groups")
    w = mtp.elasticache_group_tag_warning(ec, "c1", "g1",
                                          action="elasticache:TestFailover")
    assert "replication group" in w
    assert ec.describe_calls == [{"ReplicationGroupId": "g1"}]


def test_elasticache_cache_cluster_is_a_DIFFERENT_resource_from_the_group():
    """RebootCacheCluster is gated on the cache cluster (the node)."""
    ec = _RdsLike("CacheClusters", "ARN", "arn:aws:elasticache:::cluster:n1",
                  UNTAGGED, "describe_cache_clusters")
    w = mtp.elasticache_cache_cluster_tag_warning(
        ec, "c1", "n1", action="elasticache:RebootCacheCluster")
    assert "캐시 클러스터" in w
    assert ec.describe_calls == [{"CacheClusterId": "n1"}]


def test_dynamodb_table_handles_the_dict_container_and_ResourceArn_kwarg():
    ddb = _Ddb("arn:aws:dynamodb:::table/t1", UNTAGGED)
    w = mtp.dynamodb_table_tag_warning(ddb, "c1", "t1",
                                       action="dynamodb:UpdateTable")
    assert "DynamoDB 테이블" in w
    # the fake's asserts already pinned the kwargs; confirm the call happened
    assert ddb.tag_calls == [{"ResourceArn": "arn:aws:dynamodb:::table/t1"}]


def test_dynamodb_table_silent_when_tagged():
    ddb = _Ddb("arn:x", TAGGED)
    assert mtp.dynamodb_table_tag_warning(ddb, "c1", "t1", action="a") == ""


# --------------------------------------------------------------------------
# fail-open: every uncertainty produces NO warning
# --------------------------------------------------------------------------

def test_same_account_makes_no_api_call_at_all(monkeypatch):
    """The gate runs BEFORE the describe, so a same-account cluster (the common
    case) costs nothing."""
    monkeypatch.setattr(mtp, "is_cross_account", lambda cid: False)

    class Boom:
        def describe_db_clusters(self, **kw):
            raise AssertionError("must not describe for a same-account cluster")

    assert mtp.aurora_cluster_tag_warning(Boom(), "c1", action="a") == ""


@pytest.mark.parametrize("resolver,args", [
    (lambda c: mtp.aurora_cluster_tag_warning(c, "c1", action="a"), None),
    (lambda c: mtp.rds_instance_tag_warning(c, "c1", "i1", action="a"), None),
    (lambda c: mtp.cluster_endpoint_tag_warning(c, "c1", "e1", action="a"), None),
    (lambda c: mtp.elasticache_group_tag_warning(c, "c1", "g1", action="a"), None),
    (lambda c: mtp.elasticache_cache_cluster_tag_warning(c, "c1", "n1", action="a"), None),
    (lambda c: mtp.dynamodb_table_tag_warning(c, "c1", "t1", action="a"), None),
])
def test_failed_describe_is_never_a_warning(resolver, args):
    """A warning ASSERTS the tag is absent. A failed lookup does not know that."""
    class Failing:
        def __getattr__(self, _):
            def boom(**kw):
                raise RuntimeError("AccessDenied on the describe itself")
            return boom

    assert resolver(Failing()) == ""


def test_empty_describe_result_is_never_a_warning():
    rds = _RdsLike("DBClusters", "DBClusterArn", "", UNTAGGED, "describe_db_clusters")
    rds._describe = lambda **kw: {"DBClusters": []}
    rds.describe_db_clusters = rds._describe
    assert mtp.aurora_cluster_tag_warning(rds, "c1", action="a") == ""


@pytest.mark.parametrize("resolver", [
    lambda blank: mtp.rds_instance_tag_warning(object(), "c1", blank, action="a"),
    lambda blank: mtp.cluster_endpoint_tag_warning(object(), "c1", blank, action="a"),
    lambda blank: mtp.elasticache_group_tag_warning(object(), "c1", blank, action="a"),
    lambda blank: mtp.elasticache_cache_cluster_tag_warning(object(), "c1", blank, action="a"),
    lambda blank: mtp.dynamodb_table_tag_warning(object(), "c1", blank, action="a"),
])
@pytest.mark.parametrize("blank", ["", None])
def test_missing_identifier_short_circuits_before_touching_the_client(resolver, blank):
    """`object()` has no describe method at all, so reaching AWS would raise
    AttributeError rather than return "". This pins the early return."""
    assert resolver(blank) == ""


# --------------------------------------------------------------------------
# coverage guard: the template is the source of truth for the action count
# --------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).resolve().parents[4]
_TEMPLATE = _REPO / "cdk" / "cross-account" / "spoke-role-template.yaml"
_TOOLS = _REPO / "mcp-servers" / "mcp_servers" / "operations" / "tools"


class _IntrinsicTolerantLoader(yaml.SafeLoader):
    """The template uses CloudFormation intrinsics (!Equals, !Sub, !Ref) that
    yaml.SafeLoader rejects outright. Rendering each one as a placeholder string
    is enough: this test only reads Action lists and Condition keys."""


_IntrinsicTolerantLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: f"<!{suffix}>")


def _tag_gated_actions():
    """Every action inside a policy statement whose Condition mentions
    aws:ResourceTag.

    Parsed structurally, not with a regex over the raw text. A first attempt
    split the file on `- Sid:` and kept blocks containing "ResourceTag", which
    silently pulled in rds:AddTagsToResource from the neighbouring
    UNCONDITIONED RDSTagOnCreate statement: a false "gated" action that would
    have demanded a preflight for a write that needs none.
    """
    doc = yaml.load(_TEMPLATE.read_text(), Loader=_IntrinsicTolerantLoader)

    def statements(node):
        if isinstance(node, dict):
            if isinstance(node.get("Statement"), list):
                yield from node["Statement"]
            for v in node.values():
                yield from statements(v)
        elif isinstance(node, list):
            for v in node:
                yield from statements(v)

    actions = set()
    for s in statements(doc):
        if "ResourceTag" not in json.dumps(s.get("Condition") or {}):
            continue
        acts = s.get("Action") or []
        actions |= set([acts] if isinstance(acts, str) else acts)
    return actions


def test_template_still_gates_the_actions_this_module_exists_for():
    actions = _tag_gated_actions()
    assert len(actions) == 15, f"tag-gated action count moved: {sorted(actions)}"


def test_every_tag_gated_action_has_a_wired_preflight():
    """Each gated action must be named at a preflight site.

    Two sources, because the two parameter-group helpers hardcode their action
    INSIDE managed_tag_preflight rather than taking it from the call site, so
    scanning only the tools directory reports them as missing.
    """
    _ACTION_RX = re.compile(r'action=\n?\s*"((?:rds|elasticache|dynamodb):\w+)"')
    wired = set(_ACTION_RX.findall(
        pathlib.Path(mtp.__file__).read_text()))
    for p in _TOOLS.glob("*.py"):
        src = p.read_text()
        if "tag_warning" not in src:
            continue
        wired |= set(_ACTION_RX.findall(src))

    missing = _tag_gated_actions() - wired
    assert not missing, (
        "tag-gated spoke actions with no preflight warning: " + ", ".join(sorted(missing))
    )
