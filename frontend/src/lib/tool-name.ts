// The AgentCore Gateway prefixes every MCP tool name with its target, e.g.
// "dbops-dev-performance-target___get_performance_summary". Showing that raw in
// the chat / RCA tool chips is noisy — strip to the bare tool name the DBA
// recognizes. Local agent tools (search_aws_documentation) have no prefix.
export function prettyToolName(name: string): string {
  if (!name) return name;
  const i = name.lastIndexOf("___");
  return i >= 0 ? name.slice(i + 3) : name;
}
