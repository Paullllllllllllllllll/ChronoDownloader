"""Credential detection for the insecure-retry guard and log redaction.

Copied from ChronoDownloader v1.30.3 ``api/core/network.py`` (lines 267-398)
on 19.10.2026. Changed: typed mappings, and :func:`redact_url` catches only
the ``ValueError`` a malformed URL raises.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

__all__ = [
    "credential_values",
    "redact_text",
    "redact_url",
    "request_carries_credential",
]

# Names are normalized to lowercase alphanumerics, so "API-Key", "api_key" and
# "apiKey" all collapse to "apikey". Exact names cover the common cases; the
# suffixes catch vendor variants ("access_key", "private_token",
# "client_secret", "url_signature").
_CREDENTIAL_PARAM_NAMES = frozenset(
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
        # Europeana authenticates every API and manifest request with "wskey";
        # it matches no generic suffix, so it is named outright.
        "wskey",
        # Europeana's Search API echoes the caller's key in each record's
        # guid as "utm_campaign".
        "utmcampaign",
    }
)

_CREDENTIAL_NAME_SUFFIXES = (
    "apikey",
    "token",
    "secret",
    "password",
    "signature",
    "credential",
    # DDB's "oauth_consumer_key" normalizes to "oauthconsumerkey".
    "consumerkey",
)

_CREDENTIAL_HEADER_NAMES = frozenset(
    {"authorization", "proxyauthorization", "cookie", "wwwauthenticate"}
)


def _normalized(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _is_credential_name(name: str) -> bool:
    """Report whether a parameter or header name looks like a secret."""
    normalized = _normalized(name)
    if normalized in _CREDENTIAL_PARAM_NAMES:
        return True
    return any(normalized.endswith(suffix) for suffix in _CREDENTIAL_NAME_SUFFIXES)


def request_carries_credential(
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, Any] | None = None,
) -> bool:
    """Report whether an outgoing request carries a secret.

    ``retry_insecure_once`` resends a request over an unverified connection;
    a request that carries an API key, bearer token or session cookie would
    hand it to whoever produced the bad certificate, so the caller fails
    instead. Detection is name-based and errs toward True: a false positive
    only costs the insecure retry.

    Args:
        url: Request URL, whose query string is inspected.
        params: Query parameters passed separately to the HTTP layer.
        headers: Request headers as finally merged for the call.

    Returns:
        True when the URL carries user information, or any query parameter
        or header carries a credential value.
    """
    parsed = urlparse(url)
    try:
        if parsed.username or parsed.password:
            return True
    except ValueError:
        return True
    query_items = list(parse_qsl(parsed.query, keep_blank_values=False))
    if params:
        query_items.extend(
            (str(name), str(value))
            for name, value in params.items()
            if value not in (None, "")
        )
    if any(_is_credential_name(name) and value for name, value in query_items):
        return True
    for name, value in (headers or {}).items():
        if not value:
            continue
        if _normalized(str(name)) in _CREDENTIAL_HEADER_NAMES or _is_credential_name(
            str(name)
        ):
            return True
    return False


def redact_url(url: str) -> str:
    """Return *url* with user information and credential query values masked.

    Providers such as Europeana put the API key into the URL itself, so log
    lines and exceptions carry this form. A URL without a credential comes
    back unchanged; one too malformed to parse is masked as text.
    """
    try:
        parsed = urlparse(url)
        items = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return redact_text(url)
    if "@" in parsed.netloc:
        parsed = parsed._replace(netloc="***@" + parsed.netloc.rpartition("@")[2])
    if any(_is_credential_name(name) for name, _ in items):
        masked = [
            (name, "***" if _is_credential_name(name) else value)
            for name, value in items
        ]
        parsed = parsed._replace(query=urlencode(masked))
    return urlunparse(parsed) if parsed != urlparse(url) else url


_QUERY_PAIR = re.compile(
    r"(?P<lead>[?&;])(?P<name>[^=&;?\s'\"#]+)=(?P<value>[^&;\s'\"#)]*)"
)
_USERINFO = re.compile(r"(?P<scheme>//)[^/@\s'\"]+@")


def redact_text(text: str) -> str:
    """Return *text* with credential query values and URL user information masked.

    For exception messages, which quote URLs or bare paths with their query.
    """

    def mask(match: re.Match[str]) -> str:
        if not _is_credential_name(match["name"]):
            return match[0]
        return f"{match['lead']}{match['name']}=***"

    return _USERINFO.sub(r"\g<scheme>***@", _QUERY_PAIR.sub(mask, text))


def credential_values(
    url: str, headers: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    """Return the credential values a request carries in its query and headers.

    For masking a response that may echo them (a bearer header yields the
    token without its scheme too), longest first.
    """
    try:
        items = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except ValueError:
        items = []
    values = [value for name, value in items if _is_credential_name(name)]
    for name, value in (headers or {}).items():
        if _is_credential_name(name) or _normalized(name) in _CREDENTIAL_HEADER_NAMES:
            text = str(value)
            values.append(text)
            values.append(text.partition(" ")[2])
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))
