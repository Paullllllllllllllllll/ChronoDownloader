"""The provider contract's records: requests, completeness, errors."""

from __future__ import annotations

import pytest

from chronodownloader.coord import QuotaStatus, SlotLost
from chronodownloader.net import BreakerOpen, Cause, QuotaSpent, RequestFailed
from chronodownloader.providers.base import (
    Completeness,
    DownloadRequest,
    DownloadResult,
    ItemNotFound,
    MissingApiKey,
    ProviderBlocked,
    ProviderError,
    SearchRecord,
    page_item,
    page_limit,
    provider_error,
    source_id_of,
)
from tests.net.support import SLOT


class TestDownloadRequest:
    def test_defaults_follow_decision_d2(self) -> None:
        request = DownloadRequest(SLOT)
        assert request.format == "pdf"
        assert request.max_pages == 0
        assert request.page_limit is None
        assert request.include_metadata is True
        assert request.present == frozenset()

    def test_rejects_an_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="format"):
            DownloadRequest(SLOT, format="tiff")  # type: ignore[arg-type]

    def test_rejects_a_negative_page_limit(self) -> None:
        with pytest.raises(ValueError, match="max_pages"):
            DownloadRequest(SLOT, max_pages=-1)


@pytest.mark.parametrize(
    ("requested", "provider", "expected"),
    [(0, 0, 0), (20, 0, 20), (0, 50, 50), (20, 50, 20), (80, 50, 50)],
)
def test_page_limit_is_the_smaller_nonzero(
    requested: int, provider: int, expected: int
) -> None:
    assert page_limit(requested, provider) == expected


def test_page_items_follow_the_canvas_position() -> None:
    assert page_item(1) == "p00001"
    assert page_item(454) == "p00454"


class TestCompleteness:
    def test_every_expected_item_present_is_complete(self) -> None:
        part = Completeness("images", ("p00001", "p00002"), ("p00001", "p00002"))
        assert part.missing == ()
        assert part.complete

    def test_a_missing_item_is_partial(self) -> None:
        part = Completeness("images", ("p00001", "p00002", "p00003"), ("p00003",))
        assert part.missing == ("p00001", "p00002")
        assert not part.complete

    def test_nothing_expected_is_not_complete(self) -> None:
        assert not Completeness("images", (), ()).complete

    def test_present_items_must_be_expected(self) -> None:
        with pytest.raises(ValueError, match="not expected"):
            Completeness("images", ("p00001",), ("p00002",))

    def test_causes_are_optional_and_immutable(self) -> None:
        assert Completeness("images", ("p00001",), ()).causes == {}
        causes = {"p00001": "blocked"}
        part = Completeness("images", ("p00001",), (), causes=causes)
        causes["p00001"] = "transient"
        assert part.causes == {"p00001": "blocked"}
        with pytest.raises(TypeError):
            part.causes["p00001"] = "dns"  # type: ignore[index]

    def test_causes_name_missing_items_only(self) -> None:
        with pytest.raises(ValueError, match="not missing"):
            Completeness("images", ("p00001",), ("p00001",), causes={"p00001": "x"})

    def test_truncation_reads_the_page_limit(self) -> None:
        limited = Completeness("images", ("p00001",), (), page_limit=1, available=3)
        whole = Completeness("images", ("p00001",), (), page_limit=5, available=1)
        assert limited.truncated
        assert not whole.truncated


class TestDownloadResult:
    def test_complete_needs_parts_and_every_part_complete(self) -> None:
        done = Completeness("pdf", ("rendering",), ("rendering",))
        open_part = Completeness("images", ("p00001",), ())
        assert DownloadResult("mdz", "x", "pdf", (done,)).complete
        assert not DownloadResult("mdz", "x", "pdf", (done, open_part)).complete
        assert not DownloadResult("mdz", "x", "pdf", ()).complete

    def test_part_lookup(self) -> None:
        done = Completeness("pdf", ("rendering",), ("rendering",))
        result = DownloadResult("mdz", "x", "both", (done,))
        assert result.part("pdf") is done
        assert result.part("images") is None


def test_source_id_of_a_record_or_an_identifier() -> None:
    assert source_id_of(SearchRecord("mdz", "bsb1", "Kochbuch")) == "bsb1"
    assert source_id_of("bsb2") == "bsb2"


class TestProviderError:
    def test_not_found_names_the_item(self) -> None:
        error = provider_error(
            "mdz", RequestFailed(Cause.NOT_FOUND, "https://x", status=404), "bsb1"
        )
        assert isinstance(error, ItemNotFound)
        assert error.item == "bsb1"

    @pytest.mark.parametrize(
        "cause", [Cause.BLOCKED, Cause.TRANSIENT, Cause.DNS, Cause.REJECTED]
    )
    def test_other_failures_are_blocked_with_their_cause(self, cause: Cause) -> None:
        error = provider_error("bnf_gallica", RequestFailed(cause, "https://x"))
        assert isinstance(error, ProviderBlocked)
        assert error.cause == cause.value

    def test_a_missing_api_key_names_the_variable(self) -> None:
        error = MissingApiKey("dpla", "DPLA_API_KEY")
        assert isinstance(error, ProviderError)
        assert (error.provider, error.env_var) == ("dpla", "DPLA_API_KEY")
        assert "DPLA_API_KEY" in str(error)

    def test_breaker_quota_and_slot(self) -> None:
        breaker = provider_error("mdz", BreakerOpen("mdz", 5.0, "blocked"))
        quota = provider_error(
            "annas_archive",
            QuotaSpent("annas_archive", QuotaStatus("annas_archive", 875, 875, 9.0)),
        )
        lost = provider_error("mdz", SlotLost("expired", SLOT))
        assert isinstance(breaker, ProviderBlocked)
        assert (breaker.cause, breaker.until) == ("breaker_open", 5.0)
        assert isinstance(quota, ProviderBlocked)
        assert (quota.cause, quota.until) == ("quota_spent", 9.0)
        assert isinstance(lost, ProviderBlocked)
        assert lost.cause == "slot_lost"
