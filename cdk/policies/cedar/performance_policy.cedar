// Performance MCP target: READ-ONLY — permit all tools on the target.
//
// AgentCore's action form is target-as-action-group: `action in
// AgentCore::Action::"<target>"` permits every tool registered under that
// target. __TARGET__ (env-specific target name) and the gateway resource are
// substituted at deploy time by agent_stack.py (the gateway ARN is only known
// at deploy; constrained-action policies require a specific gateway resource).
permit(
  principal,
  action in AgentCore::Action::"__TARGET__",
  resource is AgentCore::Gateway
);
