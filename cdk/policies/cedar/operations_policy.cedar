// Operations MCP target: MIXED (reads auto / writes need approval).
//
// LOG_ONLY rollout: this coarse "permit all tools on the target" is deployed
// FIRST so Cedar binds and every decision is observable in CloudWatch, without
// risking a default-DENY that would break legitimate calls. It does not weaken
// security today: the REAL write enforcement is in the tool code —
// execute_sql.py classifies SQL (SELECT/EXPLAIN/SHOW/DESCRIBE auto-allowed;
// other SQL needs approved=true; DROP/TRUNCATE/DELETE need force=true) plus the
// server-side approval_guard (payload-hash + atomic single-use); the structured
// write tools mirror the same approval model.
//
// STEP 2 — before flipping the gateway binding to ENFORCE, replace this coarse
// permit with per-tool rules. Cedar's action form is "<target>___<tool>" (three
// underscores), and forbid wins over permit. For example (substitute __TARGET__
// and verify each context.input.<param> is a REAL declared tool parameter via
// `aws bedrock-agentcore-control list-gateway-targets` — CreatePolicy validates
// context attrs against the auto-generated schema):
//
//   permit(principal,
//     action in [
//       AgentCore::Action::"__TARGET_____modify_parameter",
//       AgentCore::Action::"__TARGET_____modify_scaling",
//       AgentCore::Action::"__TARGET_____manage_maintenance",
//       AgentCore::Action::"__TARGET_____create_snapshot",
//       AgentCore::Action::"__TARGET_____restore_cluster",
//       AgentCore::Action::"__TARGET_____modify_dynamodb_capacity",
//       AgentCore::Action::"__TARGET_____modify_dynamodb_ttl",
//       AgentCore::Action::"__TARGET_____enable_dynamodb_pitr",
//       AgentCore::Action::"__TARGET_____set_docdb_profiler",
//       AgentCore::Action::"__TARGET_____create_docdb_index"
//     ],
//     resource is AgentCore::Gateway)
//   when { context.input has approved && context.input.approved == true };
//
//   forbid(principal,
//     action == AgentCore::Action::"__TARGET_____enable_dynamodb_pitr",
//     resource is AgentCore::Gateway)
//   when { context.input has enabled && context.input.enabled == false }
//   unless { context.input has force && context.input.force == true };
//
// (execute_sql is intentionally NOT gated by Cedar — its SELECT/DROP/force
//  decisions are SQL-content-dependent, which Cedar can't express; the tool
//  code owns it.)
permit(
  principal,
  action in AgentCore::Action::"__TARGET__",
  resource is AgentCore::Gateway
);
