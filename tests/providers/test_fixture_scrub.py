"""Every recorded provider response and eval record is scrubbed of keys and cookies.

Each file under ``tests/fixtures/`` is checked for a ``Set-Cookie`` header,
credential-named query parameters and fields (``key``, ``wskey``, ``api_key``,
``oauth_consumer_key``, ``token``, Europeana's ``utm_campaign``, which echoes
the caller's key, ...) whose value is not the
placeholder ``REDACTED``, Authorization header values, the shapes of common
API keys, and the values of the key variables set in this environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from chronodownloader.settings import DEFAULT_API_KEYS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FILES = sorted(path for path in FIXTURES.rglob("*") if path.is_file())

PLACEHOLDERS = frozenset({"", "REDACTED", "***", "%2A%2A%2A"})
"""Values a scrubbed credential may carry."""

_CREDENTIAL_NAMES = frozenset(
    {
        "key",
        "apikey",
        "token",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "idtoken",
        "secret",
        "clientsecret",
        "password",
        "passwd",
        "pwd",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "sig",
        "signature",
        "sessionid",
        "wskey",
        "utmcampaign",
    }
)
_CREDENTIAL_SUFFIXES = (
    "apikey",
    "token",
    "secret",
    "password",
    "signature",
    "credential",
    "consumerkey",
)
_FIELD_EXEMPT = frozenset({"key"})
"""Field names too generic to flag outside a query string (``"key": ...``
labels metadata entries)."""

_QUERY_PAIR = re.compile(r"[?&;](?P<name>[A-Za-z0-9_.\-]+)=(?P<value>[^&;\s\"'<>#\\]*)")
_FIELD = re.compile(
    r"[\"']?(?P<name>[A-Za-z0-9_.\-]+)[\"']?\s*[:=]\s*[\"'](?P<value>[^\"']*)[\"']"
)
_AUTHORIZATION = re.compile(
    r"authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token)?\s*"
    r"(?P<value>[A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_KEY_SHAPES = (
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"sk-or-v1-[0-9a-f]{32,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
)
_MIN_SECRET_LENGTH = 6


def _is_credential_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized in _CREDENTIAL_NAMES or normalized.endswith(_CREDENTIAL_SUFFIXES)


def _text(path: Path) -> str:
    """Return the file as text with JSON and HTML escapes of URLs undone."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    for escaped, plain in (("\\/", "/"), ("\\u0026", "&"), ("&amp;", "&")):
        text = text.replace(escaped, plain)
    return text


def scrub_findings(text: str, secrets: frozenset[str] = frozenset()) -> list[str]:
    """Return what in *text* looks like a credential or a cookie."""
    findings: list[str] = []
    if "set-cookie" in text.lower():
        findings.append("Set-Cookie")
    for match in _QUERY_PAIR.finditer(text):
        name, value = match["name"], match["value"]
        if _is_credential_name(name) and value not in PLACEHOLDERS:
            findings.append(f"query parameter {name}={value[:4]}...")
    for match in _FIELD.finditer(text):
        name, value = match["name"], match["value"]
        if (
            name.lower() not in _FIELD_EXEMPT
            and _is_credential_name(name)
            and value not in PLACEHOLDERS
        ):
            findings.append(f"field {name}: {value[:4]}...")
    for match in _AUTHORIZATION.finditer(text):
        if match["value"] not in PLACEHOLDERS:
            findings.append("Authorization value")
    for shape in _KEY_SHAPES:
        if shape.search(text):
            findings.append(f"key-shaped value ({shape.pattern[:8]}...)")
    findings.extend(
        "the value of a key variable" for secret in secrets if secret in text
    )
    return findings


def _environment_secrets() -> frozenset[str]:
    values = (os.environ.get(name, "") for name in DEFAULT_API_KEYS.values())
    return frozenset(v for v in values if len(v) >= _MIN_SECRET_LENGTH)


def test_there_are_fixtures_to_check() -> None:
    assert FILES


@pytest.mark.parametrize(
    "path", FILES, ids=[path.relative_to(FIXTURES).as_posix() for path in FILES]
)
def test_the_fixture_carries_no_key_or_cookie(path: Path) -> None:
    assert scrub_findings(_text(path), _environment_secrets()) == []


@pytest.mark.parametrize(
    ("text", "found"),
    [
        pytest.param("Set-Cookie: sid=1", True, id="set-cookie"),
        pytest.param("https://x/api?md5=1&key=abc123", True, id="query-key"),
        pytest.param("https://x/api?wskey=REDACTED&q=1", False, id="query-redacted"),
        pytest.param(
            "https://x/?oauth_consumer_key=abcdef", True, id="query-consumer-key"
        ),
        pytest.param('{"api_key": "abcdef"}', True, id="field-api-key"),
        pytest.param('{"apiKey": "REDACTED"}', False, id="field-redacted"),
        pytest.param('{"key": "dc.title"}', False, id="field-generic-key"),
        pytest.param("Authorization: Bearer abcdefgh1234", True, id="authorization"),
        pytest.param("AIza" + "A" * 35, True, id="google-key-shape"),
        pytest.param('{"author": "La Varenne"}', False, id="author"),
        pytest.param("https://x/?monkey_business=1", False, id="unrelated-name"),
        pytest.param("https://x/?utm_campaign=abcdef", True, id="europeana-guid"),
    ],
)
def test_the_scrub_check_flags_credentials(text: str, found: bool) -> None:
    assert bool(scrub_findings(text)) is found


def test_the_scrub_check_flags_the_value_of_a_key_variable() -> None:
    assert scrub_findings(
        "id=1 member-secret-4f2a9c", frozenset({"member-secret-4f2a9c"})
    )
