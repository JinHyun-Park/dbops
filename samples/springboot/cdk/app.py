import os
import sys

import aws_cdk as cdk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "cdk"))
from config.settings import Settings  # noqa: E402
from springboot_apm_stack import SpringbootApmStack  # noqa: E402

app = cdk.App()
env = cdk.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)
SpringbootApmStack(app, f"dbops-{Settings.ENV}-springboot-apm", env=env)
app.synth()
