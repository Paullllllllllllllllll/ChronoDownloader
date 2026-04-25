"""Core utilities for ChronoDownloader API.

Foundational primitives shared by every provider and orchestration layer:

- config: Configuration loading and provider-specific settings
- network: HTTP session, requests, rate limiting, circuit breaker
- context: Thread-local context for work/entry/provider tracking
- naming: Filename sanitization and provider slug utilities
- budget: Download budgeting and enforcement
- download: Central file download with validation and save_json
"""

__all__ = [
    "config",
    "network",
    "context",
    "naming",
    "budget",
    "download",
]
