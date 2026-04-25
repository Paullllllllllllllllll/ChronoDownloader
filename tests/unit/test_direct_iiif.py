"""Unit tests for direct IIIF manifest URL detection and handling."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from api.iiif import (
    is_iiif_manifest_url,
    detect_provider_from_url,
    extract_item_id_from_url,
    extract_manifest_metadata,
    preview_manifest,
    download_from_iiif_manifest,
    is_direct_download_enabled,
    get_direct_link_column,
    get_naming_template,
    resolve_file_stem,
)


class TestIsIIIFManifestUrl:
    """Tests for IIIF manifest URL detection."""
    
    def test_internet_archive_manifest(self) -> None:
        """Test Internet Archive IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://iiif.archive.org/iiif/collectionofsacr00smit/manifest.json")
        assert is_iiif_manifest_url("https://iiif.archivelab.org/iiif/testid123/manifest.json")

    def test_gallica_manifest(self) -> None:
        """Test BnF Gallica IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1234567/manifest.json")

    def test_mdz_manifest(self) -> None:
        """Test MDZ (Bavarian State Library) IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb00001234/manifest")
        assert is_iiif_manifest_url("https://api.digitale-sammlungen.de/iiif/presentation/v3/bsb00001234/manifest")

    def test_hathitrust_manifest(self) -> None:
        """Test HathiTrust IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://babel.hathitrust.org/cgi/imgsrv/manifest/mdp.39015012345678")

    def test_loc_manifest(self) -> None:
        """Test Library of Congress IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://www.loc.gov/item/2021667416/manifest.json")

    def test_wellcome_manifest(self) -> None:
        """Test Wellcome Collection IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://iiif.wellcomecollection.org/presentation/v3/b12345678")

    def test_british_library_manifest(self) -> None:
        """Test British Library IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://api.bl.uk/metadata/iiif/ark:/81055/vdc_12345678/manifest.json")

    def test_erara_manifest(self) -> None:
        """Test e-rara IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://www.e-rara.ch/i3f/v20/12345678/manifest")

    def test_slub_manifest(self) -> None:
        """Test SLUB Dresden IIIF manifest URLs."""
        assert is_iiif_manifest_url("https://digital.slub-dresden.de/data/kitodo/something/manifest.json")

    def test_generic_manifest_json(self) -> None:
        """Test generic manifest.json suffix."""
        assert is_iiif_manifest_url("https://example.org/iiif/12345/manifest.json")
        assert is_iiif_manifest_url("https://example.org/item/manifest.json")

    def test_generic_manifest_suffix(self) -> None:
        """Test generic /manifest suffix without .json."""
        assert is_iiif_manifest_url("https://example.org/iiif/presentation/v2/12345/manifest")

    def test_non_iiif_urls(self) -> None:
        """Test that non-IIIF URLs are correctly rejected."""
        assert not is_iiif_manifest_url("https://www.google.com")
        assert not is_iiif_manifest_url("https://archive.org/details/testid")
        assert not is_iiif_manifest_url("https://example.org/page.html")
        assert not is_iiif_manifest_url("")
        assert not is_iiif_manifest_url(None)  # type: ignore[arg-type]

    def test_invalid_inputs(self) -> None:
        """Test handling of invalid inputs."""
        assert not is_iiif_manifest_url(123)  # type: ignore[arg-type]
        assert not is_iiif_manifest_url([])  # type: ignore[arg-type]
        assert not is_iiif_manifest_url({})  # type: ignore[arg-type]
        assert not is_iiif_manifest_url("not a url")
        assert not is_iiif_manifest_url("ftp://example.org/manifest.json")


class TestDetectProviderFromUrl:
    """Tests for provider detection from IIIF manifest URLs."""
    
    def test_internet_archive(self) -> None:
        """Test Internet Archive detection."""
        key, name = detect_provider_from_url("https://iiif.archive.org/iiif/test/manifest.json")
        assert key == "internet_archive"
        assert name == "Internet Archive"

    def test_gallica(self) -> None:
        """Test Gallica detection."""
        key, name = detect_provider_from_url("https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/manifest.json")
        assert key == "bnf_gallica"
        assert name == "BnF Gallica"

    def test_mdz(self) -> None:
        """Test MDZ detection."""
        key, name = detect_provider_from_url(
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb123/manifest"
        )
        assert key == "mdz"
        assert name == "MDZ"

    def test_hathitrust(self) -> None:
        """Test HathiTrust detection."""
        key, name = detect_provider_from_url("https://babel.hathitrust.org/cgi/imgsrv/manifest/mdp.123")
        assert key == "hathitrust"
        assert name == "HathiTrust"

    def test_loc(self) -> None:
        """Test Library of Congress detection."""
        key, name = detect_provider_from_url("https://www.loc.gov/item/123/manifest.json")
        assert key == "loc"
        assert name == "Library of Congress"

    def test_wellcome(self) -> None:
        """Test Wellcome Collection detection."""
        key, name = detect_provider_from_url("https://iiif.wellcomecollection.org/presentation/v3/b123")
        assert key == "wellcome"
        assert name == "Wellcome Collection"

    def test_unknown_provider(self) -> None:
        """Test fallback for unknown providers."""
        key, name = detect_provider_from_url("https://unknown.library.org/iiif/123/manifest.json")
        assert key == "direct_iiif"
        assert name == "Direct IIIF"


class TestExtractItemIdFromUrl:
    """Tests for item ID extraction from IIIF manifest URLs."""
    
    def test_internet_archive_id(self) -> None:
        """Test ID extraction from Internet Archive URLs."""
        item_id = extract_item_id_from_url(
            "https://iiif.archive.org/iiif/collectionofsacr00smit/manifest.json"
        )
        assert item_id == "collectionofsacr00smit"

    def test_mdz_id(self) -> None:
        """Test ID extraction from MDZ URLs."""
        item_id = extract_item_id_from_url(
            "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb00001234/manifest"
        )
        assert item_id == "bsb00001234"

    def test_generic_view_id(self) -> None:
        """Test ID extraction from /view/ pattern."""
        item_id = extract_item_id_from_url("https://example.org/view/abc123/manifest.json")
        assert item_id == "abc123"

    def test_fallback_hash(self) -> None:
        """Test that a hash is generated for unrecognized patterns."""
        item_id = extract_item_id_from_url("https://strange.url.org/x")
        assert len(item_id) == 12  # MD5 hash truncated to 12 chars


class TestConfigFunctions:
    """Tests for configuration-related functions."""
    
    @patch('api.iiif._direct.get_config')
    def test_is_direct_download_enabled_default(self, mock_config: MagicMock) -> None:
        """Test default enabled state."""
        mock_config.return_value = {}
        assert is_direct_download_enabled() == True

    @patch('api.iiif._direct.get_config')
    def test_is_direct_download_enabled_explicit(self, mock_config: MagicMock) -> None:
        """Test explicit enabled/disabled state."""
        mock_config.return_value = {"direct_iiif": {"enabled": False}}
        assert is_direct_download_enabled() == False

        mock_config.return_value = {"direct_iiif": {"enabled": True}}
        assert is_direct_download_enabled() == True

    @patch('api.iiif._direct.get_config')
    def test_get_direct_link_column_default(self, mock_config: MagicMock) -> None:
        """Test default column name."""
        mock_config.return_value = {}
        assert get_direct_link_column() == "direct_link"

    @patch('api.iiif._direct.get_config')
    def test_get_direct_link_column_custom(self, mock_config: MagicMock) -> None:
        """Test custom column name from config."""
        mock_config.return_value = {"direct_iiif": {"link_column": "iiif_url"}}
        assert get_direct_link_column() == "iiif_url"

    @patch('api.iiif._direct.get_config')
    def test_get_naming_template_default(self, mock_config: MagicMock) -> None:
        """Test default naming template."""
        mock_config.return_value = {}
        assert get_naming_template() == "{provider}_{item_id}"

    @patch('api.iiif._direct.get_config')
    def test_get_naming_template_custom(self, mock_config: MagicMock) -> None:
        """Test custom naming template from config."""
        mock_config.return_value = {"direct_iiif": {"naming_template": "{entry_id}_{name}"}}
        assert get_naming_template() == "{entry_id}_{name}"


class TestResolveFileStem:
    """Tests for naming template resolution."""
    
    def test_default_template(self) -> None:
        """Test default {provider}_{item_id} template."""
        stem = resolve_file_stem(
            "{provider}_{item_id}",
            provider_key="mdz",
            item_id="bsb123",
        )
        assert stem == "mdz_bsb123"

    def test_custom_template_with_name(self) -> None:
        """Test custom template with entry_id and name."""
        stem = resolve_file_stem(
            "{entry_id}_{name}",
            entry_id="E0001",
            name="Taillevent",
        )
        assert stem == "E0001_Taillevent"

    def test_missing_values_produce_empty_segments(self) -> None:
        """Test that missing values produce empty strings in template."""
        stem = resolve_file_stem(
            "{entry_id}_{name}",
            entry_id="E0001",
            name=None,
        )
        assert stem == "E0001"

    def test_empty_result_falls_back(self) -> None:
        """Test that empty result falls back to provider_item_id."""
        stem = resolve_file_stem(
            "{name}",
            name=None,
            provider_key="gallica",
            item_id="ark123",
        )
        assert stem == "gallica_ark123"

    def test_all_variables(self) -> None:
        """Test template with all variables."""
        stem = resolve_file_stem(
            "{entry_id}_{name}_{provider}_{item_id}",
            entry_id="E0001",
            name="Test",
            provider_key="mdz",
            item_id="bsb123",
        )
        assert stem == "E0001_Test_mdz_bsb123"

    def test_invalid_template_falls_back(self) -> None:
        """Test that invalid format strings fall back gracefully."""
        stem = resolve_file_stem(
            "{unknown_var}",
            provider_key="mdz",
            item_id="bsb123",
        )
        assert stem == "mdz_bsb123"


class TestExtractManifestMetadata:
    """Tests for IIIF manifest metadata extraction."""
    
    def test_v2_manifest(self) -> None:
        """Test metadata extraction from a v2 manifest."""
        manifest = {
            "label": "Le Viandier de Taillevent",
            "attribution": "BnF Gallica",
            "metadata": [
                {"label": "Title", "value": "Le Viandier"},
                {"label": "Author", "value": "Taillevent"},
                {"label": "Date", "value": "1486"},
            ],
        }
        meta = extract_manifest_metadata(manifest)
        assert meta["label"] == "Le Viandier de Taillevent"
        assert meta["attribution"] == "BnF Gallica"
        assert meta["metadata"]["Title"] == "Le Viandier"
        assert meta["metadata"]["Author"] == "Taillevent"
        assert meta["metadata"]["Date"] == "1486"

    def test_v3_manifest_language_map(self) -> None:
        """Test metadata extraction from a v3 manifest with language maps."""
        manifest = {
            "label": {"en": ["The Cookbook"], "de": ["Das Kochbuch"]},
            "requiredStatement": {
                "value": {"en": ["Public Domain"]}
            },
            "metadata": [
                {
                    "label": {"en": ["Creator"]},
                    "value": {"en": ["Anonymous"]},
                },
            ],
        }
        meta = extract_manifest_metadata(manifest)
        assert meta["label"] in ("The Cookbook", "Das Kochbuch")
        assert meta["attribution"] == "Public Domain"
        assert meta["metadata"]["Creator"] == "Anonymous"

    def test_empty_manifest(self) -> None:
        """Test metadata extraction from an empty manifest."""
        meta = extract_manifest_metadata({})
        assert meta["label"] is None
        assert meta["attribution"] is None
        assert meta["metadata"] == {}

    def test_list_label_fallback(self) -> None:
        """Test v2 label as list."""
        manifest = {"label": ["Some Title"]}
        meta = extract_manifest_metadata(manifest)
        assert meta["label"] == "Some Title"


class TestPreviewManifest:
    """Tests for manifest preview function."""
    
    @patch('api.iiif._direct.make_request')
    def test_preview_success(self, mock_request: MagicMock) -> None:
        """Test successful manifest preview."""
        mock_request.return_value = {
            "label": "Test Manifest",
            "sequences": [{
                "canvases": [
                    {"images": [{"resource": {"service": {"@id": "https://example.org/iiif/img1"}}}]},
                    {"images": [{"resource": {"service": {"@id": "https://example.org/iiif/img2"}}}]},
                ]
            }],
            "rendering": {"format": "application/pdf"},
        }
        result = preview_manifest("https://example.org/iiif/123/manifest.json")
        assert result is not None
        assert result["label"] == "Test Manifest"
        assert result["page_count"] == 2
        assert result["has_renderings"] is True
        assert "application/pdf" in result["rendering_formats"]

    @patch('api.iiif._direct.make_request')
    def test_preview_failure(self, mock_request: MagicMock) -> None:
        """Test manifest preview when fetch fails."""
        mock_request.return_value = None
        result = preview_manifest("https://example.org/iiif/bad/manifest.json")
        assert result is None


class TestDownloadFromIIIFManifestFileStem:
    """Tests for download_from_iiif_manifest with file_stem parameter."""
    
    @patch('api.iiif._direct.download_one_from_service')
    @patch('api.iiif._direct.extract_image_service_bases')
    @patch('api.iiif._direct.download_iiif_renderings')
    @patch('api.iiif._direct.save_json')
    @patch('api.iiif._direct.make_request')
    @patch('api.iiif._direct.get_config')
    def test_file_stem_used_for_naming(
        self,
        mock_config: MagicMock,
        mock_request: MagicMock,
        mock_save: MagicMock,
        mock_render: MagicMock,
        mock_extract: MagicMock,
        mock_dl_one: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that file_stem controls output file naming."""
        mock_config.return_value = {"direct_iiif": {"enabled": True}}
        mock_request.return_value = {"@context": "test"}
        mock_render.return_value = 0
        mock_extract.return_value = ["https://example.org/iiif/img1"]
        mock_dl_one.return_value = True
        
        result = download_from_iiif_manifest(
            manifest_url="https://example.org/iiif/123/manifest.json",
            output_folder=str(tmp_path),
            title="Test",
            entry_id="E001",
            file_stem="MyCustomStem",
        )
        
        assert result["success"] is True
        # Verify file_stem was used in naming
        mock_save.assert_called_once()
        save_args = mock_save.call_args
        assert "MyCustomStem_manifest" in save_args[0][2]
        
        mock_dl_one.assert_called_once()
        dl_args = mock_dl_one.call_args
        assert dl_args[0][2] == "MyCustomStem_p00001.jpg"
    
    @patch('api.iiif._direct.download_one_from_service')
    @patch('api.iiif._direct.extract_image_service_bases')
    @patch('api.iiif._direct.download_iiif_renderings')
    @patch('api.iiif._direct.save_json')
    @patch('api.iiif._direct.make_request')
    @patch('api.iiif._direct.get_config')
    def test_no_file_stem_uses_template(
        self,
        mock_config: MagicMock,
        mock_request: MagicMock,
        mock_save: MagicMock,
        mock_render: MagicMock,
        mock_extract: MagicMock,
        mock_dl_one: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that without file_stem, config naming template is used."""
        mock_config.return_value = {
            "direct_iiif": {"enabled": True, "naming_template": "{provider}_{item_id}"}
        }
        mock_request.return_value = {"@context": "test"}
        mock_render.return_value = 0
        mock_extract.return_value = ["https://example.org/iiif/img1"]
        mock_dl_one.return_value = True
        
        result = download_from_iiif_manifest(
            manifest_url="https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb123/manifest",
            output_folder=str(tmp_path),
        )
        
        assert result["success"] is True
        dl_args = mock_dl_one.call_args
        # Should use provider_key + item_id from URL
        assert dl_args[0][2].startswith("mdz_")
