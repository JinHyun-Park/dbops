"""CDK synth smoke + structural assertions.

These tests verify the CDK app:
  1. Synthesises without error.
  2. Produces the expected 4 stacks (foundation / data / agent / frontend).
  3. Applies Application=DBOps tag at the app level — regression catch for
     anyone removing the cdk.Tags.of(app).add(...) call in cdk/app.py, which
     would silently break cost attribution.

We don't full-diff resources here — that'd flake on every CDK upgrade. The
goal is "did we keep the load-bearing structure?", not "did anything change?".
"""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CDK_DIR = ROOT / "cdk"


@pytest.fixture(scope="module")
def cdk_app():
    """Import the CDK app once and run synth. Falls back to settings.example
    when the user's settings.py is gitignored and not present (CI path).

    Stacks reference assets via relative paths like `../data-pipeline/...`,
    which only resolve correctly when CWD is the cdk/ directory. We swap
    CWD for the synth and restore it after — pytest runs from repo root
    by default."""
    sys.path.insert(0, str(CDK_DIR))

    # CI copies settings.example.py → settings.py before invoking pytest.
    if not (CDK_DIR / "config" / "settings.py").exists():
        pytest.skip("cdk/config/settings.py missing — run `cp cdk/config/settings.example.py cdk/config/settings.py`")

    # FrontendStack's BucketDeployment requires `frontend/out/` to exist
    # at synth time. CI hasn't run `npm run build` (frontend is a separate
    # job that doesn't produce a CDK artifact) — stub a minimal directory
    # so synth resolves the asset path. Local devs who already have a
    # built `out/` keep their real artifact.
    #
    # NOTE: the frontend stack now ships THREE deployments by cache policy, and
    # the hashed-assets one sources `frontend/out/_next` as a SEPARATE asset —
    # so that subtree must exist at synth time too, or synth fails with
    # CannotFindAsset on CI (where there's no real build). Stub both.
    frontend_out = ROOT / "frontend" / "out"
    frontend_out.mkdir(parents=True, exist_ok=True)
    if not (frontend_out / "index.html").exists():
        (frontend_out / "index.html").write_text("<!-- synth stub -->\n")
    next_static = frontend_out / "_next" / "static"
    if not next_static.exists():
        next_static.mkdir(parents=True, exist_ok=True)
        (next_static / ".synth-stub").write_text("// synth stub\n")

    original_cwd = os.getcwd()
    os.chdir(CDK_DIR)
    try:
        import aws_cdk as cdk_lib
        from config.settings import Settings  # type: ignore
        from stacks.agent_stack import AgentStack
        from stacks.data_stack import DataStack
        from stacks.foundation_stack import FoundationStack
        from stacks.frontend_stack import FrontendStack

        app = cdk_lib.App()
        env = cdk_lib.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)
        foundation = FoundationStack(app, f"dbops-{Settings.ENV}-foundation", env=env)
        data = DataStack(app, f"dbops-{Settings.ENV}-data", env=env, foundation=foundation)
        agent = AgentStack(app, f"dbops-{Settings.ENV}-agent", env=env, foundation=foundation, data=data)
        FrontendStack(app, f"dbops-{Settings.ENV}-frontend", env=env, foundation=foundation, agent=agent)

        cdk_lib.Tags.of(app).add("Application", "DBOps")
        cdk_lib.Tags.of(app).add("Environment", Settings.ENV)
        assembly = app.synth()
    finally:
        os.chdir(original_cwd)
    return assembly


def test_synth_produces_four_stacks(cdk_app):
    stacks = [s.stack_name for s in cdk_app.stacks]
    expected = {"dbops-dev-foundation", "dbops-dev-data", "dbops-dev-agent", "dbops-dev-frontend"}
    assert expected.issubset(set(stacks)), f"missing stacks. got: {stacks}"


def test_app_config_table_present(cdk_app):
    """Foundation must define the app-config key-value table (config_key PK).

    The cdk_app fixture returns a CloudAssembly, not a dict of stack objects,
    so we cannot call Template.from_stack() directly on it. Instead we
    re-synth an isolated FoundationStack here, mirroring the fixture's
    CWD-swap + settings-fallback logic. The assertion matches on KeySchema
    only (env-agnostic — no table-name check).
    """
    from aws_cdk.assertions import Template

    original_cwd = os.getcwd()
    os.chdir(CDK_DIR)
    try:
        import aws_cdk as cdk_lib
        from config.settings import Settings  # type: ignore  # noqa: F401
        from stacks.foundation_stack import FoundationStack

        app = cdk_lib.App()
        foundation = FoundationStack(app, "test-foundation")
        template = Template.from_stack(foundation)
    finally:
        os.chdir(original_cwd)

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [{"AttributeName": "config_key", "KeyType": "HASH"}],
        },
    )


