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


# --- broadcast efficiency: one scan per invocation, and bounded waits ---------
#
# broadcast() was called inside alert_evaluator's per-rule loop, so N fired rules
# meant N full scans of the connections table and N fresh boto3 clients. And the
# management client had no explicit timeouts, so botocore's 60s default read
# timeout meant ONE unresponsive socket could outlast the evaluator's own Lambda
# timeout and take the remaining notifications with it: one dead socket, several
# missed alerts.


def _ws_env(monkeypatch):
    monkeypatch.setenv("WS_CONNECTIONS_TABLE", "conns")
    monkeypatch.setenv("WS_MGMT_ENDPOINT", "https://ws.example/prod")


class _Conns:
    """A connections table that counts scans, so 'scanned once' is measurable."""

    def __init__(self, ids):
        self.ids = list(ids)
        self.scans = 0
        self.deleted = []

    def scan(self, **kwargs):
        self.scans += 1
        return {"Items": [{"connection_id": c} for c in self.ids]}

    def delete_item(self, Key):
        self.deleted.append(Key["connection_id"])


class _Mgmt:
    class exceptions:
        class GoneException(Exception):
            pass

    def __init__(self, gone=()):
        self.gone = set(gone)
        self.posted = []

    def post_to_connection(self, ConnectionId, Data):
        self.posted.append(ConnectionId)
        if ConnectionId in self.gone:
            raise _Mgmt.exceptions.GoneException()


def _wire(monkeypatch, conns, mgmt):
    _ws_env(monkeypatch)
    monkeypatch.setattr(notify, "_table", lambda _n: conns)
    monkeypatch.setattr(notify, "_mgmt", lambda _e: mgmt)


def test_a_prescanned_list_is_not_rescanned_per_broadcast(monkeypatch):
    conns = _Conns(["a", "b"])
    mgmt = _Mgmt()
    _wire(monkeypatch, conns, mgmt)

    ids = notify.load_connections()
    assert conns.scans == 1
    for _ in range(5):
        notify.broadcast({"x": 1}, connections=ids)
    assert conns.scans == 1, f"rescanned {conns.scans} times for 5 broadcasts"
    assert len(mgmt.posted) == 10  # 2 connections x 5 broadcasts


def test_omitting_the_list_still_scans_for_a_single_shot_caller(monkeypatch):
    """incident_webhook broadcasts once per request, so it must keep working
    without the caller having to pre-scan."""
    conns = _Conns(["a"])
    mgmt = _Mgmt()
    _wire(monkeypatch, conns, mgmt)
    assert notify.broadcast({"x": 1}) == 1
    assert conns.scans == 1


def test_a_gone_connection_is_pruned_from_the_callers_list(monkeypatch):
    """The point of sharing the list: a socket found dead on rule 1 must not be
    pushed again on rule 2."""
    conns = _Conns(["live", "dead"])
    mgmt = _Mgmt(gone=["dead"])
    _wire(monkeypatch, conns, mgmt)

    ids = notify.load_connections()
    assert notify.broadcast({"x": 1}, connections=ids) == 1
    assert ids == ["live"], f"the dead id was left in the list: {ids}"
    assert conns.deleted == ["dead"], "the row must also be pruned from the table"

    mgmt.posted.clear()
    notify.broadcast({"x": 2}, connections=ids)
    assert mgmt.posted == ["live"], "a known-dead socket was retried"


def test_the_management_client_has_explicit_timeouts():
    """Bounded waits, asserted on the config rather than on behaviour: a hung post
    is exactly what cannot be reproduced in a unit test, so the value that
    prevents it is what gets pinned."""
    cfg = notify._MGMT_CONFIG
    assert cfg.connect_timeout and cfg.connect_timeout <= 5, cfg.connect_timeout
    assert cfg.read_timeout and cfg.read_timeout <= 10, cfg.read_timeout
    # A push to a live socket returns in milliseconds; retrying a timed-out one
    # mostly means pushing to a connection that has already gone away.
    assert cfg.retries.get("max_attempts", 99) <= 3, cfg.retries


def test_an_unconfigured_channel_is_still_a_no_op(monkeypatch):
    monkeypatch.delenv("WS_CONNECTIONS_TABLE", raising=False)
    monkeypatch.delenv("WS_MGMT_ENDPOINT", raising=False)
    assert notify.broadcast({"x": 1}) == 0
    assert notify.load_connections() == []


def test_a_failed_scan_yields_no_connections_rather_than_raising(monkeypatch):
    class Boom:
        def scan(self, **_):
            raise RuntimeError("throttled")
    _ws_env(monkeypatch)
    monkeypatch.setattr(notify, "_table", lambda _n: Boom())
    assert notify.load_connections() == []
