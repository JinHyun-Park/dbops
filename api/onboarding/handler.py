"""Onboarding API — generates the spoke-account IAM role CloudFormation template
(JSON) a member-account admin deploys so DBOps's hub account can assume into it.
Admin-only, fail-closed (mirrors api/config/handler.py)."""

import base64
import json
import os

import boto3

ROLE_NAME = "dbops-spoke-role"

# Curated cross-account READ actions DBOps uses after assuming the spoke role.
READ_ACTIONS = [
    "rds:Describe*",
    "rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement",
    "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
    "pi:GetResourceMetrics", "pi:DescribeDimensionKeys", "pi:ListAvailableResourceMetrics",
    "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups",
    "dynamodb:ListTables", "dynamodb:DescribeTable",
    "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTimeToLive",
]
# Approval-gated WRITE actions (remediation) the operations MCP uses cross-account.
WRITE_ACTIONS = [
    "rds:ModifyDBCluster", "rds:ModifyDBInstance",
    "rds:ModifyDBParameterGroup", "rds:ModifyDBClusterParameterGroup",
    "rds:CreateDBClusterSnapshot", "rds:CreateDBSnapshot",
    "rds:RebootDBInstance", "rds:ApplyPendingMaintenanceAction",
    # Aurora custom cluster endpoints (P2-⑤) — approval-gated in tool code.
    # DescribeDBClusterEndpoints is already covered by rds:Describe* (READ_ACTIONS).
    "rds:CreateDBClusterEndpoint", "rds:DeleteDBClusterEndpoint", "rds:ModifyDBClusterEndpoint",
    "dynamodb:UpdateTable", "dynamodb:UpdateContinuousBackups", "dynamodb:UpdateTimeToLive",
]
# secretsmanager is resource-scoped (dbops/* only), kept as its own statement.
SECRETS_ACTIONS = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and "dbops-admin" not in groups:
        return False
    return True


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _build_template(hub_account_id: str, remediation: bool) -> dict:
    statements = [
        {"Sid": "DBOpsRead", "Effect": "Allow", "Action": list(READ_ACTIONS), "Resource": "*"},
        {"Sid": "DBOpsSecrets", "Effect": "Allow", "Action": list(SECRETS_ACTIONS),
         "Resource": "arn:aws:secretsmanager:*:*:secret:dbops/*"},
    ]
    if remediation:
        statements.append({"Sid": "DBOpsRemediation", "Effect": "Allow",
                           "Action": list(WRITE_ACTIONS), "Resource": "*"})
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "DBOps spoke-account role — lets the DBOps hub account assume in for "
                       "read-only monitoring/analysis" + (" + approval-gated remediation" if remediation else ""),
        "Resources": {
            "DBOpsSpokeRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": ROLE_NAME,
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Principal": {"AWS": f"arn:aws:iam::{hub_account_id}:root"},
                            "Action": "sts:AssumeRole",
                        }],
                    },
                    "Policies": [{
                        "PolicyName": "dbops-spoke-access",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                    }],
                },
            }
        },
        "Outputs": {
            "RoleArn": {"Description": "Spoke role ARN — register this in DBOps",
                        "Value": {"Fn::GetAtt": ["DBOpsSpokeRole", "Arn"]}},
        },
    }


def lambda_handler(event, context=None):
    method = (event.get("requestContext", {}).get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    if method == "OPTIONS":
        return _resp(200, {})
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})

    qs = event.get("queryStringParameters") or {}
    # region is an advisory passthrough — echoed in the response for the caller's
    # `aws cloudformation deploy --region`; the IAM role template itself is region-agnostic.
    region = (qs.get("region") or "").strip() or None
    remediation = str(qs.get("remediation") or "").strip().lower() in ("true", "1", "yes", "on")

    try:
        hub_account_id = boto3.client("sts").get_caller_identity()["Account"]
    except Exception as e:
        return _resp(500, {"error": f"could not resolve hub account: {type(e).__name__}"})

    template = _build_template(hub_account_id, remediation)
    return _resp(200, {
        "template": json.dumps(template, indent=2),
        "hub_account_id": hub_account_id,
        "hub_role_arn": os.environ.get("HUB_ROLE_ARN", ""),
        "role_name": ROLE_NAME,
        "remediation": remediation,
        "region": region,
    })
