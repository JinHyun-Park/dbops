import os

import aws_cdk as cdk
from config.settings import Settings
from stacks.agent_stack import AgentStack
from stacks.data_stack import DataStack
from stacks.foundation_stack import FoundationStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

env = cdk.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)

foundation = FoundationStack(app, f"dbops-{Settings.ENV}-foundation", env=env)
data = DataStack(app, f"dbops-{Settings.ENV}-data", env=env, foundation=foundation)
agent = AgentStack(app, f"dbops-{Settings.ENV}-agent", env=env, foundation=foundation, data=data)
frontend = FrontendStack(app, f"dbops-{Settings.ENV}-frontend", env=env, foundation=foundation, agent=agent)

# Tag every CDK-managed resource with Application=DBOps so the Cost page can
# attribute Bedrock/Lambda/RDS spend back to this project once the tag is
# activated as a cost allocation tag in the AWS Billing console.
cdk.Tags.of(app).add("Application", "DBOps")
cdk.Tags.of(app).add("Environment", Settings.ENV)

# cdk-nag (AWS Solutions ruleset) — synth-time security / best-practice lint.
# Gated behind CDK_NAG=1 so it never blocks a normal deploy; run on demand or in
# a dedicated CI step (`CDK_NAG=1 cdk synth`). Each finding is resolved by a real
# fix or a documented NagSuppressions entry in the owning stack.
if os.environ.get("CDK_NAG") == "1":
    from cdk_nag import AwsSolutionsChecks, NagSuppressions

    # Stack-level suppressions for the three rule classes that are accepted by
    # design for this single-tenant, self-hosted DBA tool. Each carries a
    # justification; the remaining ~31 specific findings (S3 SSL, DDB PITR,
    # Cognito MFA, RDS, CloudFront, APIG, VPC flow logs, Secrets rotation) are
    # triaged individually (fix or per-resource suppress) — see BACKLOG.
    for _stack in (foundation, data, agent, frontend):
        NagSuppressions.add_stack_suppressions(
            _stack,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWS-managed AWSLambdaBasicExecutionRole is the standard "
                        "CloudWatch Logs baseline for every Lambda; acceptable for "
                        "a single-tenant self-hosted deployment."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Wildcard resources are confined to inherently "
                        "account/region-wide control-plane calls (rds:Describe*, "
                        "pi/cloudwatch GetMetric*, sts:AssumeRole on dbops-spoke-role, "
                        "dynamodb:ListTables) — documented inline in each stack."
                    ),
                },
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "Lambdas pin Python 3.12 (a current supported runtime) "
                        "intentionally for reproducible builds rather than tracking "
                        "an implicit 'latest'."
                    ),
                },
            ],
            apply_to_nested_stacks=True,
        )

    # Per-stack accepted / deferred findings (genuine quick wins — DDB PITR,
    # S3/SNS SSL, Cognito password policy — were FIXED in the stacks; these are
    # the remainder, each with a justification).
    NagSuppressions.add_stack_suppressions(foundation, [
        {"id": "AwsSolutions-COG2", "reason": "MFA not required for this self-hosted DBA console; deferred pending an operator-MFA product decision (TOTP enrollment UX)."},
        {"id": "AwsSolutions-COG8", "reason": "Cognito advanced-security (plus) feature plan is a paid tier; out of scope for the default self-hosted deployment."},
    ], apply_to_nested_stacks=True)
    # Alert-push WebSocket API (apigatewayv2). Two findings are accepted by
    # design and documented inline in foundation_stack.py:
    #   - APIG1 (no access logging on AlertWsStage): the $connect authorizer's
    #     identity source is the Cognito access token in the query string; WS
    #     access logs would persist that token in plaintext. No access logging
    #     is the intended mitigation — see the HARDENING GUARD comment + BACKLOG
    #     "WS-ticket" before ever enabling it.
    #   - APIG4 ($disconnect route has no authorizer): WebSocket authorization
    #     happens once at $connect (Cognito Lambda authorizer); $disconnect fires
    #     on an already-authorized, server-initiated teardown and cannot carry an
    #     authorizer by design.
    _fnd = foundation.node.path  # e.g. "dbops-dev-foundation" — env-agnostic
    NagSuppressions.add_resource_suppressions_by_path(
        foundation,
        f"/{_fnd}/AlertWsStage/Resource",
        [{"id": "AwsSolutions-APIG1", "reason": "WS access logging is intentionally disabled: the $connect authorizer reads the Cognito access token from the query string, so access logs would record it in plaintext. Mitigation per foundation_stack.py HARDENING GUARD; revisit only via the WS-ticket pattern (BACKLOG)."}],
    )
    NagSuppressions.add_resource_suppressions_by_path(
        foundation,
        f"/{_fnd}/AlertWs/$disconnect-Route/Resource",
        [{"id": "AwsSolutions-APIG4", "reason": "WebSocket $disconnect route carries no authorizer by design — authorization is enforced once at $connect via a Cognito Lambda authorizer; $disconnect is a server-side teardown on an already-authorized connection."}],
    )
    NagSuppressions.add_stack_suppressions(agent, [
        {"id": "AwsSolutions-APIG4", "reason": "The only routes without the default Cognito JWT authorizer are the Slack webhooks, /health, and /api/incident-webhook, which authenticate by HMAC / shared-secret token instead."},
        {"id": "AwsSolutions-APIG1", "reason": "HTTP API access logging deferred — needs a log destination + retention decision; per-request handler activity is already in CloudWatch Lambda logs."},
        {"id": "AwsSolutions-COG1", "reason": "The Gateway's auto-created Cognito pool is machine-to-machine (client_credentials) only — no human users/passwords, so a password policy is N/A."},
        {"id": "AwsSolutions-COG2", "reason": "Gateway M2M pool has no human users; MFA is N/A."},
        {"id": "AwsSolutions-COG8", "reason": "Gateway M2M pool; advanced-security plus tier is N/A and paid."},
    ], apply_to_nested_stacks=True)
    NagSuppressions.add_stack_suppressions(data, [
        {"id": "AwsSolutions-RDS2", "reason": "Storage encryption on the EXISTING cache Aurora requires a cluster replacement; the cache is rebuildable but disruptive — deferred to a scheduled maintenance window."},
        {"id": "AwsSolutions-RDS6", "reason": "Access is via the RDS Data API + Secrets Manager, not IAM database authentication."},
        {"id": "AwsSolutions-RDS10", "reason": "The cache cluster is intentionally disposable (removal_policy=DESTROY, repopulated by the ETL); deletion protection would contradict that design."},
        {"id": "AwsSolutions-S1", "reason": "Archive bucket server-access logging deferred — needs a dedicated log bucket + lifecycle; the bucket is private (BlockPublicAccess) and now SSL-enforced."},
        {"id": "AwsSolutions-SMG4", "reason": "Automatic secret rotation deferred — needs a rotation Lambda; the cache DB secret is least-privilege and access-scoped."},
        {"id": "AwsSolutions-VPC7", "reason": "VPC flow logs deferred — cost/retention decision; Lambdas use the VPC only for private DB egress and there are no inbound paths."},
    ], apply_to_nested_stacks=True)
    NagSuppressions.add_stack_suppressions(frontend, [
        {"id": "AwsSolutions-CFR1", "reason": "No geo-restriction requirement for a self-hosted console; operators access from anywhere."},
        {"id": "AwsSolutions-CFR2", "reason": "AWS WAF is a paid add-on; out of scope for the default deployment (auth is enforced at the API/app layer)."},
        {"id": "AwsSolutions-CFR3", "reason": "CloudFront access logging deferred — needs a log bucket + retention decision."},
        {"id": "AwsSolutions-CFR4", "reason": "Distribution uses the default *.cloudfront.net cert, which pins AWS's modern TLS; minimum_protocol_version is only valid with a custom ACM cert (CFN rejects it otherwise), so it's set once a custom domain is configured."},
        {"id": "AwsSolutions-S1", "reason": "Frontend bucket server-access logging deferred; the bucket is private (OAC-only, BlockPublicAccess) and now SSL-enforced."},
    ], apply_to_nested_stacks=True)

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
