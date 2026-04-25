"""Provider connector package.

Exposes the central :data:`PROVIDERS` registry mapping provider keys to
their (search_fn, download_fn, display_name) tuples. Individual connector
modules live inside this package as ``api.providers.<provider_key>`` and
follow the uniform search/download interface.
"""
from __future__ import annotations

from ._registry import PROVIDERS

__all__ = ["PROVIDERS"]
