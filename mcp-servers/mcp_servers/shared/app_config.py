"""Read DB-backed feature config with an env/default fallback.

Resolution precedence for a key:
  1. the stored value in the dbops-{env}-app-config DynamoDB table (admin-set
     via GET/PUT /api/config), if present;
  2. the environment variable of the same name (the deploy-time default);
  3. the caller-supplied default.

Values are cached per-key for a short TTL so the hot path doesn't hit DDB on
every call; a freshly-changed setting takes effect within the TTL on a warm
container. NEVER raises — any DDB/permission error falls back to env/default,
because this gates opt-in features and must not break the work it wraps.
"""

import os
import time

import boto3

_TTL_SECONDS = 60
_CACHE: dict = {}  # key -> (value_or_None, expiry_epoch)


def _table():
    return boto3.resource("dynamodb").Table(os.environ["APP_CONFIG_TABLE"])


def _stored(key: str):
    """Return the stored string value for key, or None. Cached; never raises."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and hit[1] > now:
        return hit[0]
    value = None
    try:
        if os.environ.get("APP_CONFIG_TABLE"):
            item = _table().get_item(Key={"config_key": key}).get("Item")
            if item is not None:
                value = item.get("value")
    except Exception as e:  # noqa: BLE001 - fail-safe by design
        print(f"[app-config] read failed for {key}: {type(e).__name__}: {e}")
        value = None
    _CACHE[key] = (value, now + _TTL_SECONDS)
    return value


def get_config(key: str, default: str) -> str:
    """Resolve key: DB value -> env var of same name -> default."""
    stored = _stored(key)
    if stored is not None:
        return stored
    return os.environ.get(key, default)
