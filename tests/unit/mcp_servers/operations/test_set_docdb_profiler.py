"""Tests for set_docdb_profiler — the DocumentDB Mongo-protocol profiler write.

These MUST run without pymongo installed: the tool imports pymongo lazily inside
its _CLIENT_FACTORY, and we patch the module-level _CLIENT_FACTORY hook with a
fake client. We also patch _write_creds / lookup_cluster so no AWS is touched.

Covered: 3-state flow (approval_required / approval_denied / modified), no-write-
secret → unsupported_engine no-op, level/slowms validation (no connect), idempotent
no-change, TOCTOU drift, never-raise on connect/command error. The FAIL-CLOSED
engine gate lives in the handler (see test_operations_engine_gate.py)."""

from unittest.mock import MagicMock, patch

import mcp_servers.operations.tools.set_docdb_profiler as mod
from mcp_servers.operations.tools.set_docdb_profiler import set_docdb_profiler_impl

_CREDS = {"host": "docdb.local", "port": 27017, "username": "rw", "password": "pw"}


class _FakeDB:
    """Fake `client[db]` exposing .command for profile read (-1) + write."""

    def __init__(self, profile_seq, write_spy, raise_on_write=False):
        self._profile_seq = list(profile_seq)
        self._write_spy = write_spy
        self._raise_on_write = raise_on_write

    def command(self, name, *args, **kwargs):
        assert name == "profile", f"unexpected command: {name}"
        # Read form: profile, -1
        if args and args[0] == -1:
            return self._profile_seq.pop(0)
        # Write form: profile, level, slowms=...
        if self._raise_on_write:
            raise RuntimeError("write boom")
        self._write_spy(args[0], kwargs.get("slowms"))
        return {"ok": 1}


class _FakeClient:
    def __init__(self, profile_seq, write_spy, raise_on_write=False, raise_on_connect=False):
        if raise_on_connect:
            raise RuntimeError("connection refused")
        self._db = _FakeDB(profile_seq, write_spy, raise_on_write)
        self.closed = False

    def __getitem__(self, db_name):
        return self._db

    def close(self):
        self.closed = True


def _factory(profile_seq, write_spy, **kw):
    def make(host, port, username, password):
        return _FakeClient(profile_seq, write_spy, **kw)

    return make


def _with_creds():
    """Patch _write_creds to return valid creds (skips Secrets Manager)."""
    return patch.object(mod, "_write_creds", lambda cid: (_CREDS, None))


# ===== no write secret → unsupported_engine no-op =====


def test_no_write_secret_unsupported_no_op():
    """A documentdb cluster with no mongo_write_secret_arn → unsupported_engine,
    no Mongo connection ever attempted."""
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with patch.object(mod, "lookup_cluster", lambda cid: {}), patch.object(
        mod, "_CLIENT_FACTORY", factory
    ):
        result = set_docdb_profiler_impl(MagicMock(), cluster_id="docdb-1", level=1)
    assert result["status"] == "unsupported_engine"
    assert result["reason"] == "no write credentials configured"
    factory.assert_not_called()


# ===== validation (before any connect) =====


def test_invalid_level_rejected_no_connect():
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", factory):
        result = set_docdb_profiler_impl(MagicMock(), cluster_id="docdb-1", level=3)
    assert result["status"] == "error"
    assert "level" in result["reason"]
    factory.assert_not_called()


def test_negative_slowms_rejected_no_connect():
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", factory):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", level=1, slowms=-5
        )
    assert result["status"] == "error"
    assert "slowms" in result["reason"]
    factory.assert_not_called()


# ===== 3-state flow =====


def test_requires_approval():
    write_spy = MagicMock()
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([{"was": 0, "slowms": 100}], write_spy)
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", db="app", level=1, slowms=50
        )
    assert result["status"] == "approval_required"
    assert result["level"] == 1 and result["slowms"] == 50 and result["db"] == "app"
    write_spy.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_executes_when_approved():
    write_spy = MagicMock()
    # request-time read + TOCTOU re-read both return level 0 (no drift).
    seq = [{"was": 0, "slowms": 100}, {"was": 0, "slowms": 100}]
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", _factory(seq, write_spy)):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", db="app", level=1, slowms=50, approved=True
        )
    assert result["status"] == "modified"
    assert result["level"] == 1 and result["slowms"] == 50
    write_spy.assert_called_once_with(1, 50)


def test_approval_denied_when_guard_rejects():
    write_spy = MagicMock()
    seq = [{"was": 0, "slowms": 100}]
    # No bypass env → verify_approval returns not-ok (no approval row).
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", _factory(seq, write_spy)), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": False, "reason": "nope"}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", level=1, approved=True, approval_id="x"
        )
    assert result["status"] == "approval_denied"
    write_spy.assert_not_called()


# ===== idempotent =====


def test_idempotent_skip_already_at_level():
    write_spy = MagicMock()
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([{"was": 1, "slowms": 50}], write_spy)
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", level=1, slowms=50
        )
    assert result["status"] == "skipped"
    write_spy.assert_not_called()


def test_idempotent_skip_level_zero_ignores_slowms():
    """level 0 (off) → slowms is irrelevant; already-off is a no-change skip."""
    write_spy = MagicMock()
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([{"was": 0, "slowms": 999}], write_spy)
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", level=0, slowms=100
        )
    assert result["status"] == "skipped"
    write_spy.assert_not_called()


# ===== TOCTOU =====


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_toctou_drift_denied():
    """fix #6: profiling status drifted between request-time read and the execute-
    time re-read → approval_denied, no write."""
    write_spy = MagicMock()
    # request-time: level 0; re-read: level 1 (someone else changed it).
    seq = [{"was": 0, "slowms": 100}, {"was": 1, "slowms": 50}]
    with _with_creds(), patch.object(mod, "_CLIENT_FACTORY", _factory(seq, write_spy)):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", level=2, slowms=50, approved=True
        )
    assert result["status"] == "approval_denied"
    assert "changed since approval" in result["reason"]
    write_spy.assert_not_called()


# ===== never-raise =====


def test_connect_failure_is_error_not_raise():
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory([], MagicMock(), raise_on_connect=True)
    ):
        result = set_docdb_profiler_impl(MagicMock(), cluster_id="docdb-1", level=1)
    assert result["status"] == "error"
    assert "연결 실패" in result["reason"]


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
def test_write_failure_is_error_not_raise():
    seq = [{"was": 0, "slowms": 100}, {"was": 0, "slowms": 100}]
    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", _factory(seq, MagicMock(), raise_on_write=True)
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", level=1, approved=True
        )
    assert result["status"] == "error"
    assert "프로파일러 설정 실패" in result["reason"]


def test_read_failure_is_error_not_raise():
    """A failing request-time profile read → error, never raises."""

    class _BoomDB:
        def command(self, *a, **k):
            raise RuntimeError("read boom")

    class _BoomClient:
        def __getitem__(self, _):
            return _BoomDB()

        def close(self):
            pass

    with _with_creds(), patch.object(
        mod, "_CLIENT_FACTORY", lambda *a, **k: _BoomClient()
    ):
        result = set_docdb_profiler_impl(MagicMock(), cluster_id="docdb-1", level=1)
    assert result["status"] == "error"
