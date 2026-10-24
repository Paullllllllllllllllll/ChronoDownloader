"""Test helpers: hermetic primitives and the scripted wizard prompter.

The hermetic primitives classify environment variables, network addresses
and subprocess launches, and record file-system writes outside allowed roots
through an audit hook. ``ScriptedPrompter`` replays wizard answers and
records a transcript. The module does not import pytest.

Copied from ChronoTranscriber v1.0.0 ``common/testing.py`` on 19.10.2026,
trimmed: removed the scripted chat model (``ScriptedChatModel``,
``ScriptedStructured``, ``FakeRequest``, ``StructuredCall``,
``STRUCTURED_KWARG`` and the ``langchain_core`` imports).
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal

from chronodownloader.options.wizard import (
    BACK,
    BACK_TEXT,
    BACK_TITLE,
    NO_DEFAULT,
    Choice,
    Nav,
    Validator,
    choice_label,
    default_index,
)

HOME_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
XDG_VARS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


def is_secret_env_var(name: str, prefixes: Sequence[str] = ()) -> bool:
    """Return True for an ``*API_KEY*`` variable or one starting with *prefixes*."""
    upper = name.upper()
    return "API_KEY" in upper or upper.startswith(tuple(p.upper() for p in prefixes))


def home_env(home: Path) -> dict[str, str]:
    """Return the environment that makes *home* the user's home directory."""
    drive, rest = os.path.splitdrive(str(home))
    env = dict.fromkeys(HOME_VARS, str(home))
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = rest
    return env


def is_loopback_address(address: Any) -> bool:
    """Return True when *address* is local (loopback IP or a non-IP family)."""
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


@dataclass(frozen=True)
class LaunchEntry:
    """The names under which a tool can be started.

    ``programs`` are executable-name prefixes, ``scripts`` script paths with
    forward slashes (a part containing one, or ending in its file name,
    matches), ``modules`` the module prefixes accepted after ``-m``.
    """

    programs: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()


def _as_text(value: Any) -> str:
    try:
        return os.fsdecode(value)
    except TypeError:
        return str(value)


def launches_entry(args: Any, executable: Any, entry: LaunchEntry) -> bool:
    """Return True when a Popen argv would start *entry*."""
    if isinstance(args, str | bytes):
        parts = _as_text(args).split()
    elif isinstance(args, os.PathLike):
        parts = [_as_text(args)]
    else:
        parts = [_as_text(arg) for arg in args]
    programs = [_as_text(p) for p in (executable, *parts[:1]) if p is not None]
    prefixes = tuple(name.lower() for name in entry.programs)
    if any(Path(p).name.lower().startswith(prefixes) for p in programs):
        return True
    scripts = [script.lower().replace("\\", "/") for script in entry.scripts]
    script_names = {Path(script).name for script in scripts}
    modules = tuple(module.lower() for module in entry.modules)
    module_flags = tuple(f"-m{module}" for module in modules)
    for index, part in enumerate(parts):
        lowered = part.lower()
        normalized = lowered.replace("\\", "/")
        if any(script in normalized for script in scripts):
            return True
        if Path(normalized).name in script_names:
            return True
        if lowered.startswith(module_flags):
            return True
        following = parts[index + 1].lower() if index + 1 < len(parts) else ""
        if part == "-m" and following.startswith(modules):
            return True
    return False


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_GUARDED_EVENTS = frozenset(
    {
        "open",
        "os.mkdir",
        "os.rename",
        "os.remove",
        "os.rmdir",
        "shutil.rmtree",
        "shutil.copyfile",
        "shutil.move",
    }
)
_REMOVAL_EVENTS = frozenset({"os.remove", "os.rmdir", "shutil.rmtree"})


