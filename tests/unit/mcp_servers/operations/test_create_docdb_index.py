"""Tests for create_docdb_index — the DocumentDB Mongo-protocol index write.

Run without pymongo installed (lazy import; patch _CLIENT_FACTORY). _write_creds /
lookup_cluster are patched so no AWS is touched.

Covered: 3-state flow, no-write-secret → unsupported_engine no-op, keys/name
validation (no connect), key ORDER preserved into create_index (fix #2), idempotent
no-change (named index exists), TOCTOU drift, never-raise. The FAIL-CLOSED engine
gate lives in the handler (test_operations_engine_gate.py)."""

from unittest.mock import MagicMock, patch

import mcp_servers.operations.tools.create_docdb_index as mod
from mcp_servers.operations.tools.create_docdb_index import create_docdb_index_impl

_CREDS = {"host": "docdb.local", "port": 27017, "username": "rw", "password": "pw"}

# A driver/AWS error carries the cluster endpoint, the write-secret ARN and the
# platform role name. None of it may reach a response field: the request-time
# list_indexes runs BEFORE any approval exists, so a plain chat user can surface
# these messages just by asking for an index.
_LEAK_MSG = (
    "connection refused: docdb-prod.cluster-abc123.ap-northeast-2.docdb.amazonaws.com:27017 "
    "(secret arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:mongo-rw, "
    "role dbops-dev-operations-role) boom"
)


def _no_exception_text(result):
    """No response value may carry raw exception text (project hard rule)."""
    blob = " ".join(str(v) for v in result.values())
    for leak in ("123456789012", "arn:aws", "docdb.amazonaws.com", "dbops-dev-operations-role",
                 "connection refused", "boom", "Traceback", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked into response: {result}"


class _FakeCollection:
    def __init__(self, existing_seq, create_spy, raise_on_create=False):
        self._existing_seq = list(existing_seq)
        self._create_spy = create_spy
        self._raise_on_create = raise_on_create

    def list_indexes(self):
        names = self._existing_seq.pop(0)
        return [{"name": n} for n in names]

    def create_index(self, keys, **kwargs):
        if self._raise_on_create:
            raise RuntimeError(_LEAK_MSG)
        self._create_spy(keys, kwargs)
        return kwargs.get("name")


class _FakeDB:
    def __init__(self, collection):
        self._collection = collection

    def __getitem__(self, _coll):
        return self._collection


class _FakeClient:
    def __init__(self, existing_seq, create_spy, raise_on_create=False, raise_on_connect=False):
        if raise_on_connect:
            raise RuntimeError(_LEAK_MSG)
        self._db = _FakeDB(_FakeCollection(existing_seq, create_spy, raise_on_create))
        self.closed = False

    def __getitem__(self, _db):
        return self._db

    def close(self):
        self.closed = True


def _factory(existing_seq, create_spy, **kw):
    def make(host, port, username, password):
        return _FakeClient(existing_seq, create_spy, **kw)

    return make


def _with_creds():
    return patch.object(mod, "_write_creds", lambda cid: (_CREDS, None))


def _args(**over):
    base = {
        "cluster_id": "docdb-1",
        "db": "app",
        "collection": "events",
        "keys": [["user_id", 1], ["created_at", -1]],
        "name": "ix_user_created",
    }
    base.update(over)
    return base


# ===== no write secret → unsupported_engine no-op =====


def test_no_write_secret_unsupported_no_op():
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with patch.object(mod, "lookup_cluster", lambda cid: {}), patch.object(
        mod, "_CLIENT_FACTORY", factory
    ):
        result = create_docdb_index_impl(MagicMock(), **_args())
    assert result["status"] == "unsupported_engine"
    assert result["reason"] == "no write credentials configured"
    factory.assert_not_called()


# ===== validation (no connect) =====


def test_empty_keys_rejected_no_connect():
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", factory):
        result = create_docdb_index_impl(MagicMock(), **_args(keys=[]))
    assert result["status"] == "error"
    assert "keys" in result["reason"]
    factory.assert_not_called()


def test_missing_name_rejected_no_connect():
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", factory):
        result = create_docdb_index_impl(MagicMock(), **_args(name=""))
    assert result["status"] == "error"
    assert "name" in result["reason"]
    factory.assert_not_called()


def test_bad_direction_rejected_no_connect():
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", factory):
        result = create_docdb_index_impl(MagicMock(), **_args(keys=[["f", 2]]))
    assert result["status"] == "error"
    assert "direction" in result["reason"]
    factory.assert_not_called()


# ===== 3-state flow =====


def test_requires_approval():
    create_spy = MagicMock()
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", _factory([[]], create_spy)):
        result = create_docdb_index_impl(MagicMock(), **_args())
    assert result["status"] == "approval_required"
    assert result["keys"] == [["user_id", 1], ["created_at", -1]]
    create_spy.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_executes_when_approved_preserves_key_order():
    """fix #2: keys are passed to create_index as an ORDERED list of (field, dir)
    tuples, NOT sorted — compound-index order is semantic."""
    create_spy = MagicMock()
    # request-time list + TOCTOU re-read both empty (no drift).
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([[], []], create_spy)
    ):
        result = create_docdb_index_impl(MagicMock(), approved=True, **_args())
    assert result["status"] == "modified"
    create_spy.assert_called_once()
    passed_keys, kwargs = create_spy.call_args.args
    # Exact ordered tuples, original order preserved.
    assert passed_keys == [("user_id", 1), ("created_at", -1)]
    assert kwargs["background"] is True
    assert kwargs["name"] == "ix_user_created"


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_reversed_key_order_passed_through_unsorted():
    """The reversed compound order must reach create_index reversed (not sorted)."""
    create_spy = MagicMock()
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([[], []], create_spy)
    ):
        create_docdb_index_impl(
            MagicMock(), approved=True, **_args(keys=[["created_at", -1], ["user_id", 1]])
        )
    passed_keys, _ = create_spy.call_args.args
    assert passed_keys == [("created_at", -1), ("user_id", 1)]


