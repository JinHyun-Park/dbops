"""Idempotent Application Inference Profile setup for DBOps.

Creates one AIP per supported Claude generation, tags them with the same
cost-allocation labels, and exports the AIP ARN map for the agent runtime to
consume via SSM Parameter Store. Re-runs are safe — existing profiles are
re-tagged in place.
"""

import json
import os

import boto3

_ENV = os.environ.get("ENV", "dev")
_SSM_PARAM = os.environ.get("AIP_SSM_PARAM", f"/dbops/{_ENV}/inference-profile-map")
# The in-app model picker is built from these and nothing else: api/models/handler.py
# lists every `dbops-` Application Inference Profile, so this list IS the menu the
# operator sees. Keep it curated (newest of each family plus one step back), not
# exhaustive: every entry is one more row in the dropdown.
#
# Each base id was verified callable in this account before being listed here
# (2026-09-03, ap-northeast-2): `converse_stream` returned text AND `converse` with a
# toolConfig returned a well-formed toolUse block with correct arguments. That second
# check is the one that matters, because a model can pick the right tool and still fail
# to GENERATE a valid tool-use sequence (measured previously with Nova Pro against this
# project's 65-tool surface), and the agent is useless without tool calls.
_BASE_MODELS = [
    ("opus-5", "global.anthropic.claude-opus-5", "Opus 5"),
    ("sonnet-5", "global.anthropic.claude-sonnet-5", "Sonnet 5"),
    ("opus-4-8", "global.anthropic.claude-opus-4-8", "Opus 4.8"),
    ("sonnet-4-6", "global.anthropic.claude-sonnet-4-6", "Sonnet 4.6"),
    ("fable-5-1", "global.anthropic.claude-fable-5-1", "Fable 5.1"),
    ("haiku-4-5", "global.anthropic.claude-haiku-4-5-20251001-v1:0", "Haiku 4.5"),
]
_TAGS = [
    {"key": "Application", "value": "DBOps"},
    {"key": "Environment", "value": _ENV},
    {"key": "ManagedBy", "value": "cdk"},
    {"key": "CostCategory", "value": "DBOps"},
]


def _arn_for(profile_id_or_arn: str, region: str, account: str) -> str:
    if profile_id_or_arn.startswith("arn:"):
        return profile_id_or_arn
    return f"arn:aws:bedrock:{region}:{account}:application-inference-profile/{profile_id_or_arn}"


