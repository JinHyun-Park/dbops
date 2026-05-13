import aws_cdk as cdk
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_rds as rds,
)
from constructs import Construct


class SampleStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, data_stack, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        vpc = data_stack.vpc

        pg_cluster = rds.DatabaseCluster(
            self, "SamplePG",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_15_10,
            ),
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("writer"),
            vpc=vpc,
            default_database_name="sampledb",
            enable_data_api=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        mysql_cluster = rds.DatabaseCluster(
            self, "SampleMySQL",
            engine=rds.DatabaseClusterEngine.aurora_mysql(
                version=rds.AuroraMysqlEngineVersion.VER_3_08_0,
            ),
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("writer"),
            vpc=vpc,
            default_database_name="sampledb",
            enable_data_api=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        load_gen = lambda_.Function(
            self, "LoadGenerator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(LOAD_GEN_CODE),
            timeout=cdk.Duration.minutes(5),
            memory_size=256,
            vpc=vpc,
            environment={
                "PG_CLUSTER_ARN": pg_cluster.cluster_arn,
                "PG_SECRET_ARN": pg_cluster.secret.secret_arn,
                "PG_DATABASE": "sampledb",
                "MYSQL_CLUSTER_ARN": mysql_cluster.cluster_arn,
                "MYSQL_SECRET_ARN": mysql_cluster.secret.secret_arn,
                "MYSQL_DATABASE": "sampledb",
            },
        )

        pg_cluster.secret.grant_read(load_gen)
        pg_cluster.grant_data_api_access(load_gen)
        mysql_cluster.secret.grant_read(load_gen)
        mysql_cluster.grant_data_api_access(load_gen)

        events.Rule(
            self, "LoadSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(2)),
            targets=[targets.LambdaFunction(load_gen)],
        )

        cdk.CfnOutput(self, "PGClusterArn", value=pg_cluster.cluster_arn)
        cdk.CfnOutput(self, "PGClusterId", value=pg_cluster.cluster_identifier)
        cdk.CfnOutput(self, "PGSecretArn", value=pg_cluster.secret.secret_arn)
        cdk.CfnOutput(self, "PGEndpoint", value=pg_cluster.cluster_endpoint.hostname)
        cdk.CfnOutput(self, "MySQLClusterArn", value=mysql_cluster.cluster_arn)
        cdk.CfnOutput(self, "MySQLClusterId", value=mysql_cluster.cluster_identifier)
        cdk.CfnOutput(self, "MySQLSecretArn", value=mysql_cluster.secret.secret_arn)
        cdk.CfnOutput(self, "MySQLEndpoint", value=mysql_cluster.cluster_endpoint.hostname)


LOAD_GEN_CODE = '''
import json
import os
import random
import time
import boto3

def handler(event, context):
    results = {}

    # PostgreSQL load
    try:
        results["pg"] = run_pg_load()
    except Exception as e:
        results["pg_error"] = str(e)

    # MySQL load
    try:
        results["mysql"] = run_mysql_load()
    except Exception as e:
        results["mysql_error"] = str(e)

    return {"statusCode": 200, "body": json.dumps(results)}


def run_pg_load():
    rds = boto3.client("rds-data")
    arn = os.environ["PG_CLUSTER_ARN"]
    secret = os.environ["PG_SECRET_ARN"]
    db = os.environ["PG_DATABASE"]

    def sql(statement):
        return rds.execute_statement(
            resourceArn=arn, secretArn=secret, database=db, sql=statement
        )

    # Create tables if not exist
    sql("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_name VARCHAR(100),
            product VARCHAR(100),
            quantity INT,
            price DECIMAL(10,2),
            status VARCHAR(20),
            region VARCHAR(50),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    sql("""
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100),
            email VARCHAR(200),
            tier VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    sql("""
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            product VARCHAR(100),
            warehouse VARCHAR(50),
            stock INT,
            last_updated TIMESTAMP DEFAULT NOW()
        )
    """)

    # Insert random data
    names = ["Alice","Bob","Charlie","Diana","Eve","Frank","Grace","Hank"]
    products = ["Laptop","Phone","Tablet","Monitor","Keyboard","Mouse","Headset","Camera"]
    statuses = ["pending","processing","shipped","delivered","cancelled"]
    regions = ["us-east-1","eu-west-1","ap-northeast-2","us-west-2"]
    tiers = ["free","basic","premium","enterprise"]

    ops = 0
    for _ in range(50):
        name = random.choice(names)
        product = random.choice(products)
        qty = random.randint(1, 10)
        price = round(random.uniform(10, 2000), 2)
        status = random.choice(statuses)
        region = random.choice(regions)
        sql(f"INSERT INTO orders (customer_name, product, quantity, price, status, region) VALUES (\\'{name}\\', \\'{product}\\', {qty}, {price}, \\'{status}\\', \\'{region}\\')")
        ops += 1

    for _ in range(10):
        name = random.choice(names) + str(random.randint(1,999))
        email = f"{name.lower()}@example.com"
        tier = random.choice(tiers)
        sql(f"INSERT INTO customers (name, email, tier) VALUES (\\'{name}\\', \\'{email}\\', \\'{tier}\\')")
        ops += 1

    # Run some read queries (simulate real workload)
    for _ in range(20):
        sql("SELECT * FROM orders WHERE status = \\'pending\\' ORDER BY created_at DESC LIMIT 10")
        sql("SELECT customer_name, SUM(price) as total FROM orders GROUP BY customer_name ORDER BY total DESC LIMIT 5")
        sql("SELECT product, COUNT(*) as cnt FROM orders WHERE region = \\'ap-northeast-2\\' GROUP BY product")
        ops += 3

    # Intentionally slow query (no index on status + region combo)
    for _ in range(5):
        sql("SELECT o.*, c.tier FROM orders o LEFT JOIN customers c ON o.customer_name = c.name WHERE o.status = \\'pending\\' AND o.region = \\'ap-northeast-2\\' ORDER BY o.price DESC")
        ops += 1

    return {"operations": ops, "engine": "postgresql"}


def run_mysql_load():
    rds = boto3.client("rds-data")
    arn = os.environ["MYSQL_CLUSTER_ARN"]
    secret = os.environ["MYSQL_SECRET_ARN"]
    db = os.environ["MYSQL_DATABASE"]

    def sql(statement):
        return rds.execute_statement(
            resourceArn=arn, secretArn=secret, database=db, sql=statement
        )

    sql("""
        CREATE TABLE IF NOT EXISTS products (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100),
            category VARCHAR(50),
            price DECIMAL(10,2),
            stock INT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    sql("""
        CREATE TABLE IF NOT EXISTS sales (
            id INT AUTO_INCREMENT PRIMARY KEY,
            product_id INT,
            quantity INT,
            total_price DECIMAL(10,2),
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    names = ["Laptop","Phone","Tablet","Monitor","Keyboard","Mouse","Headset","Camera","Speaker","Charger"]
    categories = ["Electronics","Accessories","Computing","Audio","Mobile"]

    ops = 0
    for _ in range(30):
        name = random.choice(names)
        cat = random.choice(categories)
        price = round(random.uniform(10, 2000), 2)
        stock = random.randint(0, 500)
        sql(f"INSERT INTO products (name, category, price, stock) VALUES (\\'{name}\\', \\'{cat}\\', {price}, {stock})")
        ops += 1

    for _ in range(40):
        pid = random.randint(1, 100)
        qty = random.randint(1, 5)
        total = round(qty * random.uniform(10, 500), 2)
        sql(f"INSERT INTO sales (product_id, quantity, total_price) VALUES ({pid}, {qty}, {total})")
        ops += 1

    for _ in range(15):
        sql("SELECT p.name, SUM(s.quantity) as sold FROM products p LEFT JOIN sales s ON p.id = s.product_id GROUP BY p.name ORDER BY sold DESC LIMIT 10")
        sql("SELECT category, AVG(price) as avg_price, COUNT(*) as cnt FROM products GROUP BY category")
        ops += 2

    return {"operations": ops, "engine": "mysql"}
'''
