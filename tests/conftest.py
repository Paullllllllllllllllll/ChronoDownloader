"""Pytest configuration and shared fixtures for ChronoDownloader tests."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ============================================================================
# Path and Directory Fixtures
# ============================================================================

@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    """Create a temporary directory for test files."""
    dirpath = tempfile.mkdtemp(prefix="chrono_test_")
    yield dirpath
    shutil.rmtree(dirpath, ignore_errors=True)


@pytest.fixture
def temp_output_dir(temp_dir: str) -> str:
    """Create a temporary output directory structure."""
    output_dir = os.path.join(temp_dir, "downloaded_works")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def sample_config() -> Dict[str, Any]:
    """Return a sample configuration dictionary."""
    return {
        "general": {
            "interactive_mode": False,
            "default_output_dir": "downloaded_works",
            "default_csv_path": "sample_works.csv"
        },
        "providers": {
            "internet_archive": True,
            "bnf_gallica": True,
            "loc": True,
            "europeana": False,
            "dpla": False,
            "ddb": False,
            "british_library": False,
            "mdz": True,
            "polona": False,
            "bne": False,
            "google_books": False,
            "hathitrust": False,
            "wellcome": False,
            "annas_archive": False
        },
        "selection": {
            "strategy": "collect_and_select",
            "provider_hierarchy": ["mdz", "bnf_gallica", "internet_archive", "loc"],
            "min_title_score": 85,
            "creator_weight": 0.2,
            "year_tolerance": 2,
            "max_candidates_per_provider": 5,
            "download_strategy": "selected_only",
            "keep_non_selected_metadata": True
        },
        "download": {
            "prefer_pdf_over_images": True,
            "download_manifest_renderings": True,
            "max_renderings_per_manifest": 1,
            "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
            "overwrite_existing": False,
            "include_metadata": True,
            "resume_mode": "skip_completed",
            "max_parallel_downloads": 2
        },
        "download_limits": {
            "total": {
                "images_gb": 100,
                "pdfs_gb": 50,
                "metadata_gb": 1
            },
            "per_work": {
                "images_gb": 5,
                "pdfs_gb": 3,
                "metadata_mb": 10
            },
            "on_exceed": "skip"
        },
        "provider_settings": {
            "gallica": {
                "max_pages": 500,
                "delay_ms": 100,
                "network": {
                    "max_attempts": 3,
                    "base_backoff_s": 1.0
                }
            },
            "internet_archive": {
                "max_pages": 1000,
                "delay_ms": 50
            }
        }
    }


@pytest.fixture
def config_file(temp_dir: str, sample_config: Dict[str, Any]) -> str:
    """Create a temporary config file."""
    config_path = os.path.join(temp_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(sample_config, f)
    return config_path


@pytest.fixture
def mock_config(sample_config: Dict[str, Any]):
    """Mock the config module to return sample config."""
    with patch("api.core.config._CONFIG_CACHE", sample_config):
        with patch("api.core.config.get_config", return_value=sample_config):
            yield sample_config


# ============================================================================
# CSV Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_csv_data() -> pd.DataFrame:
    """Return sample CSV data as a DataFrame."""
    return pd.DataFrame({
        "entry_id": ["E0001", "E0002", "E0003", "E0004", "E0005"],
        "short_title": [
            "The Art of Cooking",
            "A History of France",
            "Mathematical Principles",
            "Botanical Gardens",
            "Ancient Architecture"
        ],
        "main_author": [
            "John Smith",
            "Marie Dupont",
            "Isaac Newton",
            "Charles Darwin",
            "Marcus Vitruvius"
        ],
        "full_title": [
            "The Art of Cooking: A Complete Guide",
            "A Complete History of France from Ancient Times",
            "Mathematical Principles of Natural Philosophy",
            "Botanical Gardens of the World",
            "Ten Books on Architecture"
        ],
        "earliest_year": [1850, 1920, 1687, 1859, 1486],
        "retrievable": [pd.NA, True, False, pd.NA, pd.NA],
        "link": [pd.NA, "https://example.com/france", pd.NA, pd.NA, pd.NA]
    })


@pytest.fixture
def sample_csv_file(temp_dir: str, sample_csv_data: pd.DataFrame) -> str:
    """Create a temporary CSV file with sample data."""
    csv_path = os.path.join(temp_dir, "sample_works.csv")
    sample_csv_data.to_csv(csv_path, index=False)
    return csv_path


# ============================================================================
# Search Result Fixtures
# ============================================================================

@pytest.fixture
def sample_search_result():
    """Return a sample SearchResult object."""
    from api.model import SearchResult
    return SearchResult(
        provider="Internet Archive",
        title="The Art of Cooking",
        creators=["John Smith"],
        date="1850",
        source_id="artofcooking1850",
        iiif_manifest="https://archive.org/iiif/artofcooking1850/manifest.json",
        item_url="https://archive.org/details/artofcooking1850",
        thumbnail_url="https://archive.org/services/img/artofcooking1850",
        provider_key="internet_archive",
        raw={
            "identifier": "artofcooking1850",
            "title": "The Art of Cooking",
            "creator": ["John Smith"],
            "year": "1850"
        }
    )


@pytest.fixture
def sample_search_results():
    """Return multiple sample SearchResult objects."""
    from api.model import SearchResult
    return [
        SearchResult(
            provider="Internet Archive",
            title="The Art of Cooking",
            creators=["John Smith"],
            date="1850",
            source_id="artofcooking1850",
            item_url="https://archive.org/details/artofcooking1850",
            provider_key="internet_archive",
            raw={"identifier": "artofcooking1850", "title": "The Art of Cooking"}
        ),
        SearchResult(
            provider="BnF Gallica",
            title="L'art de la cuisine",
            creators=["Jean Dupont"],
            date="1845",
            source_id="bpt6k12345",
            item_url="https://gallica.bnf.fr/ark:/12148/bpt6k12345",
            provider_key="bnf_gallica",
            raw={"ark_id": "bpt6k12345", "title": "L'art de la cuisine"}
        ),
        SearchResult(
            provider="MDZ",
            title="Die Kunst des Kochens",
            creators=["Hans Mueller"],
            date="1860",
            source_id="bsb12345678",
            item_url="https://www.digitale-sammlungen.de/view/bsb12345678",
            provider_key="mdz",
            raw={"id": "bsb12345678", "title": "Die Kunst des Kochens"}
        )
    ]


# ============================================================================
# Mock Response Fixtures
# ============================================================================

@pytest.fixture
def mock_response():
    """Create a mock HTTP response."""
    def _create_mock(
        status_code: int = 200,
        json_data: Dict[str, Any] | None = None,
        content: bytes = b"",
        headers: Dict[str, str] | None = None
    ) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.ok = 200 <= status_code < 300
        response.json.return_value = json_data or {}
        response.content = content
        response.text = content.decode("utf-8") if content else ""
        response.headers = headers or {"Content-Type": "application/json"}
        response.iter_content = MagicMock(return_value=[content] if content else [])
        response.raise_for_status = MagicMock()
        if not response.ok:
            response.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
        return response
    return _create_mock


@pytest.fixture
def mock_ia_search_response() -> Dict[str, Any]:
    """Return a mock Internet Archive search API response."""
    return {
        "response": {
            "numFound": 2,
            "docs": [
                {
                    "identifier": "artofcooking1850",
                    "title": "The Art of Cooking",
                    "creator": ["John Smith"],
                    "year": "1850",
                    "mediatype": "texts"
                },
                {
                    "identifier": "cookingarts1855",
                    "title": "The Cooking Arts",
                    "creator": ["Jane Doe"],
                    "year": "1855",
                    "mediatype": "texts"
                }
            ]
        }
    }


@pytest.fixture
def mock_gallica_search_response() -> Dict[str, Any]:
    """Return a mock BnF Gallica search API response."""
    return {
        "records": [
            {
                "identifier": "ark:/12148/bpt6k12345",
                "title": "L'art de la cuisine",
                "creator": "Jean Dupont",
                "date": "1845"
            }
        ]
    }


# ============================================================================
# Network Mocking Fixtures
# ============================================================================

@pytest.fixture
def mock_requests_get(mock_response):
    """Mock requests.get to return controlled responses."""
    with patch("requests.Session.get") as mock_get:
        mock_get.return_value.__enter__ = mock_response()
        mock_get.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_get


@pytest.fixture
def mock_make_request():
    """Mock the make_request function."""
    with patch("api.core.network.make_request") as mock:
        yield mock


# ============================================================================
# Context Reset Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_context():
    """Reset thread-local context before and after each test."""
    from api.core.context import clear_all_context, reset_counters
    clear_all_context()
    reset_counters()
    yield
    clear_all_context()
    reset_counters()


@pytest.fixture(autouse=True)
def reset_config_cache():
    """Reset config cache before each test."""
    import api.core.config as config_module
    original_cache = config_module._CONFIG_CACHE
    config_module._CONFIG_CACHE = None
    yield
    config_module._CONFIG_CACHE = original_cache


# ============================================================================
# Budget Fixtures
# ============================================================================

@pytest.fixture
def fresh_budget():
    """Create a fresh DownloadBudget instance."""
    from api.core.budget import DownloadBudget
    return DownloadBudget()


# ============================================================================
# Work Directory Fixtures  
# ============================================================================

@pytest.fixture
def work_dir_structure(temp_output_dir: str) -> Dict[str, str]:
    """Create a complete work directory structure."""
    work_id = "e_0001_the_art_of_cooking"
    work_dir = os.path.join(temp_output_dir, work_id)
    objects_dir = os.path.join(work_dir, "objects")
    metadata_dir = os.path.join(work_dir, "metadata")
    
    os.makedirs(objects_dir, exist_ok=True)
    os.makedirs(metadata_dir, exist_ok=True)
    
    # Create work.json
    work_json = {
        "work_id": work_id,
        "title": "The Art of Cooking",
        "creator": "John Smith",
        "entry_id": "E0001",
        "status": "pending"
    }
    work_json_path = os.path.join(work_dir, "work.json")
    with open(work_json_path, "w", encoding="utf-8") as f:
        json.dump(work_json, f)
    
    return {
        "work_dir": work_dir,
        "objects_dir": objects_dir,
        "metadata_dir": metadata_dir,
        "work_json_path": work_json_path,
        "work_id": work_id
    }


# ============================================================================
# IIIF Manifest Fixtures
# ============================================================================

@pytest.fixture
def sample_iiif_manifest_v2() -> Dict[str, Any]:
    """Return a sample IIIF Presentation v2 manifest."""
    return {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "@type": "sc:Manifest",
        "label": "Le Viandier de Taillevent",
        "attribution": "BnF Gallica",
        "metadata": [
            {"label": "Title", "value": "Le Viandier"},
            {"label": "Author", "value": "Taillevent"},
            {"label": "Date", "value": "1486"},
        ],
        "sequences": [{
            "canvases": [
                {
                    "images": [{
                        "resource": {
                            "service": {"@id": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f1"}
                        }
                    }]
                },
                {
                    "images": [{
                        "resource": {
                            "service": {"@id": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f2"}
                        }
                    }]
                },
            ]
        }],
        "rendering": {"@id": "https://gallica.bnf.fr/ark:/12148/bpt6k123.pdf", "format": "application/pdf"},
    }


@pytest.fixture
def sample_iiif_manifest_v3() -> Dict[str, Any]:
    """Return a sample IIIF Presentation v3 manifest."""
    return {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "type": "Manifest",
        "label": {"en": ["The Modern Cook"]},
        "requiredStatement": {
            "label": {"en": ["Attribution"]},
            "value": {"en": ["Public Domain"]},
        },
        "metadata": [
            {"label": {"en": ["Creator"]}, "value": {"en": ["La Chapelle"]}},
            {"label": {"en": ["Date"]}, "value": {"en": ["1733"]}},
        ],
        "items": [
            {
                "type": "Canvas",
                "items": [{
                    "type": "AnnotationPage",
                    "items": [{
                        "type": "Annotation",
                        "body": {
                            "type": "Image",
                            "service": [{"id": "https://example.org/iiif/img1", "type": "ImageService3"}]
                        }
                    }]
                }]
            },
        ],
    }