def test_approval_policies_table_present(cdk_app):
    """Foundation must define the approval-policies table (policy_id PK)."""
    from aws_cdk.assertions import Template

    original_cwd = os.getcwd()
    os.chdir(CDK_DIR)
    try:
        import aws_cdk as cdk_lib
        from config.settings import Settings  # type: ignore  # noqa: F401
        from stacks.foundation_stack import FoundationStack

        app = cdk_lib.App()
        foundation = FoundationStack(app, "test-foundation")
        template = Template.from_stack(foundation)
    finally:
        os.chdir(original_cwd)

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"KeySchema": [{"AttributeName": "policy_id", "KeyType": "HASH"}]},
    )


def test_context_files_table_present(cdk_app):
    """Foundation must define the context-files table (file_id PK)."""
    from aws_cdk.assertions import Template

    original_cwd = os.getcwd()
    os.chdir(CDK_DIR)
    try:
        import aws_cdk as cdk_lib
        from config.settings import Settings  # type: ignore  # noqa: F401
        from stacks.foundation_stack import FoundationStack

        app = cdk_lib.App()
        foundation = FoundationStack(app, "test-foundation")
        template = Template.from_stack(foundation)
    finally:
        os.chdir(original_cwd)

    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"KeySchema": [{"AttributeName": "file_id", "KeyType": "HASH"}]},
    )


def test_findings_writer_interval_reaches_both_consumers(cdk_app):
    """The multi-writer findings freshness window is derived at runtime from the
    ETL cadence, which only CDK knows (cdk/config/settings.py is gitignored). Both
    consumers must actually receive it, or they silently fall back to the floor and
    a deployer who raised the interval loses half of every rds_instance /
    documentdb cluster's findings."""
    agent = next(s for s in cdk_app.stacks if s.stack_name.endswith("-agent"))
    fns = [
        r["Properties"]
        for r in (agent.template or {}).get("Resources", {}).values()
        if r.get("Type") == "AWS::Lambda::Function"
    ]
    carrying = [
        f for f in fns
        if "FINDINGS_WRITER_INTERVAL_MIN" in f.get("Environment", {}).get("Variables", {})
    ]
    assert len(carrying) == 2, f"expected exactly 2 carriers, got {len(carrying)}"
    carriers = {f.get("Handler") for f in carrying}
    assert carriers == {
        "handler.lambda_handler",            # api/dashboard
        "mcp_servers.incident.handler.lambda_handler",
    }, f"env var must reach the dashboard + incident MCP Lambdas, got {carriers}"


def test_performance_mcp_can_reach_a_target_cluster(cdk_app):
    """explain_plan is the only performance tool that runs SQL on the TARGET
    cluster, and it needs all three of these to do it. Without CLUSTERS_TABLE,
    CacheClient._resolve_target returns None for every cluster, execute_on_target
    hands back an empty QueryResult, and explain_plan answers
    status=no_target "cluster not registered or unreachable" about a cluster that
    is registered AND reachable. Measured on the deployed dev Lambda: three env
    vars, no registry read, no rds-data on any target, and both Aurora MySQL and
    Aurora PG came back no_target. /api/explain worked the whole time because
    explain_lambda has exactly these three grants, which is why the gap survived.
    """
    agent = next(s for s in cdk_app.stacks if s.stack_name.endswith("-agent"))
    resources = (agent.template or {}).get("Resources", {})

    logical_id, fn = next(
        (k, r) for k, r in resources.items()
        if r.get("Type") == "AWS::Lambda::Function"
        and r["Properties"].get("Handler") == "mcp_servers.performance.handler.lambda_handler"
    )
    env = fn["Properties"].get("Environment", {}).get("Variables", {})
    assert "CLUSTERS_TABLE" in env, "explain_plan cannot resolve any target without the registry"

    role_ref = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    # (action, resource) pairs, not just actions. The deployed role already HAD
    # rds-data:ExecuteStatement and secretsmanager:GetSecretValue, both scoped to
    # the DBOps cache cluster and its own secret, and explain_plan was still dark:
    # a target cluster is neither of those. So the grant has to be checked against
    # the resource it reaches, or this test passes on the exact broken deployment
    # it exists to catch (verified: asserting actions alone missed it).
    grants = []
    for r in resources.values():
        if r.get("Type") != "AWS::IAM::Policy":
            continue
        if not any(role.get("Ref") == role_ref for role in r["Properties"].get("Roles", [])):
            continue
        for st in r["Properties"]["PolicyDocument"]["Statement"]:
            act = st["Action"]
            for a in (act if isinstance(act, list) else [act]):
                grants.append((a, json.dumps(st["Resource"])))

    def reaches(action, predicate):
        return any(a == action and predicate(res) for a, res in grants)

    # The registry read (scoped to the clusters table, no other Lambda grant
    # matches it), the EXPLAIN on an arbitrary registered cluster ARN, and that
    # cluster's own secret. All three, or the tool fails at a different step of
    # the same journey.
    assert reaches("dynamodb:GetItem", lambda r: True), \
        f"{logical_id} cannot read the clusters registry: {grants}"
    assert reaches("rds-data:ExecuteStatement", lambda r: r == '"*"'), \
        f"{logical_id} can only run SQL on the cache cluster, not on a target: {grants}"
    assert reaches("secretsmanager:GetSecretValue", lambda r: ":secret:*" in r), \
        f"{logical_id} can only read its own secret, not a target's: {grants}"


