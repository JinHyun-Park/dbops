"""The WS-ticket handshake: minting, and the authorizer that consumes it.

This replaced "the Cognito access token rides the query string", so the properties
under test are the ones that made that replacement worth doing:

  a ticket is SINGLE USE            two handshakes with one ticket -> one Allow
  expiry is checked by the CODE     not by DynamoDB TTL, which lags up to 48h
  everything unknown is a DENY      no ticket, unknown, spent, expired, no table

The authorizer is driven against an in-memory table. What the single-use guarantee
actually rests on is NOT the ConditionExpression: DeleteItem on an absent key
succeeds in real DynamoDB and simply returns no Attributes, and DeleteItem is
atomic per item, so of two concurrent handshakes exactly one receives the old
image. The guarantee is "an empty old image is a Deny".

That distinction was measured, not assumed: removing the ConditionExpression left
every test here green, which is why
test_single_use_survives_the_condition_being_removed drives the property against a
table that does not honour conditions at all.
"""

import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[3]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


authorizer = _load("ws_authorizer_h", "api/ws_authorizer/handler.py")
minter = _load("ws_ticket_h", "api/ws_ticket/handler.py")


class _Table:
    """Just enough DynamoDB: an atomic delete that returns the old image once, and
    a ConditionalCheckFailed when a condition is supplied for a row that is gone."""

    def __init__(self):
        self.rows = {}
        self.deletes = 0

    def put_item(self, Item):
        self.rows[Item["ticket"]] = dict(Item)

    def delete_item(self, Key, ConditionExpression=None, ReturnValues=None):
        self.deletes += 1
        key = Key["ticket"]
        if ConditionExpression and key not in self.rows:
            # The real service raises this. It gives the authorizer something
            # explicit to log; it is NOT what makes the ticket single-use.
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "The conditional request failed"}},
                "DeleteItem",
            )
        old = self.rows.pop(key, None)
        return {"Attributes": old} if (old and ReturnValues == "ALL_OLD") else {}


def _authorize(table, ticket, env_table="tickets"):
    with patch.dict(os.environ, {"WS_TICKETS_TABLE": env_table}, clear=False), \
         patch.object(authorizer, "_ddb") as ddb:
        ddb.Table.return_value = table
        return authorizer.lambda_handler(
            {"methodArn": "arn:x", "queryStringParameters": {"ticket": ticket}}, None)


def _effect(resp):
    return resp["policyDocument"]["Statement"][0]["Effect"]


def _seed(table, ticket="t-1", sub="user-1", ttl_offset=60):
    now = int(time.time())
    table.put_item(Item={"ticket": ticket, "sub": sub,
                         "expires_at": now + ttl_offset, "ttl": now + 300})


# ---------------------------------------------------------------------------
# the authorizer
# ---------------------------------------------------------------------------


def test_a_valid_ticket_is_allowed_and_carries_the_subject():
    t = _Table()
    _seed(t)
    resp = _authorize(t, "t-1")
    assert _effect(resp) == "Allow", resp
    assert resp["context"]["sub"] == "user-1"
    assert resp["principalId"] == "user-1"


def test_a_ticket_is_single_use():
    """The property the whole pattern rests on. The delete IS the consume, so the
    second handshake must lose even though it presents a ticket that was valid a
    moment ago."""
    t = _Table()
    _seed(t)
    first = _authorize(t, "t-1")
    second = _authorize(t, "t-1")
    assert _effect(first) == "Allow"
    assert _effect(second) == "Deny", "a replayed ticket was accepted"
    assert t.rows == {}, "the row must be gone after the first use"


def test_an_expired_ticket_is_denied_even_though_the_row_still_exists():
    """DynamoDB TTL deletion is best-effort and can lag by up to 48 hours, so the
    row being present says nothing about the ticket being live. The code compares
    expires_at against its own clock; if it did not, a 60-second ticket would
    quietly become a 48-hour one."""
    t = _Table()
    _seed(t, ttl_offset=-1)
    resp = _authorize(t, "t-1")
    assert _effect(resp) == "Deny", resp
    # Still consumed: an expired ticket is spent either way, so a client cannot
    # retry with it.
    assert t.rows == {}


