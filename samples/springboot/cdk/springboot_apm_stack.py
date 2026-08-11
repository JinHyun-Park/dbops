import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
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
            description="todoapp - egress only, no inbound",
        )
        # No inbound rules. Load generator ingress is added below.

        jar_asset = s3_assets.Asset(
            self, "TodoJar",
            path="../app/target/todoapp.jar",
        )
        jar_asset.grant_read(role)

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            "dnf install -y java-17-amazon-corretto amazon-cloudwatch-agent",
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
