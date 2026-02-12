"""Providers registry mapping provider keys to (search_func, download_func, display_name).

Centralizes provider imports and the mapping used by the downloader.
"""
from __future__ import annotations

from typing import Any

from . import bnf_gallica_api
from . import internet_archive_api
from . import loc_api
from . import europeana_api
from . import dpla_api
from . import ddb_api
from . import british_library_api
from . import mdz_api
from . import polona_api
from . import bne_api
from . import google_books_api
from . import hathitrust_api
from . import wellcome_api
from . import annas_archive_api
from . import slub_api
from . import e_rara_api
from . import sbb_digital_api

PROVIDERS: dict[str, tuple[Any, Any, str]] = {
    "bnf_gallica": (bnf_gallica_api.search_gallica, bnf_gallica_api.download_gallica_work, "BnF Gallica"),
    "internet_archive": (
        internet_archive_api.search_internet_archive,
        internet_archive_api.download_ia_work,
        "Internet Archive",
    ),
    "loc": (loc_api.search_loc, loc_api.download_loc_work, "Library of Congress"),
    "europeana": (europeana_api.search_europeana, europeana_api.download_europeana_work, "Europeana"),
    "dpla": (dpla_api.search_dpla, dpla_api.download_dpla_work, "DPLA"),
    "ddb": (ddb_api.search_ddb, ddb_api.download_ddb_work, "DDB"),
    "british_library": (
        british_library_api.search_british_library,
        british_library_api.download_british_library_work,
        "British Library",
    ),
    "mdz": (mdz_api.search_mdz, mdz_api.download_mdz_work, "MDZ"),
    "polona": (polona_api.search_polona, polona_api.download_polona_work, "Polona"),
    "bne": (bne_api.search_bne, bne_api.download_bne_work, "BNE"),
    "google_books": (google_books_api.search_google_books, google_books_api.download_google_books_work, "Google Books"),
    "hathitrust": (hathitrust_api.search_hathitrust, hathitrust_api.download_hathitrust_work, "HathiTrust"),
    "wellcome": (wellcome_api.search_wellcome, wellcome_api.download_wellcome_work, "Wellcome Collection"),
    "annas_archive": (annas_archive_api.search_annas_archive, annas_archive_api.download_annas_archive_work, "Anna's Archive"),
    "slub": (slub_api.search_slub, slub_api.download_slub_work, "SLUB Dresden"),
    "e_rara": (e_rara_api.search_e_rara, e_rara_api.download_e_rara_work, "e-rara"),
    "sbb_digital": (
        sbb_digital_api.search_sbb_digital,
        sbb_digital_api.download_sbb_digital_work,
        "SBB Digital Collections",
    ),
}

__all__ = ["PROVIDERS"]
