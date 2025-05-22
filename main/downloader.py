import os
import argparse
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


def process_work(title, creator=None, base_output_dir="downloaded_works"):
    print(f"\nProcessing work: '{title}'" + (f" by '{creator}'" if creator else ""))
    sanitized_title = utils.sanitize_filename(title)
    work_output_folder = os.path.join(base_output_dir, sanitized_title)
    if not os.path.exists(work_output_folder):
        os.makedirs(work_output_folder, exist_ok=True)
        print(f"Created directory: {work_output_folder}")
    found_items_overall = []
    for search_func, download_func, api_name in ENABLED_APIS:
        print(f"\n--- Searching on {api_name} for '{title}' ---")
        try:
            if creator:
                try:
                    search_results = search_func(title, creator=creator)
                except TypeError:
                    search_results = search_func(title)
            else:
                search_results = search_func(title)
            if search_results:
                print(f"Found {len(search_results)} item(s) on {api_name}:")
                for i, item in enumerate(search_results):
                    item_title = item.get('title', 'Unknown Title')
                    item_id = item.get('id', item.get('identifier', item.get('ark_id', f"item_{i}")))
                    print(f"  - {item_title} (ID: {item_id})")
                    item_folder_name = utils.sanitize_filename(f"{api_name}_{item_id}_{item_title}")
                    item_output_folder = os.path.join(work_output_folder, item_folder_name)
                    os.makedirs(item_output_folder, exist_ok=True)
                    if download_func(item, item_output_folder):
                        print(f"Successfully processed item from {api_name} in {item_output_folder}")
                    else:
                        print(f"Problem processing item from {api_name} in {item_output_folder}")
                    found_items_overall.append(item)
            else:
                print(f"No items found for '{title}' on {api_name}.")
        except Exception as e:
            print(f"Error during search/download with {api_name} for '{title}': {e}")
            import traceback
            traceback.print_exc()
    if not found_items_overall:
        print(f"No items found for '{title}' across all enabled APIs.")
    else:
        print(f"Finished processing '{title}'. Check '{work_output_folder}' for results.")


def main():
    parser = argparse.ArgumentParser(description="Download historical sources from various digital libraries.")
    parser.add_argument("csv_file", help="Path to the CSV file containing works to download. Must have a 'Title' column. Optional 'Creator' column.")
    parser.add_argument("--output_dir", default="downloaded_works", help="Directory to save downloaded files.")
    args = parser.parse_args()
    if not os.path.exists(args.csv_file):
        print(f"Error: CSV file not found at {args.csv_file}")
        return
    try:
        works_df = pd.read_csv(args.csv_file)
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        return
    if "Title" not in works_df.columns:
        print("Error: CSV file must contain a 'Title' column.")
        return
    print(f"Starting downloader. Output directory: {args.output_dir}")
    for index, row in works_df.iterrows():
        title = row["Title"]
        creator = row.get("Creator")
        if pd.isna(title) or not str(title).strip():
            print(f"Skipping row {index+1} due to missing or empty title.")
            continue
        process_work(title, creator, args.output_dir)
        print("-" * 50)
    print("\nAll works processed.")


if __name__ == "__main__":
    main()
