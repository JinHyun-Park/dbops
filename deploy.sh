#!/bin/bash
set -e

echo "========================================="
echo "  DBOps Platform — Full Deployment"
echo "========================================="
echo ""

# Check prerequisites
command -v cdk >/dev/null 2>&1 || { echo "❌ AWS CDK CLI required. Run: npm install -g aws-cdk"; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "❌ AWS CLI required."; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Step 1: Bundle SQL schemas into the schema_migrator lambda asset
echo "▶ [1/4] Bundling SQL schemas..."
mkdir -p "$SCRIPT_DIR/data-pipeline/schema_migrator/sql"
cp "$SCRIPT_DIR/data-pipeline/sql/"schema*.sql "$SCRIPT_DIR/data-pipeline/schema_migrator/sql/"
echo "✅ SQL bundled"

# Step 1b: Build frontend
echo "▶ [1b/4] Building frontend..."
cd "$SCRIPT_DIR/frontend"
npm install --silent
npm run build
echo "✅ Frontend built"

# Step 2: Build agent ARM64 dependencies (for AgentCore Runtime)
echo ""
echo "▶ [2/4] Building agent ARM64 dependencies..."
bash "$SCRIPT_DIR/agent/build-deps.sh"

# Step 3: Deploy all CDK stacks (Foundation → Data → Agent → Frontend)
echo ""
echo "▶ [3/4] Deploying CDK stacks..."
cd "$SCRIPT_DIR/cdk"
pip install -r requirements.txt -q
cdk deploy --all --require-approval never
echo "✅ All stacks deployed (Foundation, Data, Agent + AgentCore, Frontend)"

# Schema migrations now run automatically via the SchemaMigrator Custom Resource
# inside dbops-dev-data stack. No manual SQL execution needed.

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

RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --stack-name dbops-dev-agent \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue" \
  --output text 2>/dev/null || echo "N/A")

GATEWAY_ID=$(aws cloudformation describe-stacks \
  --stack-name dbops-dev-agent \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" \
  --output text 2>/dev/null || echo "N/A")

echo "  Web UI:       $WEB_URL"
echo "  API:          $API_URL"
echo "  Runtime ARN:  $RUNTIME_ARN"
echo "  Gateway ID:   $GATEWAY_ID"
echo ""
echo "  Next: Open Web UI and register your Aurora clusters"
echo ""

# Step 4: Smoke test the deployment
echo "▶ [4/4] Running smoke test..."
if [ -x "$SCRIPT_DIR/smoke-test.sh" ]; then
  "$SCRIPT_DIR/smoke-test.sh" || echo "  ⚠ Smoke test reported failures (some are expected before first cluster registration)"
fi
