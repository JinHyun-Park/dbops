"""List Bedrock inference profiles across regions and return only current Claude
generations, so the chat dropdown never surfaces a deprecated pick.

"Current" is a NUMERIC floor (_VERSION_FLOOR, currently 4.5) applied to a family and
version parsed out of the profile id, covering opus / sonnet / haiku / fable. It is
deliberately not a list of known-good version strings: that is what this file used to
do, and it silently hid every model released after the list was last edited.

Cross-region scan is required because AWS rolls new Claude generations out to
us-east-1/eu-central-1 first; ap-northeast-2 lags by 1-2 release cycles. Invoking
a us.* or eu.* profile from an APAC runtime works as long as the profile exists,
because the runtime role's bedrock grant uses a wildcard REGION on each resource
shape (cdk/stacks/agent_stack.py).

An earlier version of this docstring credited that to the role granting
`Resource:"*"`. It does not: that was a hand-added inline policy present on one
deployment and in no committed code. The distinction matters here because THIS
handler decides which ARN shape the agent is asked to invoke. When any tagged
Application Inference Profile exists, the tagged branch below returns
`application-inference-profile/...` ARNs, which are a resource TYPE that
`inference-profile/*` does not match. Adding a branch that returns a new ARN
shape means extending the grant in agent_stack.py, not just this file.
"""

import json
import os
import re

import boto3

_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
# Fallback only: CDK passes DEFAULT_MODEL_ID from Settings.AGENT_MODEL_ID. The literal
# matters for a local run or a mis-wired deploy, so keep it on a current model.
_DEFAULT_PROFILE = os.environ.get(
    "DEFAULT_MODEL_ID",
    "global.anthropic.claude-sonnet-5",
)
_SCAN_REGIONS = [
    r.strip()
    for r in os.environ.get(
        "MODEL_SCAN_REGIONS",
        "ap-northeast-2,us-east-1,us-west-2,eu-central-1",
    ).split(",")
    if r.strip()
]

# A model is "latest" if its family+version parses at or above this floor. Version is
# compared NUMERICALLY, not matched against a list of strings.
#
# It used to be a hardcoded allowlist of substrings (`sonnet-4-5`, `sonnet-4-6`,
# `sonnet-4-7`, and the same for opus/haiku) duplicated across three places: the
# filter, the label table, and the sort key. Measured 2026-09-03: the account had
# claude-opus-5, claude-sonnet-5, claude-opus-4-8 and claude-fable-5-1 all ACTIVE and
# callable, and every one of them was invisible in the picker because the allowlist
# stopped at 4-7. The failure is silent, which is what makes it bad: a new model just
# never appears, and nothing logs a reason.
_FAMILY_RANK = {"opus": 0, "sonnet": 1, "haiku": 2, "fable": 3}
_VERSION_FLOOR = (4, 5)

# Current id form:  claude-<family>-<major>[-<minor>]
#   claude-sonnet-5, claude-opus-4-8, claude-haiku-4-5-20251001-v1:0
# The minor group is capped at two digits AND must not be followed by another digit,
# so a trailing release date is not read as a minor version. Without the guard,
# `claude-sonnet-4-20250514` parses as 4.20250514, which sorts ABOVE 4.5 and would let
# an old model through the floor.
_NEW_FORM = re.compile(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2})(?!\d))?")
# Legacy id form (family AFTER the version): claude-3-5-sonnet, claude-3-haiku.
# Parsed only so the floor can exclude it; these never rank.
_OLD_FORM = re.compile(r"claude-(\d+)(?:-(\d{1,2})(?!\d))?-(opus|sonnet|haiku|fable)")
# Friendly label from an Application Inference Profile name: "Opus 5", "Sonnet 4.6".
_LABEL_FORM = re.compile(r"(opus|sonnet|haiku|fable)\s*([0-9]+)(?:\.([0-9]+))?", re.I)


def _parse_model(text: str):
    """(family, (major, minor)) from a profile id or a friendly label, else None."""
    low = (text or "").lower()
    m = _NEW_FORM.search(low)
    if m:
        return m.group(1), (int(m.group(2)), int(m.group(3) or 0))
    m = _OLD_FORM.search(low)
    if m:
        return m.group(3), (int(m.group(1)), int(m.group(2) or 0))
    m = _LABEL_FORM.search(low)
    if m:
        return m.group(1).lower(), (int(m.group(2)), int(m.group(3) or 0))
    return None


def _cors():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _response(status, body):
    return {"statusCode": status, "headers": _cors(), "body": json.dumps(body, default=str)}


def _is_latest(profile_id: str) -> bool:
    parsed = _parse_model(profile_id)
    if not parsed:
        return False
    family, version = parsed
    return family in _FAMILY_RANK and version >= _VERSION_FLOOR


def _label(profile_id: str) -> str:
    parsed = _parse_model(profile_id)
    if not parsed:
        return profile_id
    family, (major, minor) = parsed
    version = f"{major}.{minor}" if minor else str(major)
    return f"{family.capitalize()} {version}"


def _rank_by_label(label: str) -> tuple:
    """Opus before Sonnet before Haiku before Fable; newer version first."""
    parsed = _parse_model(label)
    if not parsed:
        return (len(_FAMILY_RANK), 0, label)
    family, (major, minor) = parsed
    # Negated so a higher version sorts first under an ascending sort.
    return (_FAMILY_RANK.get(family, len(_FAMILY_RANK)), -(major * 100 + minor), label)


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

    # Tagged Application Inference Profiles take precedence: invocations
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

    # Group by friendly label (Opus 5, Sonnet 4.6, etc.): same generation,
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