def _normalize_path(raw: Any, dir_fd: Any = None) -> str | None:
    """Return an absolute, case-normalized path, or None when not checkable."""
    if isinstance(raw, int):
        return None
    text = os.fsdecode(raw)
    if text.lower() == os.devnull.lower():
        return None
    if not os.path.isabs(text):
        if dir_fd is not None:
            return None
        text = os.path.join(os.getcwd(), text)
    return os.path.normcase(os.path.normpath(text))


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class WriteGuard:
    """Record file-system writes outside the allowed roots.

    Register ``audit`` with ``sys.addaudithook``; the caller fails the test
    on the violations, since production code may swallow an ``OSError``
    raised from a write. C-level writes (PyMuPDF save, sqlite) are invisible
    to it.
    """

    def __init__(self) -> None:
        self.active = False
        self.violations: list[str] = []
        self._roots: tuple[str, ...] = ()

    def start(self, allowed_roots: Iterable[Path]) -> None:
        """Begin recording writes that fall outside *allowed_roots*."""
        roots: set[str] = set()
        for root in allowed_roots:
            for variant in (Path(root), Path(root).resolve()):
                normalized = _normalize_path(variant)
                if normalized is not None:
                    roots.add(normalized)
        self._roots = tuple(roots)
        self.violations = []
        self.active = True

    def stop(self) -> list[str]:
        """Stop recording and return the unconsumed violations, deduplicated."""
        self.active = False
        found = list(dict.fromkeys(self.violations))
        self.violations = []
        return found

    def consume(self, path: str | os.PathLike[str] | None = None) -> list[str]:
        """Remove and return the violations at or under *path* (all if None)."""
        if path is None:
            taken, self.violations = self.violations, []
            return taken
        root = _normalize_path(path)
        if root is None:
            return []
        taken = [v for v in self.violations if _is_within(v, root)]
        self.violations = [v for v in self.violations if not _is_within(v, root)]
        return taken

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        """Audit hook: record guarded writes; never raises."""
        if not self.active or event not in _GUARDED_EVENTS:
            return
        with contextlib.suppress(Exception):
            for raw, dir_fd, must_exist in self._targets(event, args):
                path = _normalize_path(raw, dir_fd)
                if path is None or self._is_allowed(path):
                    continue
                if event == "os.mkdir" and os.path.isdir(path):
                    continue
                if must_exist and not os.path.lexists(path):
                    continue
                self.violations.append(path)

    @staticmethod
    def _targets(event: str, args: tuple[Any, ...]) -> list[tuple[Any, Any, bool]]:
        """Return (path, dir_fd, must_exist) triples that *event* modifies."""
        if event == "open":
            mode, flags = args[1], args[2]
            writes = isinstance(mode, str) and any(c in mode for c in "wax+")
            if isinstance(flags, int) and flags & _WRITE_FLAGS:
                writes = True
            return [(args[0], None, False)] if writes else []
        if event == "os.mkdir":
            return [(args[0], args[2], False)]
        if event in _REMOVAL_EVENTS:
            return [(args[0], args[1], True)]
        if event == "os.rename":
            return [(args[0], args[2], True), (args[1], args[3], False)]
        if event == "shutil.move":
            return [(args[0], None, True), (args[1], None, False)]
        return [(args[1], None, False)]

    def _is_allowed(self, path: str) -> bool:
        parts = path.replace("\\", "/").split("/")
        if "__pycache__" in parts or ".pytest_cache" in parts:
            return True
        return any(_is_within(path, root) for root in self._roots)


class _Keep(Enum):
    KEEP = "keep"


KEEP: Final = _Keep.KEEP
"""Scripted answer that accepts the preselected value or the default text."""


class ScriptError(AssertionError):
    """A scripted answer does not fit its question, or answers ran out."""


