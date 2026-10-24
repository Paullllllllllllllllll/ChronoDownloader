"""Rendering selection and the manifest summary.

Ported from v1.30.3 ``tests/unit/test_iiif.py`` (rendering classes),
``test_utils.py`` and ``test_utils_extended.py`` (``download_iiif_renderings``)
and ``test_direct_iiif.py`` (``preview_manifest``). The configured whitelist
and per-manifest limit are gone (fixed defaults, one rendering per item), so
the selection is asserted where v1.30.3 asserted the attempted downloads;
the download attempts themselves are covered in ``test_download.py``.
"""

from __future__ import annotations

from typing import Any

from chronodownloader.iiif import (
    DEFAULT_WHITELIST,
    Rendering,
    select_renderings,
    summarize_manifest,
)


def _urls(
    manifest: dict[str, Any], whitelist: tuple[str, ...] | None = None
) -> list[str]:
    selected = (
        select_renderings(manifest)
        if whitelist is None
        else select_renderings(manifest, whitelist)
    )
    return [rendering.url for rendering in selected]


def _manifest(*renderings: dict[str, Any]) -> dict[str, Any]:
    return {"rendering": list(renderings)}


class TestRenderingSelection:
    def test_declared_format_outranks_the_url_suffix(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.pdf", "format": "application/pdf"},
            {"@id": "https://example.org/book.epub", "format": "application/epub+zip"},
        )
        assert _urls(manifest, ("application/epub+zip",)) == [
            "https://example.org/book.epub"
        ]

    def test_url_suffix_still_rescues_an_undeclared_format(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.pdf"},
            {"id": "https://example.org/other.pdf", "type": "Text"},
        )
        assert _urls(manifest) == [
            "https://example.org/book.pdf",
            "https://example.org/other.pdf",
        ]

    def test_skips_non_whitelisted_format(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/file.xml", "format": "text/xml"}
        )
        assert _urls(manifest) == []

    def test_respects_whitelist(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.com/pdf", "format": "application/pdf"},
            {"@id": "https://example.com/html", "format": "text/html"},
        )
        assert _urls(manifest) == ["https://example.com/pdf"]

    def test_deduplicates_rendering_urls(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/same.pdf", "format": "application/pdf"},
            {"@id": "https://example.org/same.pdf", "format": "application/pdf"},
        )
        assert _urls(manifest) == ["https://example.org/same.pdf"]

    def test_handles_empty_manifest(self) -> None:
        assert select_renderings({}) == []

    def test_rendering_as_dict_not_list(self) -> None:
        manifest = {
            "rendering": {
                "@id": "https://example.org/file.pdf",
                "format": "application/pdf",
            }
        }
        assert _urls(manifest) == ["https://example.org/file.pdf"]

    def test_allows_pdf_by_url_suffix_without_a_format(self) -> None:
        manifest = _manifest({"@id": "https://example.org/file.pdf", "format": ""})
        assert _urls(manifest) == ["https://example.org/file.pdf"]

    def test_default_whitelist_is_pdf_then_epub(self) -> None:
        assert DEFAULT_WHITELIST == ("application/pdf", "application/epub+zip")

    def test_selection_records_format_and_label(self) -> None:
        manifest = _manifest(
            {
                "@id": "https://example.org/v.pdf",
                "format": "Application/PDF",
                "label": {"en": ["Download as PDF"]},
            }
        )
        assert select_renderings(manifest) == [
            Rendering("https://example.org/v.pdf", "application/pdf", "Download as PDF")
        ]


class TestSelectRenderingsOrder:
    def test_format_less_pdf_ranks_with_the_pdf_entry(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/viewer", "format": "text/html"},
            {"@id": "https://example.org/book.epub", "format": "application/epub+zip"},
            {"@id": "https://example.org/plain.pdf"},
        )
        assert _urls(manifest) == [
            "https://example.org/plain.pdf",
            "https://example.org/book.epub",
        ]

    def test_whitelist_restricts_the_selection(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.epub", "format": "application/epub+zip"}
        )
        assert _urls(manifest, ("application/pdf",)) == []


class TestSequenceLevelRenderings:
    def test_sequence_level_pdf_is_collected(self) -> None:
        manifest = {
            "@context": "http://iiif.io/api/presentation/2/context.json",
            "sequences": [
                {
                    "@type": "sc:Sequence",
                    "rendering": {
                        "@id": "https://example.org/volume.pdf",
                        "format": "application/pdf",
                        "label": "Download as PDF",
                    },
                    "canvases": [],
                }
            ],
        }
        assert _urls(manifest) == ["https://example.org/volume.pdf"]

    def test_manifest_level_renderings_come_first(self) -> None:
        manifest = {
            "rendering": [
                {"@id": "https://example.org/top.pdf", "format": "application/pdf"}
            ],
            "sequences": [
                {
                    "rendering": [
                        {
                            "@id": "https://example.org/seq.pdf",
                            "format": "application/pdf",
                        }
                    ]
                }
            ],
        }
        assert _urls(manifest) == [
            "https://example.org/top.pdf",
            "https://example.org/seq.pdf",
        ]

    def test_duplicate_rendering_across_levels_is_selected_once(self) -> None:
        shared = {"@id": "https://example.org/vol.pdf", "format": "application/pdf"}
        manifest = {"rendering": [shared], "sequences": [{"rendering": [shared]}]}
        assert _urls(manifest) == ["https://example.org/vol.pdf"]

    def test_non_list_sequences_are_ignored(self) -> None:
        assert select_renderings({"sequences": "not-a-list"}) == []