@pytest.mark.parametrize("ticket", ["", "   ", "no-such-ticket"])
def test_missing_or_unknown_tickets_are_denied(ticket):
    t = _Table()
    _seed(t)
    resp = _authorize(t, ticket)
    assert _effect(resp) == "Deny", (ticket, resp)
    assert resp["context"]["sub"] == ""


def test_an_unconfigured_table_denies_rather_than_erroring():
    t = _Table()
    _seed(t)
    with patch.dict(os.environ, {"WS_TICKETS_TABLE": ""}, clear=False):
        resp = authorizer.lambda_handler(
            {"methodArn": "arn:x", "queryStringParameters": {"ticket": "t-1"}}, None)
    assert _effect(resp) == "Deny"


def test_an_unexpected_ddb_error_denies():
    class Boom:
        def delete_item(self, **_):
            raise RuntimeError("network")
    resp = _authorize(Boom(), "t-1")
    assert _effect(resp) == "Deny"


def test_a_row_without_a_subject_is_denied():
    """A ticket that cannot identify anyone must not be turned into an anonymous
    Allow, which would put an unidentified socket on the fleet-wide feed."""
    t = _Table()
    now = int(time.time())
    t.put_item(Item={"ticket": "t-1", "expires_at": now + 60, "ttl": now + 300})
    assert _effect(_authorize(t, "t-1")) == "Deny"


def test_a_non_numeric_expiry_is_denied_not_crashed():
    t = _Table()
    t.put_item(Item={"ticket": "t-1", "sub": "u", "expires_at": "soon", "ttl": 0})
    assert _effect(_authorize(t, "t-1")) == "Deny"


# ---------------------------------------------------------------------------
# the minting endpoint
# ---------------------------------------------------------------------------


