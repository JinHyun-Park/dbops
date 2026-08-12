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

# cdk-nag (AWS Solutions ruleset): synth-time security / best-practice lint.
# Gated behind CDK_NAG=1 so it never blocks a normal deploy; run on demand or in
# a dedicated CI step (`CDK_NAG=1 cdk synth`). Each finding is resolved by a real
# fix or a documented NagSuppressions entry in the owning stack.
if os.environ.get("CDK_NAG") == "1":
    from cdk_nag import AwsSolutionsChecks, NagSuppressions

    # Stack-level suppressions for the three rule classes that are accepted by
    # design for this single-tenant, self-hosted DBA tool. Each carries a
    # justification; the remaining ~31 specific findings (S3 SSL, DDB PITR,
    # Cognito MFA, RDS, CloudFront, APIG, VPC flow logs, Secrets rotation) are
    # triaged individually (fix or per-resource suppress): see BACKLOG.
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
                        # An earlier version of this reason said wildcards were "confined
                        # to inherently account/region-wide control-plane calls
                        # (rds:Describe*, GetMetric*, ListTables)". That read as
                        # read-only-only and was materially incomplete: 32 wildcard
                        # statements across agent_stack and data_stack include ~25
                        # MUTATING actions. A suppression is an argument made in public,
                        # so it has to state the grant at its real size.
                        "Resource='*' covers THREE groups, not one. (1) Reads that AWS "
                        "cannot resource-scope: rds:Describe*, pi/cloudwatch GetMetric*, "
                        "dynamodb:ListTables. (2) WRITES: rds cluster/instance/snapshot/"
                        "parameter-group/custom-endpoint modify+create+delete+reboot+"
                        "restore, dynamodb UpdateTable/UpdateTimeToLive/"
                        "UpdateContinuousBackups, elasticache Modify/CreateSnapshot/"
                        "Reboot/TestFailover, bedrock Create+DeleteInferenceProfile, ssm "
                        "Put+DeleteParameter, and rds-data:ExecuteStatement. (3) "
                        "sts:AssumeRole, which IS scoped, to "
                        "arn:aws:iam::*:role/dbops-spoke-role. Plus (4) MODEL "
                        "INVOCATION: bedrock:InvokeModel + InvokeModelWithResponseStream "
                        "on foundation-model/*, inference-profile/* AND "
                        "application-inference-profile/*, held by the AgentCore runtime "
                        "role, the task worker and the report generator. Those THREE "
                        "cannot be narrowed or reduced to two. Invoking any profile "
                        "authorizes against BOTH the profile ARN and the underlying "
                        "foundation-model ARN in whichever region it fans out to (an "
                        "APAC cross-region profile fans out to 8 regions), so "
                        "foundation-model/* is load-bearing for the profile paths too. "
                        "Bedrock treats system-defined and APPLICATION inference "
                        "profiles as DISTINCT resource types and inference-profile/* "
                        "does not match application-inference-profile/*: the in-app "
                        "model picker serves Application Inference Profile ARNs (created "
                        "by data-pipeline/inference_profile_setup/), while AGENT_MODEL_ID "
                        "is a system-defined one, and both are invoked in practice. The "
                        "model is user-switchable at runtime, so the grant cannot be "
                        "pinned to a model id. "
                        "The writes are not resource-scoped because the targets are "
                        "operator-registered databases in arbitrary accounts, resolved "
                        "from the DynamoDB registry at request time and therefore not "
                        "enumerable at synth time. They are NOT ungated: every one is "
                        "fail-closed behind the tool-level approval_guard "
                        "(payload-hash-bound, single-use) in "
                        "mcp_servers/shared/approval_guard.py, and cross-account reach is "
                        "bounded by the spoke role's own trust policy plus its "
                        "aws:ResourceTag/ManagedBy=dbops condition "
                        "(cdk/cross-account/spoke-role-template.yaml). Each wildcard "
                        "block carries an inline comment naming its writes."
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

    # Per-stack accepted / deferred findings (genuine quick wins: DDB PITR,
    # S3/SNS SSL, Cognito password policy: were FIXED in the stacks; these are
    # the remainder, each with a justification).
    NagSuppressions.add_stack_suppressions(foundation, [
        {"id": "AwsSolutions-COG2", "reason": "MFA not required for this self-hosted DBA console; deferred pending an operator-MFA product decision (TOTP enrollment UX)."},
        {"id": "AwsSolutions-COG8", "reason": "Cognito advanced-security (plus) feature plan is a paid tier; out of scope for the default self-hosted deployment."},
    ], apply_to_nested_stacks=True)
    # Alert-push WebSocket API (apigatewayv2). Two findings are accepted by
    # design and documented inline in foundation_stack.py:
    #   - APIG1 (no access logging on AlertWsStage): off because nothing consumes
    #     it, NOT because enabling it would leak a credential. The $connect
    #     identity source is `route.request.querystring.ticket`, a random 60-second
    #     single-use ticket that $connect spends before the log line is written, so
    #     a recorded ticket is worth nothing. (This reason previously said the
    #     Cognito access token was in the URL and cited a HARDENING GUARD comment.
    #     Both described the pre-2026-07-30 design: the WS-ticket pattern replaced
    #     the token and the comment is gone. A suppression is an argument made in
    #     public, so it has to describe the code that ships.)
    #   - APIG4 ($disconnect route has no authorizer): WebSocket authorization
    #     happens once at $connect, by a Lambda authorizer that spends a single-use
    #     ticket; $disconnect fires on an already-authorized, server-initiated
    #     teardown and cannot carry an authorizer by design.
    _fnd = foundation.node.path  # e.g. "dbops-dev-foundation": env-agnostic
    NagSuppressions.add_resource_suppressions_by_path(
        foundation,
        f"/{_fnd}/AlertWsStage/Resource",
        [{"id": "AwsSolutions-APIG1", "reason": "WS access logging is off because nothing consumes it, not because it would leak a credential: the $connect identity source is route.request.querystring.ticket, a random 60-second single-use ticket that $connect spends before any log line is written. Enabling it is safe and unblocked; see the comment above AlertWsStage in foundation_stack.py."}],
    )
    NagSuppressions.add_resource_suppressions_by_path(
        foundation,
        f"/{_fnd}/AlertWs/$disconnect-Route/Resource",
        [{"id": "AwsSolutions-APIG4", "reason": "WebSocket $disconnect route carries no authorizer by design: authorization is enforced once at $connect by a Lambda authorizer that spends a single-use ticket (itself minted only by the Cognito-gated POST /api/ws-ticket). $disconnect is a server-side teardown on an already-authorized connection and cannot carry an authorizer."}],
    )
    NagSuppressions.add_stack_suppressions(agent, [
        {"id": "AwsSolutions-APIG4", "reason": "The only routes without the default Cognito JWT authorizer are the Slack webhooks, /health, and /api/incident-webhook, which authenticate by HMAC / shared-secret token instead."},
        {"id": "AwsSolutions-APIG1", "reason": "HTTP API access logging deferred: needs a log destination + retention decision; per-request handler activity is already in CloudWatch Lambda logs."},
        {"id": "AwsSolutions-COG1", "reason": "The Gateway's auto-created Cognito pool is machine-to-machine (client_credentials) only: no human users/passwords, so a password policy is N/A."},
        {"id": "AwsSolutions-COG2", "reason": "Gateway M2M pool has no human users; MFA is N/A."},
        {"id": "AwsSolutions-COG8", "reason": "Gateway M2M pool; advanced-security plus tier is N/A and paid."},
    ], apply_to_nested_stacks=True)
    NagSuppressions.add_stack_suppressions(data, [
        {"id": "AwsSolutions-RDS2", "reason": "Storage encryption on the EXISTING cache Aurora requires a cluster replacement; the cache is rebuildable but disruptive: deferred to a scheduled maintenance window."},
        {"id": "AwsSolutions-RDS6", "reason": "Access is via the RDS Data API + Secrets Manager, not IAM database authentication."},
        {"id": "AwsSolutions-RDS10", "reason": "The cache cluster is intentionally disposable (removal_policy=DESTROY, repopulated by the ETL); deletion protection would contradict that design."},
        {"id": "AwsSolutions-S1", "reason": "Archive bucket server-access logging deferred: needs a dedicated log bucket + lifecycle; the bucket is private (BlockPublicAccess) and now SSL-enforced."},
        {"id": "AwsSolutions-SMG4", "reason": "Automatic secret rotation deferred: needs a rotation Lambda; the cache DB secret is least-privilege and access-scoped."},
        {"id": "AwsSolutions-VPC7", "reason": "VPC flow logs deferred: cost/retention decision; Lambdas use the VPC only for private DB egress and there are no inbound paths."},
    ], apply_to_nested_stacks=True)
    NagSuppressions.add_stack_suppressions(frontend, [
        {"id": "AwsSolutions-CFR1", "reason": "No geo-restriction requirement for a self-hosted console; operators access from anywhere."},
        {"id": "AwsSolutions-CFR2", "reason": "AWS WAF is a paid add-on; out of scope for the default deployment (auth is enforced at the API/app layer)."},
        {"id": "AwsSolutions-CFR3", "reason": "CloudFront access logging deferred: needs a log bucket + retention decision."},
        {"id": "AwsSolutions-CFR4", "reason": "Distribution uses the default *.cloudfront.net cert, which pins AWS's modern TLS; minimum_protocol_version is only valid with a custom ACM cert (CFN rejects it otherwise), so it's set once a custom domain is configured."},
        {"id": "AwsSolutions-S1", "reason": "Frontend bucket server-access logging deferred; the bucket is private (OAC-only, BlockPublicAccess) and now SSL-enforced."},
    ], apply_to_nested_stacks=True)

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
