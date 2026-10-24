"""The IIIF completeness contract: expected, present and missing pages.

Ported from v1.30.3 ``tests/unit/test_iiif_completeness.py``: there a direct
manifest recorded ``pages_expected``/``pages_downloaded`` and a ``partial``
status while the search-driven strategy reported success on the first page
that arrived (``api/iiif/_strategies.py:40``). Both paths are now one
strategy whose result names every expected page; the old counts map to
``len(expected)`` and ``len(present)``, the status to ``complete``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from chronodownloader.net import Network
from chronodownloader.providers.base import DownloadRequest, DownloadResult
from chronodownloader.providers.iiif_download import download_manifest
from tests.net.conftest import NetworkFactory
from tests.net.support import SLOT, ScriptedServer, settings_with

from .support import (
    direct_canvas,
    jpeg,
    serve_pages,
    service_canvas,
    service_urls,
    v2_manifest,
)


@pytest.fixture
def network(make_network: NetworkFactory) -> Network:
    return make_network(settings_with("bnf_gallica"))


def _download(
    network: Network, manifest: Mapping[str, object], tmp_path: Path
) -> DownloadResult:
    return download_manifest(
        network,
        "bnf_gallica",
        manifest,
        source_id="x",
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(SLOT, format="pdf", include_metadata=False),
    )


def test_partial_when_pages_missing(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    first, third = serve_pages(server, "s1", "s3")
    (second,) = service_urls(server, "s2")
    manifest = v2_manifest(*(service_canvas(u) for u in (first, second, third)))
    result = _download(network, manifest, tmp_path)
    images = result.part("images")
    assert images is not None
    assert len(images.expected) == 3
    assert len(images.present) == 2
    assert images.missing == ("p00002",)
    assert not result.complete
    assert len(result.files) == 2


def test_completed_when_all_pages_present(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    bases = serve_pages(server, "s1", "s2")
    result = _download(
        network, v2_manifest(*(service_canvas(u) for u in bases)), tmp_path
    )
    images = result.part("images")
    assert images is not None
    assert len(images.expected) == 2
    assert len(images.present) == 2
    assert result.complete


def test_direct_url_fallback_when_no_services(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    urls = [server.script("/p1.jpg", jpeg()), server.script("/p2.jpg", jpeg())]
    result = _download(
        network, v2_manifest(*(direct_canvas(u) for u in urls)), tmp_path
    )
    images = result.part("images")
    assert images is not None
    assert result.complete
    assert len(images.expected) == 2
    assert [arrival.path for arrival in server.arrivals] == ["/p1.jpg", "/p2.jpg"]


def test_mixed_service_and_direct_canvases_are_all_expected(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    first, third = serve_pages(server, "p1", "p3")
    direct = server.script("/p2.jpg", jpeg())
    manifest = v2_manifest(
        service_canvas(first), direct_canvas(direct), service_canvas(third)
    )
    result = _download(network, manifest, tmp_path)
    images = result.part("images")
    assert images is not None
    assert len(images.expected) == 3
    assert len(images.present) == 3
    assert result.complete
    assert [file.name for file in result.files] == [
        "p00001.jpg",
        "p00002.jpg",
        "p00003.jpg",
    ]
    assert server.hits("/p2.jpg") == 1


def test_multi_sequence_manifest_expects_every_sequence(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    first, second = serve_pages(server, "v1", "v2")
    manifest = {
        "sequences": [
            {"canvases": [service_canvas(first)]},
            {"canvases": [service_canvas(second)]},
        ]
    }
    result = _download(network, manifest, tmp_path)
    images = result.part("images")
    assert images is not None
    assert len(images.expected) == 2
    assert result.complete


def test_three_of_many_pages_are_not_complete(
    server: ScriptedServer, network: Network, tmp_path: Path
) -> None:
    """The v1.30.3 search-driven path counted 3 of 500 pages as success."""
    served = serve_pages(server, "1", "2", "3")
    absent = service_urls(server, *(str(i) for i in range(4, 11)))
    manifest = v2_manifest(*(service_canvas(u) for u in served + absent))
    result = _download(network, manifest, tmp_path)
    images = result.part("images")
    assert images is not None
    assert len(images.present) == 3
    assert len(images.missing) == 7
    assert not result.complete
