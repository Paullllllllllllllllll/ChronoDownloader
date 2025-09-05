import os
import argparse
import logging
import pandas as pd
from api import utils
from api import bnf_gallica_api
from api import internet_archive_api
from api import loc_api
from api import europeana_api
from api import dpla_api
from api import ddb_api
from api import british_library_api
from api import mdz_api
from api import polona_api
from api import bne_api
from api import google_books_api
from api import hathitrust_api
from api.model import SearchResult

ENABLED_APIS = [
    (bnf_gallica_api.search_gallica, bnf_gallica_api.download_gallica_work, "BnF Gallica"),
    (internet_archive_api.search_internet_archive, internet_archive_api.download_ia_work, "Internet Archive"),
    (loc_api.search_loc, loc_api.download_loc_work, "Library of Congress"),
    (europeana_api.search_europeana, europeana_api.download_europeana_work, "Europeana"),
    (dpla_api.search_dpla, dpla_api.download_dpla_work, "DPLA"),
    (ddb_api.search_ddb, ddb_api.download_ddb_work, "DDB"),
    (british_library_api.search_british_library, british_library_api.download_british_library_work, "British Library"),
    (mdz_api.search_mdz, mdz_api.download_mdz_work, "MDZ"),
    (polona_api.search_polona, polona_api.download_polona_work, "Polona"),
    (bne_api.search_bne, bne_api.download_bne_work, "BNE"),
    (google_books_api.search_google_books, google_books_api.download_google_books_work, "Google Books"),
    (hathitrust_api.search_hathitrust, hathitrust_api.download_hathitrust_work, "HathiTrust"),
]


def process_work(title, creator=None, base_output_dir="downloaded_works", dry_run: bool = False):
    logger = logging.getLogger(__name__)
    logger.info("Processing work: '%s'%s", title, f" by '{creator}'" if creator else "")
    sanitized_title = utils.sanitize_filename(title)
    work_output_folder = os.path.join(base_output_dir, sanitized_title)
    if not os.path.exists(work_output_folder):
        os.makedirs(work_output_folder, exist_ok=True)
        logger.info("Created directory: %s", work_output_folder)
    found_items_overall = []
    for search_func, download_func, api_name in ENABLED_APIS:
        logger.info("--- Searching on %s for '%s' ---", api_name, title)
        try:
            if creator:
                try:
                    search_results = search_func(title, creator=creator)
                except TypeError:
                    search_results = search_func(title)
            else:
                search_results = search_func(title)
            if search_results:
                logger.info("Found %d item(s) on %s", len(search_results), api_name)
                for i, item in enumerate(search_results):
                    if isinstance(item, SearchResult):
                        item_title = item.title or "Unknown Title"
                        item_id = item.source_id or item.raw.get('identifier') or item.raw.get('ark_id') or f"item_{i}"
                        provider = item.provider or api_name
                    else:
                        item_title = item.get('title', 'Unknown Title')
                        item_id = item.get('id', item.get('identifier', item.get('ark_id', f"item_{i}")))
                        provider = api_name
                    logger.info("  - %s (ID: %s)", item_title, item_id)
                    item_folder_name = utils.sanitize_filename(f"{provider}_{item_id}_{item_title}")
                    item_output_folder = os.path.join(work_output_folder, item_folder_name)
                    os.makedirs(item_output_folder, exist_ok=True)
                    if dry_run:
                        logger.info("Dry-run: skipping download for %s:%s", provider, item_id)
                    else:
                        if download_func(item, item_output_folder):
                            logger.info("Successfully processed item from %s in %s", api_name, item_output_folder)
                        else:
                            logger.warning("Problem processing item from %s in %s", api_name, item_output_folder)
                    found_items_overall.append(item)
            else:
                logger.info("No items found for '%s' on %s.", title, api_name)
        except Exception:
            logger.exception("Error during search/download with %s for '%s'", api_name, title)
    if not found_items_overall:
        logger.info("No items found for '%s' across all enabled APIs.", title)
    else:
        logger.info("Finished processing '%s'. Check '%s' for results.", title, work_output_folder)


def main():
    parser = argparse.ArgumentParser(description="Download historical sources from various digital libraries.")
    parser.add_argument("csv_file", help="Path to the CSV file containing works to download. Must have a 'Title' column. Optional 'Creator' column.")
    parser.add_argument("--output_dir", default="downloaded_works", help="Directory to save downloaded files.")
    parser.add_argument("--dry-run", action="store_true", help="Run searches and create folders, but skip downloads.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    args = parser.parse_args()

    # Configure base logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    logger = logging.getLogger(__name__)

    if not os.path.exists(args.csv_file):
        logger.error("CSV file not found at %s", args.csv_file)
        return
    try:
        works_df = pd.read_csv(args.csv_file)
    except Exception as e:
        logger.error("Error reading CSV file: %s", e)
        return
    if "Title" not in works_df.columns:
        logger.error("CSV file must contain a 'Title' column.")
        return
    logger.info("Starting downloader. Output directory: %s", args.output_dir)
    for index, row in works_df.iterrows():
        title = row["Title"]
        creator = row.get("Creator")
        if pd.isna(title) or not str(title).strip():
            logger.warning("Skipping row %d due to missing or empty title.", index + 1)
            continue
        process_work(str(title), None if pd.isna(creator) else str(creator), args.output_dir, dry_run=args.dry_run)
        logger.info("%s", "-" * 50)
    logger.info("All works processed.")


if __name__ == "__main__":
    main()
