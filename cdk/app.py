import aws_cdk as cdk
from config.settings import Settings
from stacks.foundation_stack import FoundationStack
from stacks.data_stack import DataStack
from stacks.agent_stack import AgentStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

env = cdk.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)

foundation = FoundationStack(app, f"dbops-{Settings.ENV}-foundation", env=env)
data = DataStack(app, f"dbops-{Settings.ENV}-data", env=env, foundation=foundation)
agent = AgentStack(app, f"dbops-{Settings.ENV}-agent", env=env, foundation=foundation, data=data)
FrontendStack(app, f"dbops-{Settings.ENV}-frontend", env=env, foundation=foundation, agent=agent)

# Tag every CDK-managed resource with Application=DBOps so the Cost page can
# attribute Bedrock/Lambda/RDS spend back to this project once the tag is
# activated as a cost allocation tag in the AWS Billing console.
cdk.Tags.of(app).add("Application", "DBOps")
cdk.Tags.of(app).add("Environment", Settings.ENV)

app.synth()
