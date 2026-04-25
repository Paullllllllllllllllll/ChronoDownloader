"""Provider registry mapping provider keys to (search_func, download_func, display_name).

Centralizes provider imports and the mapping consumed by the orchestration
layer. Internal to the :mod:`api.providers` package; downstream code should
import :data:`PROVIDERS` from :mod:`api.providers`.
"""
from __future__ import annotations

from typing import Any

from . import (
    annas_archive,
    bne,
    bnf_gallica,
    british_library,
    ddb,
    dpla,
    e_rara,
    europeana,
    google_books,
    hathitrust,
    internet_archive,
    loc,
    mdz,
    polona,
    sbb_digital,
    slub,
    wellcome,
)

PROVIDERS: dict[str, tuple[Any, Any, str]] = {
    "bnf_gallica": (
        bnf_gallica.search_gallica,
        bnf_gallica.download_gallica_work,
        "BnF Gallica",
    ),
    "internet_archive": (
        internet_archive.search_internet_archive,
        internet_archive.download_ia_work,
        "Internet Archive",
    ),
    "loc": (loc.search_loc, loc.download_loc_work, "Library of Congress"),
    "europeana": (
        europeana.search_europeana,
        europeana.download_europeana_work,
        "Europeana",
    ),
    "dpla": (dpla.search_dpla, dpla.download_dpla_work, "DPLA"),
    "ddb": (ddb.search_ddb, ddb.download_ddb_work, "DDB"),
    "british_library": (
        british_library.search_british_library,
        british_library.download_british_library_work,
        "British Library",
    ),
    "mdz": (mdz.search_mdz, mdz.download_mdz_work, "MDZ"),
    "polona": (polona.search_polona, polona.download_polona_work, "Polona"),
    "bne": (bne.search_bne, bne.download_bne_work, "BNE"),
    "google_books": (
        google_books.search_google_books,
        google_books.download_google_books_work,
        "Google Books",
    ),
    "hathitrust": (
        hathitrust.search_hathitrust,
        hathitrust.download_hathitrust_work,
        "HathiTrust",
    ),
    "wellcome": (
        wellcome.search_wellcome,
        wellcome.download_wellcome_work,
        "Wellcome Collection",
    ),
    "annas_archive": (
        annas_archive.search_annas_archive,
        annas_archive.download_annas_archive_work,
        "Anna's Archive",
    ),
    "slub": (slub.search_slub, slub.download_slub_work, "SLUB Dresden"),
    "e_rara": (e_rara.search_e_rara, e_rara.download_e_rara_work, "e-rara"),
    "sbb_digital": (
        sbb_digital.search_sbb_digital,
        sbb_digital.download_sbb_digital_work,
        "SBB Digital Collections",
    ),
}

__all__ = ["PROVIDERS"]
