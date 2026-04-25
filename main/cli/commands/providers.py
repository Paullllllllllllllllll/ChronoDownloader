"""``--list-providers`` CLI command."""
from __future__ import annotations

from api.providers import PROVIDERS


def list_providers() -> None:
    """Print the alphabetically-sorted provider registry."""
    print("Available providers:")
    for key, (_search_fn, _download_fn, name) in sorted(
        PROVIDERS.items(), key=lambda kv: kv[0]
    ):
        print(f"  - {key}: {name}")
