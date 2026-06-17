"""Scoped alert push — WS $connect authorizer + broadcast helper."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[3]


def _load(rel: str):
    p = _ROOT / rel
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_"), p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


authz = _load("api/ws_authorizer/handler.py")
notify = _load("api/incident_webhook/ws_notify.py")


def _effect(resp):
    return resp["policyDocument"]["Statement"][0]["Effect"]


# --- authorizer (fail-closed Cognito GetUser check) ---

def test_authorizer_no_token_denies():
    r = authz.lambda_handler({"methodArn": "arn:x", "queryStringParameters": {}}, None)
    assert _effect(r) == "Deny"


def test_authorizer_valid_token_allows(monkeypatch):
    fake = MagicMock()
    fake.get_user.return_value = {
        "Username": "alice",
        "UserAttributes": [{"Name": "sub", "Value": "s-1"}],
    }
    monkeypatch.setattr(authz, "_cognito", fake)
    r = authz.lambda_handler(
        {"methodArn": "arn:x", "queryStringParameters": {"token": "good"}}, None
    )
    assert _effect(r) == "Allow"
    assert r["context"]["sub"] == "s-1"


def test_authorizer_bad_token_denies(monkeypatch):
    fake = MagicMock()
    fake.get_user.side_effect = ClientError(
        {"Error": {"Code": "NotAuthorizedException"}}, "GetUser"
    )
    monkeypatch.setattr(authz, "_cognito", fake)
    r = authz.lambda_handler(
        {"methodArn": "arn:x", "queryStringParameters": {"token": "bad"}}, None
    )
    assert _effect(r) == "Deny"


# --- broadcast (no-op unconfigured; post + prune Gone) ---

def test_broadcast_noop_without_env(monkeypatch):
    monkeypatch.delenv("WS_CONNECTIONS_TABLE", raising=False)
    monkeypatch.delenv("WS_MGMT_ENDPOINT", raising=False)
    assert notify.broadcast({"type": "alert"}) == 0


def test_broadcast_posts_and_prunes_gone(monkeypatch):
    monkeypatch.setenv("WS_CONNECTIONS_TABLE", "t")
    monkeypatch.setenv("WS_MGMT_ENDPOINT", "https://e")

    table = MagicMock()
    table.scan.return_value = {
        "Items": [{"connection_id": "c1"}, {"connection_id": "c2"}]
    }
    ddb_res = MagicMock()
    ddb_res.Table.return_value = table

    class Gone(Exception):
        pass

    mgmt = MagicMock()
    mgmt.exceptions.GoneException = Gone

    def _post(ConnectionId, Data):
        if ConnectionId == "c2":
            raise Gone()

    mgmt.post_to_connection.side_effect = _post

    monkeypatch.setattr(notify.boto3, "client", lambda svc, **k: mgmt)
    monkeypatch.setattr(notify.boto3, "resource", lambda svc: ddb_res)

    delivered = notify.broadcast({"type": "alert", "cluster_id": "prod-pg"})
    assert delivered == 1  # c1 delivered; c2 Gone → pruned
    table.delete_item.assert_called_once_with(Key={"connection_id": "c2"})
