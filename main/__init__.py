"""ChronoDownloader main package.

Organized into five deep subpackages:

- :mod:`main.cli` -- CLI entry point, argparse, and per-command dispatch
- :mod:`main.ui` -- interactive workflow, console output, mode detection
- :mod:`main.orchestration` -- search-select-download pipeline and scheduler
- :mod:`main.state` -- persistent download state (quotas, deferred queue,
  background retry scheduler)
- :mod:`main.data` -- works CSV, index.csv, and work.json I/O
"""

__all__ = [
    "cli",
    "ui",
    "orchestration",
    "state",
    "data",
]