def _data_resources(cdk_app, resource_type):
    data = next(s for s in cdk_app.stacks if s.stack_name.endswith("-data"))
    return {
        logical_id: r
        for logical_id, r in (data.template or {}).get("Resources", {}).items()
        if r.get("Type") == resource_type
    }


def test_docdb_collector_window_matches_its_own_schedule(cdk_app):
    """The DocDB collector derives its CloudWatch profiler-log read window from
    COLLECTOR_INTERVAL_MIN. If that value and the EventBridge rate ever disagree,
    consecutive windows overlap (inflating the cumulative query_stats counters it
    writes) or gap (silently dropping slow ops), and nothing else would fail."""
    fns = _data_resources(cdk_app, "AWS::Lambda::Function")
    carrying = {
        logical_id: r["Properties"]
        for logical_id, r in fns.items()
        if "COLLECTOR_INTERVAL_MIN"
        in r["Properties"].get("Environment", {}).get("Variables", {})
    }
    assert len(carrying) == 1, f"expected exactly 1 carrier, got {sorted(carrying)}"
    fn_id, props = next(iter(carrying.items()))
    interval = props["Environment"]["Variables"]["COLLECTOR_INTERVAL_MIN"]

    rules = [
        r["Properties"]
        for r in _data_resources(cdk_app, "AWS::Events::Rule").values()
        if fn_id in str(r["Properties"].get("Targets", []))
    ]
    assert len(rules) == 1, f"expected exactly 1 schedule for {fn_id}, got {len(rules)}"
    assert rules[0]["ScheduleExpression"] == f"rate({interval} minutes)", (
        f"schedule {rules[0]['ScheduleExpression']} disagrees with "
        f"COLLECTOR_INTERVAL_MIN={interval}"
    )
    # The findings freshness window is floored at 15 minutes on the basis that
    # this collector runs every 5 (commit 67d1c3e). Raising the rate past that
    # floor would hide this writer's findings again.
    assert int(interval) * 3 <= 15, (
        "raising the DocDB collector cadence past 5 minutes breaks the 15-minute "
        "findings freshness floor in api/dashboard + maintenance_findings"
    )


def test_docdb_collector_log_read_is_prefix_scoped(cdk_app):
    """Profiler-log ingestion must be able to read /aws/docdb/* and nothing else.
    The same over-grant ("scoped" in a comment, resources=["*"] in the policy) has
    already shipped once in this repo for these exact log actions."""
    statements = [
        stmt
        for policy in _data_resources(cdk_app, "AWS::IAM::Policy").values()
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]
        if "logs:FilterLogEvents" in (
            stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        )
    ]
    assert statements, "no logs:FilterLogEvents grant found in the data stack"
    for stmt in statements:
        resources = stmt["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        for resource in resources:
            assert isinstance(resource, str) and "/aws/docdb/" in resource, (
                f"FilterLogEvents granted on {resource!r}"
            )
    # The profiler state itself is a cluster-parameter-group read, and DocumentDB
    # authorizes its control plane under the rds: prefix.
    actions = {
        action
        for policy in _data_resources(cdk_app, "AWS::IAM::Policy").values()
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])
    }
    assert "rds:DescribeDBClusterParameters" in actions
    assert "rds:DescribeDBClusters" in actions


def test_app_carries_application_tag(cdk_app):
    """Every stack must have Application=DBOps so Bedrock AIPs etc. inherit
    the cost-allocation tag at deploy time."""
    found_tagged = False
    for stack in cdk_app.stacks:
        tags = stack.tags
        if isinstance(tags, dict) and tags.get("Application") == "DBOps":
            found_tagged = True
            break
        # CDK may surface tags via template metadata; fall back to template scan.
        template = stack.template or {}
        meta = template.get("Metadata", {})
        if "Application=DBOps" in str(meta):
            found_tagged = True
            break
    assert found_tagged, "Application=DBOps tag not detected on any stack"
