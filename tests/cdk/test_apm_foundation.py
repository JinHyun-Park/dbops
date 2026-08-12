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

    original_cwd = os.getcwd()
    os.chdir(CDK_DIR)
    try:
        # This import MUST come after the chdir, not before. Importing anything from
        # aws_cdk spawns the jsii child process, and that child inherits the CWD it was
        # spawned with. A later os.chdir in the Python parent does not move it, so a
        # jsii process born at the repo root resolves every relative asset path
        # ("../api/ws_connect") one level too high and fails with CannotFindAsset.
        #
        # The jsii process is process-global and outlives this test, so getting the
        # order wrong did not just break this test: it poisoned the whole pytest
        # session and took all 12 tests in test_synth.py down with it (measured: 1
        # failed + 12 errors on this branch versus 27 passed on main). The module
        # docstring already stated the intent, "import lazily inside the test after
        # chdir to cdk/"; only this one import was on the wrong side of it.
        import aws_cdk as cdk_lib
        from aws_cdk.assertions import Template
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
