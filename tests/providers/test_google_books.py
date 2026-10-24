"""The Google Books adapter over the real network client and a local server.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestGoogleBooksProvider``, the Google case of ``TestQuotedQueryEscaping``),
``tests/unit/test_provider_parsing.py`` and
``tests/unit/test_provider_metadata.py``. The placeholder test asserted the
budget singleton's refund and the naming counter's rollback; both are gone,
and it now asserts the result: the placeholder is deleted, never charged,
and its page stays missing while the next page keeps its own item. New:
search failures surface, the ``--format`` rule, and the recorded responses.
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
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
    page_item,
)
from chronodownloader.providers.google_books import (
    DEFAULT_PAGES,
    GoogleBooks,
    is_placeholder_image,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import GoogleBooksSettings, Settings
from tests.iiif.support import json_reply, pdf
from tests.net.support import Reply, ScriptedServer, settings_with

from .fakes import FileBudget

KEY = "google_books"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / KEY

PAGE = Reply(
    200, b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"p" * 20_000, {"Content-Type": "image/jpeg"}
)
"""A page image above the placeholder sizes."""
PLACEHOLDER = Reply(
    200, b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"x" * 9093, {"Content-Type": "image/jpeg"}
)
"""A 9,103-byte image: the placeholder size."""


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


def _adapter(server: ScriptedServer, **options: Any) -> GoogleBooks:
    return GoogleBooks(
        options=GoogleBooksSettings(**options),
        api_url=server.url("/books/v1/volumes"),
        content_url=server.url("/books/content"),
    )


def _queries(server: ScriptedServer) -> list[dict[str, list[str]]]:
    return [parse_qs(urlsplit(arrival.path).query) for arrival in server.arrivals]


def _search(
    server: ScriptedServer, network: Network, *replies: Reply, **query: Any
) -> list[SearchRecord]:
    server.script("/books/v1/volumes", *replies)
    title = query.pop("title", "Cookery")
    return _adapter(server).search(
        SearchQuery(title, **query), network=network, limit=3
    )


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _volume(server: ScriptedServer, *, pdf_link: bool = True) -> dict[str, Any]:
    """The recorded volume with its PDF link pointing at the local server."""
    volume = _fixture("volume_zGlhAAAAcAAJ.json")
    if pdf_link:
        volume["accessInfo"]["pdf"]["downloadLink"] = server.url("/books/download.pdf")
    else:
        volume["accessInfo"]["pdf"] = {"isAvailable": False}
    server.script("/books/v1/volumes/zGlhAAAAcAAJ", json_reply(volume))
    return volume


# Search (v1.30.3 behavior)


def test_author_query_uses_space_not_plus(
    server: ScriptedServer, network: Network
) -> None:
    """Field clauses are space-separated: a literal '+' is URL-encoded to
    %2B, nullifying the combined title and author query."""
    _search(server, network, json_reply({}), creator="Glasse")

    first_q = _queries(server)[0]["q"][0]
    assert "+inauthor" not in first_q
    assert 'inauthor:"Glasse"' in first_q


def test_search_handles_null_and_non_string_authors(
    server: ScriptedServer, network: Network
) -> None:
    response = {
        "items": [
            {
                "id": "g1",
                "volumeInfo": {"title": "T1", "authors": None},
                "accessInfo": {"publicDomain": True},
            },
            {
                "id": "g2",
                "volumeInfo": {"title": "T2", "authors": [123, "X"]},
                "accessInfo": {"publicDomain": True},
            },
        ]
    }
    results = _search(server, network, json_reply(response))

    assert len(results) == 2
    assert results[0].raw["creators"] == []
    assert list(results[0].creators) == []
    assert list(results[1].creators) == ["123", "X"]


def test_strips_quotes_in_strict_variant(
    server: ScriptedServer, network: Network
) -> None:
    _search(
        server,
        network,
        json_reply({}),
        title='Der "wahre" Koch',
        creator='Jean "le" Bon',
    )

    first_q = _queries(server)[0]["q"][0]
    assert 'intitle:"Der  wahre  Koch"' in first_q
    assert 'inauthor:"Jean  le  Bon"' in first_q


def test_search_skips_only_the_bad_volume(
    server: ScriptedServer, network: Network
) -> None:
    payload = {
        "items": [
            "not-a-dict",
            {
                "id": "vol1",
                "volumeInfo": {"title": "Cookery", "publishedDate": "1828"},
                "accessInfo": {"publicDomain": True},
            },
        ]
    }
    results = _search(server, network, json_reply(payload))

    assert [r.source_id for r in results] == ["vol1"]
    assert results[0].date == "1828"


def test_search_reads_published_date(server: ScriptedServer, network: Network) -> None:
    response = {
        "items": [
            {
                "id": "vol1",
                "volumeInfo": {
                    "title": "The Cook and Housewife's Manual",
                    "authors": ["Margaret Dods"],
                    "publishedDate": "1828",
                },
                "accessInfo": {
                    "viewability": "ALL_PAGES",
                    "publicDomain": True,
                    "pdf": {"isAvailable": True, "downloadLink": "https://x/p.pdf"},
                },
            }
        ]
    }
    results = _search(server, network, json_reply(response), title="manual")

    assert results[0].date == "1828"
    assert results[0].year == 1828


def test_free_only_drops_volumes_that_are_not_free(
    server: ScriptedServer, network: Network
) -> None:
    response = {
        "items": [
            {"id": "paid", "volumeInfo": {"title": "T"}, "accessInfo": {}},
            {
                "id": "free",
                "volumeInfo": {"title": "T"},
                "accessInfo": {"viewability": "ALL_PAGES"},
            },
        ]
    }
    results = _search(server, network, json_reply(response))

    assert [r.source_id for r in results] == ["free"]


def test_the_variants_run_under_each_filter_until_one_finds_volumes(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(server, network, json_reply(_fixture("search_empty.json")))

    queries = _queries(server)
    assert results == []
    assert len(queries) == 4 * 3
    assert [q.get("filter", [None])[0] for q in queries[:3]] == [
        "free-ebooks",
        "ebooks",
        None,
    ]


def test_a_503_storm_is_not_an_empty_result(
    server: ScriptedServer, network: Network
) -> None:
    """v1.30.3 reported a 503 storm as "no items found"."""
    storm = Reply(503, json.dumps(_fixture("search_503.json")).encode("utf-8"))
    with pytest.raises(ProviderBlocked) as caught:
        _search(server, network, storm)
    assert caught.value.cause == "transient"


def test_a_failed_variant_moves_on_to_the_next(
    server: ScriptedServer, network: Network
) -> None:
    storm = Reply(503, b"busy")
    results = _search(
        server, network, storm, storm, storm, json_reply(_fixture("search.json"))
    )

    assert [r.source_id for r in results] == ["zGlhAAAAcAAJ"]


def test_an_open_breaker_ends_the_search(
    server: ScriptedServer, coord: Coordinator
) -> None:
    settings = settings_with(KEY, circuit_breaker_threshold=2, max_attempts=1)
    with Network(settings, coord, sleep=lambda _seconds: None) as client:
        server.script("/books/v1/volumes", Reply(503, b"busy"))
        with pytest.raises(ProviderBlocked) as caught:
            _adapter(server).search(SearchQuery("Cookery"), network=client, limit=3)
    assert caught.value.cause == "breaker_open"
    assert len(server.arrivals) == 2


def test_recorded_search_response_parses(
    server: ScriptedServer, network: Network
) -> None:
    results = _search(
        server,
        network,
        json_reply(_fixture("search.json")),
        title="Handboekje voor koek- en banketbakkers",
    )

    assert len(results) == 1
    record = results[0]
    assert record.source_id == "zGlhAAAAcAAJ"
    assert record.year == 1840
    assert record.raw["has_download"] is True
    assert record.item_url == "https://books.google.com/books?id=zGlhAAAAcAAJ"


# Download


def test_null_volume_and_access_info_do_not_raise(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    volume = {"id": "vol1", "volumeInfo": None, "accessInfo": None}
    server.script("/books/v1/volumes/vol1", json_reply(volume))

    result = _adapter(server).download(
        "vol1",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot),
    )

    assert result.complete is False
    assert _files(tmp_path / "staging") == ["metadata/volume.json"]


def test_placeholder_is_discarded_uncharged_and_missing(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """v1.30.3 refunded the budget singleton and rolled the naming counter
    back. Now the placeholder is recognized before it is kept: it is never
    charged, its page stays missing, and the next real page keeps p00002."""
    _volume(server, pdf_link=False)
    server.script("/books/content", PLACEHOLDER, PAGE)
    budget = FileBudget()
    staging = tmp_path / "staging"

    result = _adapter(server).download(
        "zGlhAAAAcAAJ",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="images", max_pages=2, budget=budget),
    )

    images = result.part("images")
    assert images is not None
    assert images.missing == ("p00001",)
    assert images.present == ("p00002",)
    assert not result.complete
    assert _files(staging) == ["metadata/volume.json", "p00002.jpg"]
    assert [kind for kind, _size in budget.charged] == ["metadata", "image"]


def test_three_placeholders_in_a_row_end_the_pages(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    _volume(server, pdf_link=False)
    server.script("/books/content", PLACEHOLDER)

    result = _adapter(server).download(
        "zGlhAAAAcAAJ",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot, format="images", max_pages=10),
    )

    images = result.part("images")
    assert images is not None
    assert len(images.expected) == 10
    assert images.present == ()
    assert len(server.arrivals) == 1 + 3


def test_an_unbounded_probe_leaves_the_extent_unknown(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """Without pageCount and page limit, 50 good pages (the volume has more)
    do not make the work complete."""
    volume = _fixture("volume_zGlhAAAAcAAJ.json")
    volume["accessInfo"]["pdf"] = {"isAvailable": False}
    volume["volumeInfo"].pop("pageCount", None)
    server.script("/books/v1/volumes/zGlhAAAAcAAJ", json_reply(volume))
    server.script("/books/content", PAGE)

    result = _adapter(server).download(
        "zGlhAAAAcAAJ",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot, format="images"),
    )

    images = result.part("images")
    assert images is not None
    assert len(images.present) == DEFAULT_PAGES
    assert images.missing == (page_item(DEFAULT_PAGES + 1),)
    assert dict(images.causes) == {page_item(DEFAULT_PAGES + 1): "extent_unknown"}
    assert not result.complete
    pages = [a for a in server.arrivals if a.path.startswith("/books/content")]
    assert len(pages) == DEFAULT_PAGES


def test_pdf_takes_the_direct_download(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    _volume(server)
    server.script("/books/download.pdf", pdf())

    result = _adapter(server).download(
        "zGlhAAAAcAAJ",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot),
    )

    assert result.complete
    assert [part.part for part in result.parts] == ["pdf"]
    assert _files(tmp_path / "staging") == ["metadata/volume.json", "rendering.pdf"]


def test_a_refused_pdf_falls_back_to_page_images(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """The baseline's PDF answered 429; the pages are tried, the work stays
    partial and the rendering is reported missing."""
    _volume(server)
    server.script("/books/download.pdf", Reply(429, b"slow down"))
    server.script("/books/content", PAGE)

    result = _adapter(server).download(
        "zGlhAAAAcAAJ",
        network=network,
        staging_dir=tmp_path / "staging",
        request=DownloadRequest(slot, max_pages=2),
    )

    pdf_part = result.part("pdf")
    images = result.part("images")
    assert pdf_part is not None and pdf_part.missing == ("rendering",)
    assert images is not None and images.complete
    assert not result.complete
    assert result.stopped is None


def test_a_429_storm_opens_the_breaker_and_stops_the_download(
    server: ScriptedServer, coord: Coordinator, slot: SlotToken, tmp_path: Path
) -> None:
    settings = settings_with(KEY, circuit_breaker_threshold=2)
    _volume(server)
    server.script("/books/download.pdf", Reply(429, b"slow down"))
    with Network(settings, coord, sleep=lambda _seconds: None) as client:
        result = _adapter(server).download(
            "zGlhAAAAcAAJ",
            network=client,
            staging_dir=tmp_path / "staging",
            request=DownloadRequest(slot),
        )

    assert result.stopped == "breaker_open"
    assert not result.complete


def test_recorded_volume_offers_its_pdf_only(server: ScriptedServer) -> None:
    volume = _fixture("volume_zGlhAAAAcAAJ.json")

    links = _adapter(server)._links(volume["accessInfo"])

    assert [link.format for link in links] == ["pdf"]
    assert "sig=REDACTED" in links[0].url


def test_max_files_bounds_the_links(server: ScriptedServer) -> None:
    access = {
        "pdf": {"isAvailable": True, "downloadLink": "https://x/a.pdf"},
        "epub": {"isAvailable": True, "downloadLink": "https://x/a.epub"},
    }

    assert len(_adapter(server, max_files=2)._links(access)) == 2
    assert [link.format for link in _adapter(server, max_files=1)._links(access)] == [
        "pdf"
    ]
    preferred = _adapter(server, prefer="epub", max_files=1)._links(access)
    assert [link.format for link in preferred] == ["epub"]


def test_drm_links_only_when_allowed(server: ScriptedServer) -> None:
    access = {"pdf": {"isAvailable": False, "acsTokenLink": "https://x/acs"}}

    assert _adapter(server)._links(access) == []
    assert len(_adapter(server, allow_drm=True)._links(access)) == 1


def test_placeholder_signatures(tmp_path: Path) -> None:
    small = tmp_path / "small.jpg"
    small.write_bytes(b"x" * 9103)
    page = tmp_path / "page.jpg"
    page.write_bytes(b"x" * 20_000)

    assert is_placeholder_image(small)
    assert not is_placeholder_image(page)
    assert not is_placeholder_image(tmp_path / "missing.jpg")


def test_pages_are_not_offered(server: ScriptedServer, network: Network) -> None:
    with pytest.raises(UnsupportedCapability):
        _adapter(server).pages("vol1", network=network, first_n=1, max_px=800)


def test_the_registry_builds_the_adapter_with_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "REDACTED")
    adapter = Registry(Settings()).get(KEY)

    assert isinstance(adapter, GoogleBooks)
    assert adapter.api_key == "REDACTED"
    assert adapter.options.max_pages == 50
    assert adapter.capabilities.api_key == KEY
