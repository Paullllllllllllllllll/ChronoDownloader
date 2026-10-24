"""Manifest URL recognition, provider detection and item identifiers.

Ported from v1.30.3 ``tests/unit/test_direct_iiif.py`` with assertions
unchanged; the configuration getters and the file-stem template were
dropped with the ``direct_iiif`` settings.
"""

from __future__ import annotations

from chronodownloader.iiif import (
    detect_provider_from_url,
    extract_item_id_from_url,
    is_iiif_manifest_url,
)


class TestIsIIIFManifestUrl:
    def test_internet_archive_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://iiif.archive.org/iiif/collectionofsacr00smit/manifest.json"
        )
        assert is_iiif_manifest_url(
            "https://iiif.archivelab.org/iiif/testid123/manifest.json"
        )

    def test_gallica_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1234567/manifest.json"
        )

    def test_mdz_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb00001234/manifest"
        )
        assert is_iiif_manifest_url(
            "https://api.digitale-sammlungen.de/iiif/presentation/v3/bsb00001234/manifest"
        )

    def test_hathitrust_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://babel.hathitrust.org/cgi/imgsrv/manifest/mdp.39015012345678"
        )

    def test_loc_manifest(self) -> None:
        assert is_iiif_manifest_url("https://www.loc.gov/item/2021667416/manifest.json")

    def test_wellcome_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://iiif.wellcomecollection.org/presentation/v3/b12345678"
        )

    def test_british_library_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://api.bl.uk/metadata/iiif/ark:/81055/vdc_12345678/manifest.json"
        )

    def test_erara_manifest(self) -> None:
        assert is_iiif_manifest_url("https://www.e-rara.ch/i3f/v20/12345678/manifest")

    def test_slub_manifest(self) -> None:
        assert is_iiif_manifest_url(
            "https://digital.slub-dresden.de/data/kitodo/something/manifest.json"
        )

    def test_generic_manifest_json(self) -> None:
        assert is_iiif_manifest_url("https://example.org/iiif/12345/manifest.json")
        assert is_iiif_manifest_url("https://example.org/item/manifest.json")

    def test_generic_manifest_suffix(self) -> None:
        assert is_iiif_manifest_url(
            "https://example.org/iiif/presentation/v2/12345/manifest"
        )

    def test_non_iiif_urls(self) -> None:
        assert not is_iiif_manifest_url("https://www.google.com")
        assert not is_iiif_manifest_url("https://archive.org/details/testid")
        assert not is_iiif_manifest_url("https://example.org/page.html")
        assert not is_iiif_manifest_url("")
        assert not is_iiif_manifest_url(None)

    def test_invalid_inputs(self) -> None:
        assert not is_iiif_manifest_url(123)
        assert not is_iiif_manifest_url([])
        assert not is_iiif_manifest_url({})
        assert not is_iiif_manifest_url("not a url")
        assert not is_iiif_manifest_url("ftp://example.org/manifest.json")


class TestDetectProviderFromUrl:
    def test_internet_archive(self) -> None:
        assert detect_provider_from_url(
            "https://iiif.archive.org/iiif/test/manifest.json"
        ) == ("internet_archive", "Internet Archive")

    def test_gallica(self) -> None:
        assert detect_provider_from_url(
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json"
        ) == ("bnf_gallica", "BnF Gallica")

    def test_mdz(self) -> None:
        assert detect_provider_from_url(
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb123/manifest"
        ) == ("mdz", "MDZ")

    def test_hathitrust(self) -> None:
        assert detect_provider_from_url(
            "https://babel.hathitrust.org/cgi/imgsrv/manifest/mdp.123"
        ) == ("hathitrust", "HathiTrust")

    def test_loc(self) -> None:
        assert detect_provider_from_url(
            "https://www.loc.gov/item/123/manifest.json"
        ) == ("loc", "Library of Congress")

    def test_wellcome(self) -> None:
        assert detect_provider_from_url(
            "https://iiif.wellcomecollection.org/presentation/v3/b123"
        ) == ("wellcome", "Wellcome Collection")

    def test_unknown_provider(self) -> None:
        assert detect_provider_from_url(
            "https://unknown.library.org/iiif/123/manifest.json"
        ) == ("direct_iiif", "Direct IIIF")


class TestExtractItemIdFromUrl:
    def test_internet_archive_id(self) -> None:
        assert (
            extract_item_id_from_url(
                "https://iiif.archive.org/iiif/collectionofsacr00smit/manifest.json"
            )
            == "collectionofsacr00smit"
        )

    def test_mdz_id(self) -> None:
        assert (
            extract_item_id_from_url(
                "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb00001234/manifest"
            )
            == "bsb00001234"
        )

    def test_generic_view_id(self) -> None:
        assert (
            extract_item_id_from_url("https://example.org/view/abc123/manifest.json")
            == "abc123"
        )

    def test_fallback_hash(self) -> None:
        assert len(extract_item_id_from_url("https://strange.url.org/x")) == 12
