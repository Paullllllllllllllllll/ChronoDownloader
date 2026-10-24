"""A IIIF provider test double and a size budget that counts files."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from chronodownloader.net import Network
from chronodownloader.providers.base import (
    Capabilities,
    DownloadRequest,
    DownloadResult,
    FileKind,
    PageRef,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
    source_id_of,
)
from chronodownloader.providers.iiif_download import (
    download_manifest,
    fetch_json,
    manifest_page_refs,
)


@dataclass
class FakeIIIFProvider:
    """A IIIF adapter whose items are manifests at known URLs.

    ``manifests`` maps item identifiers to manifest URLs; ``records`` are the
    search hits, matched by a title substring.
    """

    manifests: Mapping[str, str]
    key: str = "mdz"
    capabilities: Capabilities = field(default_factory=lambda: Capabilities(iiif=True))
    records: tuple[SearchRecord, ...] = ()

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        title = query.title.lower()
        return [record for record in self.records if title in record.title.lower()][
            :limit
        ]

    def _manifest_url(self, record_or_id: SearchRecord | str) -> str:
        if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
            return record_or_id.iiif_manifest
        return self.manifests[source_id_of(record_or_id)]

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        if not self.capabilities.downloads:
            raise UnsupportedCapability(self.key, "download")
        source_id = source_id_of(record_or_id)
        url = self._manifest_url(record_or_id)
        manifest = fetch_json(network, self.key, url, slot=request.slot, item=source_id)
        return download_manifest(
            network,
            self.key,
            manifest,
            source_id=source_id,
            staging_dir=staging_dir,
            request=request,
            manifest_url=url,
            image_max_px=self.capabilities.image_max_px,
        )

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        url = self._manifest_url(record_or_id)
        manifest = fetch_json(network, self.key, url, item=source_id_of(record_or_id))
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)


@dataclass
class FileBudget:
    """Allows ``limits[kind]`` files of each kind; records the charges."""

    limits: Mapping[str, int] = field(default_factory=dict)
    charged: list[tuple[FileKind, int]] = field(default_factory=list)

    def allows(self, kind: FileKind) -> bool:
        used = sum(1 for charged, _size in self.charged if charged == kind)
        return used < self.limits.get(kind, 1_000_000)

    def charge(self, kind: FileKind, size: int) -> None:
        self.charged.append((kind, size))
