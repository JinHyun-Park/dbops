"""Regression: the chat-session / memory / onboarding / tasks / scheduled-task
REST handlers must not echo AWS exception text into their response.

Every one of these was `except ...: return {"error": str(e)[:200]}`. A DynamoDB
or RDS Data API failure spells out the hub account id, the platform IAM role
name, the target table ARN and (for scheduled_tasks) the SQL statement, and
these bodies render straight into the browser.

The fix is message content only: a static Korean reason in the payload, the
exception to CloudWatch via print. These tests inject a fault carrying all four
identifiers and assert none of it reaches the body, while the status codes stay
exactly where they were.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_API = Path(__file__).resolve().parents[3] / "api"

# tasks / scheduled_tasks do `import tenancy` from their own Lambda dir.
for _d in ("tasks", "scheduled_tasks"):
    sys.path.insert(0, str(_API / _d))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_leakcheck", _API / name / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chat = _load("chat_sessions")
memory = _load("memory")
onboarding = _load("onboarding")
tasks = _load("tasks")
sched = _load("scheduled_tasks")


HUB_ACCOUNT = "999988887777"
PLATFORM_ROLE = "dbops-prod-api-role"
TARGET_ARN = f"arn:aws:dynamodb:ap-northeast-2:{HUB_ACCOUNT}:table/dbops-prod-sessions"
SECRET_FAULT = (
    f"User: arn:aws:sts::{HUB_ACCOUNT}:assumed-role/{PLATFORM_ROLE}/api is not "
    f"authorized to perform: dynamodb:Query on resource: {TARGET_ARN}; "
    'SQL: SELECT id FROM scheduled_tasks WHERE cluster_id = :cid'
)


def _boom(code="AccessDeniedException"):
    return ClientError({"Error": {"Code": code, "Message": SECRET_FAULT}}, "Query")


def _assert_clean(body: str, where: str):
    for needle in (
        SECRET_FAULT, HUB_ACCOUNT, PLATFORM_ROLE, TARGET_ARN,
        "assumed-role", "not authorized to perform", "scheduled_tasks WHERE",
    ):
        assert needle not in body, f"{needle!r} leaked into {where}"


def _jwt(claims=None) -> str:
    payload = claims or {"sub": "user-a", "cognito:username": "alice",
                         "cognito:groups": ["dbops-admin"]}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, path="/api/x", path_params=None, qs=None, body=None):
    e = {
        "httpMethod": method,
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "headers": {"authorization": f"Bearer {_jwt()}"},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SESSIONS_TABLE", "sessions")
    monkeypatch.setenv("MEMORY_ID", "mem-123")
    monkeypatch.setenv("AGENT_TASKS_TABLE", "t")
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "a")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "b")
    monkeypatch.delenv("CLUSTERS_TABLE", raising=False)


def _chat_table(**kw):
    table = MagicMock()
    for op in ("query", "get_item", "put_item", "delete_item"):
        setattr(table, op, MagicMock(**kw.get(op, {"side_effect": _boom()})))
    return table


def _chat(method, table, path_params=None):
    with patch.object(chat, "_table", return_value=table):
        return chat.lambda_handler(_event(method, path_params=path_params,
                                          body={} if method == "PUT" else None), None)


def _case_chat_list():
    return _chat("GET", _chat_table())


def _case_chat_get():
    return _chat("GET", _chat_table(), {"id": "s1"})


def _case_chat_put_write():
    """get_item succeeds (ownership check passes), the WRITE fails."""
    table = _chat_table(get_item={"return_value": {}})
    return _chat("PUT", table, {"id": "s1"})


def _case_chat_delete_write():
    table = _chat_table(
        get_item={"return_value": {"Item": {"session_id": "s1", "user_id": "user-a"}}}
    )
    return _chat("DELETE", table, {"id": "s1"})


def _case_memory_list():
    ac = MagicMock(list_memory_records=MagicMock(side_effect=_boom()))
    with patch.object(memory, "_agentcore", return_value=ac):
        return memory.lambda_handler(_event("GET", qs={"kind": "facts"}), None)


def _case_memory_delete():
    ac = MagicMock(delete_memory_record=MagicMock(side_effect=_boom()))
    with patch.object(memory, "_agentcore", return_value=ac):
        return memory.lambda_handler(
            _event("DELETE", path_params={"id": "rec-1"}, qs={"kind": "facts"}), None
        )


def _case_onboarding():
    sts = MagicMock(get_caller_identity=MagicMock(side_effect=_boom()))
    with patch.object(onboarding, "boto3", MagicMock(client=lambda *_a, **_k: sts)):
        return onboarding.lambda_handler(_event("GET"), None)


def _tasks(method, path="/api/tasks", **kw):
    table = MagicMock()
    for op in ("query", "get_item", "put_item"):
        setattr(table, op, MagicMock(side_effect=_boom()))
    with patch.object(tasks, "_table", return_value=table):
        return tasks.lambda_handler(_event(method, path=path, **kw), None)


def _case_tasks_stats():
    return _tasks("GET", path="/api/tasks/stats")


def _case_tasks_get():
    return _tasks("GET", path_params={"id": "task-1"})


def _case_tasks_list():
    return _tasks("GET")


def _case_tasks_post():
    return _tasks("POST", body={"cluster_id": "c1", "kind": "manual_rca"})


def _case_sched_list():
    with patch.object(sched, "_query", side_effect=_boom()):
        return sched.lambda_handler(_event("GET"), None)


def _case_sched_post():
    with patch.object(sched, "_query", side_effect=_boom()), \
         patch.object(sched, "_cluster_exists", return_value=True):
        return sched.lambda_handler(
            _event("POST", body={"cluster_id": "c1", "interval_kind": "daily"}), None
        )


def _case_sched_delete_select():
    with patch.object(sched, "_query", side_effect=_boom()):
        return sched.lambda_handler(_event("DELETE", path_params={"id": "5"}), None)


def _case_sched_delete_write():
    """The SELECT succeeds, the DELETE fails: a distinct static reason."""
    with patch.object(sched, "_query",
                      side_effect=[[{"id": 5, "cluster_id": "c1"}], _boom()]):
        return sched.lambda_handler(_event("DELETE", path_params={"id": "5"}), None)


_CASES = {
    "chat_list": _case_chat_list,
    "chat_get": _case_chat_get,
    "chat_put_write": _case_chat_put_write,
    "chat_delete_write": _case_chat_delete_write,
    "memory_list": _case_memory_list,
    "memory_delete": _case_memory_delete,
    "onboarding": _case_onboarding,
    "tasks_stats": _case_tasks_stats,
    "tasks_get": _case_tasks_get,
    "tasks_list": _case_tasks_list,
    "tasks_post": _case_tasks_post,
    "sched_list": _case_sched_list,
    "sched_post": _case_sched_post,
    "sched_delete_select": _case_sched_delete_select,
    "sched_delete_write": _case_sched_delete_write,
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_fault_response_carries_no_exception_text(case):
    resp = _CASES[case]()
    _assert_clean(resp["body"], case)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_fault_still_returns_500_with_a_reason(case):
    """Message content only: the 500 and the `error` field stay put, and the
    static reason is non-empty so the UI still shows something actionable."""
    resp = _CASES[case]()
    assert resp["statusCode"] == 500, case
    assert json.loads(resp["body"])["error"].strip(), case


def test_memory_keeps_the_bounded_aws_error_code():
    """The CODE is an enum, not free text, and it is what makes the reason
    actionable (AccessDeniedException tells the DBA it is a permissions gap)."""
    body = json.loads(_case_memory_list()["body"])["error"]
    assert "AccessDeniedException" in body


def test_memory_not_found_branch_unchanged():
    """The message was the only thing separating not-found from a real fault;
    the 404/200 branches must still be reached by the error CODE."""
    ac = MagicMock(delete_memory_record=MagicMock(
        side_effect=_boom("ResourceNotFoundException")))
    with patch.object(memory, "_agentcore", return_value=ac):
        resp = memory.lambda_handler(
            _event("DELETE", path_params={"id": "rec-1"}, qs={"kind": "facts"}), None)
    assert resp["statusCode"] == 404

    ac = MagicMock(list_memory_records=MagicMock(
        side_effect=_boom("ResourceNotFoundException")))
    with patch.object(memory, "_agentcore", return_value=ac):
        resp = memory.lambda_handler(_event("GET", qs={"kind": "facts"}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["records"] == []


def test_chat_write_and_ownership_failures_read_differently():
    """Two DDB failures inside the same PUT: the ownership read and the write.
    The static text has to keep telling them apart."""
    write = json.loads(_case_chat_put_write()["body"])["error"]
    ownership = json.loads(_chat("PUT", _chat_table(), {"id": "s1"})["body"])["error"]
    assert write != ownership


def test_sched_delete_steps_read_differently():
    select = json.loads(_case_sched_delete_select()["body"])["error"]
    write = json.loads(_case_sched_delete_write()["body"])["error"]
    assert select != write
