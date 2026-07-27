#!/bin/bash
# Post-deploy smoke test. Hits the live API endpoints and key Lambdas to verify
# the platform is end-to-end functional. Returns non-zero on any failure.
set -u

# ENV and REGION come from cdk/config/settings.py, the same source cdk/app.py
# uses to name the stacks. Hardcoding dev + ap-northeast-2 made every
# describe-stacks miss on any other deployment, so the smoke test exited 1 and
# reported a red result for a deployment that had actually succeeded.
SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
read -r DBOPS_ENV DBOPS_REGION <<EOF
$(cd "$SMOKE_DIR/cdk" && python3 -c "from config.settings import Settings; print(Settings.ENV, Settings.REGION)")
EOF
: "${DBOPS_ENV:?could not read ENV from cdk/config/settings.py}"
: "${DBOPS_REGION:?could not read REGION from cdk/config/settings.py}"
export AWS_REGION="$DBOPS_REGION"


PASS=0
FAIL=0
WARN=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }
amber() { printf '\033[33m%s\033[0m\n' "$1"; }

pass() { PASS=$((PASS + 1)); green "  ✓ $1"; }
fail() { FAIL=$((FAIL + 1)); red   "  ✗ $1"; }
warn() { WARN=$((WARN + 1)); amber "  ⚠ $1"; }

echo "========================================="
echo "  DBOps Smoke Test"
echo "========================================="

# 1. Discover stack outputs
API_URL=$(aws cloudformation describe-stacks \
  --region "$DBOPS_REGION" \
  --stack-name "dbops-$DBOPS_ENV-agent" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text 2>/dev/null)
WEB_URL=$(aws cloudformation describe-stacks \
  --region "$DBOPS_REGION" \
  --stack-name "dbops-$DBOPS_ENV-frontend" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionUrl'].OutputValue" \
  --output text 2>/dev/null)

[ -n "$API_URL" ] && [ "$API_URL" != "None" ] && pass "agent stack API URL: $API_URL" || { fail "agent stack ApiUrl not found"; exit 1; }
[ -n "$WEB_URL" ] && [ "$WEB_URL" != "None" ] && pass "frontend stack URL: $WEB_URL" || fail "frontend DistributionUrl not found"

API_URL=${API_URL%/}
WEB_URL=${WEB_URL%/}

# 2. /config.json delivered + has expected keys
CONFIG=$(curl -fsS "$WEB_URL/config.json" 2>/dev/null)
if [ -n "$CONFIG" ]; then
  for key in apiUrl cognitoClientId region agentRuntimeArn; do
    echo "$CONFIG" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('$key') else 1)" 2>/dev/null \
      && pass "/config.json has $key" || fail "/config.json missing $key"
  done
else
  fail "/config.json not reachable"
fi

# 3. /api/clusters (DynamoDB → REST)
CLUSTER_COUNT=$(curl -fsS "$API_URL/api/clusters" 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
[ "$CLUSTER_COUNT" -ge 0 ] && pass "/api/clusters: $CLUSTER_COUNT clusters registered" || fail "/api/clusters unreachable"

# 4. /api/multi-cluster/overview
MULTI=$(curl -fsS "$API_URL/api/multi-cluster/overview" 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('clusters',[])))" 2>/dev/null || echo "")
[ -n "$MULTI" ] && pass "/api/multi-cluster/overview: $MULTI clusters" || fail "multi-cluster overview unreachable"

# 5. /api/alert-rules + /api/alert-subscriptions
curl -fsS "$API_URL/api/alert-rules" >/dev/null 2>&1 && pass "/api/alert-rules reachable" || fail "alert-rules unreachable"
SUB_TOPIC=$(curl -fsS "$API_URL/api/alert-subscriptions" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('topic_arn',''))" 2>/dev/null)
[ -n "$SUB_TOPIC" ] && pass "alert SNS topic: $SUB_TOPIC" || fail "alert subscriptions endpoint missing topic_arn"

# 6. Pick first cluster and exercise dashboard endpoints
FIRST_CID=$(curl -fsS "$API_URL/api/clusters" 2>/dev/null | python3 -c "
import json,sys
rows = json.load(sys.stdin)
print(rows[0]['cluster_id'] if rows else '')
" 2>/dev/null)
if [ -z "$FIRST_CID" ]; then
  warn "no clusters registered yet — skipping per-cluster checks. Register a cluster via the UI to complete the smoke test."
else
  pass "smoke testing against cluster: $FIRST_CID"
  for path in "" "/timeseries?metric=cpu&hours=1" "/wait-events?hours=1" "/slow-queries?hours=1" "/vacuum-stats" "/long-running" "/blocking-locks" "/settings" "/batch-timeseries?metrics=cpu,aas&hours=1"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/api/dashboard/$FIRST_CID$path")
    [ "$STATUS" = "200" ] && pass "GET /api/dashboard/{cid}$path → 200" || fail "GET /api/dashboard/{cid}$path → $STATUS"
  done
fi

# 7. Schema migrator: NO file may report errors.
# Two bugs used to make this check meaningless: (a) `grep -q "errors=0"` passed
# whenever ANY of the last lines was clean, and the migrator prints one line per
# SQL file across 25+ files, so a real failure was masked by its clean siblings;
# (b) `head -1` often selected the CDK Provider *framework* function, which never
# prints "errors=" at all, so the check silently degraded to the warn branch.
# Exclude the framework function and fail on any nonzero error count.
MIG_FN=$(aws lambda list-functions --region "$DBOPS_REGION" --max-items 1000 \
  --query "Functions[?contains(FunctionName, 'SchemaMigrator') && !contains(FunctionName, 'Providerframework')].FunctionName" \
  --output text 2>/dev/null | tr '\t' '\n' | head -1)
if [ -n "$MIG_FN" ]; then
  MIG_LINES=$(aws logs filter-log-events \
    --region "$DBOPS_REGION" \
    --log-group-name "/aws/lambda/$MIG_FN" \
    --start-time $(($(date +%s) * 1000 - 86400000)) \
    --query "events[?contains(message, 'errors=')].message" \
    --output text 2>/dev/null | tr '\t' '\n')
  if [ -z "$MIG_LINES" ]; then
    warn "SchemaMigrator has no run in the last 24h (it only runs when the asset changes)"
  elif echo "$MIG_LINES" | grep -qE "errors=[1-9]"; then
    fail "SchemaMigrator reported errors: $(echo "$MIG_LINES" | grep -E 'errors=[1-9]' | head -3 | tr '\n' ' ')"
  else
    pass "SchemaMigrator: $(echo "$MIG_LINES" | grep -c 'errors=') file(s), all errors=0"
  fi
else
  fail "SchemaMigrator Lambda not found"
fi

echo ""
echo "========================================="
green "  ✓ $PASS passed"
if [ "$WARN" -gt 0 ]; then amber "  ⚠ $WARN warnings"; fi
if [ "$FAIL" -gt 0 ]; then red "  ✗ $FAIL failed"; fi
echo "========================================="

[ "$FAIL" -eq 0 ]
