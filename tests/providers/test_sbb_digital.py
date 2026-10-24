"""The SBB adapter: StaBiKat SRU search and METS-driven downloads.

Ported from v1.30.3 ``tests/integration/test_providers.py``
(``TestSbbDigitalProvider``), ``tests/unit/test_provider_parsing.py``
(``TestSbbMetsPdfDeduplication``) and ``tests/unit/test_provider_metadata.py``
(``TestSbbRecordIdentifier``). The patched ``make_request`` and
``download_file`` are replaced by a local server behind the real network
client; ``get_max_pages``, ``prefer_pdf_over_images`` and ``budget_exhausted``
become the request's ``max_pages``, ``format`` and ``budget``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    DownloadRequest,
    ItemNotFound,
    ProviderBlocked,
    SearchQuery,
)
from chronodownloader.providers.sbb_digital import (
    METS_URL,
    SbbDigital,
    build,
    candidate_queries,
    collect_mets_urls,
    parse_sru_records,
)
from chronodownloader.settings import Settings
from tests.iiif.support import jpeg, pdf
from tests.net.support import Reply, ScriptedServer, ScriptedSession, settings_with

from .fakes import FileBudget

KEY = "sbb_digital"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers" / KEY
QUERY = SearchQuery("Allgemeines deutsches Kochbuch")
DATED_QUERY = SearchQuery("Allgemeines deutsches Kochbuch", year=1846)


@pytest.fixture
def net(coord: Coordinator) -> Iterator[Network]:
    with Network(
        settings_with(KEY, max_attempts=1), coord, sleep=lambda _s: None
    ) as client:
        yield client


@pytest.fixture
def sbb_slot(coord: Coordinator) -> SlotToken:
    token = coord.acquire_slot(KEY, 1)
    assert token is not None
    return token


def _mets(pdfs: list[str], images: list[str]) -> bytes:
    def group(use: str, mimetype: str, urls: list[str]) -> str:
        files = "".join(
            f'<mets:file MIMETYPE="{mimetype}">'
            f'<mets:FLocat xlink:href="{url}"/></mets:file>'
            for url in urls
        )
        return f'<mets:fileGrp USE="{use}">{files}</mets:fileGrp>'

    return (
        '<?xml version="1.0"?>'
        '<mets:mets xmlns:mets="http://www.loc.gov/METS/" '
        'xmlns:xlink="http://www.w3.org/1999/xlink"><mets:fileSec>'
        + group("DOWNLOAD", "application/pdf", pdfs)
        + group("DEFAULT", "image/jpeg", images)
        + "</mets:fileSec></mets:mets>"
    ).encode()


def _adapter(server: ScriptedServer, pdfs: int, images: int) -> SbbDigital:
    """Serve a METS of *pdfs* PDF and *images* image files for PPN123."""
    pdf_urls = [server.script(f"/p{n}.pdf", pdf()) for n in range(1, pdfs + 1)]
    image_urls = [server.script(f"/i{n}.jpg", jpeg()) for n in range(1, images + 1)]
    server.script(
        "/mets/PPN123",
        Reply(200, _mets(pdf_urls, image_urls), {"Content-Type": "application/xml"}),
    )
    return SbbDigital(
        sru_url=server.url("/stabikat"), mets_url=server.url("/mets/{ppn}")
    )


def _files_fetched(server: ScriptedServer) -> list[str]:
    return [a.path for a in server.arrivals if not a.path.startswith("/mets/")]


def _staging(tmp_path: Path) -> Path:
    return tmp_path / "work" / "objects" / ".staging" / "1"


def _sru(xml: str) -> Reply:
    return Reply(200, xml.encode("utf-8"), {"Content-Type": "application/xml"})


def _volume_sru(own_ppn: str | None = "PPN723456789") -> str:
    """A multi-volume item as the GBV SRU endpoint returns it.

    The volume's own recordInfo sits at the top level of the MODS record;
    the series it belongs to carries a recordInfo of its own inside
    ``<relatedItem type="host">``, which comes first in document order.
    """
    own = (
        f"""<mods:recordInfo>
            <mods:recordIdentifier source="gbv-ppn">{own_ppn}</mods:recordIdentifier>
          </mods:recordInfo>"""
        if own_ppn
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:version>1.2</srw:version>
  <srw:numberOfRecords>1</srw:numberOfRecords>
  <srw:records>
    <srw:record>
      <srw:recordSchema>mods</srw:recordSchema>
      <srw:recordPacking>xml</srw:recordPacking>
      <srw:recordData>
        <mods:mods xmlns:mods="http://www.loc.gov/mods/v3" version="3.7">
          <mods:titleInfo>
            <mods:title>Allgemeines deutsches Kochbuch</mods:title>
            <mods:subTitle>Zweiter Band</mods:subTitle>
          </mods:titleInfo>
          <mods:name type="personal">
            <mods:namePart>Loeffler, Friederike Luise</mods:namePart>
          </mods:name>
          <mods:originInfo>
            <mods:place><mods:placeTerm>Stuttgart</mods:placeTerm></mods:place>
            <mods:dateIssued>1846</mods:dateIssued>
          </mods:originInfo>
          <mods:relatedItem type="host">
            <mods:titleInfo>
              <mods:title>Allgemeines deutsches Kochbuch</mods:title>
            </mods:titleInfo>
            <mods:recordInfo>
              <mods:recordIdentifier source="gbv-ppn"
                >PPN100000000</mods:recordIdentifier>
            </mods:recordInfo>
          </mods:relatedItem>
          {own}
        </mods:mods>
      </srw:recordData>
      <srw:recordPosition>1</srw:recordPosition>
    </srw:record>
  </srw:records>
</srw:searchRetrieveResponse>
"""


