"""Scripted wizard sessions: a workspace, the player and its record.

:func:`play` runs the wizard of a command with a
:class:`~tests.support.testing.ScriptedPrompter` and records, per step, the
options whose answers the step changed and those it set to a value other
than the code default. The workspace holds a works CSV, a settings file
like the run-core tests' (state folder under the test's directory, the fake
providers as ``defaults.providers``) and an output root.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, Final

from rich.console import Console

from chronodownloader.options.options import OptionTable
from chronodownloader.options.wizard import Nav, Outcome, State, Step, StepContext
from chronodownloader.settings import load
from chronodownloader.spec import DOWNLOAD_OPTIONS
from chronodownloader.ui.app import FLOWS, wizard_for
from chronodownloader.ui.session import Flow, Session, new_state
from tests.run.support import PROVIDERS, settings_file, write_csv
from tests.support.testing import KEEP, ScriptedPrompter, ScriptError

WIDTH: Final = 100
_MISSING: Final = object()

WORKS: Final = (
    {"entry_id": "E1", "title": "Le cuisinier francois"},
    {"entry_id": "E2", "title": "Ein new Kochbuch"},
)


class Workspace:
    """The works CSV, settings file and output root of one test."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.csv = write_csv(tmp_path / "works.csv", WORKS)
        self.out = tmp_path / "out root"
        self._settings = settings_file(tmp_path)

    def settings_path(self, defaults: Mapping[str, Any] | None = None) -> Path:
        """Return a settings file, with *defaults* added to its block."""
        if not defaults:
            return self._settings
        lines = [f"  {name}: {_yaml(value)}" for name, value in defaults.items()]
        path = self.tmp / "settings_defaults.yaml"
        text = self._settings.read_text(encoding="utf-8")
        path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
        return path

    def session(
        self, defaults: Mapping[str, Any] | None = None, *, named: bool = True
    ) -> Session:
        """Return a session over a settings file with *defaults*.

        Without *named* the session has the file's settings but no path, as
        the ui started without ``--settings`` has.
        """
        path = self.settings_path(defaults)
        settings = load(path, DOWNLOAD_OPTIONS)
        return Session(settings, path if named else None)


def _yaml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(str(item) for item in value) + "]"
    if isinstance(value, Path):
        return f"'{value.as_posix()}'"
    return str(value)


@dataclasses.dataclass
class Played:
    """A finished session: outcome, transcript and what each step set."""

    flow: Flow
    session: Session
    outcome: Outcome
    prompter: ScriptedPrompter
    changed: dict[str, set[str]]
    reached: dict[str, set[str]]

    @property
    def command(self) -> tuple[str, ...]:
        command = self.outcome.command
        assert command is not None, self.prompter.transcript
        return command

    def spec(self) -> Any:
        """Resolve the outcome's answers as the run does."""
        defaults = new_state(self.session, self.flow.table).defaults
        state = State(self.flow.table, defaults, self.outcome.answers)
        return self.flow.resolve(state, self.session).spec


def _recording(
    steps: Iterable[Step],
    table: OptionTable,
    changed: dict[str, set[str]],
    reached: dict[str, set[str]],
) -> list[Step]:
    """Wrap *steps* to record the answers each one changes and reaches."""

    def wrap(step: Step) -> Step:
        def ask(ctx: StepContext) -> Nav:
            before = ctx.state.answers
            result = step.ask(ctx)
            after = ctx.state.answers
            names = {
                name
                for name in before.keys() | after.keys()
                if before.get(name, _MISSING) != after.get(name, _MISSING)
            }
            changed.setdefault(step.name, set()).update(names)
            reached.setdefault(step.name, set()).update(
                name
                for name in names
                if name in after and after[name] != table[name].default
            )
            return result

        return dataclasses.replace(step, ask=ask)

    return [wrap(step) for step in steps]


def play(command: str, session: Session, answers: Sequence[Any]) -> Played:
    """Run the wizard of *command* with *answers* after the command step,
    which keeps *command* preselected."""
    flow = FLOWS[command]
    changed: dict[str, set[str]] = {}
    reached: dict[str, set[str]] = {}
    recorded = dataclasses.replace(
        flow, steps=lambda s: _recording(flow.steps(s), flow.table, changed, reached)
    )
    prompter = ScriptedPrompter([KEEP, *answers])
    wizard = wizard_for(recorded, session, prompter, WIDTH, False)
    try:
        outcome = wizard.run()
        prompter.finish()
    except ScriptError as exc:
        raise ScriptError(f"{exc}\n{prompter.transcript}") from exc
    return Played(flow, session, outcome, prompter, changed, reached)


def console(*, terminal: bool = False) -> tuple[Console, StringIO]:
    """Return a console of width :data:`WIDTH` without colors and its buffer;
    *terminal* makes it act as a terminal, which starts the live display."""
    buffer = StringIO()
    screen = Console(
        file=buffer, width=WIDTH, color_system=None, force_terminal=terminal
    )
    return screen, buffer


__all__ = [
    "PROVIDERS",
    "WIDTH",
    "WORKS",
    "Played",
    "Workspace",
    "console",
    "play",
]
