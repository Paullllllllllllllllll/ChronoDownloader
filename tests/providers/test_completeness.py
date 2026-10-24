"""Completeness from provider to result, over the real network and coordinator.

A fake IIIF provider downloads from a local server through the real
:class:`~chronodownloader.net.Network` with a held slot of the real
coordination database. The cases: a failed middle page, a budget cutoff, a
page limit, the resume of a partial work, the v1.30.3 direct-manifest
partial-status regression, and the stops (lost slot, open breaker).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from chronodownloader.coord import Coordinator, SlotToken
from chronodownloader.net import Cause, Network, RequestFailed
from chronodownloader.providers.base import (
    Capabilities,
    DownloadRequest,
    FileKind,
    InvalidResponse,
    ItemNotFound,
    ProviderError,
    SearchRecord,
    UnsupportedCapability,
)
from chronodownloader.providers.iiif_download import fetch_json
from chronodownloader.providers.staging import stage_download
from tests.iiif.support import (
    FULL,
    HTML,
    jpeg,
    json_reply,
    serve_pages,
    service_canvas,
    service_urls,
    v2_manifest,
)
from tests.net.support import Reply, ScriptedServer, settings_with

from .conftest import PROVIDER
from .fakes import FakeIIIFProvider, FileBudget


def _provider(server: ScriptedServer, *bases: str) -> FakeIIIFProvider:
    manifest = v2_manifest(*(service_canvas(base) for base in bases))
    url = server.script("/item1/manifest.json", json_reply(manifest))
    return FakeIIIFProvider({"item1": url})


def _staging(tmp_path: Path) -> Path:
    return tmp_path / "out" / "work" / "objects" / ".staging" / "1"


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def test_a_failed_middle_page_leaves_the_work_partial(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    (first,) = serve_pages(server, "1")
    (middle,) = service_urls(server, "2")
    (last,) = serve_pages(server, "3")
    provider = _provider(server, first, middle, last)
    staging = _staging(tmp_path)
    result = provider.download(
        "item1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="images"),
    )
    images = result.part("images")
    assert images is not None
    assert images.expected == ("p00001", "p00002", "p00003")
    assert images.present == ("p00001", "p00003")
    assert images.missing == ("p00002",)
    assert dict(images.causes) == {"p00002": "not_found"}
    assert not result.complete
    assert result.stopped is None
    assert result.quota_charged is False
    assert _files(staging) == ["metadata/manifest.json", "p00001.jpg", "p00003.jpg"]


def test_an_unusable_canvas_keeps_positions_and_stays_missing(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """Canvas 3 stays ``p00003`` when canvas 2 has no image; canvas 4
    repeats canvas 1's source and gets a copy, not a second transfer."""
    first, third = serve_pages(server, "1", "3")
    manifest = v2_manifest(
        service_canvas(first),
        {"label": "blank", "images": []},
        service_canvas(third),
        service_canvas(first),
    )
    url = server.script("/item1/manifest.json", json_reply(manifest))
    staging = _staging(tmp_path)
    result = FakeIIIFProvider({"item1": url}).download(
        "item1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="images", include_metadata=False),
    )
    images = result.part("images")
    assert images is not None
    assert images.expected == ("p00001", "p00002", "p00003", "p00004")
    assert images.missing == ("p00002",)
    assert dict(images.causes) == {"p00002": "no_source"}
    assert not result.complete
    assert server.hits(f"/iiif/1{FULL}") == 1
    assert _files(staging) == ["p00001.jpg", "p00003.jpg", "p00004.jpg"]


@pytest.mark.parametrize(
    ("path", "kind", "content_type"),
    [
        ("/x.pdf", "pdf", "application/octet-stream"),
        ("/x.jpg", "image", "application/octet-stream"),
        ("/y.jpg", "image", "image/jpeg"),
    ],
)
def test_bytes_that_contradict_the_format_are_an_invalid_payload(
    path: str,
    kind: FileKind,
    content_type: str,
    server: ScriptedServer,
    network: Network,
    slot: SlotToken,
    tmp_path: Path,
) -> None:
    url = server.script(path, Reply(200, HTML, {"Content-Type": content_type}))
    staging = _staging(tmp_path)
    with pytest.raises(RequestFailed) as excinfo:
        stage_download(network, PROVIDER, url, staging, "item", slot=slot, kind=kind)
    assert excinfo.value.cause is Cause.INVALID_PAYLOAD
    assert _files(staging) == []


