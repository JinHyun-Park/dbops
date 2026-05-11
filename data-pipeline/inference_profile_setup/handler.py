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
_BASE_MODELS = [
    ("opus-4-7", "global.anthropic.claude-opus-4-7", "Opus 4.7"),
    ("opus-4-6", "global.anthropic.claude-opus-4-6-v1", "Opus 4.6"),
    ("opus-4-5", "global.anthropic.claude-opus-4-5-20251101-v1:0", "Opus 4.5"),
    ("sonnet-4-6", "global.anthropic.claude-sonnet-4-6", "Sonnet 4.6"),
    ("sonnet-4-5", "global.anthropic.claude-sonnet-4-5-20250929-v1:0", "Sonnet 4.5"),
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


def lambda_handler(event, context):
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    account = os.environ["ACCOUNT_ID"]
    bedrock = boto3.client("bedrock", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    request_type = event.get("RequestType", "Create")
    print(f"InferenceProfileSetup invoked: {request_type}")

    if request_type == "Delete":
        # Best-effort cleanup. CFN Custom Resource still expects success on delete.
        try:
            for short, _base, _label in _BASE_MODELS:
                name = f"dbops-{_ENV}-{short}"
                existing = _find_existing(bedrock, name)
                if existing:
                    arn = existing.get("inferenceProfileArn")
                    if arn:
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

    # Publish ARN map to SSM so the agent runtime can resolve "Opus 4.7" → AIP ARN.
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
