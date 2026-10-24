"""The source label of values a run resolves itself.

A run's effective spec lists every field with its value and source. A field
left unset that the run fills in when it plans takes the source
:data:`RESOLVED` in the view the dry run prints.

Copied from ChronoTranscriber v1.0.0 ``common/spec_view.py`` on 19.10.2026,
trimmed: removed ``resolved_view`` and ``image_values`` (model output limit,
response format, service tier and page-image values).
"""

from __future__ import annotations

__all__ = ["RESOLVED"]

RESOLVED = "resolved"
"""The source of a value the run resolved instead of the user setting it."""
