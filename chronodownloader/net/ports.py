"""The coordinator methods the network client calls.

:class:`CoordinatorPort` is the part of :class:`chronodownloader.coord.Coordinator`
that :class:`~chronodownloader.net.Network` uses: pacing, the shared circuit
breaker, the daily quota and the download slots it holds for transfers
from another provider's hosts. Tests pass an in-memory stand-in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from chronodownloader.coord import BreakerState, QuotaStatus, QuotaToken, SlotToken

if TYPE_CHECKING:
    from chronodownloader.settings import Coordination

__all__ = ["CoordinatorPort"]


class CoordinatorPort(Protocol):
    """Machine-wide pacing, breaker, quota and slots, shared by every process."""

    @property
    def settings(self) -> Coordination:
        """The coordination settings."""
        ...

    def acquire_slot(self, provider: str, limit: int) -> SlotToken | None:
        """Take a download slot of *provider*; None while *limit* are held."""
        ...

    def renew_slot(self, token: SlotToken) -> SlotToken:
        """Extend the slot's expiry and return the new token."""
        ...

    def release_slot(self, token: SlotToken) -> None:
        """Free the slot when this holder still holds it."""
        ...

    def wait_turn(
        self,
        provider: str,
        delay_s: float,
        jitter_s: float,
        *,
        slot: SlotToken | None = None,
        sleep: Callable[[float], None] = ...,
    ) -> None:
        """Return when this caller may send its next request to *provider*."""
        ...

    def breaker_check(self, provider: str) -> BreakerState:
        """Return the breaker state; an elapsed cooldown grants one probe."""
        ...

    def record_success(self, provider: str) -> None:
        """Close the breaker and reset its failure count."""
        ...

    def record_failure(
        self, provider: str, *, threshold: int, cooldown_s: float, cause: str
    ) -> BreakerState:
        """Count a failure; open the breaker at *threshold*; return the state."""
        ...

    def reserve_quota(
        self, provider: str, *, daily_limit: int, reset_hours: float
    ) -> QuotaToken | None:
        """Reserve one quota unit; return its token, or None when spent."""
        ...

    def release_quota(self, token: QuotaToken) -> None:
        """Give back a unit whose request provably was not sent."""
        ...

    def quota_status(self, provider: str) -> QuotaStatus:
        """Return the provider's quota use and reset time."""
        ...
