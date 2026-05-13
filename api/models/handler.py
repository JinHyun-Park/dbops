"""List Bedrock inference profiles across regions and return only the latest
Claude generations (Sonnet 4.5+, Opus 4.5+, Haiku 4.5+) — older profiles are
intentionally hidden so the chat dropdown never surfaces deprecated picks.

Cross-region scan is required because AWS rolls new Claude generations out to
us-east-1/eu-central-1 first; ap-northeast-2 lags by 1-2 release cycles. Since
the AgentCore Runtime role grants bedrock:InvokeModel Resource:"*", invoking a
us.* or eu.* profile from an APAC runtime works as long as the profile exists.
"""

import json
import os

import boto3

_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
_DEFAULT_PROFILE = os.environ.get(
    "DEFAULT_MODEL_ID",
    "apac.anthropic.claude-sonnet-4-20250514-v1:0",
)
_SCAN_REGIONS = [
    r.strip()
    for r in os.environ.get(
        "MODEL_SCAN_REGIONS",
        "ap-northeast-2,us-east-1,us-west-2,eu-central-1",
    ).split(",")
    if r.strip()
]

# Match the model family + minimum version we consider "latest". Substrings
# matched against the profile ID (case-insensitive). Anything not in this
# allowlist is filtered out, no matter what region it lives in.
_LATEST_MARKERS = (
    # Sonnet 4.5 and forward
    "sonnet-4-5", "sonnet-4-6", "sonnet-4-7",
    # Opus 4.5 and forward
    "opus-4-5", "opus-4-6", "opus-4-7",
    # Haiku 4.5 and forward
    "haiku-4-5", "haiku-4-6", "haiku-4-7",
)

# Friendly label patterns — first match wins.
_LABEL_RULES = [
    ("opus-4-7", "Opus 4.7"),
    ("opus-4-6", "Opus 4.6"),
    ("opus-4-5", "Opus 4.5"),
    ("sonnet-4-7", "Sonnet 4.7"),
    ("sonnet-4-6", "Sonnet 4.6"),
    ("sonnet-4-5", "Sonnet 4.5"),
    ("haiku-4-7", "Haiku 4.7"),
    ("haiku-4-6", "Haiku 4.6"),
    ("haiku-4-5", "Haiku 4.5"),
]

# Stable sort: Opus → Sonnet → Haiku, newer → older, default region first.
_FAMILY_RANK = {"opus": 0, "sonnet": 1, "haiku": 2}


def _cors():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _response(status, body):
    return {"statusCode": status, "headers": _cors(), "body": json.dumps(body, default=str)}


def _is_latest(profile_id: str) -> bool:
    lower = profile_id.lower()
    return any(m in lower for m in _LATEST_MARKERS)


def _label(profile_id: str) -> str:
    lower = profile_id.lower()
    for needle, name in _LABEL_RULES:
        if needle in lower:
            return name
    return profile_id


def _rank_by_label(label: str) -> tuple:
    lower = label.lower()
    family = "haiku" if "haiku" in lower else "opus" if "opus" in lower else "sonnet"
    # version score: extract "4.7" -> 47, "4.6" -> 46, etc., negate so higher first
    version = 0
    for token in ("4.7", "4.6", "4.5"):
        if token in lower:
            version = -int(token.replace(".", ""))
            break
    return (_FAMILY_RANK[family], version, label)


def _rank(profile_id_or_summary) -> tuple:
    # Backwards-compat for SYSTEM_DEFINED fallback path: id contains the model name.
    if isinstance(profile_id_or_summary, str):
        return _rank_by_label(profile_id_or_summary)
    return _rank_by_label(profile_id_or_summary.get("label", ""))


