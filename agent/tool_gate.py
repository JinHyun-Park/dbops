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
            args = tool_use.get("input") or {}
            cid = args.get("cluster_id") if isinstance(args, dict) else None
        except Exception:
            return  # can't parse — let it through (fleet/doc tools have no cluster_id)
        if not cid:
            return  # no cluster scope on this tool
        if cid not in self._visible:
            event.cancel_tool = _DENY
