"""Main package for ChronoDownloader.

This package contains:
- downloader: CLI entry point supporting dual interactive/CLI modes
- pipeline: Core orchestration logic for searching and downloading
- selection: Candidate scoring and selection logic
- mode_selector: Mode detection for interactive vs CLI execution
- interactive: Interactive workflow UI components
"""

__all__ = [
    "pipeline",
    "selection",
    "execution",
    "downloader",
    "mode_selector",
]