# Download loops (v1.30.3 TestSbbDigitalProvider)


def test_pdf_loop_honours_max_pages(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    """Per-page PDF filegroups can hold one PDF per page, so the PDF loop
    applies the same page limit as the image loop."""
    adapter = _adapter(server, pdfs=4, images=0)
    result = adapter.download(
        "PPN123",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(sbb_slot, format="both", max_pages=2),
    )
    assert result.complete
    assert len(_files_fetched(server)) == 2


def test_pdf_loop_stops_on_exhausted_budget(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    """The PDF loop respects the download budget."""
    adapter = _adapter(server, pdfs=2, images=0)
    result = adapter.download(
        "PPN123",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(
            sbb_slot, format="both", budget=FileBudget({"pdf": 0, "image": 0})
        ),
    )
    assert not result.complete
    assert result.stopped == "budget"
    assert _files_fetched(server) == []


def test_prefer_pdf_does_not_cut_the_pdf_loop_short(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    """Returning after the first PDF nullified the page cap: a per-page PDF
    group downloaded page one and reported the work complete."""
    adapter = _adapter(server, pdfs=3, images=1)
    result = adapter.download(
        "PPN123",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(sbb_slot, format="pdf"),
    )
    assert result.complete
    # Every PDF, and no image once the PDFs landed.
    assert _files_fetched(server) == ["/p1.pdf", "/p2.pdf", "/p3.pdf"]


def test_prefer_pdf_still_honours_max_pages(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    """The cap applies whether or not PDFs are preferred over images."""
    adapter = _adapter(server, pdfs=4, images=1)
    result = adapter.download(
        "PPN123",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(sbb_slot, format="pdf", max_pages=2),
    )
    assert result.complete
    assert _files_fetched(server) == ["/p1.pdf", "/p2.pdf"]


def test_images_are_still_tried_when_no_pdf_succeeds(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    """The image fallback survives a PDF group that fails outright."""
    adapter = _adapter(server, pdfs=1, images=2)
    server.script("/p1.pdf", Reply(404))
    result = adapter.download(
        "PPN123",
        network=net,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(sbb_slot, format="pdf"),
    )
    assert _files_fetched(server) == ["/p1.pdf", "/i1.jpg", "/i2.jpg"]
    pdf_part, images = result.part("pdf"), result.part("images")
    assert pdf_part is not None and pdf_part.missing == ("rendering",)
    assert images is not None and images.complete
    assert not result.complete


METS_MULTI_FILEGRP = (
    '<?xml version="1.0"?>'
    '<mets:mets xmlns:mets="http://www.loc.gov/METS/" '
    'xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<mets:fileSec><mets:fileGrp USE="THUMBS">'
    '<mets:file MIMETYPE="image/jpeg">'
    '<mets:FLocat xlink:href="https://x/t1.jpg"/></mets:file>'
    '<mets:file MIMETYPE="image/jpeg">'
    '<mets:FLocat xlink:href="https://x/t2.jpg"/></mets:file>'
    "</mets:fileGrp>"
    '<mets:fileGrp USE="MAX">'
    '<mets:file MIMETYPE="image/jpeg">'
    '<mets:FLocat xlink:href="https://x/m1.jpg"/></mets:file>'
    '<mets:file MIMETYPE="image/jpeg">'
    '<mets:FLocat xlink:href="https://x/m2.jpg"/></mets:file>'
    "</mets:fileGrp></mets:fileSec></mets:mets>"
)


def test_mets_collects_one_filegrp_not_every_resolution() -> None:
    """Collecting across every fileGrp fetched each page three to five times
    and spent max_pages on thumbnails, which come first in document order."""
    _pdfs, images = collect_mets_urls(METS_MULTI_FILEGRP)
    assert images == ["https://x/m1.jpg", "https://x/m2.jpg"]


def test_mets_without_use_labels_keeps_every_image() -> None:
    """An unlabeled fileGrp cannot be ranked, so nothing is dropped."""
    mets = (
        '<?xml version="1.0"?>'
        '<mets:mets xmlns:mets="http://www.loc.gov/METS/" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        "<mets:fileSec><mets:fileGrp>"
        '<mets:file MIMETYPE="image/jpeg">'
        '<mets:FLocat xlink:href="https://x/a.jpg"/></mets:file>'
        '<mets:file MIMETYPE="application/pdf">'
        '<mets:FLocat xlink:href="https://x/a.pdf"/></mets:file>'
        "</mets:fileGrp></mets:fileSec></mets:mets>"
    )
    pdfs, images = collect_mets_urls(mets)
    assert pdfs == ["https://x/a.pdf"]
    assert images == ["https://x/a.jpg"]


def test_duplicate_pdf_href_is_collected_once() -> None:
    """A PDF listed in several fileGrps is downloaded once, not per group."""
    mets = """<mets:mets xmlns:mets="http://www.loc.gov/METS/"
                         xmlns:xlink="http://www.w3.org/1999/xlink">
      <mets:fileSec>
        <mets:fileGrp USE="DEFAULT">
          <mets:file MIMETYPE="application/pdf">
            <mets:FLocat xlink:href="https://sbb.example/doc.pdf"/>
          </mets:file>
        </mets:fileGrp>
        <mets:fileGrp USE="DOWNLOAD">
          <mets:file MIMETYPE="application/pdf">
            <mets:FLocat xlink:href="https://sbb.example/doc.pdf"/>
          </mets:file>
        </mets:fileGrp>
      </mets:fileSec>
    </mets:mets>"""
    pdf_urls, _ = collect_mets_urls(mets)
    assert pdf_urls == ["https://sbb.example/doc.pdf"]


# Record identifiers (v1.30.3 TestSbbRecordIdentifier)


def test_the_volumes_own_ppn_wins_over_the_host_record(
    server: ScriptedServer, net: Network
) -> None:
    """A volume is downloaded under its own PPN, not the series'."""
    server.script("/stabikat", _sru(_volume_sru()))
    adapter = SbbDigital(sru_url=server.url("/stabikat"))
    results = adapter.search(QUERY, network=net, limit=3)
    assert len(results) == 1
    first = results[0]
    assert first.source_id == "PPN723456789"
    assert first.raw["mets_url"].endswith("PPN723456789")
    assert first.raw["item_url"].endswith("PPN723456789")
    assert first.creators == ("Loeffler, Friederike Luise",)
    assert first.date == "1846"


def test_a_record_with_only_a_host_ppn_is_skipped(
    server: ScriptedServer, net: Network
) -> None:
    """Without its own identifier the record cannot be resolved."""
    server.script("/stabikat", _sru(_volume_sru(own_ppn=None)))
    adapter = SbbDigital(sru_url=server.url("/stabikat"))
    assert adapter.search(QUERY, network=net, limit=3) == []


# Search failures (phase 3 fix: SBB timeouts)


def test_a_timeout_is_transient_and_stops_the_variants(coord: Coordinator) -> None:
    """A timed-out variant is not "no result", and the next variant is not
    sent to the same unreachable host."""
    session = ScriptedSession(requests.exceptions.ConnectTimeout("connect timed out"))
    settings = settings_with(KEY, max_attempts=2)
    with (
        Network(
            settings, coord, session_factory=lambda: session, sleep=lambda _s: None
        ) as client,
        pytest.raises(ProviderBlocked) as excinfo,
    ):
        SbbDigital().search(
            SearchQuery("Koch-Buch", creator="Anon"), network=client, limit=3
        )
    assert excinfo.value.cause == "transient"
    queries = {call["params"]["query"] for call in session.calls}
    assert queries == {"pica.xtit=koch-buch and pica.xper=anon"}
    assert len(session.calls) == 2


def test_a_rejected_variant_moves_on_to_the_next(
    server: ScriptedServer, net: Network
) -> None:
    server.script("/stabikat", Reply(400, b"bad index"), _sru(_volume_sru()))
    adapter = SbbDigital(sru_url=server.url("/stabikat"))
    results = adapter.search(DATED_QUERY, network=net, limit=3)
    assert [r.source_id for r in results] == ["PPN723456789"]
    sent = [parse_qs(urlsplit(a.path).query)["query"][0] for a in server.arrivals]
    assert sent == candidate_queries(DATED_QUERY.title, None, DATED_QUERY.year)[:2]


def test_every_variant_rejected_is_reported(
    server: ScriptedServer, net: Network
) -> None:
    server.script("/stabikat", Reply(400, b"bad index"))
    adapter = SbbDigital(sru_url=server.url("/stabikat"))
    with pytest.raises(ProviderBlocked) as excinfo:
        adapter.search(DATED_QUERY, network=net, limit=3)
    assert excinfo.value.cause == "rejected"
    assert len(server.arrivals) == 2


@pytest.mark.parametrize(
    ("title", "creator", "year", "expected"),
    [
        pytest.param(
            "Das Brandenburgische Koch-Buch",
            "Johann Andreas Rüdiger",
            1732,
            [
                "pica.xtit=brandenburgische and pica.xtit=koch-buch"
                " and pica.xper=rüdiger",
                "pica.xtit=brandenburgische and pica.xtit=koch-buch",
            ],
            id="two-words-no-year-variant",
        ),
        pytest.param(
            "Unterricht, alle Arten Gefrorenes zu verfertigen",
            None,
            1812,
            [
                "pica.xtit=unterricht and pica.xtit=alle and pica.xtit=arten"
                " and pica.xtit=gefrorenes and pica.xtit=verfertigen",
                "pica.xtit=verfertigen and pica.xtit=unterricht and pica.xjah=1812",
            ],
            id="year-variant",
        ),
        pytest.param("De la", None, None, [], id="no-searchable-word"),
    ],
)
def test_candidate_queries_use_the_k10plus_word_indexes(
    title: str, creator: str | None, year: int | None, expected: list[str]
) -> None:
    assert candidate_queries(title, creator, year) == expected


# Recorded responses


def test_fixture_sru_search_parses() -> None:
    records = parse_sru_records((FIXTURES / "sru_search.xml").read_bytes(), limit=5)
    assert [r.source_id for r in records] == ["PPN723456789", "PPN598254749"]
    first, second = records
    assert first.title == "Allgemeines deutsches Kochbuch Zweiter Band"
    assert first.year == 1846
    assert first.raw["mets_url"] == METS_URL.format(ppn="PPN723456789")
    # A subject heading is not the creator.
    assert second.creators == ()
    assert (second.date, second.year) == ("[ca. 1839]", 1839)


def test_fixture_mets_picks_the_default_group() -> None:
    pdfs, images = collect_mets_urls((FIXTURES / "mets_PPN598254749.xml").read_bytes())
    assert pdfs == []
    assert images == [
        "https://content.staatsbibliothek-berlin.de/dc/598254749-0000000"
        f"{n}/full/max/0/default.jpg"
        for n in (1, 2, 3)
    ]


def test_pages_come_from_the_mets_at_reduced_size(
    server: ScriptedServer, net: Network
) -> None:
    mets = (FIXTURES / "mets_PPN598254749.xml").read_bytes()
    server.script("/mets/PPN598254749", Reply(200, mets))
    adapter = SbbDigital(mets_url=server.url("/mets/{ppn}"))
    refs = adapter.pages("598254749", network=net, first_n=2, max_px=800)
    assert [(r.item, r.index) for r in refs] == [("p00001", 1), ("p00002", 2)]
    base = "https://content.staatsbibliothek-berlin.de/dc/598254749-00000001"
    assert refs[0].service == base
    assert refs[0].urls[0] == f"{base}/full/!800,800/0/default.jpg"


def test_download_stages_metadata_and_page_images(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    adapter = _adapter(server, pdfs=0, images=2)
    staging = _staging(tmp_path)
    result = adapter.download(
        "123", network=net, staging_dir=staging, request=DownloadRequest(sbb_slot)
    )
    assert result.source_id == "PPN123"
    assert result.complete
    assert [f.name for f in result.files] == [
        "metadata/mets.json",
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert (staging / "p00002.jpg").is_file()


def test_a_missing_mets_is_item_not_found(
    server: ScriptedServer, net: Network, sbb_slot: SlotToken, tmp_path: Path
) -> None:
    adapter = SbbDigital(mets_url=server.url("/mets/{ppn}"))
    with pytest.raises(ItemNotFound):
        adapter.download(
            "PPN404",
            network=net,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(sbb_slot),
        )


def test_build_returns_the_adapter() -> None:
    adapter = build(Settings())
    assert adapter.key == KEY
    assert not adapter.capabilities.iiif
