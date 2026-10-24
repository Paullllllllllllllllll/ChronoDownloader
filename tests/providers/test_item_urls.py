"""The ``item_id_from_url`` and ``item_manifest_url`` hooks (live round D3).

The URL shapes are those v1.30.3 resolved (``api/identifier_resolver.py``
manifest templates, ``api/iiif/_direct.py`` item patterns) plus each
library's item and viewer pages; every URL is first mapped to its provider
by host, as ``link`` rows and ``fetch --url`` do.
"""

from __future__ import annotations

import pytest

from chronodownloader.providers.base import item_id_from_url, item_manifest_url
from chronodownloader.providers.registry import Registry, host_key
from chronodownloader.settings import Settings

RESOLVED: list[tuple[str, str, str]] = [
    ("https://www.digitale-sammlungen.de/view/bsb00027043", "mdz", "bsb00027043"),
    (
        "https://www.digitale-sammlungen.de/en/view/bsb00027043?page=5",
        "mdz",
        "bsb00027043",
    ),
    ("https://www.digitale-sammlungen.de/de/details/bsb11280551", "mdz", "bsb11280551"),
    (
        "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb11280551/manifest",
        "mdz",
        "bsb11280551",
    ),
    ("https://gallica.bnf.fr/ark:/12148/bpt6k1511262r", "bnf_gallica", "bpt6k1511262r"),
    (
        "https://gallica.bnf.fr/ark:/12148/bpt6k1511262r/f7.item.zoom",
        "bnf_gallica",
        "bpt6k1511262r",
    ),
    (
        "https://gallica.bnf.fr/ark:/12148/btv1b8600016k.image",
        "bnf_gallica",
        "btv1b8600016k",
    ),
    ("https://archive.org/details/b30510168", "internet_archive", "b30510168"),
    (
        "https://archive.org/details/b30510168/page/n5/mode/2up",
        "internet_archive",
        "b30510168",
    ),
    (
        "https://archive.org/download/b30510168/b30510168.pdf",
        "internet_archive",
        "b30510168",
    ),
    (
        "https://iiif.archive.org/iiif/b30510168/manifest.json",
        "internet_archive",
        "b30510168",
    ),
    ("https://www.e-rara.ch/zut/content/titleinfo/29924315", "e_rara", "29924315"),
    ("https://www.e-rara.ch/i3f/v20/29924315/manifest", "e_rara", "29924315"),
    ("https://digital.slub-dresden.de/id313701709", "slub", "313701709"),
    (
        "https://iiif.slub-dresden.de/iiif/2/313701709/manifest.json",
        "slub",
        "313701709",
    ),
    ("https://www.loc.gov/item/65078367/", "loc", "65078367"),
    (
        "https://access.bl.uk/item/viewer/ark:/81055/vdc_100000000123.0x000002",
        "british_library",
        "vdc_100000000123.0x000002",
    ),
    (
        "https://api.bl.uk/metadata/iiif/ark:/81055/vdc_100000000123/manifest.json",
        "british_library",
        "vdc_100000000123",
    ),
    (
        "https://babel.hathitrust.org/cgi/pt?id=ucbk.ark:/28722/h2rf5m094&seq=7",
        "hathitrust",
        "ucbk.ark:/28722/h2rf5m094",
    ),
    (
        "https://babel.hathitrust.org/cgi/imgsrv/manifest/mdp.39015012345678",
        "hathitrust",
        "mdp.39015012345678",
    ),
    ("https://catalog.hathitrust.org/Record/100158800", "hathitrust", "100158800"),
    (
        "https://polona.pl/preview/52c7d7fd-e857-4cd8-95dc-eea728738df5",
        "polona",
        "52c7d7fd-e857-4cd8-95dc-eea728738df5",
    ),
    (
        "https://polona.pl/item-view/52C7D7FD-E857-4CD8-95DC-EEA728738DF5?page=2",
        "polona",
        "52c7d7fd-e857-4cd8-95dc-eea728738df5",
    ),
    (
        "https://www.europeana.eu/en/item/9200379/BibliographicResource_3000118436165",
        "europeana",
        "/9200379/BibliographicResource_3000118436165",
    ),
    (
        "https://iiif.europeana.eu/presentation/794/ark__12148_bpt6k114423k/manifest",
        "europeana",
        "/794/ark__12148_bpt6k114423k",
    ),
    ("https://wellcomecollection.org/works/a2239muq", "wellcome", "a2239muq"),
    ("https://wellcomecollection.org/works/a2239muq/items", "wellcome", "a2239muq"),
    (
        "https://www.deutsche-digitale-bibliothek.de/item/O4JTHXQWZ6Q2ZVQ5V3C6Y4LYJ3QH5ZHE",
        "ddb",
        "O4JTHXQWZ6Q2ZVQ5V3C6Y4LYJ3QH5ZHE",
    ),
    (
        "https://dp.la/item/4727cb2d032c186ddbae1caed16a6221",
        "dpla",
        "4727cb2d032c186ddbae1caed16a6221",
    ),
    (
        "https://digital.staatsbibliothek-berlin.de/werkansicht?PPN=PPN3346520873&PHYSID=PHYS_0005",
        "sbb_digital",
        "PPN3346520873",
    ),
    (
        "https://bnedigital.bne.es/bd/card?id=0b4c7b2e-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
        "bne",
        "0b4c7b2e-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
    ),
    (
        "https://books.google.com/books?id=zGlhAAAAcAAJ&hl=de",
        "google_books",
        "zGlhAAAAcAAJ",
    ),
]

