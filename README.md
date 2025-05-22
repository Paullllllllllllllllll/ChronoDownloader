# Historical Sources Downloader

This project provides a Python-based tool to search for and download digitized historical sources (books, manuscripts, etc.) from various European and American digital libraries and platforms.

## Project Structure

- `api/`: Contains modules for interacting with specific digital library APIs.
  - `__init__.py`
  - `utils.py`: Utility functions (e.g., sanitizing filenames, downloading files).
  - `[library_name]_api.py`: Individual modules for each API (e.g., `bnf_gallica_api.py`, `internet_archive_api.py`, etc.).
- `main/`: Contains the main downloader script.
  - `__init__.py`
  - `downloader.py`: The main script to process a CSV input file and orchestrate downloads.
- `sample_works.csv`: An example CSV file format.
- `requirements.txt`: Python dependencies.
- `README.md`: This file.

## Setup

1. **Clone the repository (or create the files as listed).**
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **API Keys:**
   Some APIs require an API key. You'll need to obtain these from the respective platforms and insert them into the relevant API modules. Currently Google Books, DPLA, DDB and HathiTrust require keys for full access.

## Usage

1. **Prepare your input CSV file.** It should contain at least a column named `Title`. Example (`sample_works.csv`):
   ```csv
   Title,Creator
   "Le Morte d'Arthur","Thomas Malory"
   "The Raven","Edgar Allan Poe"
   "Philosophiæ Naturalis Principia Mathematica","Isaac Newton"
   ```
2. **Run the downloader script:**
   ```bash
   python main/downloader.py your_input_file.csv
   ```
  The script will create a directory for each work found and save metadata or downloads there.

## Implemented APIs
The downloader currently supports connectors for:

- **BnF Gallica** (France)
- **Internet Archive** (US)
- **Library of Congress** (US)
- **Europeana** (EU aggregator)
- **Digital Public Library of America**
- **Deutsche Digitale Bibliothek**
- **British Library**
- **Münchener DigitalisierungsZentrum**
- **Polona** (Polish National Library)
- **Biblioteca Nacional de España**
- **Google Books**
- **HathiTrust**

## Extending the Tool
- Implement more APIs in `api/`.
- Enhance search capabilities by using more fields from the CSV.
- Improve download logic (IIIF parsing, etc.).
