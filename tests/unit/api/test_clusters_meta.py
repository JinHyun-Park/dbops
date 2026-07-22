"""PATCH /api/clusters/{id}/meta — admin-editable Map note (purpose + service_tags)."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
sys.path.insert(0, str(_CLUSTERS_DIR))
_spec = importlib.util.spec_from_file_location("clusters_handler_meta", _CLUSTERS_DIR / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def _table_with(item):
    t = MagicMock()
    t.get_item.return_value = {"Item": item} if item is not None else {}
    return t


def test_sets_purpose_and_tags_trimmed_deduped():
    t = _table_with({"cluster_id": "c1", "engine": "aurora-postgresql"})
    resp = handler._handle_update_meta(
        t, "c1",
        {"purpose": "  checkout primary  ", "service_tags": ["checkout", "orders", "checkout", "  "]},
    )
    assert resp["statusCode"] == 200
    t.update_item.assert_called_once()
    vals = t.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":purpose"] == "checkout primary"          # trimmed + capped
    assert vals[":service_tags"] == ["checkout", "orders"]  # blank dropped, deduped


def test_404_when_cluster_absent():
    # The conditional update (attribute_exists) fails atomically when the cluster
    # is gone — translated to 404, never a phantom item.
    from botocore.exceptions import ClientError

    t = MagicMock()
    t.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    resp = handler._handle_update_meta(t, "nope", {"purpose": "x"})
    assert resp["statusCode"] == 404
    # the update was ATTEMPTED with the existence guard (not skipped)
    assert "ConditionExpression" in t.update_item.call_args.kwargs


def test_rejects_non_list_service_tags():
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {"service_tags": "not-a-list"})
    assert resp["statusCode"] == 400
    t.update_item.assert_not_called()


def test_rejects_empty_body():
    t = _table_with({"cluster_id": "c1"})
    assert handler._handle_update_meta(t, "c1", {})["statusCode"] == 400


def test_caps_tag_count():
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {"service_tags": [f"svc{i}" for i in range(50)]})
    assert resp["statusCode"] == 200
    tags = t.update_item.call_args.kwargs["ExpressionAttributeValues"][":service_tags"]
    assert len(tags) == handler._TAGS_MAX


def test_partial_update_only_provided_field():
    t = _table_with({"cluster_id": "c1"})
    handler._handle_update_meta(t, "c1", {"purpose": "x"})
    vals = t.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert ":purpose" in vals and ":service_tags" not in vals


def test_patch_gate_fail_closed_for_non_admin():
    # The PATCH dispatch gates on _forbid_viewer; no bearer => 403 (fail-closed).
    assert handler._forbid_viewer({"headers": {}})["statusCode"] == 403


def test_sets_db_secret_arn_and_write_secret_arn():
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {
        "db_secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:read",
        "db_write_secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:write",
    })
    assert resp["statusCode"] == 200
    vals = t.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":db_secret_arn"] == "arn:aws:secretsmanager:us-east-1:1:secret:read"
    assert vals[":db_write_secret_arn"] == "arn:aws:secretsmanager:us-east-1:1:secret:write"
    assert vals[":db_secret_source"] == "override"
    assert "ConditionExpression" in t.update_item.call_args.kwargs


def test_rejects_invalid_secret_arn_with_static_message():
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {"db_write_secret_arn": "not-an-arn"})
    assert resp["statusCode"] == 400
    assert "not-an-arn" not in resp["body"]
    t.update_item.assert_not_called()


def test_clearing_db_secret_arn_sets_source_missing():
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {"db_secret_arn": ""})
    assert resp["statusCode"] == 200
    vals = t.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":db_secret_arn"] == ""
    assert vals[":db_secret_source"] == "missing"


def test_clearing_write_secret_arn_leaves_source_untouched():
    # db_secret_source tracks db_secret_arn only; write secret has no such field.
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {"db_write_secret_arn": ""})
    assert resp["statusCode"] == 200
    vals = t.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":db_write_secret_arn"] == ""
    assert ":db_secret_source" not in vals


def test_mixed_purpose_and_secret_arn_update():
    t = _table_with({"cluster_id": "c1"})
    resp = handler._handle_update_meta(t, "c1", {
        "purpose": "checkout primary",
        "db_write_secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:write",
    })
    assert resp["statusCode"] == 200
    vals = t.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":purpose"] == "checkout primary"
    assert vals[":db_write_secret_arn"] == "arn:aws:secretsmanager:us-east-1:1:secret:write"
