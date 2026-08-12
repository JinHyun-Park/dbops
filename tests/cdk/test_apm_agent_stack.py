"""Synth-level: the APM routes and Lambda exist. Mirrors other cdk route tests."""
from pathlib import Path


def test_agent_stack_declares_apm_routes():
    src = Path("cdk/stacks/agent_stack.py").read_text()
    assert '"/api/apm/targets"' in src
    assert '"/api/apm/targets/{id}"' in src
    assert '"/api/apm/targets/{id}/logs/search"' in src
    assert 'code=lambda_.Code.from_asset("../api/apm")' in src


def test_spoke_template_has_ec2_describe():
    tpl = Path("cdk/cross-account/spoke-role-template.yaml").read_text()
    assert "ec2:DescribeInstances" in tpl
    assert "logs:FilterLogEvents" in tpl
