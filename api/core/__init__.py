"""Core utilities for ChronoDownloader API.

This package contains modular components extracted from the monolithic utils.py:
- config: Configuration loading and provider settings
- network: HTTP session, requests, rate limiting
- context: Thread-local context for work/entry tracking
- naming: Filename sanitization and naming conventions
- budget: Download budgeting and limits enforcement
"""

__all__ = [
    "config",
    "network",
    "context",
    "naming",
    "budget",
]
