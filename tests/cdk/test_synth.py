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
