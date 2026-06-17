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

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
