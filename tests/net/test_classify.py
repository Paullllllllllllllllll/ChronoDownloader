"""Tests for net/classify.py and net/credentials.py.

Ported from v1.30.3 ``tests/unit/test_network_extended.py``
(``TestConnectFailureFastFail``, ``TestCredentialDetection``,
``TestRedactUrl``); the Retry-After, status and challenge tests are new.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
import requests

from chronodownloader.net.classify import (
    CONNECT_FAILURE_MAX_ATTEMPTS,
    backoff_seconds,
    classify_exception,
    connect_aware_attempt_cap,
    is_challenge,
    is_connect_failure,
    retry_after_seconds,
    status_failure,
)
from chronodownloader.net.credentials import (
    credential_values,
    redact_text,
    redact_url,
    request_carries_credential,
)
from chronodownloader.net.errors import Cause

NOW = 1_800_000_000.0


def test_credential_values_lists_query_and_header_secrets() -> None:
    values = credential_values(
        "https://x.org/api?md5=abc&key=SECRETKEY",
        {"Authorization": "Bearer TOKEN1", "Accept": "text/html"},
    )
    assert set(values) == {"SECRETKEY", "Bearer TOKEN1", "TOKEN1"}
    assert values[0] == "Bearer TOKEN1"


def _http_date(seconds: float) -> str:
    return format_datetime(datetime.fromtimestamp(seconds, UTC), usegmt=True)


class TestRetryAfter:
    def test_seconds(self) -> None:
        assert retry_after_seconds("7", max_s=60.0, now=NOW) == 7.0
        assert retry_after_seconds(" 2.5 ", max_s=60.0, now=NOW) == 2.5

    def test_http_date_is_measured_from_now(self) -> None:
        assert retry_after_seconds(_http_date(NOW + 30), max_s=60.0, now=NOW) == 30.0

    def test_values_are_clamped_to_zero_and_the_maximum(self) -> None:
        assert retry_after_seconds("-5", max_s=60.0, now=NOW) == 0.0
        assert retry_after_seconds("600", max_s=60.0, now=NOW) == 60.0
        assert retry_after_seconds(_http_date(NOW - 30), max_s=60.0, now=NOW) == 0.0
        assert retry_after_seconds(_http_date(NOW + 900), max_s=60.0, now=NOW) == 60.0

    @pytest.mark.parametrize("value", [None, "", "soon", "nan", "inf", "Mon, 99 Foo"])
    def test_unreadable_values_fall_back(self, value: str | None) -> None:
        assert retry_after_seconds(value, max_s=60.0, now=NOW) is None


def test_backoff_grows_and_is_capped() -> None:
    waits = [
        backoff_seconds(attempt, base_s=1.5, multiplier=2.0, max_s=5.0)
        for attempt in (1, 2, 3)
    ]
    assert waits == [1.5, 3.0, 5.0]
    assert backoff_seconds(5000, base_s=1.0, multiplier=10.0, max_s=9.0) == 9.0


class TestStatusFailure:
    @pytest.mark.parametrize(
        ("status", "cause"),
        [
            (401, Cause.BLOCKED),
            (403, Cause.BLOCKED),
            (451, Cause.BLOCKED),
            (404, Cause.NOT_FOUND),
            (410, Cause.NOT_FOUND),
            (400, Cause.REJECTED),
            (422, Cause.REJECTED),
            (429, Cause.TRANSIENT),
            (500, Cause.TRANSIENT),
            (503, Cause.TRANSIENT),
            (522, Cause.TRANSIENT),
        ],
    )
    def test_causes(self, status: int, cause: Cause) -> None:
        failure = status_failure(status, {}, max_backoff_s=60.0, now=NOW)
        assert failure is not None
        assert (failure.cause, failure.status, failure.sent) == (cause, status, True)

    @pytest.mark.parametrize("status", [200, 204, 301, 304])
    def test_success(self, status: int) -> None:
        assert status_failure(status, {}, max_backoff_s=60.0, now=NOW) is None

    def test_retry_after_only_on_429_and_503(self) -> None:
        headers = {"Retry-After": "4"}
        for status in (429, 503):
            failure = status_failure(status, headers, max_backoff_s=60.0, now=NOW)
            assert failure is not None and failure.retry_after_s == 4.0
        failure = status_failure(500, headers, max_backoff_s=60.0, now=NOW)
        assert failure is not None and failure.retry_after_s is None


class TestChallenge:
    def test_cloudflare_header(self) -> None:
        assert is_challenge({"cf-mitigated": "challenge"}, b"")

    @pytest.mark.parametrize(
        "title",
        ["Verify you are human - CAPTCHA", "Just a moment...", "Attention Required!"],
    )
    def test_challenge_titles(self, title: str) -> None:
        body = f"<html><head><title>{title}</title></head></html>".encode()
        assert is_challenge({"Content-Type": "text/html"}, body)

    def test_a_page_loading_recaptcha_is_not_a_challenge(self) -> None:
        body = (
            b"<html><head><title>Search results</title>"
            b'<script src="https://www.google.com/recaptcha/api.js"></script>'
        )
        assert not is_challenge({"Content-Type": "text/html"}, body)

    def test_non_html_is_not_a_challenge(self) -> None:
        assert not is_challenge(
            {"Content-Type": "application/json"}, b"<title>captcha</title>"
        )


class TestConnectFailureFastFail:
    """A host that never accepts a connection must not burn the full budget."""

    def test_connect_timeout_is_connect_level(self) -> None:
        assert is_connect_failure(requests.exceptions.ConnectTimeout("timed out"))

    def test_refused_and_unreachable_are_connect_level(self) -> None:
        for message in (
            "Failed to establish a new connection: [Errno 111] Connection refused",
            "NewConnectionError: [WinError 10061] actively refused it",
            "[Errno 101] Network is unreachable",
            "ConnectTimeoutError(Connection to sru.gbv.de timed out.)",
        ):
            exc = requests.exceptions.ConnectionError(message)
            assert is_connect_failure(exc), message

    def test_midstream_errors_keep_the_full_budget(self) -> None:
        for exc in (
            requests.exceptions.ConnectionError(
                "Connection aborted, RemoteDisconnected"
            ),
            requests.exceptions.ConnectionError("Connection reset by peer"),
            requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)"),
            requests.exceptions.ChunkedEncodingError("incomplete chunked read"),
        ):
            assert not is_connect_failure(exc), exc
            assert connect_aware_attempt_cap(exc, 25) == 25

    def test_cap_shortens_only_connect_failures(self) -> None:
        dead = requests.exceptions.ConnectTimeout("timed out")
        assert connect_aware_attempt_cap(dead, 8) == CONNECT_FAILURE_MAX_ATTEMPTS
        assert connect_aware_attempt_cap(dead, 1) == 1


class TestClassifyException:
    def test_dns(self) -> None:
        failure = classify_exception(
            requests.exceptions.ConnectionError(
                "Max retries exceeded (Caused by NameResolutionError("
                "'Failed to resolve gone.example'))"
            )
        )
        assert (failure.cause, failure.sent) == (Cause.DNS, False)

    def test_ssl(self) -> None:
        failure = classify_exception(
            requests.exceptions.SSLError("certificate verify failed")
        )
        assert (failure.cause, failure.sent) == (Cause.SSL, False)

    def test_an_ssl_error_without_a_certificate_verdict_counts_as_sent(self) -> None:
        failure = classify_exception(
            requests.exceptions.SSLError("[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC]")
        )
        assert (failure.cause, failure.sent) == (Cause.SSL, True)

    def test_errors_after_the_response_started_count_as_sent(self) -> None:
        for exc in (
            requests.exceptions.SSLError("certificate verify failed"),
            requests.exceptions.ConnectionError("Connection refused"),
            requests.exceptions.ChunkedEncodingError("IncompleteRead"),
        ):
            failure = classify_exception(exc, after_response=True)
            assert (failure.sent, failure.connect) == (True, False), exc

    def test_the_detail_is_redacted(self) -> None:
        failure = classify_exception(
            requests.exceptions.ConnectionError("Max retries with url: /m?wskey=SECRET")
        )
        assert "SECRET" not in failure.detail

    def test_connect_timeout_was_not_sent(self) -> None:
        failure = classify_exception(requests.exceptions.ConnectTimeout("timed out"))
        assert (failure.cause, failure.connect, failure.sent) == (
            Cause.TRANSIENT,
            True,
            False,
        )

    def test_read_timeout_was_sent(self) -> None:
        failure = classify_exception(requests.exceptions.ReadTimeout("read timed out"))
        assert (failure.cause, failure.connect, failure.sent) == (
            Cause.TRANSIENT,
            False,
            True,
        )

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.MissingSchema("no schema"),
            requests.exceptions.InvalidSchema("no adapter"),
            requests.exceptions.InvalidURL("invalid label"),
            requests.exceptions.URLRequired("no url"),
        ],
    )
    def test_invalid_url(self, exc: requests.exceptions.RequestException) -> None:
        assert classify_exception(exc).cause is Cause.INVALID_URL


class TestCredentialDetection:
    """Name-based detection guarding the insecure-retry downgrade."""

    def test_query_string_key_is_a_credential(self) -> None:
        assert request_carries_credential("https://x.example/api?md5=abc&key=s3cr3t")

    def test_separate_params_are_inspected(self) -> None:
        assert request_carries_credential(
            "https://x.example/api", {"md5": "abc", "api_key": "s3cr3t"}
        )

    def test_vendor_variants_are_caught(self) -> None:
        for name in ("access_token", "clientSecret", "sig", "url-signature", "pwd"):
            assert request_carries_credential("https://x.example/a", {name: "v"}), name

    def test_europeana_wskey_is_a_credential(self) -> None:
        assert request_carries_credential("https://x.example/m?wskey=s3cr3t")
        assert request_carries_credential("https://x.example/m", {"wskey": "s3cr3t"})

    def test_europeana_guid_campaign_is_redacted(self) -> None:
        """Europeana echoes the key in each guid's utm_campaign."""
        guid = "https://www.europeana.eu/item/1/x?utm_source=api&utm_campaign=s3cr3t"
        assert "s3cr3t" not in redact_text(f"stored {guid}")

    def test_ddb_oauth_consumer_key_is_a_credential(self) -> None:
        assert request_carries_credential(
            "https://x.example/a", {"oauth_consumer_key": "s3cr3t"}
        )

    def test_authorization_and_api_key_headers_are_credentials(self) -> None:
        assert request_carries_credential(
            "https://x.example/a", None, {"Authorization": "Bearer t"}
        )
        assert request_carries_credential(
            "https://x.example/a", None, {"X-Api-Key": "t"}
        )

    def test_plain_request_carries_no_credential(self) -> None:
        assert not request_carries_credential(
            "https://x.example/api?q=cookbook&page=2",
            {"format": "json"},
            {"Accept": "application/json", "User-Agent": "chrono"},
        )

    def test_empty_values_do_not_count(self) -> None:
        assert not request_carries_credential(
            "https://x.example/api", {"key": ""}, {"Authorization": ""}
        )


