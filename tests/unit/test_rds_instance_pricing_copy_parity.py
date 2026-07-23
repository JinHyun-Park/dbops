"""Byte-parity guard for the two rds_instance_pricing.py copies.

`api/` cannot import `mcp_servers`, so the Price List helper is deliberately
duplicated: the canonical copy lives in mcp-servers/mcp_servers/shared/ and a
verbatim replica in api/simulation/ (consumed by the REST route). The R-5
whole-branch review flagged the drift risk between them as the one thing that
could silently diverge. This test fails the moment the two bodies differ, so a
fix to one is never shipped without the other.

Only the module docstring is allowed to differ (the api copy carries an extra
NOTE explaining the no-mcp_servers rule); EVERYTHING after the module docstring
— imports and all code — MUST be identical. (Comparing only from `import boto3`
onward would miss a divergence in an earlier line like `import json`, so we
strip exactly the leading docstring and compare the entire remainder.)
"""

import ast
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]
_CANONICAL = _REPO / "mcp-servers/mcp_servers/shared/rds_instance_pricing.py"
_REPLICA = _REPO / "api/simulation/rds_instance_pricing.py"


def _code_after_docstring(path: pathlib.Path) -> str:
    """Return the file text with ONLY the leading module docstring removed, so
    everything else (imports + code) is compared byte-for-byte. Uses the AST to
    locate the docstring's exact end line rather than scanning for a triple-quote
    delimiter (which a triple-quote appearing inside code could fool)."""
    text = path.read_text()
    tree = ast.parse(text)
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        # Drop through the docstring's last line; compare the rest.
        end = body[0].end_lineno  # 1-indexed, inclusive
        return "\n".join(text.splitlines()[end:])
    return text  # no module docstring → compare whole file


def test_both_pricing_copies_exist():
    assert _CANONICAL.is_file(), f"missing canonical pricing helper: {_CANONICAL}"
    assert _REPLICA.is_file(), f"missing api/simulation replica: {_REPLICA}"


def test_pricing_bodies_are_identical_after_docstring():
    canonical = _code_after_docstring(_CANONICAL)
    replica = _code_after_docstring(_REPLICA)
    assert canonical == replica, (
        "rds_instance_pricing.py copies diverged (mcp_servers/shared vs "
        "api/simulation). Any change to one MUST be mirrored in the other — "
        "they price real money and api/ cannot import mcp_servers. Re-sync them."
    )
