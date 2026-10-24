"""Anna's Archive: search parsing, public links, fast-download API, quota,
login-page detection and the unconfigured mirror.

Requests go through the real :class:`~chronodownloader.net.Network` and the
real coordination database to a local server that stands in for the mirror.
Ported from v1.30.3 ``tests/unit/test_provider_parsing.py``
(``TestAnnasArchiveTableParsing``, ``TestAnnasArchiveLinkFilter``) and
``tests/unit/test_utils_extended.py`` (the Anna's Archive cases of
``TestShouldRejectHtmlResponse`` and ``TestValidateHtmlNotLoginPage``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.providers.annas_archive import (
    KEY,
    LOGIN_MARKERS,
    AnnasArchive,
    MirrorNotConfigured,
    build,
    is_annas_page,
    login_page_reason,
    parse_md5_page,
    parse_search_page,
    read_fast_download,
)
from chronodownloader.providers.base import (
    DownloadRequest,
    InvalidResponse,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
    UnsupportedCapability,
)
from chronodownloader.providers.registry import Registry
from chronodownloader.settings import PROVIDER_DEFAULTS, AnnasQuota, Settings
from tests.iiif.support import PDF, json_reply, pdf
from tests.net.support import Reply, ScriptedServer, settings_with

from .fakes import FileBudget

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / KEY
MD5 = "0123456789abcdef0123456789abcdef"
SECRET = "member-secret-4f2a9c"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _html(text: str) -> Reply:
    return Reply(200, text.encode("utf-8"), {"Content-Type": "text/html"})


def _staging(tmp_path: Path) -> Path:
    return tmp_path / "work" / "objects" / ".staging" / "1"


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _paths(server: ScriptedServer) -> list[str]:
    return [arrival.path.split("?")[0] for arrival in server.arrivals]


def _client(coord: Coordinator, daily_limit: int = 5, enabled: bool = True) -> Network:
    quota = AnnasQuota(enabled=enabled, daily_limit=daily_limit)
    settings = settings_with(KEY, quota=quota)
    return Network(settings, coord, sleep=lambda _seconds: None)


@pytest.fixture
def annas_network(coord: Coordinator) -> Iterator[Network]:
    with _client(coord) as client:
        yield client


@pytest.fixture
def annas_slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _md5_page(*links: str) -> str:
    """Return an MD5 page with one anchor per ``href|text`` link."""
    anchors = "".join(
        '<a href="{}">{}</a>'.format(*link.split("|", 1)) for link in links
    )
    return (
        f"<html><head><title>Anna’s Archive</title></head><body>{anchors}</body></html>"
    )


def _fast_answer(server: ScriptedServer, name: str = "fast_download.json") -> Reply:
    document: dict[str, Any] = json.loads(_fixture(name))
    if document.get("download_url"):
        document["download_url"] = server.url("/fast/file.pdf")
    return json_reply(document)


# Search (v1.30.3 TestAnnasArchiveTableParsing)

TABLE = """
<table>
  <tr><th>i</th><th>Title</th><th>Author</th><th>Publisher</th>
      <th>Year</th><th>File</th></tr>
  <tr>
    <td></td>
    <td><a href="/md5/0123456789abcdef0123456789abcdef">Le Cuisinier</a></td>
    <td>La Varenne</td>
    <td>Paris</td>
    <td>1651</td>
    <td>book.pdf</td>
  </tr>