class TestRedactUrl:
    def test_wskey_value_is_masked(self) -> None:
        redacted = redact_url(
            "https://iiif.europeana.eu/presentation/1/manifest?wskey=SECRET&fmt=3"
        )
        assert "SECRET" not in redacted
        assert "fmt=3" in redacted

    def test_plain_url_is_returned_unchanged(self) -> None:
        url = "https://x.example/api?q=cookbook&page=2"
        assert redact_url(url) == url

    def test_url_without_query_is_returned_unchanged(self) -> None:
        url = "https://x.example/iiif/manifest.json"
        assert redact_url(url) == url

    def test_malformed_url_is_masked_as_text(self) -> None:
        assert (
            redact_url("http://[::1/broken?key=x&q=1")
            == "http://[::1/broken?key=***&q=1"
        )

    def test_user_information_is_masked(self) -> None:
        redacted = redact_url("https://user:hunter2@x.example/a?q=1")
        assert redacted == "https://***@x.example/a?q=1"


class TestRedactText:
    def test_exception_text_loses_query_secrets_and_userinfo(self) -> None:
        text = (
            "HTTPSConnectionPool(host='api.europeana.eu', port=443): Max retries "
            "exceeded with url: /record?wskey=SECRET&rows=5 (Caused by x) "
            "https://bob:pw@x.example/?token=T0K"
        )
        redacted = redact_text(text)
        for secret in ("SECRET", "pw", "T0K"):
            assert secret not in redacted
        assert "rows=5" in redacted

    def test_plain_text_is_unchanged(self) -> None:
        text = "Read timed out. (read timeout=30) /search?q=cookbook&page=2"
        assert redact_text(text) == text
