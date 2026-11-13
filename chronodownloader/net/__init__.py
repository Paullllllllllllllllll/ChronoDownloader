"""Network layer: the run-owned client, its failure causes and helpers.

:class:`Network` sends every request of a run: paced and breaker-checked
through the machine-wide coordinator, retried by cause, and streamed to a
``.part`` file with an atomic rename for downloads. :func:`run_with_deadline`
bounds a blocking call without delaying interpreter exit; a
:class:`CallScope` entered with :meth:`Network.scoped` stops the calls of a
thread whose deadline passed and measures their time in flight. Nothing here
imports a provider.
"""

from __future__ import annotations

from .client import CLASSIFY, JUDGE, DownloadOutcome, Network, RequestOptions
from .deadline import DeadlineExceeded, run_with_deadline
from .errors import (
    BreakerOpen,
    Cancelled,
    Cause,
    NetworkError,
    QuotaSpent,
    RequestFailed,
)
from .ports import CoordinatorPort
from .scope import CallScope

__all__ = [
    "CLASSIFY",
    "JUDGE",
    "BreakerOpen",
    "CallScope",
    "Cancelled",
    "Cause",
    "CoordinatorPort",
    "DeadlineExceeded",
    "DownloadOutcome",
    "Network",
    "NetworkError",
    "QuotaSpent",
    "RequestFailed",
    "RequestOptions",
    "run_with_deadline",
]
