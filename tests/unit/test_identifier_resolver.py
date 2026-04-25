"""Tests for api.identifier_resolver -- identifier-to-manifest resolution."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api.identifier_resolver import (
    MANIFEST_TEMPLATES,
    NATIVE_DOWNLOAD_PROVIDERS,
    ResolvedIdentifier,
    build_manifest_url,
    detect_provider,
    download_by_native_provider,
    resolve_identifier,
)


# ============================================================================
# build_manifest_url
# ============================================================================

class TestBuildManifestUrl:
    """Verify manifest URL construction for each provider."""

    def test_mdz(self) -> None:
        urls = build_manifest_url("mdz", "bsb11280551")
        assert urls == [
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb11280551/manifest"
        ]

    def test_bnf_gallica(self) -> None:
        urls = build_manifest_url("bnf_gallica", "bpt6k1511262r")
        assert urls == [
            "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1511262r/manifest.json"
        ]

    def test_internet_archive(self) -> None:
        urls = build_manifest_url("internet_archive", "ThreeBooksOfCookery1575")
        assert urls == [
            "https://iiif.archivelab.org/iiif/ThreeBooksOfCookery1575/manifest.json"
        ]

    def test_e_rara(self) -> None:
        urls = build_manifest_url("e_rara", "12345")
        assert urls == ["https://www.e-rara.ch/i3f/v20/12345/manifest"]

    def test_slub(self) -> None:
        urls = build_manifest_url("slub", "ppn123456789")
        assert urls == [
            "https://iiif.slub-dresden.de/iiif/2/ppn123456789/manifest.json"
        ]

    def test_loc(self) -> None:
        urls = build_manifest_url("loc", "2004578901")
        assert urls == [
            "https://www.loc.gov/item/2004578901/manifest.json"
        ]

    def test_british_library(self) -> None:
        urls = build_manifest_url("british_library", "vdc_000000001")
        assert urls == [
            "https://api.bl.uk/metadata/iiif/ark:/81055/vdc_000000001/manifest.json"
        ]

    def test_hathitrust(self) -> None:
        urls = build_manifest_url("hathitrust", "mdp.39015012345678")
        assert urls == [
            "https://babel.hathitrust.org/cgi/imgsrv/manifest/mdp.39015012345678"
        ]

    def test_polona(self) -> None:
        urls = build_manifest_url("polona", "abc123def")
        assert urls == [
            "https://polona.pl/iiif/item/abc123def/manifest.json"
        ]

    def test_bne_returns_multiple(self) -> None:
        urls = build_manifest_url("bne", "bne_id_123")
        assert len(urls) == 2
        assert "https://iiif.bne.es/bne_id_123/manifest" in urls
        assert "https://iiif.bne.es/bne_id_123/manifest.json" in urls

    def test_europeana(self) -> None:
        urls = build_manifest_url("europeana", "9200396/BibliographicResource_3000135551475")
        assert urls == [
            "https://iiif.europeana.eu/presentation/9200396/BibliographicResource_3000135551475/manifest"
        ]

    def test_native_provider_raises_value_error(self) -> None:
        for pkey in NATIVE_DOWNLOAD_PROVIDERS:
            with pytest.raises(ValueError, match="native download"):
                build_manifest_url(pkey, "some_id")

    def test_unknown_provider_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="No manifest URL template"):
            build_manifest_url("nonexistent_provider", "id123")

    def test_all_template_providers_covered(self) -> None:
        """Every key in MANIFEST_TEMPLATES produces at least one URL."""
        for pkey in MANIFEST_TEMPLATES:
            urls = build_manifest_url(pkey, "test_id")
            assert len(urls) >= 1, f"No URL produced for {pkey}"


# ============================================================================
# detect_provider
# ============================================================================

class TestDetectProvider:
    """Verify auto-detection of provider from identifier format."""

    @pytest.mark.parametrize("identifier, expected", [
        ("bsb11280551", ["mdz"]),
        ("bsb00073751", ["mdz"]),
        ("BSB11280551", ["mdz"]),  # case-insensitive
        ("bpt6k1511262r", ["bnf_gallica"]),
        ("btv1b8600069s", ["bnf_gallica"]),
        ("cb343161870", ["bnf_gallica"]),
        ("ark:/12148/bpt6k1511262r", ["bnf_gallica"]),
        ("mdp.39015012345678", ["hathitrust"]),
        ("inu.30000088654321", ["hathitrust"]),
        ("uc1.b123456", ["hathitrust"]),
        ("hvd.hw1abc", ["hathitrust"]),
        ("nyp.33433082123456", ["hathitrust"]),
        ("vdc_100000000001", ["british_library"]),
    ])
    def test_known_patterns(self, identifier: str, expected: list[str]) -> None:
        assert detect_provider(identifier) == expected

    def test_unknown_identifier_returns_empty(self) -> None:
        assert detect_provider("some_random_string_12345") == []

    def test_no_duplicates(self) -> None:
        """Even if multiple patterns match the same provider, no dupes."""
        # bpt6k matches "^bpt6k\w+$"
        result = detect_provider("bpt6k12345")
        assert result.count("bnf_gallica") == 1


# ============================================================================
# resolve_identifier
# ============================================================================

class TestResolveIdentifier:
    """Verify end-to-end resolution logic."""

    def test_explicit_iiif_provider(self) -> None:
        results = resolve_identifier("bsb11280551", provider_key="mdz")
        assert len(results) == 1
        r = results[0]
        assert r.provider_key == "mdz"
        assert not r.use_native
        assert len(r.manifest_urls) == 1
        assert "bsb11280551" in r.manifest_urls[0]

    def test_explicit_native_provider(self) -> None:
        results = resolve_identifier("abc123md5hash", provider_key="annas_archive")
        assert len(results) == 1
        r = results[0]
        assert r.provider_key == "annas_archive"
        assert r.use_native is True
        assert r.manifest_urls == []

    def test_unknown_explicit_provider_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown provider key"):
            resolve_identifier("id123", provider_key="fantasy_library")

    def test_auto_detect_mdz(self) -> None:
        results = resolve_identifier("bsb11280551")
        assert len(results) >= 1
        assert results[0].provider_key == "mdz"
        assert not results[0].use_native

    def test_auto_detect_gallica(self) -> None:
        results = resolve_identifier("bpt6k1511262r")
        assert len(results) >= 1
        assert results[0].provider_key == "bnf_gallica"

    def test_auto_detect_hathitrust(self) -> None:
        results = resolve_identifier("mdp.39015012345678")
        assert len(results) >= 1
        assert results[0].provider_key == "hathitrust"

    def test_unrecognised_returns_empty(self) -> None:
        results = resolve_identifier("completely_unknown_id")
        assert results == []

    def test_explicit_provider_with_multiple_templates(self) -> None:
        results = resolve_identifier("item_xyz", provider_key="bne")
        assert len(results) == 1
        assert len(results[0].manifest_urls) == 2


# ============================================================================
# download_by_native_provider
# ============================================================================

class TestDownloadByNativeProvider:
    """Verify native download function dispatch."""

    def test_calls_provider_download_fn(self, temp_dir: str) -> None:
        mock_download = MagicMock(return_value=True)
        fake_providers = {
            "annas_archive": (MagicMock(), mock_download, "Anna's Archive"),
        }
        with patch("api.identifier_resolver.PROVIDERS", fake_providers):
            result = download_by_native_provider(
                "abc123", "annas_archive", temp_dir, title="Test Work"
            )

        assert result is True
        mock_download.assert_called_once()
        sr = mock_download.call_args[0][0]
        assert sr.source_id == "abc123"
        assert sr.title == "Test Work"
        assert sr.provider_key == "annas_archive"
        assert mock_download.call_args[0][1] == temp_dir

    def test_returns_false_on_exception(self, temp_dir: str) -> None:
        mock_download = MagicMock(side_effect=RuntimeError("network error"))
        fake_providers = {
            "google_books": (MagicMock(), mock_download, "Google Books"),
        }
        with patch("api.identifier_resolver.PROVIDERS", fake_providers):
            result = download_by_native_provider(
                "vol_id", "google_books", temp_dir
            )
        assert result is False

    def test_unknown_provider_raises(self, temp_dir: str) -> None:
        with pytest.raises(KeyError, match="Unknown provider key"):
            download_by_native_provider("id", "nonexistent", temp_dir)

    def test_title_defaults_to_identifier(self, temp_dir: str) -> None:
        mock_download = MagicMock(return_value=True)
        fake_providers = {
            "sbb_digital": (MagicMock(), mock_download, "SBB Digital"),
        }
        with patch("api.identifier_resolver.PROVIDERS", fake_providers):
            download_by_native_provider("PPN12345", "sbb_digital", temp_dir)

        sr = mock_download.call_args[0][0]
        assert sr.title == "PPN12345"
