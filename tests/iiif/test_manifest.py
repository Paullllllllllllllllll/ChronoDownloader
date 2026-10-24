"""Manifest parsing: service bases, direct URLs, page sources, canvases, metadata.

Ported from v1.30.3 ``tests/unit/test_iiif.py`` (parsing classes) and
``tests/unit/test_direct_iiif.py`` (``TestExtractManifestMetadata``);
assertions unchanged except that metadata is a typed record.
"""

from __future__ import annotations

from typing import Any

from chronodownloader.iiif import (
    Canvas,
    PageSource,
    extract_canvases,
    extract_direct_image_urls,
    extract_image_service_bases,
    extract_manifest_metadata,
    extract_page_sources,
)


def _v2(*resources: Any) -> dict[str, Any]:
    return {
        "sequences": [
            {"canvases": [{"images": [{"resource": res}]} for res in resources]}
        ]
    }


def _v3(*bodies: Any) -> dict[str, Any]:
    return {"items": [{"items": [{"items": [{"body": body}]}]} for body in bodies]}


class TestExtractImageServiceBasesV2:
    def test_extracts_service_ids_from_v2(
        self, sample_iiif_manifest_v2: dict[str, Any]
    ) -> None:
        bases = extract_image_service_bases(sample_iiif_manifest_v2)
        assert len(bases) == 2
        assert bases[0] == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f1"
        assert bases[1] == "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f2"

    def test_extracts_from_resource_id_fallback(self) -> None:
        manifest = _v2(
            {
                "@id": "https://example.org/iiif/img1/full/max/0/default.jpg",
                "service": {},
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_extracts_when_v2_service_is_a_list(self) -> None:
        manifest = _v2(
            {
                "service": [
                    {"@id": "https://example.org/iiif/img1"},
                    {"@id": "https://example.org/iiif/other"},
                ]
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_extracts_from_v2_oa_choice_resource(self) -> None:
        manifest = _v2(
            {
                "@type": "oa:Choice",
                "default": {"service": {"@id": "https://example.org/iiif/img1"}},
                "item": [{"service": {"@id": "https://example.org/iiif/alt"}}],
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_skips_canvas_without_images(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {"images": []},
                        {
                            "images": [
                                {
                                    "resource": {
                                        "service": {
                                            "@id": "https://example.org/iiif/img1"
                                        }
                                    }
                                }
                            ]
                        },
                    ]
                }
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_empty_manifest_returns_empty_list(self) -> None:
        assert extract_image_service_bases({}) == []

    def test_deduplicates_service_bases(self) -> None:
        manifest = _v2(
            {"service": {"@id": "https://example.org/img"}},
            {"service": {"@id": "https://example.org/img"}},
        )
        assert extract_image_service_bases(manifest) == ["https://example.org/img"]

    def test_uses_id_key_when_at_id_missing(self) -> None:
        manifest = _v2({"service": {"id": "https://example.org/iiif/img1"}})
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]


class TestExtractImageServiceBasesV3:
    def test_extracts_from_v3_manifest(
        self, sample_iiif_manifest_v3: dict[str, Any]
    ) -> None:
        bases = extract_image_service_bases(sample_iiif_manifest_v3)
        assert bases == ["https://example.org/iiif/img1"]

    def test_v3_body_as_list(self) -> None:
        manifest = _v3([{"service": [{"id": "https://example.org/iiif/img1"}]}])
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_v3_service_as_dict(self) -> None:
        manifest = _v3({"service": {"@id": "https://example.org/iiif/img1"}})
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_extracts_from_v3_choice_body(self) -> None:
        manifest = _v3(
            {
                "type": "Choice",
                "items": [
                    {
                        "type": "Image",
                        "service": [{"id": "https://example.org/iiif/img1"}],
                    },
                    {
                        "type": "Image",
                        "service": [{"id": "https://example.org/iiif/alt"}],
                    },
                ],
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_v3_fallback_from_body_id(self) -> None:
        manifest = _v3({"id": "https://example.org/iiif/img1/full/max/0/default.jpg"})
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_v3_skips_empty_annotation_pages(self) -> None:
        manifest: dict[str, Any] = {"items": [{"items": []}]}
        assert extract_image_service_bases(manifest) == []

    def test_v3_skips_empty_annotations(self) -> None:
        manifest: dict[str, Any] = {"items": [{"items": [{"items": []}]}]}
        assert extract_image_service_bases(manifest) == []


class TestExtractImageServiceBasesMixed:
    def test_both_v2_and_v3_deduped(self) -> None:
        manifest = {
            **_v2({"service": {"@id": "https://example.org/img1"}}),
            **_v3({"service": [{"id": "https://example.org/img1"}]}),
        }
        assert extract_image_service_bases(manifest) == ["https://example.org/img1"]


class TestExtractDirectImageUrls:
    def test_extracts_v2_image_urls(self) -> None:
        manifest = _v2(
            {"@id": "https://example.org/page1.jpg"},
            {"@id": "https://example.org/page2.jpg"},
        )
        assert extract_direct_image_urls(manifest) == [
            "https://example.org/page1.jpg",
            "https://example.org/page2.jpg",
        ]

    def test_extracts_v3_image_urls(self) -> None:
        manifest = _v3({"id": "https://example.org/page1.jpg"})
        assert extract_direct_image_urls(manifest) == ["https://example.org/page1.jpg"]

    def test_deduplicates_urls(self) -> None:
        manifest = _v2(
            {"@id": "https://example.org/same.jpg"},
            {"@id": "https://example.org/same.jpg"},
        )
        assert extract_direct_image_urls(manifest) == ["https://example.org/same.jpg"]

    def test_empty_manifest_returns_empty(self) -> None:
        assert extract_direct_image_urls({}) == []

    def test_skips_canvases_without_images(self) -> None:
        manifest: dict[str, Any] = {"sequences": [{"canvases": [{"images": []}]}]}
        assert extract_direct_image_urls(manifest) == []

    def test_v3_body_as_list(self) -> None:
        manifest = _v3([{"id": "https://example.org/page1.jpg"}])
        assert extract_direct_image_urls(manifest) == ["https://example.org/page1.jpg"]


class TestMultiSequenceManifests:
    """Every v2 sequence carries pages, and ``sequences`` is not always a list."""

    @staticmethod
    def _canvas(service_id: str) -> dict[str, Any]:
        return {"images": [{"resource": {"service": {"@id": service_id}}}]}

    def test_pages_from_every_sequence_are_kept(self) -> None:
        manifest = {
            "sequences": [
                {"canvases": [self._canvas("https://example.org/iiif/v1p1")]},
                {
                    "canvases": [
                        self._canvas("https://example.org/iiif/v2p1"),
                        self._canvas("https://example.org/iiif/v2p2"),
                    ]
                },
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/v1p1",
            "https://example.org/iiif/v2p1",
            "https://example.org/iiif/v2p2",
        ]

    def test_dict_valued_sequences_still_yield_pages(self) -> None:
        manifest = {
            "sequences": {"canvases": [self._canvas("https://example.org/iiif/img1")]}
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_non_dict_sequence_entries_are_skipped(self) -> None:
        manifest = {
            "sequences": [
                "not-a-sequence",
                {"canvases": [self._canvas("https://example.org/iiif/img1")]},
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_non_list_canvases_are_skipped(self) -> None:
        manifest = {
            "sequences": [
                {"canvases": "broken"},
                {"canvases": [self._canvas("https://example.org/iiif/img1")]},
            ]
        }
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_direct_urls_span_every_sequence(self) -> None:
        manifest = {
            "sequences": [
                {"canvases": [{"images": [{"resource": {"@id": "https://e/p1.jpg"}}]}]},
                {"canvases": [{"images": [{"resource": {"@id": "https://e/p2.jpg"}}]}]},
            ]
        }
        assert extract_direct_image_urls(manifest) == [
            "https://e/p1.jpg",
            "https://e/p2.jpg",
        ]


class TestServiceArrayFallback:
    """A service array may lead with an entry that carries no identifier."""

    def test_v2_skips_a_service_entry_without_an_id(self) -> None:
        manifest = _v2(
            {
                "service": [
                    {"profile": "http://iiif.io/api/auth/1/"},
                    {"@id": "https://example.org/iiif/img1"},
                ]
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_v3_skips_a_service_entry_without_an_id(self) -> None:
        manifest = _v3(
            {
                "service": [
                    {"type": "AuthCookieService1"},
                    {"id": "https://example.org/iiif/img1"},
                ]
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]

    def test_unusable_service_array_falls_back_to_the_resource_id(self) -> None:
        manifest = _v2(
            {
                "service": ["junk", {"label": "no id"}],
                "@id": "https://example.org/iiif/img1/full/max/0/default.jpg",
            }
        )
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1"
        ]


class TestExtractPageSources:
    """Mixed manifests must keep every page."""

    def test_v2_mixed_canvases_keep_every_page(self) -> None:
        manifest = _v2(
            {"service": {"@id": "https://example.org/iiif/img1"}},
            {"@id": "https://example.org/p2.jpg"},
            {"service": {"@id": "https://example.org/iiif/img3"}},
        )
        assert extract_page_sources(manifest) == [
            ("service", "https://example.org/iiif/img1"),
            ("direct", "https://example.org/p2.jpg"),
            ("service", "https://example.org/iiif/img3"),
        ]
        assert extract_image_service_bases(manifest) == [
            "https://example.org/iiif/img1",
            "https://example.org/iiif/img3",
        ]

    def test_v3_mixed_canvases_keep_every_page(self) -> None:
        manifest = _v3(
            {"service": [{"id": "https://example.org/iiif/img1"}]},
            {"id": "https://example.org/p2.jpg"},
        )
        assert extract_page_sources(manifest) == [
            ("service", "https://example.org/iiif/img1"),
            ("direct", "https://example.org/p2.jpg"),
        ]

    def test_service_wins_when_a_canvas_offers_both(self) -> None:
        manifest = _v2(
            {
                "@id": "https://example.org/p1.jpg",
                "service": {"@id": "https://example.org/iiif/img1"},
            }
        )
        assert extract_page_sources(manifest) == [
            ("service", "https://example.org/iiif/img1")
        ]

    def test_canvas_with_neither_source_is_skipped(self) -> None:
        manifest = _v2({"label": "no id at all"}, {"@id": "https://example.org/p2.jpg"})
        assert extract_page_sources(manifest) == [
            ("direct", "https://example.org/p2.jpg")
        ]

    def test_duplicate_urls_are_dropped(self) -> None:
        manifest = _v2(
            {"@id": "https://example.org/p.jpg"}, {"@id": "https://example.org/p.jpg"}
        )
        assert extract_page_sources(manifest) == [
            ("direct", "https://example.org/p.jpg")
        ]

    def test_empty_manifest_returns_empty(self) -> None:
        assert extract_page_sources({}) == []


class TestMalformedEntries:
    """Malformed canvases are skipped by inspection, without exception guards."""

    def test_non_mapping_resources_and_bodies_are_skipped(self) -> None:
        manifest = {
            **_v2("not-a-resource", {"@id": "https://example.org/p1.jpg"}),
            **_v3(["not-a-body"], [], {"id": "https://example.org/p2.jpg"}),
        }
        assert extract_page_sources(manifest) == [
            ("direct", "https://example.org/p1.jpg"),
            ("direct", "https://example.org/p2.jpg"),
        ]

    def test_non_list_items_and_images_are_skipped(self) -> None:
        assert extract_page_sources({"items": "broken"}) == []
        manifest = {"sequences": [{"canvases": [{"images": {"0": {}}}, "junk"]}]}
        assert extract_page_sources(manifest) == []


class TestExtractCanvases:
    def test_canvases_carry_position_and_label(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {
                            "label": "Titelblatt",
                            "images": [{"resource": {"@id": "https://e/p1.jpg"}}],
                        },
                        {"images": [{"resource": {"@id": "https://e/p1.jpg"}}]},
                        {
                            "label": {"de": ["S. 2"]},
                            "images": [
                                {"resource": {"service": {"@id": "https://e/iiif/2"}}}
                            ],
                        },
                    ]
                }
            ]
        }
        assert extract_canvases(manifest) == [
            Canvas(1, PageSource("direct", "https://e/p1.jpg"), "Titelblatt"),
            Canvas(2, PageSource("direct", "https://e/p1.jpg"), None),
            Canvas(3, PageSource("service", "https://e/iiif/2"), "S. 2"),
        ]

    def test_an_unusable_canvas_keeps_its_position(self) -> None:
        manifest = {
            "sequences": [
                {
                    "canvases": [
                        {"images": [{"resource": {"@id": "https://e/p1.jpg"}}]},
                        {"label": "blank", "images": []},
                        {"images": [{"resource": {"@id": "https://e/p3.jpg"}}]},
                    ]
                }
            ]
        }
        assert extract_canvases(manifest) == [
            Canvas(1, PageSource("direct", "https://e/p1.jpg")),
            Canvas(2, None, "blank"),
            Canvas(3, PageSource("direct", "https://e/p3.jpg")),
        ]


class TestExtractManifestMetadata:
    def test_v2_manifest(self) -> None:
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
        assert meta.label == "Le Viandier de Taillevent"
        assert meta.attribution == "BnF Gallica"
        assert meta.metadata["Title"] == "Le Viandier"
        assert meta.metadata["Author"] == "Taillevent"
        assert meta.metadata["Date"] == "1486"

    def test_v3_manifest_language_map(self) -> None:
        manifest = {
            "label": {"en": ["The Cookbook"], "de": ["Das Kochbuch"]},
            "requiredStatement": {"value": {"en": ["Public Domain"]}},
            "metadata": [
                {"label": {"en": ["Creator"]}, "value": {"en": ["Anonymous"]}},
            ],
        }
        meta = extract_manifest_metadata(manifest)
        assert meta.label in ("The Cookbook", "Das Kochbuch")
        assert meta.attribution == "Public Domain"
        assert meta.metadata["Creator"] == "Anonymous"

    def test_empty_manifest(self) -> None:
        meta = extract_manifest_metadata({})
        assert meta.label is None
        assert meta.attribution is None
        assert dict(meta.metadata) == {}

    def test_list_label_fallback(self) -> None:
        meta = extract_manifest_metadata({"label": ["Some Title"]})
        assert meta.label == "Some Title"
