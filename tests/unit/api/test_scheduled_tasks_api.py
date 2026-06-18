"""Scheduled Tasks REST API — list / create / delete."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "api" / "scheduled_tasks" / "handler.py"
_spec = importlib.util.spec_from_file_location("scheduled_tasks_handler", PATH)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


def _event(method, qsp=None, body=None, sid=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": "/api/scheduled-tasks",
        "pathParameters": {"id": sid} if sid else {},
        "queryStringParameters": qsp or {},
        "headers": {},
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "a")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "b")
    monkeypatch.setenv("CLUSTERS_TABLE", "c")


def test_list():
    with patch.object(h, "_query", return_value=[{"id": 1, "cluster_id": "c1"}]):
        r = h.lambda_handler(_event("GET"), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["schedules"][0]["id"] == 1


def test_post_requires_cluster():
    assert h.lambda_handler(_event("POST", body={}), None)["statusCode"] == 400


def test_post_rejects_bad_interval():
    r = h.lambda_handler(
        _event("POST", body={"cluster_id": "c1", "interval_kind": "yearly"}), None
    )
    assert r["statusCode"] == 400


def test_post_creates():
    with patch.object(h, "_query", return_value=[{"id": 7}]), \
         patch.object(h, "_cluster_exists", return_value=True):
        r = h.lambda_handler(
            _event("POST", body={"cluster_id": "c1", "interval_kind": "daily"}), None
        )
    assert r["statusCode"] == 201
    assert json.loads(r["body"])["id"] == 7


def test_post_unknown_cluster():
    with patch.object(h, "_cluster_exists", return_value=False):
        r = h.lambda_handler(
            _event("POST", body={"cluster_id": "ghost", "interval_kind": "daily"}), None
        )
    assert r["statusCode"] == 400


def test_delete():
    with patch.object(h, "_query", return_value=[]):
        r = h.lambda_handler(_event("DELETE", sid="5"), None)
    assert r["statusCode"] == 200


def test_method_not_allowed():
    assert h.lambda_handler(_event("PUT"), None)["statusCode"] == 405
