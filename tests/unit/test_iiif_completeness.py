"""Regression tests for the IIIF completeness contract (audit B8, decision 2).

Pre-fix, an IIIF download set ``success=True`` on the first page that arrived
and only logged per-page failures, so a work missing pages was reported as a
complete success and never revisited. The download must record expected-vs-
downloaded page counts and mark the work ``partial`` when they differ.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


@patch("api.iiif._direct.download_one_from_service")
@patch("api.iiif._direct.extract_page_sources")
@patch("api.iiif._direct.download_iiif_renderings", return_value=0)
@patch("api.iiif._direct.save_json")
@patch("api.iiif._direct.make_request")
@patch("api.iiif._direct.budget_exhausted", return_value=False)
def test_partial_when_pages_missing(
    mock_budget: MagicMock,
    mock_req: MagicMock,
    mock_save: MagicMock,
    mock_render: MagicMock,
    mock_extract: MagicMock,
    mock_dl: MagicMock,
    mock_config: dict[str, Any],
) -> None:
    from api.iiif._direct import download_from_iiif_manifest

    mock_req.return_value = {"@context": "v2", "sequences": []}
    mock_extract.return_value = [
        ("service", "s1"),
        ("service", "s2"),
        ("service", "s3"),
    ]
    # Page 2 fails to download.
    mock_dl.side_effect = [True, False, True]

    with (
        patch("api.iiif._direct.prefer_pdf_over_images", return_value=False),
        patch("api.iiif._direct.get_max_pages", return_value=0),
    ):
        result = download_from_iiif_manifest(
            "https://gallica.bnf.fr/iiif/ark:/12148/x/manifest.json", "/out"
        )

    assert result["pages_expected"] == 3
    assert result["pages_downloaded"] == 2
    assert result["status"] == "partial"
    assert result["success"] is True  # some content arrived, but incomplete


@patch("api.iiif._direct.download_one_from_service")
@patch("api.iiif._direct.extract_page_sources")
@patch("api.iiif._direct.download_iiif_renderings", return_value=0)
@patch("api.iiif._direct.save_json")
@patch("api.iiif._direct.make_request")
@patch("api.iiif._direct.budget_exhausted", return_value=False)
def test_completed_when_all_pages_present(
    mock_budget: MagicMock,
    mock_req: MagicMock,
    mock_save: MagicMock,
    mock_render: MagicMock,
    mock_extract: MagicMock,
    mock_dl: MagicMock,
    mock_config: dict[str, Any],
) -> None:
    from api.iiif._direct import download_from_iiif_manifest

    mock_req.return_value = {"@context": "v2", "sequences": []}
    mock_extract.return_value = [("service", "s1"), ("service", "s2")]
    mock_dl.side_effect = [True, True]

    with (
        patch("api.iiif._direct.prefer_pdf_over_images", return_value=False),
        patch("api.iiif._direct.get_max_pages", return_value=0),
    ):
        result = download_from_iiif_manifest(
            "https://gallica.bnf.fr/iiif/ark:/12148/x/manifest.json", "/out"
        )

    assert result["pages_expected"] == 2
    assert result["pages_downloaded"] == 2
    assert result["status"] == "completed"


@patch("api.iiif._direct.download_file")
@patch("api.iiif._direct.download_iiif_renderings", return_value=0)
@patch("api.iiif._direct.save_json")
@patch("api.iiif._direct.make_request")
@patch("api.iiif._direct.budget_exhausted", return_value=False)
def test_direct_url_fallback_when_no_services(
    mock_budget: MagicMock,
    mock_req: MagicMock,
    mock_save: MagicMock,
    mock_render: MagicMock,
    mock_dl: MagicMock,
    mock_config: dict[str, Any],
) -> None:
    """A manifest whose canvases carry whole-image URLs but no Image API
    service block must fall back to downloading those URLs directly rather
    than reporting no downloadable content. The parser runs un-mocked; only
    the network/download layer is stubbed."""
    from api.iiif._direct import download_from_iiif_manifest

    mock_req.return_value = {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "sequences": [
            {
                "canvases": [
                    {"images": [{"resource": {"@id": "https://example.org/p1.jpg"}}]},
                    {"images": [{"resource": {"@id": "https://example.org/p2.jpg"}}]},
                ]
            }
        ],
    }
    mock_dl.return_value = "/out/objects/p.jpg"

    with (
        patch("api.iiif._direct.prefer_pdf_over_images", return_value=False),
        patch("api.iiif._direct.get_max_pages", return_value=0),
    ):
        result = download_from_iiif_manifest(
            "https://gallica.bnf.fr/iiif/ark:/12148/x/manifest.json", "/out"
        )

    assert result["status"] == "completed"
    assert result["pages_expected"] == 2
    assert result["pages_downloaded"] == 2
    called_urls = [c.args[0] for c in mock_dl.call_args_list]
    assert called_urls == [
        "https://example.org/p1.jpg",
        "https://example.org/p2.jpg",
    ]


@patch("api.iiif._direct.make_request")
def test_preview_manifest_counts_direct_urls(mock_req: MagicMock) -> None:
    """preview_manifest must count direct image URLs when a manifest exposes no
    Image API services, so page_count is not misleadingly zero."""
    from api.iiif._direct import preview_manifest

    mock_req.return_value = {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "sequences": [
            {
                "canvases": [
                    {"images": [{"resource": {"@id": "https://example.org/p1.jpg"}}]},
                    {"images": [{"resource": {"@id": "https://example.org/p2.jpg"}}]},
                ]
            }
        ],
    }

    info = preview_manifest("https://gallica.bnf.fr/iiif/ark:/12148/x/manifest.json")
    assert info is not None
    assert info["page_count"] == 2


@patch("api.iiif._direct.download_one_from_service")
@patch("api.iiif._direct.download_file")
@patch("api.iiif._direct.download_iiif_renderings", return_value=0)
@patch("api.iiif._direct.save_json")
@patch("api.iiif._direct.make_request")
@patch("api.iiif._direct.budget_exhausted", return_value=False)
def test_mixed_service_and_direct_canvases_are_all_expected(
    mock_budget: MagicMock,
    mock_req: MagicMock,
    mock_save: MagicMock,
    mock_render: MagicMock,
    mock_file: MagicMock,
    mock_svc: MagicMock,
    mock_config: dict[str, Any],
) -> None:
    """A manifest mixing Image API canvases with direct-URL canvases used to
    report only the service-backed pages: the direct-only canvases were dropped
    before ``pages_expected`` was computed, so the gap never surfaced. The
    parser runs un-mocked; only the download layer is stubbed."""
    from api.iiif._direct import download_from_iiif_manifest

    mock_req.return_value = {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "sequences": [
            {
                "canvases": [
                    {
                        "images": [
                            {"resource": {"service": {"@id": "https://ex.org/iiif/p1"}}}
                        ]
                    },
                    {"images": [{"resource": {"@id": "https://ex.org/p2.jpg"}}]},
                    {
                        "images": [
                            {"resource": {"service": {"@id": "https://ex.org/iiif/p3"}}}
                        ]
                    },
                ]
            }
        ],
    }
    mock_svc.return_value = True
    mock_file.return_value = "/out/objects/p2.jpg"

    with (
        patch("api.iiif._direct.prefer_pdf_over_images", return_value=False),
        patch("api.iiif._direct.get_max_pages", return_value=0),
    ):
        result = download_from_iiif_manifest(
            "https://gallica.bnf.fr/iiif/ark:/12148/x/manifest.json", "/out"
        )

    assert result["pages_expected"] == 3
    assert result["pages_downloaded"] == 3
    assert result["status"] == "completed"
    assert [c.args[0] for c in mock_svc.call_args_list] == [
        "https://ex.org/iiif/p1",
        "https://ex.org/iiif/p3",
    ]
    assert [c.args[0] for c in mock_file.call_args_list] == ["https://ex.org/p2.jpg"]
    # Page numbering follows canvas order across both kinds of source.
    assert mock_svc.call_args_list[1].args[2].endswith("_p00003.jpg")
    assert mock_file.call_args_list[0].args[2].endswith("_p00002")


@patch("api.iiif._direct.download_one_from_service")
@patch("api.iiif._direct.download_file")
@patch("api.iiif._direct.download_iiif_renderings", return_value=0)
@patch("api.iiif._direct.save_json")
@patch("api.iiif._direct.make_request")
@patch("api.iiif._direct.budget_exhausted", return_value=False)
def test_multi_sequence_manifest_expects_every_sequence(
    mock_budget: MagicMock,
    mock_req: MagicMock,
    mock_save: MagicMock,
    mock_render: MagicMock,
    mock_file: MagicMock,
    mock_svc: MagicMock,
    mock_config: dict[str, Any],
) -> None:
    """Pages beyond the first sequence count toward ``pages_expected``."""
    from api.iiif._direct import download_from_iiif_manifest

    mock_req.return_value = {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "sequences": [
            {
                "canvases": [
                    {
                        "images": [
                            {"resource": {"service": {"@id": "https://ex.org/iiif/v1"}}}
                        ]
                    }
                ]
            },
            {
                "canvases": [
                    {
                        "images": [
                            {"resource": {"service": {"@id": "https://ex.org/iiif/v2"}}}
                        ]
                    }
                ]
            },
        ],
    }
    mock_svc.return_value = True

    with (
        patch("api.iiif._direct.prefer_pdf_over_images", return_value=False),
        patch("api.iiif._direct.get_max_pages", return_value=0),
    ):
        result = download_from_iiif_manifest(
            "https://gallica.bnf.fr/iiif/ark:/12148/x/manifest.json", "/out"
        )

    assert result["pages_expected"] == 2
    assert result["status"] == "completed"
    assert mock_file.call_count == 0


def test_process_direct_iiif_propagates_partial_status() -> None:
    from main.orchestration.execution import process_direct_iiif

    with (
        patch("main.orchestration.execution.download_from_iiif_manifest") as mock_dl,
        patch(
            "main.data.work.compute_work_dir",
            return_value=("/nonexistent/work", "work"),
        ),
    ):
        mock_dl.return_value = {
            "success": True,
            "status": "partial",
            "provider": "Gallica",
            "pages_expected": 3,
            "pages_downloaded": 2,
        }
        result = process_direct_iiif(
            "https://example.org/manifest.json", "/output", entry_id="E1"
        )

    assert result["status"] == "partial"
    assert result["pages_downloaded"] == 2
    assert result["pages_expected"] == 3
