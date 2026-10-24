"""The Internet Archive adapter over the real network client and a local server.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestInternetArchiveProvider``, the IA cases of ``TestDownloadFunctions``
and ``TestQuotedQueryEscaping``) and ``tests/unit/test_provider_parsing.py``;
``make_request`` patches became scripted replies. New: the page service with
its IIIF fallback, the ``--format`` rule and the recorded responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    DownloadRequest,
    InvalidResponse,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
)
from chronodownloader.providers.internet_archive import (
    InternetArchive,
    page_count,
    rendering_files,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import Settings
from tests.iiif.support import FULL, jpeg, json_reply, pdf, service_canvas, v2_manifest
from tests.net.support import Reply, ScriptedServer, settings_with

KEY = "internet_archive"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / KEY

SEARCH_RESPONSE: dict[str, Any] = {
    "response": {
        "numFound": 2,
        "docs": [
            {
                "identifier": "artofcooking1850",
                "title": "The Art of Cooking",
                "creator": ["John Smith"],
                "year": "1850",
                "mediatype": "texts",
            },
            {
                "identifier": "cookingarts1855",
                "title": "The Cooking Arts",
                "creator": ["Jane Doe"],
                "year": "1855",
                "mediatype": "texts",
            },
        ],
    }
}


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def network(coord: Coordinator) -> Iterator[Network]:
    with Network(settings_with(KEY), coord, sleep=lambda _seconds: None) as client:
        yield client


@pytest.fixture
def slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _adapter(server: ScriptedServer) -> InternetArchive:
    return InternetArchive(
        search_url=server.url("/advancedsearch.php"),
        metadata_url=server.url("/metadata/{identifier}"),
        download_url=server.url("/download/{identifier}"),
        manifest_urls=(server.url("/iiif/{identifier}/manifest.json"),),
    )


def _search(
    server: ScriptedServer,
    network: Network,
    payload: Any,
    title: str,
    *,
    creator: str | None = None,
    limit: int = 3,
) -> list[SearchRecord]:
    server.script("/advancedsearch.php", json_reply(payload))
    return _adapter(server).search(
        SearchQuery(title, creator=creator), network=network, limit=limit
    )


def _params(server: ScriptedServer) -> dict[str, list[str]]:
    return parse_qs(urlsplit(server.arrivals[-1].path).query)


def _paths(server: ScriptedServer) -> list[str]:
    return [urlsplit(arrival.path).path for arrival in server.arrivals]


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _staging(tmp_path: Path) -> Path:
    return tmp_path / "work" / "objects" / ".staging" / "1"


# Search (v1.30.3 TestInternetArchiveProvider and friends)


def test_search_returns_results(server: ScriptedServer, network: Network) -> None:
    results = _search(server, network, SEARCH_RESPONSE, "The Art of Cooking")

    assert len(results) == 2
    assert all(isinstance(r, SearchRecord) for r in results)


def test_search_extracts_metadata(server: ScriptedServer, network: Network) -> None:
    results = _search(server, network, SEARCH_RESPONSE, "The Art of Cooking")

    first = results[0]
    assert first.provider == KEY
    assert first.title == "The Art of Cooking"
    assert first.source_id == "artofcooking1850"
    assert "John Smith" in first.creators
    assert first.date == "1850"
    assert first.year == 1850


def test_search_builds_item_url(server: ScriptedServer, network: Network) -> None:
    results = _search(server, network, SEARCH_RESPONSE, "The Art of Cooking")

    assert results[0].raw["item_url"] == "https://archive.org/details/artofcooking1850"
    assert results[0].item_url == "https://archive.org/details/artofcooking1850"


def test_search_with_creator(server: ScriptedServer, network: Network) -> None:
    _search(
        server, network, SEARCH_RESPONSE, "The Art of Cooking", creator="John Smith"
    )

    assert "creator" in _params(server)["q"][0]


def test_search_empty_response(server: ScriptedServer, network: Network) -> None:
    assert _search(server, network, {"response": {"docs": []}}, "Nothing") == []


def test_search_none_response(server: ScriptedServer, network: Network) -> None:
    """v1.30.3 read a failed request as None and returned no results; a body
    without a response block still yields none, while a failed request now
    raises (see test_a_failed_search_is_not_an_empty_result)."""
    assert _search(server, network, {}, "Title") == []


def test_search_max_results(server: ScriptedServer, network: Network) -> None:
    _search(server, network, SEARCH_RESPONSE, "Title", limit=5)

    assert _params(server)["rows"] == ["5"]


def test_search_handles_null_creator(server: ScriptedServer, network: Network) -> None:
    """A present-but-null creator must not raise and non-string list entries
    are coerced; an absent creator yields no creators, not "N/A"."""
    response = {
        "response": {
            "docs": [
                {"identifier": "a", "title": "T1", "creator": None},
                {"identifier": "b", "title": "T2", "creator": [123, "X"]},
            ]
        }
    }
    results = _search(server, network, response, "Title")

    assert len(results) == 2
    assert results[0].raw["creators"] == []
    assert list(results[0].creators) == []
    assert list(results[1].creators) == ["123", "X"]


def test_search_no_year_yields_none_not_na(
    server: ScriptedServer, network: Network
) -> None:
    response = {"response": {"docs": [{"identifier": "x1", "title": "T1"}]}}
    results = _search(server, network, response, "Title")

    assert results[0].raw["year"] is None
    assert results[0].date is None
    assert results[0].year is None


def test_search_handles_string_creator(
    server: ScriptedServer, network: Network
) -> None:
    """A single-string creator must not be split into characters."""
    response = {
        "response": {
            "docs": [
                {
                    "identifier": "x1",
                    "title": "T",
                    "creator": "Jane Doe",
                    "year": "1900",
                }
            ]
        }
    }
    results = _search(server, network, response, "T")

    assert list(results[0].creators) == ["Jane Doe"]


def test_search_handles_list_creator(server: ScriptedServer, network: Network) -> None:
    response = {
        "response": {
            "docs": [
                {
                    "identifier": "x2",
                    "title": "T",
                    "creator": ["A", "B"],
                    "year": "1900",
                }
            ]
        }
    }
    results = _search(server, network, response, "T")

    assert list(results[0].creators) == ["A", "B"]


def test_strips_quotes_from_title_and_creator(
    server: ScriptedServer, network: Network
) -> None:
    _search(server, network, {}, 'Der "wahre" Koch', creator='Jean "le" Bon')

    query = _params(server)["q"][0]
    assert 'title:("Der  wahre  Koch")' in query
    assert 'creator:("Jean  le  Bon")' in query


def test_search_skips_only_the_bad_doc(
    server: ScriptedServer, network: Network
) -> None:
    payload = {
        "response": {
            "docs": [
                "not-a-dict",
                {
                    "identifier": "artofcooking1850",
                    "title": "The Art of Cooking",
                    "creator": "John Smith",
                },
            ]
        }
    }
    results = _search(server, network, payload, "The Art of Cooking")

    assert [r.source_id for r in results] == ["artofcooking1850"]


def test_all_results_are_search_records(
    server: ScriptedServer, network: Network
) -> None:
    payload = {"response": {"docs": [{"identifier": "x", "title": "y"}]}}
    results = _search(server, network, payload, "y")

    assert all(isinstance(r, SearchRecord) for r in results)


def test_a_failed_search_is_not_an_empty_result(
    server: ScriptedServer, network: Network
) -> None:
    server.script("/advancedsearch.php", Reply(503, b"busy"))
    with pytest.raises(ProviderBlocked) as caught:
        _adapter(server).search(SearchQuery("Kochbuch"), network=network, limit=3)
    assert caught.value.cause == "transient"


# Recorded responses


def test_recorded_search_response_parses(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, _fixture("search.json"), "Die Süddeutsche Küche")

    assert [r.source_id for r in results] == ["bub_gb_fu0qAAAAYAAJ"]
    assert results[0].creators == ("Scheiger, Katharina Prato, Edle von, 1818-1899",)
    assert results[0].year == 1890
    assert results[0].title.startswith("Die süddeutsche küche")


def test_recorded_search_error_raises(server: ScriptedServer, network: Network) -> None:
    """The Archive reports a failed query in a 200 body; it is not "no results"."""
    with pytest.raises(InvalidResponse, match="QUERY_NOT_READY"):
        _search(server, network, _fixture("search_error.json"), "Küche")


def test_recorded_metadata_offers_pdf_then_epub() -> None:
    metadata = _fixture("metadata_bub_gb_fu0qAAAAYAAJ.json")

    assert rendering_files(metadata) == [
        "bub_gb_fu0qAAAAYAAJ.pdf",
        "bub_gb_fu0qAAAAYAAJ.epub",
        "bub_gb_fu0qAAAAYAAJ_lcp.epub",
    ]
    assert page_count(metadata) == 730


def test_recorded_manifest_lists_its_canvases(
    server: ScriptedServer, network: Network
) -> None:
    manifest = _fixture("manifest_bub_gb_fu0qAAAAYAAJ.json")
    server.script("/metadata/bub_gb_fu0qAAAAYAAJ", json_reply({"files": []}))
    server.script("/iiif/bub_gb_fu0qAAAAYAAJ/manifest.json", json_reply(manifest))

    refs = _adapter(server).pages(
        "bub_gb_fu0qAAAAYAAJ", network=network, first_n=5, max_px=800
    )

    assert [ref.item for ref in refs] == ["p00001", "p00002", "p00003"]
    assert refs[0].service is not None
    assert refs[0].service.endswith("bub_gb_fu0qAAAAYAAJ_0000.jp2")
    assert refs[0].urls[0].endswith("/full/!800,800/0/default.jpg")


# Download (v1.30.3 TestDownloadFunctions)


def test_download_with_no_identifier(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    with pytest.raises(ItemNotFound):
        _adapter(server).download(
            "", network=network, staging_dir=tmp_path, request=DownloadRequest(slot)
        )
    assert server.arrivals == []


def test_download_with_search_record(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """An item without files or pages downloads nothing and is not complete."""
    record = SearchRecord(KEY, "artofcooking1850", "The Art of Cooking")
    metadata = {"metadata": {"identifier": "artofcooking1850"}, "files": []}
    server.script("/metadata/artofcooking1850", json_reply(metadata))
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        record, network=network, staging_dir=staging, request=DownloadRequest(slot)
    )

    assert not result.complete
    assert _files(staging) == ["metadata/ia_metadata.json"]


def test_download_thumbnail_only_is_not_complete(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """A thumbnail alone must not count as a completed download."""
    metadata = {"files": [{"name": "cover_thumb.jpg", "format": "Thumbnail"}]}
    server.script("/metadata/id1", json_reply(metadata))
    server.script("/download/id1/cover_thumb.jpg", jpeg())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1", network=network, staging_dir=staging, request=DownloadRequest(slot)
    )

    assert result.complete is False
    assert "/download/id1/cover_thumb.jpg" not in _paths(server)
    assert _files(staging) == ["metadata/ia_metadata.json"]


def test_download_skips_djvu_text_derivatives(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """Only the PDF is requested: the DjVu text and XML derivatives never
    match, and one whole-document file is enough."""
    metadata = {
        "files": [
            {"name": "id1_djvu.txt", "format": "DjVuTXT"},
            {"name": "id1_djvu.xml", "format": "Djvu XML"},
            {"name": "id1.pdf", "format": "Text PDF"},
            {"name": "id1.epub", "format": "EPUB"},
        ]
    }
    server.script("/metadata/id1", json_reply(metadata))
    server.script("/download/id1/id1.pdf", pdf())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1", network=network, staging_dir=staging, request=DownloadRequest(slot)
    )

    assert result.complete is True
    downloads = [path for path in _paths(server) if path.startswith("/download/")]
    assert downloads == ["/download/id1/id1.pdf"]
    assert _files(staging) == ["metadata/ia_metadata.json", "rendering.pdf"]


# Download: format rule and page service


def _paged_metadata(pages: int, *files: dict[str, str]) -> dict[str, Any]:
    return {"metadata": {"imagecount": str(pages)}, "files": list(files)}


def test_images_come_from_the_page_service(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/metadata/id1", json_reply(_paged_metadata(3, {"name": "id1.pdf"})))
    for index in range(3):
        server.script(f"/download/id1/page/n{index}.jpg", jpeg())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="images", max_pages=2),
    )

    images = result.part("images")
    assert images is not None
    assert images.expected == ("p00001", "p00002")
    assert images.complete
    assert images.truncated
    assert result.part("pdf") is None
    assert "/download/id1/id1.pdf" not in _paths(server)
    assert _files(staging) == [
        "metadata/ia_metadata.json",
        "p00001.jpg",
        "p00002.jpg",
    ]


def test_a_page_the_service_lacks_falls_back_to_iiif(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/metadata/id1", json_reply(_paged_metadata(2)))
    server.script("/download/id1/page/n0.jpg", jpeg())
    manifest = v2_manifest(
        service_canvas(server.url("/image/one")),
        service_canvas(server.url("/image/two")),
    )
    server.script("/iiif/id1/manifest.json", json_reply(manifest))
    server.script(f"/image/two{FULL}", jpeg())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="images"),
    )

    assert result.complete
    assert _paths(server).count("/iiif/id1/manifest.json") == 1
    assert f"/image/two{FULL}" in _paths(server)
    assert f"/image/one{FULL}" not in _paths(server)


def test_without_a_page_count_the_manifest_lists_the_pages(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/metadata/id1", json_reply({"files": []}))
    manifest = v2_manifest(service_canvas(server.url("/image/one")))
    server.script("/iiif/id1/manifest.json", json_reply(manifest))
    server.script(f"/image/one{FULL}", jpeg())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1", network=network, staging_dir=staging, request=DownloadRequest(slot)
    )

    images = result.part("images")
    assert images is not None
    assert images.expected == ("p00001",)
    assert result.complete
    assert not any("/page/" in path for path in _paths(server))


def test_pdf_takes_the_rendering_when_offered(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/metadata/id1", json_reply(_paged_metadata(5, {"name": "id1.pdf"})))
    server.script("/download/id1/id1.pdf", pdf())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1", network=network, staging_dir=staging, request=DownloadRequest(slot)
    )

    assert [part.part for part in result.parts] == ["pdf"]
    assert result.complete
    assert not any("/page/" in path for path in _paths(server))


def test_a_failed_pdf_falls_back_to_the_pages_and_stays_partial(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/metadata/id1", json_reply(_paged_metadata(1, {"name": "id1.pdf"})))
    server.script("/download/id1/page/n0.jpg", jpeg())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1", network=network, staging_dir=staging, request=DownloadRequest(slot)
    )

    pdf_part = result.part("pdf")
    images = result.part("images")
    assert pdf_part is not None and pdf_part.missing == ("rendering",)
    assert images is not None and images.complete
    assert not result.complete


def test_both_takes_the_rendering_and_the_pages(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/metadata/id1", json_reply(_paged_metadata(1, {"name": "id1.epub"})))
    server.script(
        "/download/id1/id1.epub",
        Reply(
            200,
            b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/epub+zip",
            {"Content-Type": "application/epub+zip"},
        ),
    )
    server.script("/download/id1/page/n0.jpg", jpeg())
    staging = _staging(tmp_path)

    result = _adapter(server).download(
        "id1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="both", include_metadata=False),
    )

    assert result.complete
    assert _files(staging) == ["p00001.jpg", "rendering.epub"]


def test_an_unknown_identifier_is_not_found(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """The Archive answers an unknown identifier with an empty object."""
    server.script("/metadata/nosuchitem", json_reply({}))
    with pytest.raises(ItemNotFound):
        _adapter(server).download(
            "nosuchitem",
            network=network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(slot),
        )


def test_pages_ask_the_page_service_for_half_the_size(
    server: ScriptedServer, network: Network
) -> None:
    server.script("/metadata/id1", json_reply(_paged_metadata(3)))

    refs = _adapter(server).pages("id1", network=network, first_n=2, max_px=1024)

    assert [ref.item for ref in refs] == ["p00001", "p00002"]
    assert refs[1].urls == (server.url("/download/id1/page/n1_h512.jpg"),)
    assert refs[1].service is None


def test_the_registry_builds_the_adapter() -> None:
    adapter = Registry(Settings()).get(KEY)

    assert isinstance(adapter, InternetArchive)
    assert adapter.capabilities.downloads
