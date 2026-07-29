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


# --- authorizer (fail-closed) ---
#
# The authorizer no longer validates a Cognito ACCESS TOKEN via GetUser: it
# consumes a single-use handshake TICKET, so no long-lived credential rides the
# query string. The two tests that asserted the GetUser behaviour were DELETED
# rather than adapted, because the behaviour they described is gone; the ticket
# path (single use, expiry checked in code, every unknown a Deny, and a token
# no longer opening a socket) is covered in tests/unit/api/test_ws_ticket.py.
#
# This one stays here because it is about the WS surface itself rather than the
# credential: a handshake carrying no identity source at all must be denied.

def test_authorizer_with_no_identity_source_denies():
    r = authz.lambda_handler({"methodArn": "arn:x", "queryStringParameters": {}}, None)
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


# --- drift guard: the two ws_notify.py copies must stay byte-identical ---

def test_ws_notify_copies_are_identical():
    """broadcast() is copied verbatim into both broadcasting Lambdas (no shared
    layer). If the copies drift, a fix to one path (e.g. audience filtering)
    silently skips the other — alerts get scoped but external incidents don't,
    or vice versa. Pin them equal so a partial edit fails CI."""
    a = (_ROOT / "api/incident_webhook/ws_notify.py").read_bytes()
    b = (_ROOT / "data-pipeline/alert_evaluator/ws_notify.py").read_bytes()
    assert a == b, "ws_notify.py copies drifted — re-sync the two files"