def _find_existing(bedrock, name: str):
    next_token = None
    while True:
        kwargs = {"typeEquals": "APPLICATION", "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = bedrock.list_inference_profiles(**kwargs)
        for p in resp.get("inferenceProfileSummaries", []):
            if p.get("inferenceProfileName") == name:
                return p
        next_token = resp.get("nextToken")
        if not next_token:
            return None


def _list_managed(bedrock):
    """Every Application Inference Profile this stack owns, by name prefix.

    Scoped to `dbops-{ENV}-` on purpose: the account can hold AIPs created by other
    stacks or by hand, and the prune below must never be able to reach them.
    """
    prefix = f"dbops-{_ENV}-"
    found, next_token = [], None
    while True:
        kwargs = {"typeEquals": "APPLICATION", "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = bedrock.list_inference_profiles(**kwargs)
        for prof in resp.get("inferenceProfileSummaries", []):
            name = prof.get("inferenceProfileName") or ""
            if name.startswith(prefix):
                found.append(prof)
        next_token = resp.get("nextToken")
        if not next_token:
            return found


def lambda_handler(event, context):
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    account = os.environ["ACCOUNT_ID"]
    bedrock = boto3.client("bedrock", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    request_type = event.get("RequestType", "Create")
    print(f"InferenceProfileSetup invoked: {request_type}")

    if request_type == "Delete":
        # Best-effort cleanup. CFN Custom Resource still expects success on delete.
        # Enumerate by NAME PREFIX, not by _BASE_MODELS. Iterating the list would
        # leak every profile created under an older version of it: teardown would
        # leave AIPs behind that nothing tracks and that keep the DBOps cost tag.
        try:
            for prof in _list_managed(bedrock):
                name = prof.get("inferenceProfileName") or ""
                arn = prof.get("inferenceProfileArn")
                if not arn:
                    continue
                try:
                    bedrock.delete_inference_profile(inferenceProfileIdentifier=arn)
                    print(f"deleted {name}")
                except Exception as e:
                    print(f"delete {name} failed: {e}")
            ssm.delete_parameter(Name=_SSM_PARAM)
        except Exception as e:
            print(f"delete cleanup error: {e}")
        return {"PhysicalResourceId": "dbops-inference-profile-setup", "Data": {}}

    # Create or Update — idempotent create + tag.
    arn_map = {}
    for short, base_model, label in _BASE_MODELS:
        name = f"dbops-{_ENV}-{short}"
        base_arn = _arn_for(base_model, region, account) \
            .replace(":application-inference-profile/", ":inference-profile/")
        existing = _find_existing(bedrock, name)
        if existing:
            arn = existing.get("inferenceProfileArn")
            print(f"reuse {name} -> {arn}")
        else:
            try:
                resp = bedrock.create_inference_profile(
                    inferenceProfileName=name,
                    description=f"DBOps cost-allocation profile for {label}",
                    modelSource={"copyFrom": base_arn},
                    tags=_TAGS,
                )
                arn = resp.get("inferenceProfileArn")
                print(f"created {name} -> {arn}")
            except Exception as e:
                print(f"create {name} failed (base={base_arn}): {e}")
                continue
        # Re-apply tags (idempotent — replaces tag values to current intent).
        try:
            bedrock.tag_resource(resourceARN=arn, tags=_TAGS)
        except Exception as e:
            print(f"tag {name} failed: {e}")
        arn_map[short] = {"arn": arn, "label": label, "base": base_model}

    # Prune AIPs this stack created for a model that is no longer in _BASE_MODELS.
    #
    # Without this, editing _BASE_MODELS could only ever ADD to the picker. The Update
    # path created what was listed and left everything else alone, while
    # api/models/handler.py lists EVERY `dbops-` AIP, so a model removed from the list
    # above stayed in the dropdown forever. Worse, the Delete path also iterates
    # _BASE_MODELS, so a removed entry could never be cleaned up on stack teardown
    # either: it became a permanent orphan billing under the DBOps cost tag.
    #
    # Best-effort by design: a delete failure logs and continues, because a stale
    # dropdown row is a far smaller problem than a failed CloudFormation custom
    # resource taking the whole agent stack down with it.
    wanted = {f"dbops-{_ENV}-{short}" for short, _b, _l in _BASE_MODELS}
    for prof in _list_managed(bedrock):
        name = prof.get("inferenceProfileName") or ""
        if name in wanted:
            continue
        arn = prof.get("inferenceProfileArn")
        if not arn:
            continue
        try:
            bedrock.delete_inference_profile(inferenceProfileIdentifier=arn)
            print(f"pruned {name} (no longer in _BASE_MODELS)")
        except Exception as e:
            print(f"prune {name} failed: {e}")

    # Publish ARN map to SSM so the agent runtime can resolve "Opus 5" → AIP ARN.
    try:
        ssm.put_parameter(
            Name=_SSM_PARAM,
            Value=json.dumps(arn_map),
            Type="String",
            Overwrite=True,
            Description=f"DBOps {_ENV} inference profile ARN map (short_key -> AIP details)",
        )
        print(f"published SSM param {_SSM_PARAM}")
    except Exception as e:
        print(f"SSM put failed: {e}")

    return {
        "PhysicalResourceId": "dbops-inference-profile-setup",
        "Data": {
            "ssm_param": _SSM_PARAM,
            "profile_count": len(arn_map),
        },
    }