class ScriptedPrompter:
    """Replay wizard answers and record a plain-text transcript.

    An answer is a choice value, a list of values for a checkbox, text for
    text and path prompts, ``BACK`` (or ``"<"`` for text), or ``KEEP``.
    Each answer is checked against the offered choices and the validator.
    Use it as a context manager, or call :meth:`finish`, to fail on
    unused answers.
    """

    def __init__(self, answers: Iterable[Any]) -> None:
        self._answers = list(answers)
        self._used = 0
        self.lines: list[str] = []

    @property
    def transcript(self) -> str:
        """The recorded screens, questions and answers."""
        return "\n".join(self.lines) + "\n"

    @property
    def remaining(self) -> list[Any]:
        """The answers not used yet."""
        return self._answers[self._used :]

    def finish(self) -> None:
        """Raise ScriptError when answers remain unused."""
        if self.remaining:
            raise ScriptError(f"unused answers: {self.remaining!r}")

    def __enter__(self) -> ScriptedPrompter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.finish()

    def _take(self, message: str) -> Any:
        if self._used >= len(self._answers):
            raise ScriptError(f"no answer left for {message!r}")
        answer = self._answers[self._used]
        self._used += 1
        return answer

    def show(self, text: str) -> None:
        """Record ``text``."""
        self.lines.extend(text.split("\n"))

    def select[T](
        self,
        message: str,
        choices: Sequence[Choice[T]],
        *,
        default: Any = NO_DEFAULT,
        marker: str | None = None,
    ) -> T | Literal[Nav.BACK]:
        """Return the scripted choice."""
        selected = default_index(choices, default)
        pointed = 0 if selected is None else selected
        self.lines.append(f"? {message}")
        for index, choice in enumerate(choices):
            pointer = " > " if index == pointed else "   "
            label = choice_label(choice, marker if index == selected else None)
            self.lines.append(pointer + label)
        self.lines.append(("   " if choices else " > ") + BACK_TITLE)
        answer: Any = self._take(message)
        if answer is KEEP:
            if not choices:
                raise ScriptError(f"{message!r}: nothing preselected to keep")
            return self._chosen(choices[pointed])
        if answer is BACK:
            self.lines.append(f"  answer: {BACK_TITLE}")
            return Nav.BACK
        for choice in choices:
            if choice.value == answer:
                return self._chosen(choice)
        offered = [choice.value for choice in choices]
        raise ScriptError(f"{message!r}: {answer!r} is not one of {offered!r}")

    def _chosen[T](self, choice: Choice[T]) -> T:
        self.lines.append(f"  answer: {choice.title}")
        return choice.value

    def checkbox[T](
        self,
        message: str,
        choices: Sequence[Choice[T]],
        *,
        checked: Collection[T] = (),
        marker: str | None = None,
    ) -> list[T] | Literal[Nav.BACK]:
        """Return the scripted values in choice order."""
        self.lines.append(f"? {message}")
        for choice in choices:
            on = choice.value in checked
            box = "[x]" if on else "[ ]"
            label = choice_label(choice, marker if on else None)
            self.lines.append(f"   {box} {label}")
        self.lines.append(f"   [ ] {BACK_TITLE}")
        answer = self._take(message)
        if answer is BACK:
            self.lines.append(f"  answer: {BACK_TITLE}")
            return Nav.BACK
        if answer is KEEP:
            answer = [choice.value for choice in choices if choice.value in checked]
        if isinstance(answer, str) or not isinstance(answer, Iterable):
            raise ScriptError(f"{message!r}: expected a list of values, got {answer!r}")
        wanted = list(answer)
        offered = [choice.value for choice in choices]
        for value in wanted:
            if value not in offered:
                raise ScriptError(f"{message!r}: {value!r} is not one of {offered!r}")
        picked = [choice for choice in choices if choice.value in wanted]
        titles = ", ".join(choice.title for choice in picked) or "(none)"
        self.lines.append(f"  answer: {titles}")
        return [choice.value for choice in picked]

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        marker: str | None = None,
    ) -> str | Literal[Nav.BACK]:
        """Return the scripted text."""
        return self._typed(message, default, validate, marker)

    def path(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        only_directories: bool = False,
        marker: str | None = None,
    ) -> str | Literal[Nav.BACK]:
        """Return the scripted path text."""
        return self._typed(message, default, validate, marker)

    def _typed(
        self,
        message: str,
        default: str,
        validate: Validator | None,
        marker: str | None,
    ) -> str | Literal[Nav.BACK]:
        shown = f"? {message}"
        if default:
            shown += f" [{default} {marker}]" if marker else f" [{default}]"
        answer = self._take(message)
        if answer is KEEP:
            answer = default
        if answer is BACK or (isinstance(answer, str) and answer.strip() == BACK_TEXT):
            self.lines.append(f"{shown} {BACK_TEXT}")
            return BACK
        if not isinstance(answer, str):
            raise ScriptError(f"{message!r}: expected text, got {answer!r}")
        error = validate(answer) if validate is not None else None
        if error is not None:
            raise ScriptError(f"{message!r}: {answer!r} rejected: {error}")
        self.lines.append(f"{shown} {answer}".rstrip())
        return answer