def test_approval_denied_when_guard_rejects():
    create_spy = MagicMock()
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([[]], create_spy)
    ), patch.object(mod, "verify_approval", lambda *a, **k: {"ok": False, "reason": "nope"}):
        result = create_docdb_index_impl(
            MagicMock(), approved=True, approval_id="x", **_args()
        )
    assert result["status"] == "approval_denied"
    create_spy.assert_not_called()


# ===== idempotent =====


def test_idempotent_skip_when_named_index_exists():
    create_spy = MagicMock()
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([["_id_", "ix_user_created"]], create_spy)
    ):
        result = create_docdb_index_impl(MagicMock(), **_args())
    assert result["status"] == "skipped"
    create_spy.assert_not_called()


# ===== TOCTOU =====


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_toctou_index_appeared_denied():
    """fix #6: the index appeared between request and execute → approval_denied,
    no create."""
    create_spy = MagicMock()
    # request-time: absent; re-read: present.
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([[], ["ix_user_created"]], create_spy)
    ):
        result = create_docdb_index_impl(MagicMock(), approved=True, **_args())
    assert result["status"] == "approval_denied"
    assert "changed since approval" in result["reason"]
    create_spy.assert_not_called()


# ===== never-raise =====


def test_connect_failure_is_error_not_raise():
    """Reachable BEFORE any approval: static reason, driver detail to the log."""
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([], MagicMock(), raise_on_connect=True)
    ):
        result = create_docdb_index_impl(MagicMock(), **_args())
    assert result["status"] == "error"
    assert "연결 실패" in result["reason"]
    _no_exception_text(result)


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_create_failure_is_error_not_raise():
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([[], []], MagicMock(), raise_on_create=True)
    ):
        result = create_docdb_index_impl(MagicMock(), approved=True, **_args())
    assert result["status"] == "error"
    assert "인덱스 생성 실패" in result["reason"]
    # the caller still gets the target it asked for, built from ITS OWN input
    assert "app.events" in result["reason"] and "ix_user_created" in result["reason"]
    _no_exception_text(result)


def test_list_indexes_failure_no_exception_text():
    """The request-time read fails (empty seq → IndexError): abort, no leak. This
    path is pre-approval too."""
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", _factory([], MagicMock())):
        result = create_docdb_index_impl(MagicMock(), **_args())
    assert result["status"] == "error"
    assert "인덱스 목록 조회 실패" in result["reason"]  # which step broke
    _no_exception_text(result)


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_pre_write_reread_failure_no_exception_text():
    """The TOCTOU re-read fails → abort without creating, and without leaking."""
    create_spy = MagicMock()
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", _factory([[]], create_spy)):
        result = create_docdb_index_impl(MagicMock(), approved=True, **_args())
    assert result["status"] == "error"
    assert "재조회" in result["reason"]
    create_spy.assert_not_called()
    _no_exception_text(result)


def test_secret_fetch_failure_no_exception_text():
    """A Secrets Manager error names the secret ARN and the platform role, so the
    reason must be static. Also pre-approval."""
    boto3_mock = MagicMock()
    boto3_mock.client.return_value.get_secret_value.side_effect = RuntimeError(_LEAK_MSG)
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with patch.object(
        mod, "lookup_cluster",
        lambda cid: {"mongo_write_secret_arn": "arn:aws:secretsmanager:x:1:secret:s"},
    ), patch.object(mod, "boto3", boto3_mock), patch.object(mod, "_CLIENT_FACTORY", factory):
        result = create_docdb_index_impl(MagicMock(), **_args())
    assert result["status"] == "error"
    assert "쓰기 자격증명" in result["reason"]  # which step broke
    _no_exception_text(result)
    factory.assert_not_called()


def test_dict_keys_accepted_and_ordered():
    """A dict key-spec is accepted (insertion-ordered) and normalized to tuples."""
    create_spy = MagicMock()
    with patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"}), _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([[], []], create_spy)
    ):
        result = create_docdb_index_impl(
            MagicMock(), approved=True, **_args(keys={"a": 1, "b": -1})
        )
    assert result["status"] == "modified"
    passed_keys, _ = create_spy.call_args.args
    assert passed_keys == [("a", 1), ("b", -1)]
