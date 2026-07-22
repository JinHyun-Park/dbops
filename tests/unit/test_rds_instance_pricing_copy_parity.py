"""Byte-parity guard for the two rds_instance_pricing.py copies.

`api/` cannot import `mcp_servers`, so the Price List helper is deliberately
duplicated: the canonical copy lives in mcp-servers/mcp_servers/shared/ and a
verbatim replica in api/simulation/ (consumed by the REST route). The R-5
whole-branch review flagged the drift risk between them as the one thing that
could silently diverge. This test fails the moment the two bodies differ, so a
fix to one is never shipped without the other.

Only the module docstring is allowed to differ (the api copy carries an extra
NOTE explaining the no-mcp_servers rule); everything from the first
`import boto3` line onward MUST be identical.
"""

import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]
_CANONICAL = _REPO / "mcp-servers/mcp_servers/shared/rds_instance_pricing.py"
_REPLICA = _REPO / "api/simulation/rds_instance_pricing.py"


def _body_from_import(path: pathlib.Path) -> str:
    """Return the file text from the first `import boto3` line onward (drops the
    module docstring, which is allowed to differ)."""
    text = path.read_text()
    marker = "import boto3"
    idx = text.find(marker)
    assert idx != -1, f"{path} has no `import boto3` anchor line"
    return text[idx:]


def test_both_pricing_copies_exist():
    assert _CANONICAL.is_file(), f"missing canonical pricing helper: {_CANONICAL}"
    assert _REPLICA.is_file(), f"missing api/simulation replica: {_REPLICA}"


def test_pricing_bodies_are_identical_from_import_onward():
    canonical = _body_from_import(_CANONICAL)
    replica = _body_from_import(_REPLICA)
    assert canonical == replica, (
        "rds_instance_pricing.py copies diverged (mcp_servers/shared vs "
        "api/simulation). Any change to one MUST be mirrored in the other — "
        "they price real money and api/ cannot import mcp_servers. Re-sync them."
    )
