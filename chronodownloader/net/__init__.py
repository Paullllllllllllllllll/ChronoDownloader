"""Network layer: the run-owned client, its failure causes and helpers.

:class:`Network` sends every request of a run: paced and breaker-checked
through the machine-wide coordinator, retried by cause, and streamed to a
``.part`` file with an atomic rename for downloads. :func:`run_with_deadline`
bounds a blocking call without delaying interpreter exit. Nothing here
imports a provider.
"""

from __future__ import annotations

from .client import JUDGE, DownloadOutcome, Network, RequestOptions
from .deadline import DeadlineExceeded, run_with_deadline
from .errors import BreakerOpen, Cause, NetworkError, QuotaSpent, RequestFailed
from .ports import CoordinatorPort

__all__ = [
    "JUDGE",
    "BreakerOpen",
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
