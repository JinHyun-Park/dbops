"""The spoke role must grant every CloudWatch Logs action the data-pipeline
collectors actually call.

The bug this guards shipped in ee0a63c: the DocumentDB profiler collector reads
slow ops with FilterLogEvents, the spoke role granted only StartQuery /
GetQueryResults / DescribeLogGroups, and the collector assumes the spoke role for
a cross-account row. rds:Describe* still resolved the profiler state to "on", so
only the log read failed, with AccessDenied, on every run, in every spoke
account. Permanently dark and (until this commit) silent.

The action census is DERIVED from the collector source rather than hand-kept, so
the next Logs call added to a data-pipeline Lambda cannot go dark the same way.

No CDK synth here: the spoke role is a plain CloudFormation template, and its
short-form intrinsics (!Sub / !If / !GetAtt) are not safe_load-able, so the
grant is asserted against the template TEXT.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "data-pipeline"
TEMPLATE = ROOT / "cdk" / "cross-account" / "spoke-role-template.yaml"


def _logs_client_methods(tree):
    """Every method called on a `<something>.client("logs")` object in one module.

    Two passes: collect the names bound to a logs client, then collect attribute
    calls on those names. A function PARAMETER carrying the client (the
    collector's fetch_profiler_events takes `logs_client`) is matched by name,
    which is what makes the pass find the call at all.
    """
    bound = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "client"
            and any(isinstance(a, ast.Constant) and a.value == "logs" for a in call.args)
        ):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    if not bound:
        return set()
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in bound
    }


def _iam_action(method):
    """boto3 method name -> IAM action name (filter_log_events -> FilterLogEvents)."""
    return "".join(part.title() for part in method.split("_"))


def test_spoke_role_grants_every_logs_action_the_collectors_call():
    template = TEMPLATE.read_text()
    called = set()
    for path in sorted(PIPELINE.rglob("*.py")):
        called |= _logs_client_methods(ast.parse(path.read_text()))

    # Census sanity: an empty set would make this test vacuously green (exactly
    # how the missing grant survived review).
    assert "filter_log_events" in called, (
        f"expected the DocDB profiler read in the census, got {sorted(called)}"
    )

    missing = [
        f"logs:{_iam_action(m)}"
        for m in sorted(called)
        if f'"logs:{_iam_action(m)}"' not in template
    ]
    assert not missing, (
        f"{missing} called by a data-pipeline collector but not granted in "
        f"{TEMPLATE.name}: cross-account reads fail with AccessDenied while the "
        "control-plane read still succeeds, so the feature is dark AND silent"
    )


def test_spoke_profiler_log_grant_carries_the_log_group_arn():
    """FilterLogEvents authorizes on the log-group resource type, per the service
    authorization reference (list_logs: required resource type log-group, access
    level Read). The log-group ARN is therefore the load-bearing one; dropping it
    makes a spoke-account profiler read fail while the hub keeps working, which is
    the failure mode that only shows up cross-account.

    The `:*` form is asserted too, but only because it mirrors the hub grant in
    cdk/stacks/data_stack.py. It is NOT required, and an earlier version of this
    docstring wrongly claimed both forms were, citing log streams."""
    template = TEMPLATE.read_text()
    assert '"arn:aws:logs:*:*:log-group:/aws/docdb/*"' in template, (
        "the required log-group form is missing"
    )
    assert '"arn:aws:logs:*:*:log-group:/aws/docdb/*:*"' in template, (
        "the hub grant carries the :* form; keep the two byte-identical"
    )
    # The retracted justification must not come back.
    assert "the `:*` form its log streams" not in TEMPLATE.read_text()
