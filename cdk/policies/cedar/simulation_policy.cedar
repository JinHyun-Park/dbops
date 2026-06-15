// Simulation MCP target: READ-ONLY (what-if estimates, no mutation) — permit
// all tools on the target.
//
// AgentCore action form is target-as-action-group: `action in
// AgentCore::Action::"<target>"` permits every tool under that target.
// __TARGET__ and the gateway resource are substituted at deploy by
// agent_stack.py.
permit(
  principal,
  action in AgentCore::Action::"__TARGET__",
  resource is AgentCore::Gateway
);