class TestRenderingPriority:
    def test_whitelist_order_beats_document_order(self) -> None:
        manifest = {
            "rendering": [
                {
                    "@id": "https://example.org/book.epub",
                    "format": "application/epub+zip",
                }
            ],
            "sequences": [
                {
                    "rendering": [
                        {
                            "@id": "https://example.org/volume.pdf",
                            "format": "application/pdf",
                        }
                    ]
                }
            ],
        }
        assert _urls(manifest)[0] == "https://example.org/volume.pdf"

    def test_undeclared_formats_rank_by_url_suffix(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.epub"},
            {"@id": "https://example.org/book.pdf"},
        )
        assert _urls(manifest) == [
            "https://example.org/book.pdf",
            "https://example.org/book.epub",
        ]

    def test_equal_rank_keeps_manifest_order(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/a.pdf", "format": "application/pdf"},
            {"@id": "https://example.org/b.pdf", "format": "application/pdf"},
        )
        assert _urls(manifest) == [
            "https://example.org/a.pdf",
            "https://example.org/b.pdf",
        ]

    def test_reordered_whitelist_reorders_the_candidates(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.pdf", "format": "application/pdf"},
            {"@id": "https://example.org/book.epub", "format": "application/epub+zip"},
        )
        whitelist = ("application/epub+zip", "application/pdf")
        assert _urls(manifest, whitelist)[0] == "https://example.org/book.epub"


class TestMalformedRenderingEntries:
    def test_list_valued_format_is_normalized(self) -> None:
        manifest = _manifest(
            {
                "@id": "https://example.org/book.epub",
                "format": ["application/epub+zip"],
            },
            {"@id": "https://example.org/book.pdf", "format": ["application/pdf"]},
        )
        assert _urls(manifest) == [
            "https://example.org/book.pdf",
            "https://example.org/book.epub",
        ]

    def test_list_valued_format_still_binds_the_whitelist(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.pdf", "format": ["application/pdf"]}
        )
        assert _urls(manifest, ("application/epub+zip",)) == []

    def test_non_string_format_falls_back_to_the_url_suffix(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/book.pdf", "format": {"a": 1}}
        )
        assert _urls(manifest) == ["https://example.org/book.pdf"]

    def test_entries_without_a_string_url_are_skipped(self) -> None:
        manifest = _manifest(
            {"@id": 7, "format": "application/pdf"},
            {"format": "application/pdf"},
            {"@id": "https://example.org/book.pdf", "format": "application/pdf"},
        )
        manifest["rendering"].append("junk")
        assert _urls(manifest) == ["https://example.org/book.pdf"]


def _two_service_canvases() -> dict[str, Any]:
    return {
        "sequences": [
            {
                "canvases": [
                    {
                        "images": [
                            {"resource": {"service": {"@id": "https://e/iiif/img1"}}}
                        ]
                    },
                    {
                        "images": [
                            {"resource": {"service": {"@id": "https://e/iiif/img2"}}}
                        ]
                    },
                ]
            }
        ]
    }


class TestSummarizeManifest:
    """Ported from ``TestPreviewManifest`` and ``TestPreviewMatchesDownloadSelection``:
    the summary promises exactly what the download would fetch."""

    def test_summary(self) -> None:
        manifest = {
            "label": "Test Manifest",
            **_two_service_canvases(),
            "rendering": {
                "@id": "https://example.org/volume.pdf",
                "format": "application/pdf",
            },
        }
        summary = summarize_manifest(manifest)
        assert summary.label == "Test Manifest"
        assert summary.page_count == 2
        assert summary.has_renderings is True
        assert "application/pdf" in summary.rendering_formats

    def test_summary_reports_sequence_level_renderings(self) -> None:
        manifest = {
            "label": "Wellcome-style volume",
            "sequences": [
                {
                    "rendering": {
                        "@id": "https://example.org/volume.pdf",
                        "format": "application/pdf",
                    },
                    "canvases": [],
                }
            ],
        }
        summary = summarize_manifest(manifest)
        assert summary.has_renderings is True
        assert summary.rendering_formats == ("application/pdf",)

    def test_summary_normalizes_a_list_valued_rendering_format(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/volume.pdf", "format": ["application/pdf"]}
        )
        assert summarize_manifest(manifest).rendering_formats == ("application/pdf",)

    def test_summary_counts_mixed_canvases(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "images": [
                                {"resource": {"service": {"@id": "https://ex/iiif/1"}}}
                            ]
                        },
                        {"images": [{"resource": {"@id": "https://ex.org/p2.jpg"}}]},
                    ]
                }
            ]
        }
        assert summarize_manifest(manifest).page_count == 2

    def test_summary_counts_direct_urls(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {"images": [{"resource": {"@id": "https://e.org/p1.jpg"}}]},
                        {"images": [{"resource": {"@id": "https://e.org/p2.jpg"}}]},
                    ]
                }
            ]
        }
        assert summarize_manifest(manifest).page_count == 2

    def test_non_whitelisted_rendering_is_not_advertised(self) -> None:
        manifest = {
            "label": "HTML viewer only",
            "rendering": [{"@id": "https://example.org/viewer", "format": "text/html"}],
        }
        summary = summarize_manifest(manifest)
        assert summary.has_renderings is False
        assert summary.rendering_formats == ()

    def test_format_less_pdf_rendering_is_advertised(self) -> None:
        manifest = {"rendering": [{"@id": "https://example.org/volume.pdf"}]}
        summary = summarize_manifest(manifest)
        assert summary.has_renderings is True
        assert summary.rendering_formats == ()

    def test_formats_are_listed_in_download_preference_order(self) -> None:
        manifest = _manifest(
            {"@id": "https://example.org/v.epub", "format": "application/epub+zip"},
            {"@id": "https://example.org/v.pdf", "format": "application/pdf"},
        )
        assert summarize_manifest(manifest).rendering_formats == (
            "application/pdf",
            "application/epub+zip",
        )
