"""Unit tests for agent/tool_gate.py — ClusterVisibilityGate BeforeToolCall hook."""

import importlib.util
import sys
import types
from pathlib import Path

# agent/tool_gate.py subclasses strands' HookProvider, so loading it needs the
# Strands hook API. Strands ships in the agent runtime (agent/_deps) + local
# dev, but the CI unit-test image installs only requirements-dev.txt (no agent
# runtime deps). Rather than SKIP these security-critical tenant-isolation tests
# in CI (a regression could merge unsignaled), inject a minimal stub of the
# Strands hook API when the real SDK is absent. The gate's authorization logic
# is pure Python (reads tool_use, sets cancel_tool) and does not depend on any
# Strands behavior, so the stub exercises it faithfully; the real
# Strands-honors-cancel_tool integration is covered by the opus review + the
# live smoke. When the real SDK IS present (local), it is used unchanged.
if "strands" not in sys.modules:
    try:
        import strands  # noqa: F401
    except ImportError:
        _strands = types.ModuleType("strands")
        _hooks = types.ModuleType("strands.hooks")
        _events = types.ModuleType("strands.hooks.events")

        class HookProvider:  # minimal base — register_hooks is overridden
            pass

        class HookRegistry:
            def __init__(self):
                self._registered_callbacks = {}

            def add_callback(self, event_type, callback):
                self._registered_callbacks.setdefault(event_type, []).append(callback)

        class BeforeToolCallEvent:  # marker type used as the registry key
            pass

        _hooks.HookProvider = HookProvider
        _hooks.HookRegistry = HookRegistry
        _events.BeforeToolCallEvent = BeforeToolCallEvent
        _hooks.events = _events
        _strands.hooks = _hooks
        sys.modules["strands"] = _strands
        sys.modules["strands.hooks"] = _hooks
        sys.modules["strands.hooks.events"] = _events

# ---------------------------------------------------------------------------
# Import agent/tool_gate.py via importlib (avoids sys.path pollution)
# ---------------------------------------------------------------------------
_AGENT_DIR = Path(__file__).resolve().parents[3] / "agent"


def _load_tool_gate():
    spec = importlib.util.spec_from_file_location(
        "tool_gate", _AGENT_DIR / "tool_gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool_gate = _load_tool_gate()
ClusterVisibilityGate = tool_gate.ClusterVisibilityGate
_DENY = tool_gate._DENY


# ---------------------------------------------------------------------------
# Fake event stand-in
# ---------------------------------------------------------------------------
class FakeEvent:
    """Minimal stand-in for BeforeToolCallEvent with a settable cancel_tool."""

    def __init__(self, name: str, args: dict | None):
        self.tool_use = {"name": name, "input": args}
        self.cancel_tool = False  # default per Strands dataclass

    def _invoke(self, gate: ClusterVisibilityGate) -> None:
        """Simulate the hook registry calling the callback."""
        gate._before_tool(self)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClusterVisibilityGate:
    VISIBLE = {"c-open", "c-teamA"}

    def _gate(self, visible):
        return ClusterVisibilityGate(visible)

    # --- blocked cluster ---
    def test_non_visible_cluster_sets_cancel(self):
        event = FakeEvent("get_top_queries", {"cluster_id": "c-teamB"})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool == _DENY

    # --- visible cluster ---
    def test_visible_cluster_not_cancelled(self):
        event = FakeEvent("get_top_queries", {"cluster_id": "c-teamA"})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool is False

    # --- unassigned cluster visible to all ---
    def test_unassigned_cluster_visible(self):
        event = FakeEvent("get_top_queries", {"cluster_id": "c-open"})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool is False

    # --- tool with no cluster_id (fleet/doc tool) ---
    def test_no_cluster_id_not_cancelled(self):
        event = FakeEvent("list_clusters", {"page": 1})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool is False

    # --- fleet-capable tool with NO cluster_id => denied for non-admin (C1) ---
    def test_fleet_capable_tool_without_cluster_id_denied(self):
        event = FakeEvent("query_activity_audit", {"days": 7})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool == _DENY

    def test_fleet_capable_tool_empty_cluster_id_denied(self):
        event = FakeEvent("find_similar_incidents", {"cluster_id": "", "symptoms": "x"})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool == _DENY

    # --- fleet-capable tool WITH a visible cluster_id => allowed ---
    def test_fleet_capable_tool_with_visible_cluster_allowed(self):
        event = FakeEvent("query_activity_audit", {"cluster_id": "c-teamA"})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool is False

    # --- fleet-capable tool with a NON-visible cluster_id => denied ---
    def test_fleet_capable_tool_with_hidden_cluster_denied(self):
        event = FakeEvent("query_activity_audit", {"cluster_id": "c-teamB"})
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool == _DENY

    # --- admin can call a fleet-capable tool fleet-wide (visible=None) ---
    def test_fleet_capable_tool_admin_allowed(self):
        event = FakeEvent("query_activity_audit", {"days": 7})
        event._invoke(self._gate(None))
        assert event.cancel_tool is False

    # --- admin (visible=None) never blocked ---
    def test_admin_none_not_cancelled_even_for_hidden_cluster(self):
        event = FakeEvent("get_top_queries", {"cluster_id": "c-teamB"})
        event._invoke(self._gate(None))
        assert event.cancel_tool is False

    # --- malformed input: input is None ---
    def test_malformed_input_none_not_cancelled(self):
        event = FakeEvent("some_tool", None)
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool is False

    # --- malformed input: input is a string instead of dict ---
    def test_malformed_input_string_not_cancelled(self):
        event = FakeEvent("some_tool", "not-a-dict")  # type: ignore[arg-type]
        # Override tool_use directly to simulate the malformed case
        event.tool_use = {"name": "some_tool", "input": "not-a-dict"}
        event._invoke(self._gate(self.VISIBLE))
        assert event.cancel_tool is False

    # --- empty visible set: every cluster is blocked ---
    def test_empty_visible_set_blocks_all(self):
        event = FakeEvent("get_top_queries", {"cluster_id": "c-open"})
        event._invoke(self._gate(set()))
        assert event.cancel_tool == _DENY

    # --- register_hooks wires callback (smoke) ---
    def test_register_hooks_wires_callback(self):
        from strands.hooks import HookRegistry
        from strands.hooks.events import BeforeToolCallEvent

        registry = HookRegistry()
        gate = self._gate(self.VISIBLE)
        gate.register_hooks(registry)
        assert BeforeToolCallEvent in registry._registered_callbacks
        assert len(registry._registered_callbacks[BeforeToolCallEvent]) == 1