UNRESOLVED: list[str] = [
    "https://www.digitale-sammlungen.de/en/search?query=koch",
    "https://gallica.bnf.fr/services/engine/search/sru?query=x",
    "https://archive.org/search?query=cookbook",
    "https://www.e-rara.ch/zut/content/pageview/29924320",
    "https://digital.slub-dresden.de/werkansicht/dlf/12345/1",
    "https://www.loc.gov/books/?q=cookbook",
    "https://polona.pl/search?query=kucharz",
    "https://www.europeana.eu/en/search?query=koch",
    "https://wellcomecollection.org/search/works?query=cookery",
]

MANIFESTS: list[tuple[str, str, str]] = [
    (
        "mdz",
        "bsb00027043",
        "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb00027043/manifest",
    ),
    (
        "bnf_gallica",
        "bpt6k1511262r",
        "https://gallica.bnf.fr/iiif/ark:/12148/bpt6k1511262r/manifest.json",
    ),
    (
        "internet_archive",
        "b30510168",
        "https://iiif.archive.org/iiif/b30510168/manifest.json",
    ),
    ("e_rara", "29924315", "https://www.e-rara.ch/i3f/v20/29924315/manifest"),
    (
        "slub",
        "313701709",
        "https://iiif.slub-dresden.de/iiif/2/313701709/manifest.json",
    ),
    ("loc", "65078367", "https://www.loc.gov/item/65078367/manifest.json"),
    (
        "british_library",
        "vdc_100000000123.0x000002",
        "https://api.bl.uk/metadata/iiif/ark:/81055/vdc_100000000123/manifest.json",
    ),
    (
        "polona",
        "52c7d7fd-e857-4cd8-95dc-eea728738df5",
        "https://polona.pl/api/search-service/search/iiif/"
        "52c7d7fd-e857-4cd8-95dc-eea728738df5/manifest.json",
    ),
    (
        "europeana",
        "/9200379/Bib_3",
        "https://iiif.europeana.eu/presentation/9200379/Bib_3/manifest?format=3",
    ),
]

WITHOUT_MANIFEST = ("hathitrust", "wellcome", "ddb", "dpla", "sbb_digital", "bne")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry(Settings())


@pytest.mark.parametrize(("url", "provider", "item"), RESOLVED)
def test_an_item_url_resolves_to_its_provider_and_item(
    registry: Registry, url: str, provider: str, item: str
) -> None:
    assert host_key(url) == provider
    assert item_id_from_url(registry.get(provider), url) == item


@pytest.mark.parametrize("url", UNRESOLVED)
def test_a_url_that_names_no_item_resolves_to_none(
    registry: Registry, url: str
) -> None:
    provider = host_key(url)
    assert provider is not None
    assert item_id_from_url(registry.get(provider), url) is None


def test_a_url_of_another_library_is_not_read(registry: Registry) -> None:
    assert (
        item_id_from_url(registry.get("mdz"), "https://example.org/view/bsb1") is None
    )


@pytest.mark.parametrize(("provider", "item", "manifest"), MANIFESTS)
def test_the_item_manifest_follows_the_v1_30_3_template(
    registry: Registry, provider: str, item: str, manifest: str
) -> None:
    assert item_manifest_url(registry.get(provider), item) == manifest


@pytest.mark.parametrize("provider", WITHOUT_MANIFEST)
def test_native_providers_name_no_manifest(registry: Registry, provider: str) -> None:
    assert item_manifest_url(registry.get(provider), "x") is None
