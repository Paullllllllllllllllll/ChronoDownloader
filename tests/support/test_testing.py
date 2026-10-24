"""Tests for tests/support/testing.py: the hermetic primitives.

Ported from the shared ``tests/common/test_testing.py`` (AutoExcerpter
v1.0.0); the tests of the removed scripted chat model are left out.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.support.testing import (
    LaunchEntry,
    WriteGuard,
    home_env,
    is_loopback_address,
    is_secret_env_var,
    launches_entry,
)

ENTRY = LaunchEntry(
    programs=("mytool",), scripts=("app/run.py",), modules=("mytool", "app.run")
)


class TestEnvironment:
    def test_secret_variables(self) -> None:
        assert is_secret_env_var("OPENAI_API_KEY")
        assert not is_secret_env_var("SERVICE_TOKEN")
        assert is_secret_env_var("service_token", ("SERVICE_",))

    def test_home_env_points_every_variable_at_home(self, tmp_path: Path) -> None:
        env = home_env(tmp_path)

        assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
        assert env["HOMEDRIVE"] + env["HOMEPATH"] == str(tmp_path)

    def test_loopback_classification(self) -> None:
        assert is_loopback_address(("127.0.0.1", 80))
        assert is_loopback_address(("::ffff:127.0.0.1", 80, 0, 0))
        assert is_loopback_address("/run/socket")
        assert not is_loopback_address(("192.0.2.1", 443))


class TestLaunchEntry:
    def test_entry_names_match(self) -> None:
        assert launches_entry(["mytool.exe", "--cli"], None, ENTRY)
        assert launches_entry(["python", "C:\\src\\app\\run.py"], None, ENTRY)
        assert launches_entry("python -m app.run --cli", None, ENTRY)
        assert launches_entry(["python", "-mmytool"], None, ENTRY)
        assert launches_entry(["python"], "mytool", ENTRY)

    def test_other_programs_pass(self) -> None:
        assert not launches_entry(["python", "-m", "pip", "list"], None, ENTRY)
        assert not launches_entry(["git", "status"], None, LaunchEntry())


class TestWriteGuard:
    def test_records_writes_outside_the_roots(self, tmp_path: Path) -> None:
        guard = WriteGuard()
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside.txt"
        guard.start([allowed])

        guard.audit("open", (str(allowed / "a.txt"), "w", os.O_WRONLY))
        guard.audit("open", (str(outside), "r", os.O_RDONLY))
        guard.audit("open", (str(outside), "w", os.O_WRONLY))
        guard.audit("open", (str(outside), "a", os.O_APPEND))
        guard.audit("os.mkdir", (str(tmp_path / "__pycache__" / "x"), 0o777, None))

        assert guard.stop() == [os.path.normcase(str(outside))]
        guard.audit("open", (str(outside), "w", os.O_WRONLY))
        assert guard.violations == []

    def test_consume_takes_violations_under_a_path(self, tmp_path: Path) -> None:
        guard = WriteGuard()
        guard.start([tmp_path / "allowed"])
        guard.audit("open", (str(tmp_path / "a" / "x.txt"), "w", os.O_WRONLY))
        guard.audit("open", (str(tmp_path / "b.txt"), "w", os.O_WRONLY))

        taken = guard.consume(tmp_path / "a")

        assert taken == [os.path.normcase(str(tmp_path / "a" / "x.txt"))]
        assert guard.stop() == [os.path.normcase(str(tmp_path / "b.txt"))]