def _scan_application_profiles(region: str) -> list:
    """Return tagged DBOps Application Inference Profiles. Spend through these
    is automatically attributed via cost-allocation tags."""
    try:
        bedrock = boto3.client("bedrock", region_name=region)
        out = []
        next_token = None
        while True:
            kwargs = {"typeEquals": "APPLICATION", "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = bedrock.list_inference_profiles(**kwargs)
            for p in resp.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                arn = p.get("inferenceProfileArn", "")
                name = p.get("inferenceProfileName", "")
                if not name or not name.startswith("dbops-"):
                    continue
                # Friendly label: "dbops-dev-sonnet-4-6" -> "Sonnet 4.6"
                short = name.split("-", 2)[-1] if name.count("-") >= 2 else name
                label_parts = short.split("-")
                if len(label_parts) >= 2:
                    family = label_parts[0].capitalize()
                    version = ".".join(label_parts[1:])
                    label = f"{family} {version}"
                else:
                    label = short.title()
                out.append({
                    "id": arn,
                    "label": label,
                    "region": region,
                    "status": p.get("status", ""),
                    "tagged": True,
                })
            next_token = resp.get("nextToken")
            if not next_token:
                break
        return out
    except Exception as e:
        print(f"[models] app profile scan {region} failed: {e}")
        return []


def _scan_region(region: str) -> list:
    try:
        bedrock = boto3.client("bedrock", region_name=region)
        out = []
        next_token = None
        while True:
            kwargs = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = bedrock.list_inference_profiles(**kwargs)
            for p in resp.get("inferenceProfileSummaries", []):
                pid = p.get("inferenceProfileId", "")
                if not pid:
                    continue
                if "anthropic" not in pid.lower():
                    continue
                if not pid.startswith(("apac.", "us.", "eu.", "global.")):
                    continue
                if not _is_latest(pid):
                    continue
                out.append({
                    "id": pid,
                    "label": _label(pid),
                    "region": region,
                    "status": p.get("status", ""),
                })
            next_token = resp.get("nextToken")
            if not next_token:
                break
        return out
    except Exception as e:
        print(f"[models] scan {region} failed: {e}")
        return []


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method") \
        or event.get("httpMethod", "GET")
    if method != "GET":
        return _response(405, {"error": f"method {method} not allowed"})

    # Tagged Application Inference Profiles take precedence — invocations
    # through them are automatically attributed in Cost Explorer. We only
    # scan the home region; AIPs are created there by CDK.
    tagged = _scan_application_profiles(_REGION)
    if tagged:
        models = sorted(tagged, key=lambda p: _rank_by_label(p["label"]))
        default_id = next(
            (m["id"] for m in models if "sonnet" in m["label"].lower()),
            models[0]["id"],
        )
        return _response(200, {
            "default": default_id,
            "region": _REGION,
            "scanned_regions": [_REGION],
            "models": models,
            "tagged": True,
        })

    # Fallback: untagged SYSTEM_DEFINED profiles. Spend won't be attributed,
    # but at least chat keeps working before CDK has populated AIPs.
    all_profiles = []
    for region in _SCAN_REGIONS:
        all_profiles.extend(_scan_region(region))

    PREFIX_RANK = {"global.": 0, "us.": 1, "eu.": 2, "apac.": 3}

    def prefix(pid: str) -> str:
        for pfx in PREFIX_RANK:
            if pid.startswith(pfx):
                return pfx
        return "zzz."

    # Group by friendly label (Opus 4.7, Sonnet 4.6, etc.) — same generation,
    # pick the best prefix. This hides "Opus 4.7 (us)" + "Opus 4.7 (eu)" duplicates.
    grouped: dict[str, dict] = {}
    for p in all_profiles:
        key = p["label"]
        cur = grouped.get(key)
        if cur is None or PREFIX_RANK[prefix(p["id"])] < PREFIX_RANK[prefix(cur["id"])]:
            grouped[key] = p

    models = sorted(grouped.values(), key=lambda p: _rank(p["id"]))

    if not models:
        # Fallback so chat doesn't break if every region scan failed.
        return _response(200, {
            "default": _DEFAULT_PROFILE,
            "region": _REGION,
            "models": [{"id": _DEFAULT_PROFILE, "label": "Sonnet 4 (fallback)", "region": _REGION, "status": "ACTIVE"}],
        })

    # Pick a default: prefer Sonnet (most common) over Opus/Haiku for the home region.
    default_id = next(
        (m["id"] for m in models if "sonnet" in m["id"].lower() and m["region"] == _REGION),
        models[0]["id"],
    )

    return _response(200, {
        "default": default_id,
        "region": _REGION,
        "scanned_regions": _SCAN_REGIONS,
        "models": models,
    })
