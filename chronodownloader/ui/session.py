"""What a guided session shares: settings, the command flows, the wizard.

Each command the guided run offers is a :class:`Flow`: its option table, its
steps and its resolver. :func:`build_wizard` puts the command step first, so
the user can switch commands, then the command's own steps. The wizard's
state resolves the answers like command-line flags over the settings
``defaults:`` block, so a settings default is the starting value of every
option it sets.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from chronodownloader.options.options import OptionTable
from chronodownloader.options.wizard import (
    BACK,
    DONE,
    Choice,
    Nav,
    Prompter,
    Review,
    State,
    Step,
    StepContext,
    Wizard,
)
from chronodownloader.settings import Settings
from chronodownloader.spec import Resolution, to_argv

__all__ = [
    "COMMAND_STEP",
    "PROGRAM",
    "TITLE",
    "CommandSwitch",
    "Flow",
    "Session",
    "absolute",
    "build_wizard",
    "command_step",
    "new_state",
]

TITLE: Final = "ChronoDownloader"
PROGRAM: Final = "chronodownloader"
COMMAND_STEP: Final = "command"

type Resolver = Callable[[Mapping[str, Any], Mapping[str, Any] | None], Resolution[Any]]


@dataclass(frozen=True)
class Session:
    """The settings of a guided session and the ``--settings`` file they came from.

    The file is None when the settings came from the default lookup.
    """

    settings: Settings
    settings_path: Path | None = None


def new_state(session: Session, table: OptionTable) -> State:
    """Return an empty answer state over *table* and its settings defaults."""
    defaults = {
        name: value
        for name, value in session.settings.defaults.items()
        if name in table and table[name].settable
    }
    return State(table, defaults)


_QUOTES = ('"', "'")


def absolute(path: Path | str) -> Path:
    """Make *path* absolute against the current directory.

    One matching pair of surrounding quotes is removed, as Explorer's "Copy
    as path" adds them, and ``~`` is expanded.
    """
    text = str(path).strip()
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] == text[0]:
        text = text[1:-1]
    return Path(os.path.abspath(os.path.expanduser(text)))


@dataclass(frozen=True)
class Flow:
    """One command of the guided run: its table, steps and resolver.

    ``steps`` returns the command's steps without the command step;
    ``resolver`` is the command's ``resolve_<command>`` from
    :mod:`chronodownloader.spec`.
    """

    name: str
    title: str
    table: OptionTable
    steps: Callable[[Session], Sequence[Step]]
    resolver: Resolver

    def resolve(self, state: State, session: Session) -> Resolution[Any]:
        """Resolve the answers in *state* as the command line would.

        Raises:
            ValueError: The answers do not form a valid spec.
        """
        return self.resolver(state.answers, session.settings.defaults)

    def command(self, state: State, session: Session) -> list[str]:
        """Return the words of the equivalent command line.

        Every value that differs from its code default or from the settings
        default is spelled out; ``--settings`` follows when the ui was
        started with one.

        Raises:
            ValueError: The answers do not resolve, or a settings default
                cannot be undone on the command line.
        """
        spec = self.resolve(state, session).spec
        words = [PROGRAM, self.name, *to_argv(spec, session.settings.defaults)]
        if session.settings_path is not None and "settings" in self.table:
            words += ["--settings", str(absolute(session.settings_path))]
        return words


class CommandSwitch(Exception):
    """The user chose another command on the command step."""

    def __init__(self, command: str) -> None:
        super().__init__(command)
        self.command = command


def command_step(current: str, titles: Mapping[str, str]) -> Step:
    """Return the command step: DONE for *current*, else a CommandSwitch.

    A CommandSwitch names another command of *titles* (command name to title,
    in menu order).
    """

    def ask(ctx: StepContext) -> Nav:
        answer = ctx.prompter.select(
            "Command:",
            [Choice(name, name, note=title) for name, title in titles.items()],
            default=current,
        )
        if answer is BACK:
            return BACK
        if answer != current:
            raise CommandSwitch(answer)
        return DONE

    return Step(COMMAND_STEP, "Command", ask=ask)


class _ChosenWizard(Wizard):
    """A wizard whose command step counts as answered on its first visit.

    It serves a command chosen on an earlier wizard's command step: the
    first pass skips the command step without showing it, and Back from
    the command's first step returns to it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._chosen = True

    def _ask(self, step: Step) -> Nav:
        if self._chosen and step.name == COMMAND_STEP:
            self._chosen = False
            self.visited.add(step.name)
            return DONE
        return super()._ask(step)


def build_wizard(
    flow: Flow,
    session: Session,
    prompter: Prompter,
    *,
    titles: Mapping[str, str],
    review: Callable[[State], Review],
    width: int = 80,
    chosen: bool = False,
) -> Wizard:
    """Return the wizard of *flow*: the command step, then the command's steps.

    *titles* lists the commands the command step offers; *chosen* means the
    user chose the command on an earlier wizard's command step, so the
    wizard starts at the command's first step.
    """
    steps = [command_step(flow.name, titles), *flow.steps(session)]
    wizard = _ChosenWizard if chosen else Wizard
    return wizard(
        new_state(session, flow.table),
        prompter,
        steps,
        title=TITLE,
        task=flow.title,
        review=review,
        command=lambda state: flow.command(state, session),
        width=width,
    )
