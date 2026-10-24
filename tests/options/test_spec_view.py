"""options/spec_view.py: the source label of values a run resolves.

ChronoTranscriber v1.0.0 has no test of ``common/spec_view.py``; the label is
part of the dry-run view the sibling tools share, so its value is pinned.
"""

from __future__ import annotations

from chronodownloader.options.options import Source
from chronodownloader.options.spec_view import RESOLVED


def test_the_resolved_label_is_shared_and_distinct_from_the_option_sources() -> None:
    assert RESOLVED == "resolved"
    assert RESOLVED not in {source.value for source in Source}
