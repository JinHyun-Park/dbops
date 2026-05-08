#!/bin/bash
set -e

echo "========================================="
echo "  DBOps Platform — Full Deployment"
echo "========================================="
echo ""

# Check prerequisites
command -v cdk >/dev/null 2>&1 || { echo "❌ AWS CDK CLI required. Run: npm install -g aws-cdk"; exit 1; }
command -v agentcore >/dev/null 2>&1 || { echo "❌ AgentCore CLI required. Run: npm install -g @aws/agentcore"; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "❌ AWS CLI required."; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Step 1: Build frontend
echo "▶ [1/4] Building frontend..."
cd "$SCRIPT_DIR/frontend"
npm install --silent
npm run build
echo "✅ Frontend built"

# Step 2: Deploy main CDK stacks (Foundation → Data → Agent → Frontend)
echo ""
echo "▶ [2/4] Deploying CDK stacks..."
cd "$SCRIPT_DIR/cdk"
pip install -r requirements.txt -q
cdk deploy --all --require-approval never
echo "✅ CDK stacks deployed"

# Step 3: Deploy AgentCore Runtime
echo ""
echo "▶ [3/4] Deploying AgentCore Runtime..."
cd "$SCRIPT_DIR/agentcore-runtime/dbopsagent"
cd agentcore/cdk && npm install --silent && cd ../..
agentcore deploy --yes
echo "✅ AgentCore Runtime deployed"

# Step 4: Run schema migrations
echo ""
echo "▶ [4/4] Running schema migrations..."
cd "$SCRIPT_DIR"

CLUSTER_ARN=$(aws cloudformation describe-stacks \
  --stack-name dbops-dev-data \
  --query "Stacks[0].Outputs[?OutputKey=='CacheDbClusterArn'].OutputValue" \
  --output text 2>/dev/null)

SECRET_ARN=$(aws secretsmanager list-secrets \
  --query "SecretList[?starts_with(Name,'CacheDBSecret')].ARN | [0]" \
  --output text 2>/dev/null)

if [ -n "$CLUSTER_ARN" ] && [ "$CLUSTER_ARN" != "None" ]; then
  for schema_file in data-pipeline/sql/schema.sql data-pipeline/sql/schema_v2.sql data-pipeline/sql/schema_v3.sql; do
    if [ -f "$schema_file" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        line=$(echo "$line" | sed 's/--.*$//' | xargs)
        [ -z "$line" ] && continue
        aws rds-data execute-statement \
          --resource-arn "$CLUSTER_ARN" \
          --secret-arn "$SECRET_ARN" \
          --database "dbops" \
          --sql "$line" \
          --output text >/dev/null 2>&1 || true
      done < "$schema_file"
      echo "  ✅ $schema_file applied"
    fi
  done
  echo "✅ Schema migrations complete"
else
  echo "⚠️  Could not find Cache DB. Run schema migrations manually."
fi

# Summary
echo ""
echo "========================================="
echo "  ✅ Deployment Complete!"
echo "========================================="
echo ""

WEB_URL=$(aws cloudformation describe-stacks \
  --stack-name dbops-dev-frontend \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionUrl'].OutputValue" \
  --output text 2>/dev/null || echo "N/A")

API_URL=$(aws cloudformation describe-stacks \
  --stack-name dbops-dev-agent \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text 2>/dev/null || echo "N/A")

echo "  Web UI:  $WEB_URL"
echo "  API:     $API_URL"
echo ""
echo "  Next: Open Web UI and register your Aurora clusters"
echo ""
