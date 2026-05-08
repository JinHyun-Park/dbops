import aws_cdk as cdk
import sys
sys.path.insert(0, "../../cdk")
from config.settings import Settings
from sample_stack import SampleStack

app = cdk.App()
env = cdk.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)

SampleStack(app, f"dbops-{Settings.ENV}-sample", env=env, data_stack=None)

app.synth()
