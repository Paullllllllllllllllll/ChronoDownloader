"""Interactive UI and CLI-mode dispatch package.

Three modules comprising every user-facing concern other than the CLI
entry point itself:

- :mod:`main.ui.interactive` -- interactive workflow state machine
- :mod:`main.ui.console` -- ANSI console printing and the
  :class:`DownloadConfiguration` dataclass
- :mod:`main.ui.mode` -- dual-mode detection (interactive vs CLI)

Public surface (stable imports for CLI and orchestration):

- :class:`InteractiveWorkflow`, :func:`run_interactive`
- :class:`ConsoleUI`, :class:`DownloadConfiguration`
- :func:`run_with_mode_detection`, :func:`get_general_config`
"""
from __future__ import annotations

from .console import ConsoleUI, DownloadConfiguration
from .interactive import (
    InteractiveWorkflow,
    process_csv_batch_with_stats,
    process_single_work,
    run_interactive,
    run_interactive_session,
)
from .mode import get_general_config, run_with_mode_detection

__all__ = [
    "ConsoleUI",
    "DownloadConfiguration",
    "InteractiveWorkflow",
    "run_interactive",
    "run_interactive_session",
    "process_csv_batch_with_stats",
    "process_single_work",
    "get_general_config",
    "run_with_mode_detection",
]
