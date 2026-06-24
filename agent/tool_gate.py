"""Strands BeforeToolCall hook: hard-block a tool call whose cluster_id is not
in the caller's visible set. The system-prompt constraint is advisory; THIS is
the guarantee that survives prompt manipulation — the tool never executes on a
cluster the caller can't see."""

try:
    from strands.hooks.events import BeforeToolCallEvent
except ImportError:  # older/experimental layout
    from strands.experimental.hooks import BeforeToolCallEvent  # type: ignore
from strands.hooks import HookProvider, HookRegistry

_DENY = "이 클러스터에 대한 접근 권한이 없습니다."

# Tools that widen their OWN scope server-side when cluster_id is omitted/empty
# (a fleet-wide scan or fleet fallback) and so would leak other teams' data to a
# non-admin. For these, a missing/empty cluster_id is DENIED for non-admins
# (admins skip the gate entirely). Genuinely cluster-agnostic tools (doc lookups,
# runbooks) are NOT listed and pass through with no cluster_id. Any NEW tool that
# can enumerate across clusters MUST be added here — the default for an unlisted
# no-cluster_id tool is "allowed".
_FLEET_CAPABLE = {"query_activity_audit", "find_similar_incidents"}


class ClusterVisibilityGate(HookProvider):
    """visible: a set of allowed cluster_ids, or None for admin/unrestricted."""

    def __init__(self, visible):
        self._visible = visible

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)

    def _before_tool(self, event) -> None:
        if self._visible is None:
            return  # admin / unrestricted
        try:
            tool_use = getattr(event, "tool_use", None) or {}
            name = tool_use.get("name") or ""
            args = tool_use.get("input") or {}
            cid = args.get("cluster_id") if isinstance(args, dict) else None
        except Exception:
            return  # can't parse — let it through (fleet/doc tools have no cluster_id)
        if not cid:
            # No cluster scope. Cluster-agnostic tools are fine, but a
            # fleet-capable tool would enumerate ALL clusters server-side for a
            # non-admin — deny it (the model must name a specific, visible
            # cluster, which the cid-in-visible check below then enforces).
            if name in _FLEET_CAPABLE:
                event.cancel_tool = _DENY
            return
        if cid not in self._visible:
            event.cancel_tool = _DENY
