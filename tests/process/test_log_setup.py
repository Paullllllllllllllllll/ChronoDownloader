"""Tests for process/log_setup.py.

Ported from the shared ``tests/common/test_log_setup.py`` (AutoExcerpter
v1.0.0); the import probe names this package's copied modules.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from chronodownloader.process.log_setup import configure_logging

_TEST_LOGGER = "log_setup_probe"


def _own_handlers(name: str = "") -> list[logging.Handler]:
    """Return the handlers that ``configure_logging`` installed on a logger."""
    return [
        handler
        for handler in logging.getLogger(name).handlers
        if getattr(handler, "_log_setup_handler", False)
    ]


def _remove_own_handlers(name: str) -> None:
    target = logging.getLogger(name)
    for handler in _own_handlers(name):
        target.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    probe = logging.getLogger(_TEST_LOGGER)
    levels = (root.level, probe.level)
    yield
    _remove_own_handlers("")
    _remove_own_handlers(_TEST_LOGGER)
    root.setLevel(levels[0])
    probe.setLevel(levels[1])


class TestConfigureLogging:
    def test_is_idempotent(self) -> None:
        configure_logging()
        configure_logging()
        handlers = _own_handlers()
        assert len(handlers) == 1
        assert handlers[0].level == logging.WARNING

    def test_writes_to_current_stderr_after_swap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure_logging(logger_name=_TEST_LOGGER)
        swapped = io.StringIO()
        monkeypatch.setattr(sys, "stderr", swapped)

        logging.getLogger(f"{_TEST_LOGGER}.pipeline").warning("after swap")

        assert swapped.getvalue() == "[WARNING] after swap\n"

    def test_info_hidden_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        configure_logging(logger_name=_TEST_LOGGER)
        swapped = io.StringIO()
        monkeypatch.setattr(sys, "stderr", swapped)

        log = logging.getLogger(f"{_TEST_LOGGER}.cli")
        log.info("quiet")
        log.error("loud")

        assert swapped.getvalue() == "[ERROR] loud\n"

    def test_handlers_go_on_the_top_level_logger(self) -> None:
        child = logging.getLogger(f"{_TEST_LOGGER}.rendering.docx")

        configure_logging(logger_name=f"{_TEST_LOGGER}.rendering")

        assert len(_own_handlers(_TEST_LOGGER)) == 1
        assert _own_handlers() == []
        assert child.handlers == []
        assert child.level == logging.NOTSET
        assert child.propagate is True

    def test_level_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        configure_logging(logger_name=_TEST_LOGGER, level=logging.INFO)
        swapped = io.StringIO()
        monkeypatch.setattr(sys, "stderr", swapped)
        logging.getLogger(_TEST_LOGGER).info("shown")
        assert swapped.getvalue() == "[INFO] shown\n"

    def test_file_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        log_file = tmp_path / "logs" / "run.log"

        configure_logging(logger_name=_TEST_LOGGER, log_file=log_file)
        logging.getLogger(f"{_TEST_LOGGER}.core").info("into the file")
        configure_logging(logger_name=_TEST_LOGGER)

        text = log_file.read_text(encoding="utf-8")
        assert "INFO" in text
        assert f"{_TEST_LOGGER}.core - into the file" in text
        assert len(_own_handlers(_TEST_LOGGER)) == 1

    def test_redact_rewrites_messages_and_tracebacks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        swapped = io.StringIO()
        monkeypatch.setattr(sys, "stderr", swapped)
        log_file = tmp_path / "run.log"
        configure_logging(
            logger_name=_TEST_LOGGER,
            log_file=log_file,
            redact=lambda text: text.replace("SECRET", "***"),
        )
        log = logging.getLogger(f"{_TEST_LOGGER}.core")

        log.warning("fetch %s failed", "https://x.org/a?token=SECRET")
        try:
            raise ValueError("bad https://x.org/b?token=SECRET")
        except ValueError:
            log.exception("crash")
        configure_logging(logger_name=_TEST_LOGGER)

        text = log_file.read_text(encoding="utf-8")
        assert "SECRET" not in text + swapped.getvalue()
        assert "token=***" in text
        assert "ValueError: bad https://x.org/b?token=***" in text

    def test_an_unwritable_log_file_falls_back_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        swapped = io.StringIO()
        monkeypatch.setattr(sys, "stderr", swapped)
        taken = tmp_path / "run.log"
        taken.mkdir()  # a folder where the file should be: it cannot be opened

        configure_logging(logger_name=_TEST_LOGGER, log_file=taken)
        logging.getLogger(f"{_TEST_LOGGER}.core").warning("still logged")

        assert len(_own_handlers(_TEST_LOGGER)) == 1
        assert "cannot be written" in swapped.getvalue()
        assert "[WARNING] still logged" in swapped.getvalue()


_IMPORT_PROBE = """
import logging
for name in (
    "options.options",
    "options.prompts",
    "options.settings_loader",
    "options.spec_view",
    "options.standard_options",
    "options.wizard",
    "process.contract",
    "process.heartbeat",
    "process.interrupt",
    "process.log_setup",
):
    __import__(f"chronodownloader.{name}")
loggers = [logging.getLogger()] + [
    entry
    for entry in logging.Logger.manager.loggerDict.values()
    if isinstance(entry, logging.Logger)
]
print(sum(len(entry.handlers) for entry in loggers))
"""


def test_importing_copied_modules_configures_nothing(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        check=True,
    )
    assert result.stdout.strip() == "0"
