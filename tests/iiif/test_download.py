"""Downloading IIIF items through the real network client to a local server.

Ported from v1.30.3 ``tests/unit/test_iiif.py`` (``TestDownloadOneFromService``,
``TestFetchInfoJson``, ``TestMixedManifestStrategy``, rendering limits and
provenance), ``tests/unit/test_download_helpers.py`` (page loops,
manifest orchestration, rendering placement) and the IIIF strategy tests of
``tests/integration/test_providers.py``. v1.30.3 mocked the download
function and counted calls; here a local server answers and the assertions
read the staged files, the server's arrivals and the result's completeness.
The info.json cache and the configurable rendering limit are gone; budget
checks go through the request's budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chronodownloader.coord import BreakerState, BreakerStatus
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    DownloadFormat,
    DownloadRequest,
    DownloadResult,
    FileKind,
    InvalidResponse,
    ItemNotFound,
    ProviderBlocked,
)
from chronodownloader.providers.iiif_download import (
    download_manifest,
    fetch_info,
    fetch_json,
    stage_service_image,
)
from tests.net.conftest import NetworkFactory
from tests.net.support import (
    NOW,
    SLOT,
    FakeCoordinator,
    Reply,
    ScriptedServer,
    settings_with,
)

from .support import (
    FULL,
    HTML,
    PNG,
    direct_canvas,
    jpeg,
    json_reply,
    pdf,
    serve_pages,
    service_canvas,
    service_urls,
    v2_manifest,
)

PROVIDER = "mdz"


@pytest.fixture
def network(make_network: NetworkFactory) -> Network:
    return make_network(settings_with(PROVIDER))


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    return tmp_path / "objects" / ".staging" / "1"


@dataclass
class CountingBudget:
    """Allows *limits[kind]* files per kind and records the charges."""

    limits: Mapping[str, int] = field(default_factory=dict)
    charged: list[tuple[FileKind, int]] = field(default_factory=list)
    checks: int = 0

    def allows(self, kind: FileKind) -> bool:
        self.checks += 1
        used = sum(1 for charged_kind, _ in self.charged if charged_kind == kind)
        return used < self.limits.get(kind, 1_000_000)

    def charge(self, kind: FileKind, size: int) -> None:
        self.charged.append((kind, size))


def _download(
    network: Network,
    manifest: Mapping[str, Any],
    staging: Path,
    *,
    fmt: DownloadFormat = "images",
    image_max_px: int | None = None,
    **request: Any,
) -> DownloadResult:
    return download_manifest(
        network,
        PROVIDER,
        manifest,
        source_id="item1",
        staging_dir=staging,
        request=DownloadRequest(SLOT, format=fmt, **request),
        manifest_url="https://example.org/manifest.json",
        image_max_px=image_max_px,
    )


def _staged(staging: Path) -> list[str]:
    return sorted(
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    )


class TestStageServiceImage:
    def test_succeeds_on_first_candidate(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        (base,) = serve_pages(server, "img1")
        staged = stage_service_image(
            network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
        )
        assert staged is not None
        assert staged.name == "p00001.jpg"
        assert staged.kind == "image"
        assert len(server.arrivals) == 1
        assert _staged(staging) == ["p00001.jpg"]

    def test_falls_back_to_info_json(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script("/iiif/img1/info.json", json_reply({"sizes": [{"width": 2000}]}))
        server.script("/iiif/img1/full/2000,/0/default.jpg", jpeg())
        base = server.url("/iiif/img1")
        staged = stage_service_image(
            network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
        )
        assert staged is not None
        assert server.hits("/iiif/img1/info.json") == 1
        assert server.hits("/iiif/img1/full/2000,/0/default.jpg") == 1

    def test_speculative_attempts_are_bounded_before_info_json(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        (base,) = service_urls(server, "img1")
        assert (
            stage_service_image(
                network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
            )
            is None
        )
        paths = [arrival.path for arrival in server.arrivals]
        assert paths == [
            "/iiif/img1/full/full/0/default.jpg",
            "/iiif/img1/full/max/0/default.jpg",
            "/iiif/img1/full/pct:100/0/default.jpg",
            "/iiif/img1/info.json",
        ]

    def test_returns_none_when_all_fail(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script("/iiif/img1/info.json", json_reply({"sizes": [{"width": 1000}]}))
        (base,) = service_urls(server, "img1")
        assert (
            stage_service_image(
                network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
            )
            is None
        )
        assert server.hits("/iiif/img1/full/1000,/0/default.jpg") == 1
        assert _staged(staging) == []

    def test_handles_an_unreadable_info_json(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script("/iiif/img1/info.json", Reply(200, b"{not json"))
        (base,) = service_urls(server, "img1")
        assert (
            stage_service_image(
                network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
            )
            is None
        )

    def test_a_refusal_ends_the_attempts_for_the_page(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script(f"/iiif/img1{FULL}", Reply(403))
        (base,) = service_urls(server, "img1")
        assert (
            stage_service_image(
                network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
            )
            is None
        )
        assert len(server.arrivals) == 1

    def test_an_html_body_is_not_a_page(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script(
            f"/iiif/img1{FULL}", Reply(200, HTML, {"Content-Type": "text/html"})
        )
        server.script("/iiif/img1/full/max/0/default.jpg", jpeg())
        (base,) = service_urls(server, "img1")
        staged = stage_service_image(
            network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
        )
        assert staged is not None
        assert _staged(staging) == ["p00001.jpg"]

    def test_the_payload_type_decides_the_extension(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script(
            f"/iiif/img1{FULL}", Reply(200, PNG, {"Content-Type": "image/png"})
        )
        (base,) = service_urls(server, "img1")
        staged = stage_service_image(
            network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=None
        )
        assert staged is not None
        assert staged.name == "p00001.png"

    def test_a_cap_bounds_the_requested_size(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        server.script(
            "/iiif/img1/info.json", json_reply({"width": 3000, "height": 4000})
        )
        server.script("/iiif/img1/full/768,/0/default.jpg", jpeg())
        (base,) = service_urls(server, "img1")
        staged = stage_service_image(
            network, PROVIDER, base, staging, "p00001", slot=SLOT, max_px=1024
        )
        assert staged is not None
        assert [arrival.path for arrival in server.arrivals] == [
            "/iiif/img1/full/!1024,1024/0/default.jpg",
            "/iiif/img1/info.json",
            "/iiif/img1/full/768,/0/default.jpg",
        ]


class TestFetchInfo:
    def test_fetches_the_document(
        self, server: ScriptedServer, network: Network
    ) -> None:
        server.script(
            "/iiif/img1/info.json", json_reply({"width": 1000, "height": 800})
        )
        info = fetch_info(network, PROVIDER, server.url("/iiif/img1/"), slot=SLOT)
        assert info == {"width": 1000, "height": 800}
        assert server.hits("/iiif/img1/info.json") == 1

    def test_returns_none_on_failure(
        self, server: ScriptedServer, network: Network
    ) -> None:
        assert (
            fetch_info(network, PROVIDER, server.url("/iiif/img1"), slot=SLOT) is None
        )


class TestFetchJson:
    def test_returns_the_object(self, server: ScriptedServer, network: Network) -> None:
        url = server.script("/m.json", json_reply({"label": "x"}))
        assert fetch_json(network, PROVIDER, url) == {"label": "x"}

    def test_not_found(self, server: ScriptedServer, network: Network) -> None:
        with pytest.raises(ItemNotFound) as excinfo:
            fetch_json(network, PROVIDER, server.url("/missing.json"), item="bsb1")
        assert excinfo.value.item == "bsb1"

    def test_blocked_carries_its_cause(
        self, server: ScriptedServer, network: Network
    ) -> None:
        url = server.script("/m.json", Reply(403))
        with pytest.raises(ProviderBlocked) as excinfo:
            fetch_json(network, PROVIDER, url)
        assert excinfo.value.cause == "blocked"

    def test_an_open_breaker_is_reported_as_blocked(
        self,
        server: ScriptedServer,
        network: Network,
        coordinator: FakeCoordinator,
    ) -> None:
        coordinator.breakers[PROVIDER] = BreakerState(
            BreakerStatus.OPEN, 3, NOW + 60.0, "blocked"
        )
        with pytest.raises(ProviderBlocked) as excinfo:
            fetch_json(network, PROVIDER, server.url("/m.json"))
        assert excinfo.value.cause == "breaker_open"
        assert excinfo.value.until == NOW + 60.0
        assert server.arrivals == []

    @pytest.mark.parametrize("body", [b"<html>", b"[1, 2]"])
    def test_a_body_that_is_no_json_object_is_invalid(
        self, server: ScriptedServer, network: Network, body: bytes
    ) -> None:
        url = server.script("/m.json", Reply(200, body))
        with pytest.raises(InvalidResponse):
            fetch_json(network, PROVIDER, url)


class TestDownloadPages:
    def test_downloads_all_pages(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        bases = serve_pages(server, "1", "2", "3")
        manifest = v2_manifest(*(service_canvas(base) for base in bases))
        result = _download(network, manifest, staging, include_metadata=False)
        images = result.part("images")
        assert images is not None
        assert images.expected == ("p00001", "p00002", "p00003")
        assert images.present == images.expected
        assert result.complete
        assert _staged(staging) == ["p00001.jpg", "p00002.jpg", "p00003.jpg"]
        assert [file.name for file in result.files] == _staged(staging)

    def test_respects_the_page_limit(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        bases = serve_pages(server, "1", "2", "3")
        manifest = v2_manifest(*(service_canvas(base) for base in bases))
        result = _download(network, manifest, staging, max_pages=2)
        images = result.part("images")
        assert images is not None
        assert images.expected == ("p00001", "p00002")
        assert images.page_limit == 2
        assert images.available == 3
        assert images.truncated
        assert result.complete
        assert server.hits(f"/iiif/3{FULL}") == 0

    def test_an_item_without_pages_is_not_complete(
        self, network: Network, staging: Path
    ) -> None:
        result = _download(network, v2_manifest(), staging)
        images = result.part("images")
        assert images is not None
        assert images.expected == ()
        assert not result.complete
        assert [file.name for file in result.files] == ["metadata/manifest.json"]

    def test_stops_on_budget_exhausted(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        bases = serve_pages(server, "1", "2")
        manifest = v2_manifest(*(service_canvas(base) for base in bases))
        budget = CountingBudget({"image": 0})
        result = _download(network, manifest, staging, budget=budget)
        assert server.arrivals == []
        assert result.stopped == "budget"
        assert not result.complete

    def test_stops_mid_loop_when_budget_runs_out(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        bases = serve_pages(server, "1", "2", "3")
        manifest = v2_manifest(*(service_canvas(base) for base in bases))
        budget = CountingBudget({"image": 1})
        result = _download(network, manifest, staging, budget=budget)
        images = result.part("images")
        assert images is not None
        assert images.present == ("p00001",)
        assert images.missing == ("p00002", "p00003")
        assert result.stopped == "budget"
        assert ("image", len(jpeg().body)) in budget.charged

    def test_a_failed_page_does_not_abort_the_work(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        missing = service_urls(server, "1")
        bases = missing + serve_pages(server, "2", "3")
        manifest = v2_manifest(*(service_canvas(base) for base in bases))
        result = _download(network, manifest, staging, include_metadata=False)
        images = result.part("images")
        assert images is not None
        assert images.present == ("p00002", "p00003")
        assert images.missing == ("p00001",)
        assert result.stopped is None
        assert not result.complete

    def test_direct_urls_get_page_numbered_names(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        first = server.script("/img/1", jpeg())
        second = server.script("/img/2.jpg", Reply(200, PNG, {"Content-Type": ""}))
        manifest = v2_manifest(direct_canvas(first), direct_canvas(second))
        result = _download(network, manifest, staging, include_metadata=False)
        assert result.complete
        assert _staged(staging) == ["p00001.jpg", "p00002.png"]

    def test_a_direct_url_of_unknown_type_is_missing(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script("/img/1", Reply(200, b"\x01\x02 unknown bytes"))
        result = _download(
            network, v2_manifest(direct_canvas(url)), staging, include_metadata=False
        )
        assert not result.complete
        assert _staged(staging) == []

    def test_both_kinds_of_canvas_are_downloaded(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        (base,) = serve_pages(server, "img1")
        direct = server.script("/p2.jpg", jpeg())
        manifest = v2_manifest(service_canvas(base), direct_canvas(direct))
        result = _download(network, manifest, staging, include_metadata=False)
        assert result.complete
        assert _staged(staging) == ["p00001.jpg", "p00002.jpg"]
        assert server.hits("/p2.jpg") == 1

    def test_metadata_is_staged_on_request(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        bases = serve_pages(server, "1")
        manifest = v2_manifest(service_canvas(bases[0]))
        result = _download(network, manifest, staging)
        metadata = [file for file in result.files if file.kind == "metadata"]
        assert [file.name for file in metadata] == ["metadata/manifest.json"]
        assert metadata[0].source_url == "https://example.org/manifest.json"
        assert (staging / "metadata" / "manifest.json").is_file()


def _rendering(url: str, fmt: str = "application/pdf", **extra: Any) -> dict[str, Any]:
    return {"@id": url, "format": fmt, **extra}


class TestRenderings:
    def test_pdf_takes_the_rendering_and_skips_the_images(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script("/v.pdf", pdf())
        bases = serve_pages(server, "1")
        manifest = v2_manifest(service_canvas(bases[0]), rendering=_rendering(url))
        result = _download(
            network, manifest, staging, fmt="pdf", include_metadata=False
        )
        assert [part.part for part in result.parts] == ["pdf"]
        assert result.complete
        assert _staged(staging) == ["rendering.pdf"]
        assert server.hits(f"/iiif/1{FULL}") == 0

    def test_a_dead_rendering_does_not_consume_the_limit(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        live = server.script("/live.pdf", pdf())
        manifest = v2_manifest(
            rendering=[_rendering(server.url("/dead.pdf")), _rendering(live)]
        )
        result = _download(
            network, manifest, staging, fmt="pdf", include_metadata=False
        )
        assert result.complete
        assert [arrival.path for arrival in server.arrivals] == [
            "/dead.pdf",
            "/live.pdf",
        ]

    def test_only_one_rendering_is_downloaded(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        urls = [server.script(f"/{i}.pdf", pdf()) for i in range(3)]
        manifest = v2_manifest(rendering=[_rendering(url) for url in urls])
        result = _download(
            network, manifest, staging, fmt="pdf", include_metadata=False
        )
        assert result.complete
        assert len(server.arrivals) == 1

    def test_attempts_stay_bounded_and_the_images_follow(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        dead = [_rendering(server.url(f"/{i}.pdf")) for i in range(40)]
        bases = serve_pages(server, "1")
        manifest = v2_manifest(service_canvas(bases[0]), rendering=dead)
        result = _download(
            network, manifest, staging, fmt="pdf", include_metadata=False
        )
        rendering_hits = [a for a in server.arrivals if a.path.endswith(".pdf")]
        assert len(rendering_hits) == 10
        pdf_part, images = result.part("pdf"), result.part("images")
        assert pdf_part is not None and pdf_part.missing == ("rendering",)
        assert images is not None and images.complete
        assert not result.complete

    def test_an_endpoint_rendering_is_staged_as_a_pdf(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script(
            "/diglitData/pdf/cpg234.fcgi", pdf("application/octet-stream")
        )
        manifest = v2_manifest(rendering=_rendering(url, label="Download as PDF"))
        result = _download(
            network, manifest, staging, fmt="pdf", include_metadata=False
        )
        assert result.complete
        (staged,) = result.files
        assert staged.name == "rendering.pdf"
        assert staged.kind == "pdf"
        assert staged.source_url == url
        assert staged.declared_format == "application/pdf"
        assert staged.label == "Download as PDF"
        assert _staged(staging) == ["rendering.pdf"]

    def test_an_html_rendering_is_not_a_document(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script(
            "/v.pdf", Reply(200, HTML, {"Content-Type": "application/pdf"})
        )
        manifest = v2_manifest(rendering=_rendering(url))
        result = _download(
            network, manifest, staging, fmt="pdf", include_metadata=False
        )
        assert not result.complete
        assert _staged(staging) == []

    def test_both_takes_the_rendering_and_the_images(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script("/v.pdf", pdf())
        bases = serve_pages(server, "1", "2")
        manifest = v2_manifest(
            *(service_canvas(base) for base in bases), rendering=_rendering(url)
        )
        result = _download(
            network, manifest, staging, fmt="both", include_metadata=False
        )
        assert [part.part for part in result.parts] == ["pdf", "images"]
        assert result.complete
        assert _staged(staging) == ["p00001.jpg", "p00002.jpg", "rendering.pdf"]

    def test_images_ignores_the_renderings(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script("/v.pdf", pdf())
        bases = serve_pages(server, "1")
        manifest = v2_manifest(service_canvas(bases[0]), rendering=_rendering(url))
        result = _download(network, manifest, staging, include_metadata=False)
        assert [part.part for part in result.parts] == ["images"]
        assert server.hits("/v.pdf") == 0

    def test_pdf_without_a_rendering_takes_the_images(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        bases = serve_pages(server, "1")
        result = _download(
            network, v2_manifest(service_canvas(bases[0])), staging, fmt="pdf"
        )
        assert [part.part for part in result.parts] == ["images"]
        assert result.complete

    def test_no_rendering_and_no_pages_is_not_complete(
        self, network: Network, staging: Path
    ) -> None:
        result = _download(network, {"@id": "m"}, staging, fmt="pdf")
        assert not result.complete

    def test_the_rendering_survives_failed_image_downloads(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script("/v.pdf", pdf())
        bases = service_urls(server, "1", "2")
        manifest = v2_manifest(
            *(service_canvas(base) for base in bases), rendering=_rendering(url)
        )
        result = _download(
            network, manifest, staging, fmt="both", include_metadata=False
        )
        pdf_part, images = result.part("pdf"), result.part("images")
        assert pdf_part is not None and pdf_part.complete
        assert images is not None and images.present == ()
        assert _staged(staging) == ["rendering.pdf"]
        assert not result.complete

    def test_both_with_a_rendering_and_no_pages_is_complete(
        self, server: ScriptedServer, network: Network, staging: Path
    ) -> None:
        url = server.script("/v.pdf", pdf())
        result = _download(
            network, v2_manifest(rendering=_rendering(url)), staging, fmt="both"
        )
        assert [part.part for part in result.parts] == ["pdf"]
        assert result.complete
