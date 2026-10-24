"""Fixtures of the IIIF tests: the network tests' local server, coordinator
stand-in and client factory, and two sample manifests."""

from __future__ import annotations

from typing import Any

import pytest

from tests.net.conftest import coordinator, make_network, server, sleeps

__all__ = [
    "coordinator",
    "make_network",
    "sample_iiif_manifest_v2",
    "sample_iiif_manifest_v3",
    "server",
    "sleeps",
]


@pytest.fixture
def sample_iiif_manifest_v2() -> dict[str, Any]:
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
        "sequences": [
            {
                "canvases": [
                    {
                        "images": [
                            {
                                "resource": {
                                    "service": {
                                        "@id": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f1"
                                    }
                                }
                            }
                        ]
                    },
                    {
                        "images": [
                            {
                                "resource": {
                                    "service": {
                                        "@id": "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k123/f2"
                                    }
                                }
                            }
                        ]
                    },
                ]
            }
        ],
        "rendering": {
            "@id": "https://gallica.bnf.fr/ark:/12148/bpt6k123.pdf",
            "format": "application/pdf",
        },
    }


@pytest.fixture
def sample_iiif_manifest_v3() -> dict[str, Any]:
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
                "items": [
                    {
                        "type": "AnnotationPage",
                        "items": [
                            {
                                "type": "Annotation",
                                "body": {
                                    "type": "Image",
                                    "service": [
                                        {
                                            "id": "https://example.org/iiif/img1",
                                            "type": "ImageService3",
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        ],
    }
