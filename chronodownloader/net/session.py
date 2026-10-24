"""The run's plain HTTP session.

:func:`build_session` returns a fresh ``requests.Session``; the run-owned
:class:`~chronodownloader.net.Network` builds one per run and closes it at the
end. No session lives at module level. The adapter makes no retries of its
own: every attempt goes through the network's retry loop, which reserves a
pacing slot for each one, so a hidden urllib3 retry would send an unpaced
request.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

__all__ = ["DEFAULT_HEADERS", "USER_AGENT", "build_session"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
"""A browser User-Agent; some providers answer the requests default with 403."""

DEFAULT_HEADERS = (
    ("User-Agent", USER_AGENT),
    ("Accept", "*/*"),
    ("Accept-Language", "en-US,en;q=0.9"),
)
"""Session headers; provider headers and then call headers override them."""


def build_session(pool_size: int = 16) -> requests.Session:
    """Return a session with the default headers and no adapter retries.

    The session ignores the environment (``trust_env`` off): no credentials
    from ``.netrc`` and no proxy settings are added to a request. *pool_size*
    bounds the pooled connections per host.
    """
    session = requests.Session()
    session.trust_env = False
    adapter = HTTPAdapter(
        max_retries=0, pool_connections=pool_size, pool_maxsize=pool_size
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(dict(DEFAULT_HEADERS))
    return session
