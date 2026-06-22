"""ticketing seam — provider resolution + the inert-by-default contract.

The seam must (1) default to a no-op that creates nothing, (2) treat a named
but unshipped provider as a loud failure rather than a silent drop, and (3) use
a real provider once one is registered.
"""

import mcp_servers.workers.ticketing as tk
import pytest


def test_default_is_noop(monkeypatch):
    monkeypatch.delenv("TICKETING_PROVIDER", raising=False)
    p = tk.get_provider()
    assert isinstance(p, tk.NoopTicketProvider)
    assert (
        p.create_ticket(task_id="t", cluster_id="c", kind="auto_rca", summary="s", result={})
        is None
    )


@pytest.mark.parametrize("val", ["none", "", "   ", "OFF", "Disabled"])
def test_disabled_values_resolve_to_noop(monkeypatch, val):
    monkeypatch.setenv("TICKETING_PROVIDER", val)
    assert isinstance(tk.get_provider(), tk.NoopTicketProvider)


def test_unwired_named_provider_raises(monkeypatch):
    monkeypatch.setenv("TICKETING_PROVIDER", "jira")
    p = tk.get_provider()
    assert isinstance(p, tk._UnwiredProvider)
    assert p.name == "jira"
    with pytest.raises(NotImplementedError):
        p.create_ticket(task_id="t", cluster_id="c", kind="auto_rca", summary="s", result={})


def test_explicit_name_overrides_env(monkeypatch):
    monkeypatch.setenv("TICKETING_PROVIDER", "jira")
    assert isinstance(tk.get_provider("none"), tk.NoopTicketProvider)


def test_registered_provider_is_used(monkeypatch):
    """Once a provider is registered in _IMPLEMENTED, get_provider returns it
    and its create_ticket result flows through."""

    class FakeProvider(tk.TicketProvider):
        name = "fake"

        def create_ticket(self, **_kw):
            return "https://tickets.example/INC-1"

    monkeypatch.setitem(tk._IMPLEMENTED, "fake", FakeProvider)
    p = tk.get_provider("fake")
    assert isinstance(p, FakeProvider)
    assert (
        p.create_ticket(task_id="t", cluster_id="c", kind="auto_rca", summary="s", result={})
        == "https://tickets.example/INC-1"
    )
