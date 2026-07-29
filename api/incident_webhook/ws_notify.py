"""Best-effort WebSocket broadcast of an alert/incident to connected clients.

Pushes the payload to every connection in the WS connections table via the API
Gateway Management API. Prunes stale (Gone) connections, never raises into the
caller, and is a no-op when the WS push channel isn't configured (env unset) —
so callers can broadcast unconditionally without guarding.

TWO THINGS THAT ARE NOT PREMATURE OPTIMISATION
----------------------------------------------
1. `alert_evaluator` calls this INSIDE its per-rule loop, so N fired rules used to
   mean N full table scans and N fresh boto3 clients in one invocation. Callers
   that broadcast more than once should now scan ONCE with `load_connections()`
   and pass the list to each `broadcast()`; the list is pruned IN PLACE, so a
   connection found Gone while pushing rule 1 is not retried for rule 2.

2. The management client carries EXPLICIT timeouts. Without them botocore's
   default read timeout is 60s, so a single unresponsive connection could stall
   an evaluator run past its own Lambda timeout and take the remaining
   notifications with it — turning one dead socket into a missed alert. A push to
   a live socket returns in milliseconds; anything slower is already lost.

Copied verbatim into each broadcasting Lambda's package (alert_evaluator,
incident_webhook) to avoid a shared layer; keep the copies in sync.
"""
import json
import os

import boto3
from botocore.config import Config

# A WS push is fire-and-forget to a socket that is either there or not. Short
# timeouts, and few retries: retrying a post that timed out mostly means pushing
# to a connection that has already gone away.
_MGMT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"max_attempts": 2, "mode": "standard"},
)

# Cached across invocations in a warm container: building a boto3 client costs
# tens of milliseconds, and the per-rule loop used to pay it every time.
_mgmt_cache = {}
_ddb_cache = {}


def _mgmt(endpoint):
    if endpoint not in _mgmt_cache:
        _mgmt_cache[endpoint] = boto3.client(
            "apigatewaymanagementapi", endpoint_url=endpoint, config=_MGMT_CONFIG
        )
    return _mgmt_cache[endpoint]


def _table(table_name):
    if table_name not in _ddb_cache:
        _ddb_cache[table_name] = boto3.resource("dynamodb").Table(table_name)
    return _ddb_cache[table_name]


def load_connections() -> list:
    """Every connection id in the table, or [] when unconfigured / on error.

    Call this ONCE per invocation when you are going to broadcast more than once,
    and hand the result to each `broadcast()`. Returns a mutable list on purpose:
    `broadcast` prunes Gone ids from it so later calls in the same invocation do
    not push to sockets already known to be dead.
    """
    table_name = os.environ.get("WS_CONNECTIONS_TABLE")
    if not table_name:
        return []
    ddb = _table(table_name)
    ids = []
    scan_kwargs = {"ProjectionExpression": "connection_id"}
    try:
        while True:
            resp = ddb.scan(**scan_kwargs)
            for it in resp.get("Items", []):
                cid = it.get("connection_id")
                if cid:
                    ids.append(cid)
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        print(f"[ws-notify] connections scan failed: {type(e).__name__}: {e}")
        return []
    return ids


def broadcast(payload: dict, connections=None) -> int:
    """Push `payload` to all connected WS clients; return the delivered count.

    `connections` is an optional pre-scanned list from `load_connections()`. When
    given it is used as-is and PRUNED IN PLACE for ids the gateway reports Gone.
    When omitted the table is scanned, which is the right shape for a caller that
    broadcasts once per invocation (incident_webhook).
    """
    table_name = os.environ.get("WS_CONNECTIONS_TABLE")
    endpoint = os.environ.get("WS_MGMT_ENDPOINT")
    if not table_name or not endpoint:
        return 0  # push channel not configured on this deployment
    ddb = _table(table_name)
    mgmt = _mgmt(endpoint)
    data = json.dumps(payload, default=str).encode("utf-8")

    own_list = connections is None
    ids = load_connections() if own_list else connections

    delivered = 0
    gone = []
    for cid in list(ids):
        try:
            mgmt.post_to_connection(ConnectionId=cid, Data=data)
            delivered += 1
        except mgmt.exceptions.GoneException:
            gone.append(cid)
            try:
                ddb.delete_item(Key={"connection_id": cid})
            except Exception:
                pass
        except Exception as e:
            print(f"[ws-notify] post_to_connection {cid[:8]} failed: {type(e).__name__}")
    if not own_list:
        # Prune the CALLER's list so the next broadcast in this invocation skips
        # sockets the gateway already told us are dead.
        for cid in gone:
            try:
                ids.remove(cid)
            except ValueError:
                pass
    return delivered
