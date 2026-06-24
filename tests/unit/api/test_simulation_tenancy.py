"""Unit tests for multi-team tenancy enforcement on the simulation handler.

Tests that any POST tool endpoint returns 403 when body.cluster_id is not
visible to the caller, BEFORE dispatching any tool.  Admin and visible
cluster_id proceed normally.

No real AWS calls: tenancy.cluster_visible is patched to control visibility;
downstream tool functions are patched to return a sentinel so we can confirm
dispatch was (or was not) reached.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = ROOT / "api" / "simulation"
HANDLER_PATH = SIM_DIR / "handler.py"
sys.path.insert(0, str(str(SIM_DIR)))


def _load():
    spec = importlib.util.spec_from_file_location("sim_handler_tenancy", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sim_handler_tenancy"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


def _body(resp):
    return json.loads(resp["body"])


def _event(path, cluster_id="c-test", extra_body=None):
    body = {"cluster_id": cluster_id, **(extra_body or {})}
    return {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": f"/api/simulation/{path}",
        "body": json.dumps(body),
        "headers": {"authorization": "Bearer viewer-token"},
    }


# ---------------------------------------------------------------------------
# 403 gate — non-visible cluster_id blocked before dispatch
# ---------------------------------------------------------------------------


def test_upgrade_compatibility_403_for_non_visible_cluster(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda event, item: False)
    monkeypatch.setattr(mod, "_cluster_item", lambda cid: {"cluster_id": cid})
    monkeypatch.setattr(mod, "_check_upgrade_compatibility", lambda *a: {"should": "not reach"})

    resp = mod.lambda_handler(_event("upgrade-compatibility", extra_body={"target_version": "16.2"}), None)
    assert resp["statusCode"] == 403
    body = _body(resp)
    assert "접근 권한" in body.get("error", "") or "권한" in body.get("error", "")


def test_upgrade_impact_403_for_non_visible_cluster(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda event, item: False)
    monkeypatch.setattr(mod, "_cluster_item", lambda cid: {"cluster_id": cid})

    resp = mod.lambda_handler(_event("upgrade-impact", extra_body={"target_version": "16.2"}), None)
    assert resp["statusCode"] == 403


def test_parameter_change_403_for_non_visible_cluster(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda event, item: False)
    monkeypatch.setattr(mod, "_cluster_item", lambda cid: {"cluster_id": cid})

    resp = mod.lambda_handler(_event("parameter-change", extra_body={"parameter_name": "max_connections", "new_value": 200}), None)
    assert resp["statusCode"] == 403


def test_scaling_403_for_non_visible_cluster(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda event, item: False)
    monkeypatch.setattr(mod, "_cluster_item", lambda cid: {"cluster_id": cid})

    resp = mod.lambda_handler(_event("scaling"), None)
    assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# Visible cluster — gate passes, tool is dispatched
# ---------------------------------------------------------------------------


def test_visible_cluster_dispatches_tool(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda event, item: True)
    monkeypatch.setattr(mod, "_cluster_item", lambda cid: {"cluster_id": cid})
    monkeypatch.setattr(mod, "_check_upgrade_compatibility", lambda *a: {"status": "ok"})

    resp = mod.lambda_handler(_event("upgrade-compatibility", extra_body={"target_version": "16.2"}), None)
    assert resp["statusCode"] == 200
    assert _body(resp)["status"] == "ok"


# ---------------------------------------------------------------------------
# Admin — gate is no-op (cluster_visible returns True for admin)
# ---------------------------------------------------------------------------


def test_admin_cluster_visible_true_dispatches(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    # Admin: cluster_visible always returns True
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda event, item: True)
    monkeypatch.setattr(mod, "_cluster_item", lambda cid: {"cluster_id": cid})
    monkeypatch.setattr(mod, "_check_upgrade_compatibility", lambda *a: {"admin": "yes"})

    admin_event = {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/api/simulation/upgrade-compatibility",
        "body": json.dumps({"cluster_id": "c-test", "target_version": "16.2"}),
        "headers": {"authorization": "Bearer admin-token"},
    }
    resp = mod.lambda_handler(admin_event, None)
    assert resp["statusCode"] == 200
    assert _body(resp)["admin"] == "yes"


# ---------------------------------------------------------------------------
# Missing cluster_id still returns 400 (unchanged behaviour)
# ---------------------------------------------------------------------------


def test_missing_cluster_id_returns_400(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    resp = mod.lambda_handler(
        {
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": "/api/simulation/upgrade-compatibility",
            "body": json.dumps({"target_version": "16.2"}),
            "headers": {},
        },
        None,
    )
    assert resp["statusCode"] == 400
