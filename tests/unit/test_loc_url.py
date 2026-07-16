"""Regression tests for LoC item-JSON URL construction.

``download_loc_work`` appends ``fo=json`` to the item URL; a second ``?`` on
a URL that already carries a query string would make LoC ignore the
parameter and serve HTML instead of JSON.
"""

from __future__ import annotations

from unittest.mock import patch

from api.model import SearchResult


def _fetched_url(item_url: str) -> str:
    """Run download_loc_work with a mocked network; return the URL it fetched."""
    from api.providers.loc import download_loc_work

    sr = SearchResult(
        provider="Library of Congress",
        title="Test",
        source_id="abc123",
        provider_key="loc",
        item_url=item_url,
        raw={"id": "abc123", "item_url": item_url},
    )
    with patch("api.providers.loc.make_request", return_value=None) as mock_req:
        assert download_loc_work(sr, "/out") is False
    return str(mock_req.call_args_list[0].args[0])


class TestLocItemJsonUrl:
    """fo=json must be joined with the correct query separator."""

    def test_plain_url_gets_question_mark(self) -> None:
        url = _fetched_url("https://www.loc.gov/item/abc123/")
        assert url == "https://www.loc.gov/item/abc123/?fo=json"

    def test_url_with_query_string_gets_ampersand(self) -> None:
        url = _fetched_url("https://www.loc.gov/item/abc123/?st=gallery")
        assert url == "https://www.loc.gov/item/abc123/?st=gallery&fo=json"

    def test_url_already_json_unchanged(self) -> None:
        url = _fetched_url("https://www.loc.gov/item/abc123/?fo=json")
        assert url == "https://www.loc.gov/item/abc123/?fo=json"