def _bearer(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"Bearer header.{payload}.sig"


def _mint(headers, env_table="tickets", table=None):
    t = table if table is not None else _Table()
    with patch.dict(os.environ, {"WS_TICKETS_TABLE": env_table}, clear=False), \
         patch.object(minter, "boto3") as b:
        b.resource.return_value.Table.return_value = t
        resp = minter.lambda_handler(
            {"headers": headers, "requestContext": {"http": {"method": "POST"}}}, None)
    return resp, t


def test_minting_returns_a_ticket_bound_to_the_caller():
    resp, table = _mint({"authorization": _bearer({"sub": "user-9"})})
    assert resp["statusCode"] == 200, resp
    body = json.loads(resp["body"])
    assert body["ticket"]
    assert body["expires_in"] == minter.TICKET_TTL_SECONDS
    row = table.rows[body["ticket"]]
    assert row["sub"] == "user-9"
    # expires_at is what the authorizer checks; ttl only reaps and must be later.
    assert row["expires_at"] > int(time.time())
    assert row["ttl"] > row["expires_at"], "the reap must not precede the expiry"


def test_a_ticket_is_never_cacheable():
    """It is single-use and its whole value is freshness; an intermediary that
    cached the response would hand the same ticket to two clients."""
    resp, _ = _mint({"authorization": _bearer({"sub": "u"})})
    assert resp["headers"]["Cache-Control"] == "no-store"


def test_tickets_are_unique_and_not_guessable_in_length():
    seen = set()
    for _ in range(25):
        resp, _ = _mint({"authorization": _bearer({"sub": "u"})})
        seen.add(json.loads(resp["body"])["ticket"])
    assert len(seen) == 25, "ticket values repeated"
    assert all(len(t) >= 32 for t in seen), "ticket too short to resist guessing"


def test_no_identity_means_no_ticket():
    for headers in ({}, {"authorization": "Basic abc"}, {"authorization": "Bearer junk"}):
        resp, table = _mint(headers)
        assert resp["statusCode"] == 401, (headers, resp)
        assert table.rows == {}, "a ticket was minted for an unidentified caller"


def test_an_unconfigured_table_reports_unavailable_and_mints_nothing():
    resp, table = _mint({"authorization": _bearer({"sub": "u"})}, env_table="")
    assert resp["statusCode"] == 503
    assert table.rows == {}


def test_a_write_failure_leaks_no_detail_and_mints_nothing():
    class Boom:
        rows = {}

        def put_item(self, Item):
            raise RuntimeError("arn:aws:dynamodb:ap-northeast-2:123456789012:table/x")
    resp, _ = _mint({"authorization": _bearer({"sub": "u"})}, table=Boom())
    assert resp["statusCode"] == 500
    blob = json.dumps(resp)
    for leak in ("arn:aws", "123456789012", "RuntimeError"):
        assert leak not in blob, f"leaked {leak}: {resp}"


# ---------------------------------------------------------------------------
# the two halves together
# ---------------------------------------------------------------------------


def test_mint_then_authorize_then_replay():
    """The end-to-end contract, with the REAL minting code feeding the REAL
    authorizer over one table."""
    table = _Table()
    resp, _ = _mint({"authorization": _bearer({"sub": "user-7"})}, table=table)
    ticket = json.loads(resp["body"])["ticket"]

    ok = _authorize(table, ticket)
    assert _effect(ok) == "Allow"
    assert ok["context"]["sub"] == "user-7"

    replay = _authorize(table, ticket)
    assert _effect(replay) == "Deny"


def test_the_access_token_no_longer_opens_a_socket():
    """The regression that matters: presenting a Cognito access token the way the
    old client did must not authorize anything now."""
    table = _Table()
    _seed(table)
    with patch.dict(os.environ, {"WS_TICKETS_TABLE": "tickets"}, clear=False), \
         patch.object(authorizer, "_ddb") as ddb:
        ddb.Table.return_value = table
        resp = authorizer.lambda_handler(
            {"methodArn": "arn:x",
             # Deliberately NOT a JWT-shaped literal: the assertion is that the
             # `token` PARAM is no longer an identity source, so the value's shape
             # is irrelevant, and a realistic-looking one trips the secret scanner
             # on every future run for no test value.
             "queryStringParameters": {"token": "former-identity-source"}},
            None)
    assert _effect(resp) == "Deny", resp
    assert table.deletes == 0, "a token-bearing handshake must not even touch a ticket"


def test_the_authorizer_never_calls_cognito_anymore():
    """It used to call GetUser on the token. If that came back, the token would be
    back in the URL, so the absence is asserted rather than assumed.

    Checked over the AST, not the text: the docstring deliberately explains the
    Cognito history, and a substring scan flags that prose. A guard that fails on
    its own explanation gets deleted.
    """
    import ast

    tree = ast.parse((_ROOT / "api/ws_authorizer/handler.py").read_text())
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # boto3.client("cognito-idp") / boto3.resource("cognito-...")
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and "cognito" in arg.value.lower():
                calls.append(arg.value)
        # <anything>.get_user(...)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get_user":
            calls.append("get_user")
    assert not calls, f"the authorizer is talking to Cognito again: {calls}"


def test_single_use_survives_the_condition_being_removed():
    """What actually enforces single use, pinned separately from the condition.

    Measured: deleting the ConditionExpression left every other test in this file
    green, so the condition is NOT the guarantee. DeleteItem on an absent key
    succeeds in real DynamoDB and simply returns no Attributes, and DeleteItem is
    atomic per item — so of two concurrent handshakes exactly one receives the old
    image. The guarantee is "an empty old image is a Deny", and that is what this
    test drives, with a table that does NOT honour conditions at all.
    """
    class NoConditionTable:
        """DynamoDB WITHOUT condition support: the pessimistic case."""

        def __init__(self):
            now = int(time.time())
            self.rows = {"t-1": {"ticket": "t-1", "sub": "u",
                                 "expires_at": now + 60, "ttl": now + 300}}

        def delete_item(self, Key, ConditionExpression=None, ReturnValues=None):
            old = self.rows.pop(Key["ticket"], None)
            return {"Attributes": old} if old else {}

    table = NoConditionTable()
    first = _authorize(table, "t-1")
    second = _authorize(table, "t-1")
    assert _effect(first) == "Allow"
    assert _effect(second) == "Deny", (
        "an empty old image must be a Deny; that, not the ConditionExpression, is "
        "what makes a ticket single-use"
    )


def test_an_empty_old_image_is_never_allowed():
    """The one line the guarantee above rests on, driven directly."""
    class AlwaysEmpty:
        def delete_item(self, **_):
            return {}
    assert _effect(_authorize(AlwaysEmpty(), "t-1")) == "Deny"
