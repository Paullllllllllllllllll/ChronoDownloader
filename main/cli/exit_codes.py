"""Shared CLI exit-code constants (the agent contract).

- 0: full success
- 1: one or more items failed or are partial
- 2: usage / configuration error
- 130: user interrupt (SIGINT)
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130

__all__ = ["EXIT_OK", "EXIT_FAILURES", "EXIT_USAGE", "EXIT_INTERRUPT"]
