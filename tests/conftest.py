"""Suite-wide isolation: the log folder, the package logger's handlers, curl_cffi.

Every test logs into its own ``tmp_path / "logs"``, and the handlers a front
end installs are closed after the test, since an open log file would block
the removal of ``tmp_path`` on Windows and route later tests' records into
an earlier test's file. curl_cffi cannot be imported unless a test is marked
``real_curl``: Gallica and BNE impersonate a browser by default, and a real
curl session would bypass the fake sessions and reach the libraries.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from chronodownloader import settings as settings_module

_LOGGER_NAMES = ("chronodownloader", "")


@pytest.fixture(autouse=True)
def _no_real_curl(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make ``import curl_cffi`` fail, so every Network uses the plain session."""
    if request.node.get_closest_marker("real_curl") is None:
        # A None entry makes the import fail as if the package were missing.
        monkeypatch.setitem(sys.modules, "curl_cffi", None)


@pytest.fixture(autouse=True)
def _isolated_logging(tmp_path: Path) -> Iterator[None]:
    """Send the default log file into *tmp_path*; restore the loggers after.

    The folder is set by hand, not through ``monkeypatch``, so a test that
    calls ``monkeypatch.undo()`` keeps it.
    """
    default_logs_dir = settings_module.DEFAULT_LOGS_DIR
    settings_module.DEFAULT_LOGS_DIR = tmp_path / "logs"
    saved = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level)
        for name in _LOGGER_NAMES
    }
    yield
    settings_module.DEFAULT_LOGS_DIR = default_logs_dir
    for name, (handlers, level) in saved.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            # Pytest swaps its own capture handlers per test phase.
            if handler in handlers or type(handler).__module__.startswith("_pytest"):
                continue
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(level)