</table>
"""


def test_year_cell_becomes_the_result_date(
    server: ScriptedServer, annas_network: Network
) -> None:
    server.script("/search", _html(TABLE))
    results = AnnasArchive(server.url("")).search(
        SearchQuery("Le Cuisinier"), network=annas_network, limit=3
    )
    assert results and results[0].date == "1651"
    assert results[0].year == 1651


def test_short_row_leaves_the_date_empty(
    server: ScriptedServer, annas_network: Network
) -> None:
    html = """
    <table>
      <tr><th>i</th><th>Title</th></tr>
      <tr>
        <td></td>
        <td><a href="/md5/0123456789abcdef0123456789abcdef">Le Cuisinier</a></td>
      </tr>
    </table>
    """
    server.script("/search", _html(html))
    results = AnnasArchive(server.url("")).search(
        SearchQuery("Le Cuisinier"), network=annas_network, limit=3
    )
    assert results and results[0].date is None


def test_search_sends_the_table_query_with_the_creator(
    server: ScriptedServer, annas_network: Network
) -> None:
    server.script("/search", _html(TABLE))
    AnnasArchive(server.url("")).search(
        SearchQuery("Le Cuisinier", creator="La Varenne"),
        network=annas_network,
        limit=3,
    )
    (arrival,) = server.arrivals
    assert "q=Le+Cuisinier+La+Varenne" in arrival.path
    assert "display=table" in arrival.path
    assert "ext=pdf" in arrival.path


def test_the_search_fixture_parses_into_records() -> None:
    records = parse_search_page(
        _fixture("search_table.html"), base_url="https://mirror.example", limit=5
    )
    assert [r.source_id for r in records] == [
        MD5,
        "96e3c65b489f21279cc996dd5c8f5d41",
    ]
    first, second = records
    assert first.provider == KEY
    assert first.title == "Le Cuisinier françois"
    assert first.creators == ("La Varenne", "Pierre de Lune")
    assert (first.date, first.year) == ("1651", 1651)
    assert first.item_url == f"https://mirror.example/md5/{MD5}"
    assert first.raw["md5"] == MD5
    assert "Le Cuisinier françois" in first.raw["title_candidates"]
    assert (second.title, second.creators, second.year) == (
        "Wiener Kochbuch",
        ("Anna Dorn",),
        1827,
    )


def test_the_search_limit_caps_the_records() -> None:
    records = parse_search_page(
        _fixture("search_table.html"), base_url="https://mirror.example", limit=1
    )
    assert [r.source_id for r in records] == [MD5]


def test_a_page_without_rows_falls_back_to_md5_links() -> None:
    html = (
        "<div><a href='/md5/96e3c65b489f21279cc996dd5c8f5d41'>Wiener Kochbuch</a>"
        "<a href='/md5/not-a-hash'>broken</a></div>"
    )
    (record,) = parse_search_page(html, base_url="https://m.example", limit=5)
    assert record.source_id == "96e3c65b489f21279cc996dd5c8f5d41"
    assert record.title == "Wiener Kochbuch"
    assert record.creators == ()


def test_a_record_without_a_usable_title_gets_an_empty_title() -> None:
    html = "<div><a href='/md5/96e3c65b489f21279cc996dd5c8f5d41'>ab</a></div>"
    (record,) = parse_search_page(html, base_url="https://m.example", limit=5)
    assert record.title == ""


def test_a_parked_mirror_is_an_invalid_response_not_no_results(
    server: ScriptedServer, annas_network: Network
) -> None:
    server.script("/search", _html(_fixture("search_parked.html")))
    assert not is_annas_page(_fixture("search_parked.html"))
    with pytest.raises(InvalidResponse, match="parked or moved mirror"):
        AnnasArchive(server.url("")).search(
            SearchQuery("Wiener Kochbuch"), network=annas_network, limit=3
        )


def test_an_empty_result_page_of_the_site_is_no_results(
    server: ScriptedServer, annas_network: Network
) -> None:
    page = "<html><head><title>Search - Anna’s Archive</title></head></html>"
    server.script("/search", _html(page))
    assert (
        AnnasArchive(server.url("")).search(
            SearchQuery("Nothing"), network=annas_network, limit=3
        )
        == []
    )


def test_a_failed_search_raises_instead_of_returning_nothing(
    server: ScriptedServer, coord: Coordinator
) -> None:
    server.script("/search", Reply(503))
    settings = settings_with(KEY, max_attempts=1)
    with (
        Network(settings, coord, sleep=lambda _s: None) as network,
        pytest.raises(ProviderBlocked) as excinfo,
    ):
        AnnasArchive(server.url("")).search(
            SearchQuery("Wiener Kochbuch"), network=network, limit=3
        )
    assert excinfo.value.cause == "transient"


# Unconfigured mirror (decision D11)


def test_an_empty_base_url_makes_the_provider_unavailable(
    annas_network: Network, annas_slot: SlotToken, tmp_path: Path
) -> None:
    block = replace(PROVIDER_DEFAULTS[KEY], base_url="")
    provider = build(Settings(providers={**PROVIDER_DEFAULTS, KEY: block}))
    assert provider.available is False
    with pytest.raises(MirrorNotConfigured, match="base_url"):
        provider.search(SearchQuery("x"), network=annas_network, limit=3)
    with pytest.raises(MirrorNotConfigured):
        provider.download(
            MD5,
            network=annas_network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot),
        )


def test_the_registry_builds_the_adapter_with_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANNAS_ARCHIVE_API_KEY", SECRET)
    provider = Registry(Settings()).get(KEY)
    assert isinstance(provider, AnnasArchive)
    assert provider.capabilities.quota is True
    assert provider.capabilities.api_key is None
    assert SECRET not in repr(provider)


def test_without_a_key_no_quota_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANNAS_ARCHIVE_API_KEY", raising=False)
    assert build(Settings()).capabilities.quota is False


def test_the_default_mirror_is_the_official_gl_domain() -> None:
    assert build(Settings()).available is True
    assert PROVIDER_DEFAULTS[KEY].base_url == "https://annas-archive.gl"


def test_the_ssl_policy_stays_fail() -> None:
    assert PROVIDER_DEFAULTS[KEY].network.ssl_error_policy == "fail"


# Public links (v1.30.3 TestAnnasArchiveLinkFilter)


def test_fragment_in_href_no_longer_skips_the_download_link(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    page = (
        "<html><body>"
        f'<a href="/slow_download/{MD5}/0/0#download">Slow download</a>'
        '<a href="#top">Back to top</a>'
        '<a href="/login">Log in</a>'
        "</body></html>"
    )
    server.script(f"/md5/{MD5}", _html(page))
    server.script(f"/slow_download/{MD5}/0/0", pdf())
    result = AnnasArchive(server.url("")).download(
        MD5,
        network=annas_network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(annas_slot),
    )
    assert result.complete is True
    paths = _paths(server)
    assert f"/slow_download/{MD5}/0/0" in paths
    assert not any("top" in p for p in paths)
    assert not any("login" in p for p in paths)


def test_the_md5_page_fixture_orders_and_filters_its_links() -> None:
    page_url = f"https://mirror.example/md5/{MD5}"
    page = parse_md5_page(_fixture("md5_page.html"), page_url, MD5)
    assert page.links == (
        "https://files.example.org/books/le_cuisinier_francois.epub",
        "https://cdn.example.org/files/le_cuisinier_francois.pdf",
        f"https://mirror.example/slow_download/{MD5}/0/0#download",
        f"https://libgen.example/ads.php?md5={MD5}",
    )
    assert dict(page.metadata) == {
        "md5": MD5,
        "url": page_url,
        "title": "Le Cuisinier françois",
    }


def test_a_public_download_stages_the_document_and_the_page_metadata(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    server.script(f"/md5/{MD5}", _html(_md5_page("/files/book.pdf|Direct")))
    server.script("/files/book.pdf", pdf())
    staging = _staging(tmp_path)
    result = AnnasArchive(server.url("")).download(
        MD5,
        network=annas_network,
        staging_dir=staging,
        request=DownloadRequest(annas_slot),
    )
    assert result.complete
    assert result.quota_charged is False
    assert result.source_id == MD5
    assert _files(staging) == ["metadata/md5_page.json", "rendering.pdf"]
    assert (staging / "rendering.pdf").read_bytes() == PDF
    (rendering,) = (f for f in result.files if f.kind == "pdf")
    assert rendering.item == "rendering"


def test_a_login_page_link_is_discarded_and_the_next_link_tried(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    server.script(
        f"/md5/{MD5}",
        _html(_md5_page("/files/a.pdf|First", f"/slow_download/{MD5}/0/0|Slow")),
    )
    server.script("/files/a.pdf", _html(_fixture("login_page.html")))
    server.script(f"/slow_download/{MD5}/0/0", pdf())
    staging = _staging(tmp_path)
    result = AnnasArchive(server.url("")).download(
        MD5,
        network=annas_network,
        staging_dir=staging,
        request=DownloadRequest(annas_slot, include_metadata=False),
    )
    assert result.complete
    assert _files(staging) == ["rendering.pdf"]
    assert server.hits("/files/a.pdf") == 1


def test_a_page_whose_links_all_fail_leaves_the_rendering_missing(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    links = [f"/files/{n}.pdf|L{n}" for n in range(7)]
    server.script(f"/md5/{MD5}", _html(_md5_page(*links)))
    for n in range(7):
        server.script(f"/files/{n}.pdf", _html("<html><body>error</body></html>"))
    staging = _staging(tmp_path)
    result = AnnasArchive(server.url("")).download(
        MD5,
        network=annas_network,
        staging_dir=staging,
        request=DownloadRequest(annas_slot, include_metadata=False),
    )
    part = result.part("pdf")
    assert part is not None and part.missing == ("rendering",)
    assert not result.complete
    assert sum(server.hits(f"/files/{n}.pdf") for n in range(7)) == 5
    assert _files(staging) == []


def test_a_missing_md5_page_is_item_not_found(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    with pytest.raises(ItemNotFound):
        AnnasArchive(server.url("")).download(
            MD5,
            network=annas_network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot),
        )


def test_an_identifier_that_is_no_md5_is_item_not_found(
    annas_network: Network, annas_slot: SlotToken, tmp_path: Path
) -> None:
    with pytest.raises(ItemNotFound):
        AnnasArchive("https://mirror.example").download(
            "not-an-md5",
            network=annas_network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot),
        )


def test_a_present_rendering_sends_no_request(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    result = AnnasArchive(server.url(""), api_key=SECRET).download(
        MD5,
        network=annas_network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(annas_slot, present=frozenset({"rendering"})),
    )
    assert result.complete
    assert server.arrivals == []


def test_a_spent_pdf_budget_stops_before_any_request(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    result = AnnasArchive(server.url("")).download(
        MD5,
        network=annas_network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(annas_slot, budget=FileBudget({"pdf": 0})),
    )
    assert result.stopped == "budget"
    assert not result.complete
    assert server.arrivals == []


def test_page_images_are_unsupported(
    annas_network: Network, annas_slot: SlotToken, tmp_path: Path
) -> None:
    provider = AnnasArchive("https://mirror.example")
    with pytest.raises(UnsupportedCapability) as excinfo:
        provider.pages(MD5, network=annas_network, first_n=3, max_px=800)
    assert excinfo.value.capability == "pages"
    with pytest.raises(UnsupportedCapability) as excinfo:
        provider.download(
            MD5,
            network=annas_network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot, format="images"),
        )
    assert excinfo.value.capability == "images"


# Fast-download API and quota


def test_the_fast_download_fixtures_read_as_offer_none_and_spent_quota() -> None:
    offer = read_fast_download(json.loads(_fixture("fast_download.json")))
    assert offer.download_url is not None
    assert offer.download_url.endswith("le_cuisinier_francois.pdf")
    assert offer.account is not None and offer.account["downloads_left"] == 874
    none = read_fast_download(json.loads(_fixture("fast_download_no_offer.json")))
    assert (none.download_url, none.error) == (None, "")
    spent = read_fast_download(json.loads(_fixture("fast_download_quota.json")))
    assert spent.quota_exhausted is True
    assert spent.download_url is None


def test_a_fast_download_charges_one_quota_unit_and_skips_the_public_page(
    server: ScriptedServer,
    coord: Coordinator,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    server.script("/dyn/api/fast_download.json", _fast_answer(server))
    server.script("/fast/file.pdf", pdf())
    staging = _staging(tmp_path)
    result = AnnasArchive(server.url(""), api_key=SECRET).download(
        MD5,
        network=annas_network,
        staging_dir=staging,
        request=DownloadRequest(annas_slot),
    )
    assert result.complete
    assert result.quota_charged is True
    assert coord.quota_status(KEY).used == 1
    assert _files(staging) == ["metadata/fast_download.json", "rendering.pdf"]
    assert server.hits(f"/md5/{MD5}") == 0
    api = next(a for a in server.arrivals if a.path.startswith("/dyn/api/"))
    assert f"md5={MD5}" in api.path
    assert f"key={SECRET}" in api.path
    assert SECRET not in (staging / "metadata" / "fast_download.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("fast_download_no_offer.json", id="no-offer"),
        pytest.param(None, id="204"),
    ],
)
def test_no_fast_offer_falls_back_to_the_public_links(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
    answer: str | None,
) -> None:
    reply = _fast_answer(server, answer) if answer else Reply(204)
    server.script("/dyn/api/fast_download.json", reply)
    server.script(f"/md5/{MD5}", _html(_md5_page("/files/book.pdf|Direct")))
    server.script("/files/book.pdf", pdf())
    result = AnnasArchive(server.url(""), api_key=SECRET).download(
        MD5,
        network=annas_network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(annas_slot),
    )
    assert result.complete
    assert result.quota_charged is True
    assert server.hits("/files/book.pdf") == 1


def test_a_quota_the_server_reports_spent_stops_without_fallback(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    server.script(
        "/dyn/api/fast_download.json", _fast_answer(server, "fast_download_quota.json")
    )
    result = AnnasArchive(server.url(""), api_key=SECRET).download(
        MD5,
        network=annas_network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(annas_slot),
    )
    assert result.stopped == "quota_spent"
    assert result.quota_charged is True
    assert not result.complete
    assert server.hits(f"/md5/{MD5}") == 0


def test_a_spent_local_quota_stops_before_the_api_is_asked(
    server: ScriptedServer,
    coord: Coordinator,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    server.script("/dyn/api/fast_download.json", _fast_answer(server))
    server.script("/fast/file.pdf", pdf())
    provider = AnnasArchive(server.url(""), api_key=SECRET)
    with _client(coord, daily_limit=1) as network:
        first = provider.download(
            MD5,
            network=network,
            staging_dir=_staging(tmp_path / "a"),
            request=DownloadRequest(annas_slot),
        )
        second = provider.download(
            MD5,
            network=network,
            staging_dir=_staging(tmp_path / "b"),
            request=DownloadRequest(annas_slot),
        )
    assert first.complete and first.quota_charged
    assert second.stopped == "quota_spent"
    assert second.quota_charged is False
    assert not second.complete
    assert _paths(server).count("/dyn/api/fast_download.json") == 1
    assert server.hits(f"/md5/{MD5}") == 0


def test_a_disabled_quota_charges_nothing(
    server: ScriptedServer, coord: Coordinator, annas_slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/dyn/api/fast_download.json", _fast_answer(server))
    server.script("/fast/file.pdf", pdf())
    with _client(coord, enabled=False) as network:
        result = AnnasArchive(server.url(""), api_key=SECRET, quota=False).download(
            MD5,
            network=network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot),
        )
    assert result.complete
    assert result.quota_charged is False
    assert coord.quota_status(KEY).used == 0


def test_a_refused_key_names_the_refusal_and_gives_back_its_quota_unit(
    server: ScriptedServer,
    coord: Coordinator,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    """Live round F1: the fast API answered 403 ``{"error": "Not a member"}``
    and the public page 403 (DDoS-Guard); the tool said only "blocked (HTTP
    403)" and kept the quota unit charged."""
    refusal = json.dumps({"error": f"Not a member (key {SECRET})"}).encode()
    server.script(
        "/dyn/api/fast_download.json",
        Reply(403, refusal, {"Content-Type": "application/json"}),
    )
    server.script(f"/md5/{MD5}", Reply(403, b"DDoS-Guard"))
    with pytest.raises(ProviderBlocked) as caught:
        AnnasArchive(server.url(""), api_key=SECRET).download(
            MD5,
            network=annas_network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot),
        )
    message = str(caught.value)
    assert "fast-download API refused: HTTP 403: Not a member" in message
    assert SECRET not in message
    assert _paths(server).count("/dyn/api/fast_download.json") == 1
    assert coord.quota_status(KEY).used == 0