def test_a_failed_rendering_carries_its_cause(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/v.pdf", Reply(403))
    manifest = v2_manifest(
        rendering={"@id": server.url("/v.pdf"), "format": "application/pdf"}
    )
    url = server.script("/item1/manifest.json", json_reply(manifest))
    result = FakeIIIFProvider({"item1": url}).download(
        "item1",
        network=network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(slot, include_metadata=False),
    )
    pdf = result.part("pdf")
    assert pdf is not None
    assert dict(pdf.causes) == {"rendering": "blocked"}


@pytest.mark.parametrize(
    ("reply", "error"),
    [(Reply(200, b"<html>"), InvalidResponse), (Reply(404), ItemNotFound)],
)
def test_fetch_json_errors_mask_the_key(
    reply: Reply,
    error: type[ProviderError],
    server: ScriptedServer,
    network: Network,
) -> None:
    url = server.script("/m.json", reply) + "?wskey=SECRET"
    with pytest.raises(error) as excinfo:
        fetch_json(network, PROVIDER, url)
    assert "wskey=" in str(excinfo.value)
    assert "SECRET" not in str(excinfo.value)
    assert "SECRET" not in repr(vars(excinfo.value))


def test_a_budget_cutoff_leaves_the_rest_missing(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    provider = _provider(server, *serve_pages(server, "1", "2", "3", "4"))
    budget = FileBudget({"image": 2})
    result = provider.download(
        "item1",
        network=network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(slot, format="images", budget=budget),
    )
    images = result.part("images")
    assert images is not None
    assert images.present == ("p00001", "p00002")
    assert images.missing == ("p00003", "p00004")
    assert result.stopped == "budget"
    assert not result.complete
    assert [kind for kind, _size in budget.charged] == ["metadata", "image", "image"]
    assert server.hits(f"/iiif/3{FULL}") == 0


def test_a_page_limit_completes_within_the_limit(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    provider = _provider(server, *serve_pages(server, "1", "2", "3", "4", "5"))
    result = provider.download(
        "item1",
        network=network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(slot, format="images", max_pages=2),
    )
    images = result.part("images")
    assert images is not None
    assert images.expected == ("p00001", "p00002")
    assert images.page_limit == 2
    assert images.available == 5
    assert images.truncated
    assert result.complete
    assert sum(server.hits(f"/iiif/{n}{FULL}") for n in "12345") == 2


def test_resume_skips_present_pages_and_counts_them(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    provider = _provider(server, *serve_pages(server, "1", "2", "3"))
    staging = _staging(tmp_path)
    result = provider.download(
        "item1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(
            slot,
            format="images",
            include_metadata=False,
            present=frozenset({"p00001", "p00002"}),
        ),
    )
    images = result.part("images")
    assert images is not None
    assert images.present == ("p00001", "p00002", "p00003")
    assert result.complete
    assert [file.item for file in result.files] == ["p00003"]
    assert server.hits(f"/iiif/1{FULL}") == 0
    assert server.hits(f"/iiif/2{FULL}") == 0
    assert _files(staging) == ["p00003.jpg"]


def test_resume_of_a_present_rendering_sends_no_request(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    manifest = v2_manifest(
        rendering={"@id": server.url("/v.pdf"), "format": "application/pdf"}
    )
    url = server.script("/item1/manifest.json", json_reply(manifest))
    result = FakeIIIFProvider({"item1": url}).download(
        "item1",
        network=network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(
            slot, include_metadata=False, present=frozenset({"rendering"})
        ),
    )
    assert result.complete
    assert result.files == ()
    assert server.hits("/v.pdf") == 0


def test_a_direct_manifest_with_a_missing_page_is_partial(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    """v1.30.3 ``test_process_direct_iiif_propagates_partial_status``: a
    direct manifest link with 2 of 3 pages is partial, not completed."""
    first, third = serve_pages(server, "s1", "s3")
    (second,) = service_urls(server, "s2")
    manifest = v2_manifest(*(service_canvas(u) for u in (first, second, third)))
    url = server.script("/direct/manifest.json", json_reply(manifest))
    record = SearchRecord("mdz", "direct1", "Kochbuch", iiif_manifest=url)
    result = FakeIIIFProvider({}).download(
        record,
        network=network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(slot),
    )
    images = result.part("images")
    assert images is not None
    assert (len(images.expected), len(images.present)) == (3, 2)
    assert result.source_id == "direct1"
    assert not result.complete


@dataclass
class _ReleasingBudget(FileBudget):
    """Releases the slot once *after* image files were charged."""

    coord: Coordinator | None = None
    slot: SlotToken | None = None
    after: int = 1

    def allows(self, kind: FileKind) -> bool:
        images = sum(1 for charged, _size in self.charged if charged == "image")
        if images >= self.after and self.coord is not None and self.slot is not None:
            self.coord.release_slot(self.slot)
        return super().allows(kind)


def test_a_lost_slot_stops_the_download(
    server: ScriptedServer,
    network: Network,
    coord: Coordinator,
    slot: SlotToken,
    tmp_path: Path,
) -> None:
    provider = _provider(server, *serve_pages(server, "1", "2", "3"))
    budget = _ReleasingBudget(coord=coord, slot=slot)
    result = provider.download(
        "item1",
        network=network,
        staging_dir=_staging(tmp_path),
        request=DownloadRequest(slot, format="images", budget=budget),
    )
    images = result.part("images")
    assert images is not None
    assert images.present == ("p00001",)
    assert result.stopped == "slot_lost"
    assert server.hits(f"/iiif/2{FULL}") == 0


def test_an_open_breaker_stops_the_download(
    server: ScriptedServer, coord: Coordinator, slot: SlotToken, tmp_path: Path
) -> None:
    for name in ("1", "2", "3"):
        server.script(f"/iiif/{name}{FULL}", Reply(403))
    provider = _provider(server, *service_urls(server, "1", "2", "3"))
    settings = settings_with(PROVIDER, circuit_breaker_threshold=2)
    with Network(settings, coord, sleep=lambda _seconds: None) as network:
        result = provider.download(
            "item1",
            network=network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(slot, format="images", include_metadata=False),
        )
    images = result.part("images")
    assert images is not None
    assert images.missing == ("p00001", "p00002", "p00003")
    assert result.stopped == "breaker_open"
    assert server.hits(f"/iiif/3{FULL}") == 0


def test_a_missing_manifest_is_item_not_found(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    provider = FakeIIIFProvider({"gone": server.url("/gone/manifest.json")})
    with pytest.raises(ItemNotFound):
        provider.download(
            "gone",
            network=network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(slot),
        )


def test_a_metadata_only_provider_refuses_to_download(
    network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    provider = FakeIIIFProvider({}, capabilities=Capabilities(downloads=False))
    with pytest.raises(UnsupportedCapability) as excinfo:
        provider.download(
            "x",
            network=network,
            staging_dir=_staging(tmp_path),
            request=DownloadRequest(slot),
        )
    assert excinfo.value.capability == "download"


def test_a_download_writes_only_into_its_staging_folder(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/iiif/1/full/full/0/default.jpg", Reply(200, b"", {}))
    server.script("/iiif/2/full/full/0/default.jpg", jpeg())
    provider = _provider(server, *service_urls(server, "1", "2"))
    staging = _staging(tmp_path)
    provider.download(
        "item1",
        network=network,
        staging_dir=staging,
        request=DownloadRequest(slot, format="images"),
    )
    out = tmp_path / "out"
    assert _files(out) == [
        "work/objects/.staging/1/metadata/manifest.json",
        "work/objects/.staging/1/p00002.jpg",
    ]
