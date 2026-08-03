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


# ---------------------------------------------------------------------------
# Privilege-escalation guard on the one tag-write statement
# ---------------------------------------------------------------------------


def _tag_write_statements():
    """Every statement granting a tag-WRITE action, with its aws:TagKeys list.

    Parsed with an intrinsic-tolerant loader rather than against the text, because
    the property that matters here is structural: which keys the Condition allows.
    """
    import json

    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: f"<!{suffix}>")
    doc = yaml.load(TEMPLATE.read_text(), Loader=_Loader)

    def statements(node):
        if isinstance(node, dict):
            if isinstance(node.get("Statement"), list):
                yield from node["Statement"]
            for v in node.values():
                yield from statements(v)
        elif isinstance(node, list):
            for v in node:
                yield from statements(v)

    out = []
    for s in statements(doc):
        acts = s.get("Action") or []
        if isinstance(acts, str):
            acts = [acts]
        if not any(k in a for a in acts for k in ("AddTags", "TagResource", "CreateTags")):
            continue
        cond = s.get("Condition") or {}
        keys = cond.get("ForAllValues:StringEquals", {}).get("aws:TagKeys")
        out.append({"sid": s.get("Sid"), "actions": acts, "keys": keys,
                    "condition": json.dumps(cond)})
    return out


def test_the_spoke_role_cannot_write_the_tag_that_unlocks_its_own_writes():
    """The key restriction on RDSTagOnCreate is a privilege-escalation guard.

    RDSModifyExisting gates 9 write actions on aws:ResourceTag/ManagedBy=dbops. If
    this role could write that tag it could stamp it on any RDS resource in the
    account and grant itself those writes on anything, which is the boundary the
    whole template exists to draw.

    This is a live trap, not a hypothetical. The tag preflight correctly warns that
    a cluster is missing ManagedBy, and the obvious next move is to add the tag
    through DBOps. Attempted 2026-08-03 against the real spoke role: AccessDenied
    on rds:AddTagsToResource, exactly as designed. Someone reading that denial as a
    bug would fix it by widening this key list. This test is what stops them.
    """
    stmts = _tag_write_statements()
    assert stmts, "no tag-write statement found; if tagging moved, update this guard"
    for s in stmts:
        assert s["keys"], (
            f"{s['sid']} grants {s['actions']} with NO aws:TagKeys restriction. "
            "An unrestricted tag write lets this role grant itself every "
            "ManagedBy-gated action on any resource."
        )
        assert "ManagedBy" not in s["keys"], (
            f"{s['sid']} allows writing the ManagedBy key ({s['keys']}). That is "
            "self-granting: RDSModifyExisting gates 9 writes on exactly that tag. "
            "The tag must be applied from the target account instead."
        )
        # Every permitted key is namespaced, so the list cannot quietly grow into
        # a key that some other policy happens to gate on.
        for k in s["keys"]:
            assert k.startswith("dbops:"), (
                f"{s['sid']} permits the un-namespaced tag key {k!r}; keep the "
                "allowed keys under the dbops: prefix so they cannot collide with "
                "a key another policy gates on"
            )