def test_a_refusal_after_the_api_documentation_keeps_its_error_field(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    """Live round N2: the member API's 403 body opens with its documentation
    lines and names the error last; the reason kept only the first
    characters, so "Not a member" never reached it."""
    documentation = {
        "///download_url": [
            "This API is intended as a stable JSON API for getting fast "
            "download files as a member.",
            "A successful request will return status code 200 or 204, a "
            "`download_url` field and `account_fast_download_info`.",
            *(f"Documentation line {n} of the fast-download API." for n in range(40)),
        ],
        "download_url": None,
        "///error": ["If there was an error, this field is set and the code is 4xx."],
        "error": "Not a member",
    }
    body = json.dumps(documentation, indent=2).encode()
    assert len(body) > 2000
    server.script(
        "/dyn/api/fast_download.json",
        Reply(403, body, {"Content-Type": "application/json"}),
    )
    server.script(f"/md5/{MD5}", Reply(403, b"DDoS-Guard"))
    with pytest.raises(ProviderBlocked) as caught:
        AnnasArchive(server.url(""), api_key=SECRET).download(
            MD5,
            network=annas_network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(annas_slot),
        )
    message = str(caught.value)
    assert message.endswith("fast-download API refused: HTTP 403: Not a member")
    assert SECRET not in message


def test_a_failed_fast_download_file_falls_back_to_the_public_links(
    server: ScriptedServer,
    annas_network: Network,
    annas_slot: SlotToken,
    tmp_path: Path,
) -> None:
    server.script("/dyn/api/fast_download.json", _fast_answer(server))
    server.script("/fast/file.pdf", _html(_fixture("login_page.html")))
    server.script(f"/md5/{MD5}", _html(_md5_page("/files/book.pdf|Direct")))
    server.script("/files/book.pdf", pdf())
    staging = _staging(tmp_path)
    result = AnnasArchive(server.url(""), api_key=SECRET).download(
        MD5,
        network=annas_network,
        staging_dir=staging,
        request=DownloadRequest(annas_slot),
    )
    assert result.complete and result.quota_charged
    assert _files(staging) == [
        "metadata/fast_download.json",
        "metadata/md5_page.json",
        "rendering.pdf",
    ]


def test_the_member_key_never_reaches_the_logs(
    server: ScriptedServer,
    coord: Coordinator,
    annas_slot: SlotToken,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.script(
        "/dyn/api/fast_download.json",
        Reply(500),
        json_reply({"download_url": None, "error": "Invalid key"}),
    )
    server.script(f"/md5/{MD5}", _html(_md5_page("/files/book.pdf|Direct")))
    server.script("/files/book.pdf", pdf())
    caplog.set_level(logging.DEBUG, logger="chronodownloader")
    provider = AnnasArchive(server.url(""), api_key=SECRET)
    with _client(coord) as network:
        for name in ("a", "b"):
            provider.download(
                MD5,
                network=network,
                staging_dir=_staging(tmp_path / name),
                request=DownloadRequest(annas_slot),
            )
    assert caplog.records
    assert SECRET not in caplog.text
    assert SECRET not in repr(provider)
    staged = "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
    assert SECRET not in staged


# Login-page detection (v1.30.3 api/core/download.py)


def test_annas_archive_login_page() -> None:
    """``_should_reject_html_response``: an HTML body of about 180 KB."""
    body = b"<html><body>" + b"x" * 179_000 + b"</body></html>"
    assert login_page_reason("text/html", len(body), body[:4096])


def test_annas_archive_login_detected() -> None:
    head = b"<html><title>log in / register</title></html>"
    assert login_page_reason("text/html", len(head), head)


def test_annas_archive_normal_page_accepted() -> None:
    head = b"<html><title>Download Page</title></html>"
    assert login_page_reason("text/html", len(head), head) == ""


def test_a_pdf_in_the_size_window_is_no_login_page() -> None:
    assert login_page_reason("application/pdf", 180_000, PDF) == ""


def test_the_login_page_fixture_is_detected() -> None:
    body = (FIXTURES / "login_page.html").read_bytes()
    assert login_page_reason("text/html; charset=utf-8", len(body), body)
    assert any(marker in body.decode("utf-8").lower() for marker in LOGIN_MARKERS)
