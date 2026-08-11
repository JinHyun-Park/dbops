"""FoundationStack must define the APM targets registry table.

Mirrors the harness convention in tests/cdk/test_synth.py: aws_cdk and the
gitignored cdk/config/settings.py are absent in CI / fresh clones, so we insert
cdk/ onto sys.path, skip cleanly when settings.py is missing, and import lazily
inside the test after chdir to cdk/ (stacks reference relative asset paths and
`from config.settings import Settings`, which only resolve from the cdk/ CWD).

The assertion is env-agnostic: it matches on KeySchema + BillingMode, not on the
env-specific table name.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CDK_DIR = ROOT / "cdk"


def test_apm_targets_table_exists():
    sys.path.insert(0, str(CDK_DIR))

    if not (CDK_DIR / "config" / "settings.py").exists():
        pytest.skip("cdk/config/settings.py missing — run `cp cdk/config/settings.example.py cdk/config/settings.py`")

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
            "KeySchema": [{"AttributeName": "target_id", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )
