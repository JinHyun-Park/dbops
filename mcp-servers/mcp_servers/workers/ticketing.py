"""ticketing — pluggable incident/task ticketing seam.

This is an INTEGRATION SEAM, not a working integration. The task worker calls
``get_provider().create_ticket(...)`` after a task completes; by default that is
a no-op (``TICKETING_PROVIDER`` unset / ``"none"``), so the seam is inert and the
task flow is unchanged.

A real provider (Jira, ServiceNow, ...) is added later by implementing a
``TicketProvider`` subclass and registering it in ``_IMPLEMENTED`` (plus its
config + a Secrets Manager grant). Until then, a configured-but-unshipped
provider name resolves to ``_UnwiredProvider`` and raises on use, so a deploy
that flips ``TICKETING_PROVIDER=jira`` before the integration ships fails loudly
instead of silently dropping tickets.

``create_ticket`` returns the created ticket's URL (str) or ``None`` when no
ticket was created. The default/disabled path never raises; the caller also
isolates any provider exception so ticketing can never break task completion.
"""

import os
from typing import Optional

from mcp_servers.shared.app_config import get_config


class TicketProvider:
    """Base seam. A real provider overrides :meth:`create_ticket`."""

    name = "base"

    def create_ticket(
        self,
        *,
        task_id: str,
        cluster_id: str,
        kind: str,
        summary: str,
        result: dict,
    ) -> Optional[str]:
        raise NotImplementedError


class NoopTicketProvider(TicketProvider):
    """Default provider: ticketing disabled. Creates nothing, returns None."""

    name = "none"

    def create_ticket(self, **_kwargs) -> Optional[str]:
        return None


class _UnwiredProvider(TicketProvider):
    """A provider that has been *named* in config but whose integration has not
    shipped yet. Raises on use so a misconfiguration is visible instead of
    silently dropping tickets."""

    def __init__(self, name: str):
        self.name = name

    def create_ticket(self, **_kwargs) -> Optional[str]:
        raise NotImplementedError(
            f"ticketing provider {self.name!r} is not wired yet; implement a "
            "TicketProvider for it and register it in ticketing._IMPLEMENTED "
            "before enabling TICKETING_PROVIDER"
        )


# Providers with a shipped implementation, keyed by their lower-case config name.
# Add an entry here (name -> TicketProvider subclass) when an integration lands.
_IMPLEMENTED: dict = {}

_DISABLED = ("", "none", "off", "disabled")


def get_provider(name: Optional[str] = None) -> TicketProvider:
    """Resolve the configured ticketing provider.

    ``name`` defaults to the ``TICKETING_PROVIDER`` env var (default ``"none"``).
    A disabled value → :class:`NoopTicketProvider`. A name with a shipped
    implementation → that provider. Any other name → :class:`_UnwiredProvider`
    (raises on use)."""
    if name is None:
        name = get_config("TICKETING_PROVIDER", os.environ.get("TICKETING_PROVIDER", "none"))
    name = name.strip().lower()
    if name in _DISABLED:
        return NoopTicketProvider()
    impl = _IMPLEMENTED.get(name)
    if impl is not None:
        return impl()
    return _UnwiredProvider(name)
