import os
import re
import json
import requests
import time


def sanitize_filename(name):
    """Sanitize string for safe filenames."""
    if not name:
        return "_untitled_"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"[\s._-]+", "_", name)
    return name[:100]


def download_file(url, folder_path, filename):
    os.makedirs(folder_path, exist_ok=True)
    filepath = os.path.join(folder_path, sanitize_filename(filename))
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Successfully downloaded {filename} to {folder_path}")
        return filepath
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {url}: {e}")
        return None
    except IOError as e:
        print(f"Error saving file {filepath}: {e}")
        return None


def save_json(data, folder_path, filename):
    os.makedirs(folder_path, exist_ok=True)
    filepath = os.path.join(folder_path, sanitize_filename(filename) + ".json")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print(f"Successfully saved {filename}.json to {folder_path}")
        return filepath
    except IOError as e:
        print(f"Error saving JSON file {filepath}: {e}")
        return None
    except TypeError as e:
        print(f"Error serializing data to JSON for {filename}: {e}")
        return None


def make_request(url, params=None, headers=None, timeout=15):
    time.sleep(0.5)
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        if "application/json" in response.headers.get("Content-Type", ""):
            return response.json()
        elif "xml" in response.headers.get("Content-Type", ""):
            return response.text
        else:
            print(f"Unexpected content type: {response.headers.get('Content-Type')}")
            return response.content
    except requests.exceptions.Timeout:
        print(f"Request timed out: {url}")
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error for {url}: {e.response.status_code} {e.response.reason}")
    except requests.exceptions.RequestException as e:
        print(f"Request failed for {url}: {e}")
    except json.JSONDecodeError:
        print(f"Failed to decode JSON from {url}")
        print(f"Response content: {response.text[:200]}...")
    return None
