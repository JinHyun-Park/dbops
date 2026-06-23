#!/usr/bin/env bash
# Dev-env live smoke — READ-ONLY, best-effort. Refreshes a Cognito token from
# the local Playwright session (frontend/e2e/.auth/state.json) and curls a few
# key API endpoints. Prints 'SMOKE: PASS' / 'SMOKE: FAIL' (or 'SMOKE: SKIP' when
# no usable session — never fails the tester just because a session expired).
#
# This is a STARTER smoke (only /api/clusters). Extend ENDPOINTS for richer
# coverage. Env overrides: DBOPS_API_URL, DBOPS_COGNITO_CLIENT_ID, AWS_REGION.
# The API URL + Cognito client id below are the dev values (not secrets — a
# public API Gateway URL + a public Cognito app-client id); the refresh token
# comes from the local, gitignored state.json.
set -uo pipefail

API="${DBOPS_API_URL:-https://vp8z6cdxcd.execute-api.ap-northeast-2.amazonaws.com}"
CLIENT_ID="${DBOPS_COGNITO_CLIENT_ID:-h587q0bq8vtmd6fdpg76trapb}"
REGION="${AWS_REGION:-ap-northeast-2}"
ENDPOINTS=("/api/clusters")

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "SMOKE: SKIP (not a repo)"; exit 0; }
STATE="$REPO/frontend/e2e/.auth/state.json"
[ -f "$STATE" ] || { echo "SMOKE: SKIP (no $STATE)"; exit 0; }

RT="$(python3 -c "import json,sys
for o in json.load(open(sys.argv[1])).get('origins',[]):
    for kv in o.get('localStorage',[]):
        if kv['name'].endswith('.refreshToken'):
            print(kv['value']); raise SystemExit" "$STATE" 2>/dev/null)"
[ -z "$RT" ] && { echo "SMOKE: SKIP (no refresh token in session)"; exit 0; }

TOK="$(aws cognito-idp initiate-auth --auth-flow REFRESH_TOKEN_AUTH \
  --client-id "$CLIENT_ID" --auth-parameters REFRESH_TOKEN="$RT" \
  --region "$REGION" --query 'AuthenticationResult.IdToken' --output text 2>/dev/null)"
{ [ -z "$TOK" ] || [ "$TOK" = "None" ]; } && { echo "SMOKE: SKIP (token refresh failed — session likely expired)"; exit 0; }

FAIL=0
for path in "${ENDPOINTS[@]}"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: $TOK" "$API$path" 2>/dev/null)"
  echo "  GET $path -> $code"
  [ "$code" = "200" ] || FAIL=1
done
[ "$FAIL" = 0 ] && echo "SMOKE: PASS" || echo "SMOKE: FAIL"
exit 0
