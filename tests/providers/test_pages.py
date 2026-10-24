"""Page sampling (``chronodownloader.pages``) over a provider's ``pages``.

Lives with the provider tests because it runs a fake provider against the
local server and the real coordinator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chronodownloader.coord import SlotToken
from chronodownloader.net import Network
from chronodownloader.pages import sample_pages
from chronodownloader.providers.base import Capabilities, UnsupportedCapability
from tests.iiif.support import (
    direct_canvas,
    jpeg,
    json_reply,
    service_canvas,
    v2_manifest,
)
from tests.net.support import ScriptedServer

from .fakes import FakeIIIFProvider


def _provider(
    server: ScriptedServer, pages: int, *, cap: int | None = None
) -> FakeIIIFProvider:
    canvases = [
        service_canvas(server.url(f"/iiif/{n}"), label=f"S. {n}")
        for n in range(1, pages + 1)
    ]
    url = server.script("/item1/manifest.json", json_reply(v2_manifest(*canvases)))
    return FakeIIIFProvider(
        {"item1": url}, capabilities=Capabilities(iiif=True, image_max_px=cap)
    )


def test_the_first_pages_are_sampled_at_the_reduced_size(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    for n in range(1, 6):
        server.script(f"/iiif/{n}/full/!500,500/0/default.jpg", jpeg())
    sample = sample_pages(
        _provider(server, 5),
        "item1",
        network=network,
        slot=slot,
        dest_dir=tmp_path / "inspect",
        first_n=2,
        max_px=500,
    )
    assert [page.item for page in sample.pages] == ["p00001", "p00002"]
    assert [page.label for page in sample.pages] == ["S. 1", "S. 2"]
    assert len(sample.sampled) == 2
    assert sample.max_px == 500
    assert sample.stopped is None
    assert sorted(p.name for p in (tmp_path / "inspect").iterdir()) == [
        "p00001.jpg",
        "p00002.jpg",
    ]
    assert server.hits("/iiif/3/full/!500,500/0/default.jpg") == 0


def test_the_provider_cap_bounds_the_sample_size(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/iiif/1/full/!300,300/0/default.jpg", jpeg())
    sample = sample_pages(
        _provider(server, 1, cap=300),
        "item1",
        network=network,
        slot=slot,
        dest_dir=tmp_path / "inspect",
        first_n=10,
        max_px=1000,
    )
    assert sample.max_px == 300
    assert len(sample.sampled) == 1


def test_a_page_falls_back_to_the_info_json_size(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    server.script("/iiif/1/info.json", json_reply({"width": 2000, "height": 3000}))
    server.script("/iiif/1/full/333,/0/default.jpg", jpeg())
    sample = sample_pages(
        _provider(server, 1),
        "item1",
        network=network,
        slot=slot,
        dest_dir=tmp_path / "inspect",
        first_n=1,
        max_px=500,
    )
    assert len(sample.sampled) == 1


def test_a_page_that_cannot_be_had_is_reported(
    server: ScriptedServer, network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    direct = server.script("/p2.jpg", jpeg())
    manifest = v2_manifest(service_canvas(server.url("/iiif/1")), direct_canvas(direct))
    url = server.script("/item1/manifest.json", json_reply(manifest))
    sample = sample_pages(
        FakeIIIFProvider({"item1": url}),
        "item1",
        network=network,
        slot=slot,
        dest_dir=tmp_path / "inspect",
        first_n=5,
        max_px=500,
    )
    assert [page.ok for page in sample.pages] == [False, True]


def test_a_metadata_only_provider_has_no_pages(
    network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    provider = FakeIIIFProvider({}, capabilities=Capabilities(downloads=False))
    with pytest.raises(UnsupportedCapability):
        sample_pages(
            provider,
            "x",
            network=network,
            slot=slot,
            dest_dir=tmp_path,
            first_n=1,
            max_px=500,
        )


def test_sizes_below_one_are_refused(
    network: Network, slot: SlotToken, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        sample_pages(
            FakeIIIFProvider({}),
            "x",
            network=network,
            slot=slot,
            dest_dir=tmp_path,
            first_n=0,
            max_px=500,
        )
