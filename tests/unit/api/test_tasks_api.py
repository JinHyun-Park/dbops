"""Agent Tasks REST API — list / get / create.

Covers the GSI routing (recency vs per-cluster), get 200/404, and POST
validation (cluster_id required, kind allow-list, registry check) plus the
pending-row payload a manual run writes.
"""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "api" / "tasks" / "handler.py"
_spec = importlib.util.spec_from_file_location("tasks_handler", PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt() -> str:
    payload = {"cognito:username": "alice"}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, qsp=None, body=None, task_id=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": "/api/tasks",
        "pathParameters": {"id": task_id} if task_id else {},
        "queryStringParameters": qsp or {},
        "headers": {"authorization": f"Bearer {_jwt()}"},
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_TABLE", "t")
    monkeypatch.setenv("CLUSTERS_TABLE", "c")


def test_list_defaults_to_recency_gsi():
    table = MagicMock()
    table.query.return_value = {"Items": [{"task_id": "1"}, {"task_id": "2"}]}
    with patch.object(handler, "_table", return_value=table):
        resp = handler.lambda_handler(_event("GET"), None)
    assert resp["statusCode"] == 200
    assert len(json.loads(resp["body"])["tasks"]) == 2
    assert table.query.call_args.kwargs["IndexName"] == "recency-index"


def test_list_by_cluster_uses_cluster_gsi():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    with patch.object(handler, "_table", return_value=table):
        resp = handler.lambda_handler(_event("GET", qsp={"cluster": "c1"}), None)
    assert resp["statusCode"] == 200
    assert table.query.call_args.kwargs["IndexName"] == "cluster-created-index"


def test_get_found_then_404():
    table = MagicMock()
    table.get_item.side_effect = [{"Item": {"task_id": "x"}}, {}]
    with patch.object(handler, "_table", return_value=table):
        ok = handler.lambda_handler(_event("GET", task_id="x"), None)
        missing = handler.lambda_handler(_event("GET", task_id="y"), None)
    assert ok["statusCode"] == 200
    assert missing["statusCode"] == 404


def test_post_requires_cluster_id():
    assert handler.lambda_handler(_event("POST", body={}), None)["statusCode"] == 400


def test_post_rejects_unknown_kind():
    resp = handler.lambda_handler(_event("POST", body={"cluster_id": "c1", "kind": "drop_db"}), None)
    assert resp["statusCode"] == 400


def test_post_rejects_unknown_cluster():
    clusters = MagicMock()
    clusters.get_item.return_value = {}  # not in registry
    with patch.object(handler, "_table", return_value=MagicMock()), \
         patch.object(handler, "_clusters_table", return_value=clusters):
        resp = handler.lambda_handler(_event("POST", body={"cluster_id": "ghost"}), None)
    assert resp["statusCode"] == 400


def test_post_creates_manual_rca():
    tasks = MagicMock()
    clusters = MagicMock()
    clusters.get_item.return_value = {"Item": {"cluster_id": "c1"}}
    with patch.object(handler, "_table", return_value=tasks), \
         patch.object(handler, "_clusters_table", return_value=clusters):
        resp = handler.lambda_handler(_event("POST", body={"cluster_id": "c1"}), None)
    assert resp["statusCode"] == 201
    item = json.loads(resp["body"])
    assert item["kind"] == "manual_rca"
    assert item["status"] == "pending"
    assert item["trigger"].startswith("manual:")
    assert item["record_type"] == "task"
    tasks.put_item.assert_called_once()


def test_method_not_allowed():
    assert handler.lambda_handler(_event("DELETE"), None)["statusCode"] == 405
