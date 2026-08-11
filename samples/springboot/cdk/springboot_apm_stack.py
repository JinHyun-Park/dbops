import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

LOG_GROUP = "/dbops/apm/todoapp"


class SpringbootApmStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs):
        super().__init__(scope, cid, **kwargs)

        vpc = ec2.Vpc(
            self, "ApmVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24),
            ],
        )

        log_group = logs.LogGroup(
            self, "AppLogGroup",
            log_group_name=LOG_GROUP,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        role = iam.Role(
            self, "AppInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy"),
            ],
        )

        sg = ec2.SecurityGroup(
            self, "AppSg", vpc=vpc, allow_all_outbound=True,
            description="todoapp app instance - egress only, load-gen ingress on 8080",
        )
        # Separate SG for the load generator Lambda. Two SGs (rather than one
        # shared SG with a self-reference ingress) avoids a CloudFormation
        # circular dependency between the instance, the Lambda, and the shared
        # SG's ingress rule. The app SG accepts 8080 only from this SG.
        load_gen_sg = ec2.SecurityGroup(
            self, "LoadGenSg", vpc=vpc, allow_all_outbound=True,
            description="todoapp load generator lambda",
        )
        sg.add_ingress_rule(
            peer=load_gen_sg,
            connection=ec2.Port.tcp(8080),
            description="load-gen lambda to app 8080",
        )

        jar_asset = s3_assets.Asset(
            self, "TodoJar",
            path="../app/target/todoapp.jar",
        )
        jar_asset.grant_read(role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            # awscli is not guaranteed on a minimal AL2023 image; install it so
            # the `aws s3 cp` below cannot abort user-data under `set -e`.
            "dnf install -y java-17-amazon-corretto amazon-cloudwatch-agent awscli",
            "mkdir -p /var/log/todoapp /opt/todoapp",
            f"aws s3 cp s3://{jar_asset.s3_bucket_name}/{jar_asset.s3_object_key} /opt/todoapp/todoapp.jar",
            "cat >/etc/systemd/system/todoapp.service <<'EOF'\n"
            "[Unit]\n"
            "Description=todoapp\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "ExecStart=/usr/bin/java -jar /opt/todoapp/todoapp.jar\n"
            "Environment=LOG_DIR=/var/log/todoapp\n"
            "Restart=always\n"
            "User=root\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "EOF",
            "systemctl daemon-reload",
            "systemctl enable --now todoapp",
            "cat >/opt/aws/amazon-cloudwatch-agent/etc/config.json <<'EOF'\n"
            "{\n"
            '  "agent": {"metrics_collection_interval": 60},\n'
            '  "logs": {"logs_collected": {"files": {"collect_list": [\n'
            f'    {{"file_path": "/var/log/todoapp/app.log", "log_group_name": "{LOG_GROUP}", "log_stream_name": "{{instance_id}}"}}\n'
            "  ]}}},\n"
            '  "metrics": {"append_dimensions": {"InstanceId": "${aws:InstanceId}"},\n'
            # aggregation_dimensions rolls the disk metric up to an InstanceId-only
            # series. Without it the agent only publishes disk_used_percent under the
            # full [InstanceId, device, fstype, path] set, and the DBOps collector
            # (which queries InstanceId alone) gets zero datapoints -> empty card.
            '    "aggregation_dimensions": [["InstanceId"]],\n'
            '    "metrics_collected": {\n'
            '      "mem": {"measurement": ["mem_used_percent"]},\n'
            '      "disk": {"measurement": ["disk_used_percent"], "resources": ["/"]}\n'
            "    }}\n"
            "}\n"
            "EOF",
            "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl "
            "-a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json",
        )

        instance = ec2.Instance(
            self, "AppInstance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            role=role,
            security_group=sg,
            user_data=user_data,
        )
        cdk.Tags.of(instance).add("Name", "dbops-apm-todoapp")
        log_group.grant_write(role)

        self.vpc = vpc
        self.app_sg = sg
        self.instance = instance

        cdk.CfnOutput(self, "InstanceId", value=instance.instance_id)
        cdk.CfnOutput(self, "LogGroup", value=LOG_GROUP)
        cdk.CfnOutput(self, "Region", value=self.region)
        cdk.CfnOutput(self, "VpcId", value=vpc.vpc_id)

        # Load generator: drives mostly-healthy traffic plus a trickle of the
        # three bug triggers so the APM dashboard has a steady signal. Runs in
        # the private subnets, reaches the app on 8080 via its own SG.
        load_gen = lambda_.Function(
            self, "LoadGen",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(LOAD_GEN_CODE),
            timeout=cdk.Duration.minutes(2),
            memory_size=256,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[load_gen_sg],
            environment={"APP_TAG": "dbops-apm-todoapp", "APP_PORT": "8080"},
        )
        load_gen.add_to_role_policy(iam.PolicyStatement(
            actions=["ec2:DescribeInstances"], resources=["*"],
        ))
        events.Rule(
            self, "LoadSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(2)),
            targets=[targets.LambdaFunction(load_gen)],
        )


LOAD_GEN_CODE = r'''
import json, os, urllib.request, urllib.error
import boto3

def _app_ip():
    ec2 = boto3.client("ec2")
    r = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [os.environ["APP_TAG"]]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    for res in r["Reservations"]:
        for inst in res["Instances"]:
            ip = inst.get("PrivateIpAddress")
            if ip:
                return ip
    return None

def _call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1

def handler(event, context):
    ip = _app_ip()
    if not ip:
        return {"error": "app instance not found"}
    base = f"http://{ip}:{os.environ.get('APP_PORT','8080')}/api"
    counts = {}
    def bump(k):
        counts[k] = counts.get(k, 0) + 1
    # healthy traffic: mostly reads. A couple of unique creates per run keep
    # INSERT traffic alive without growing the H2 table unboundedly (that growth
    # would compete with / mask the intended bug-3 leak on a t3.small).
    for i in range(20):
        bump(f"health_{_call('GET', base + '/health')}")
        bump(f"list_{_call('GET', base + '/tasks')}")
    for i in range(2):
        bump(f"create_{_call('POST', base + '/tasks', {'title': f'task-{context.aws_request_id}-{i}'})}")
    # bug 1: NPE (note, no title)
    bump(f"npe_{_call('POST', base + '/tasks', {'note': 'orphan'})}")
    # bug 2: duplicate title -> constraint violation
    _call('POST', base + '/tasks', {'title': 'dup-fixed'})
    bump(f"dup_{_call('POST', base + '/tasks', {'title': 'dup-fixed'})}")
    # bug 3: resource leak
    bump(f"leak_{_call('GET', base + '/leak')}")
    return counts
'''
