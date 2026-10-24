"""The ui command: a guided run in an interactive terminal.

:func:`execute` is what ``chronodownloader ui`` dispatches to; :func:`run_app`
does the same with an injectable prompter, console, command stream,
clipboard runner and run wiring, for tests. The session starts with
``download``; choosing another command on the command step rebuilds the
wizard for it. After the review, Run resolves the answers, plans and runs
them through :func:`chronodownloader.run.plan` and
:func:`chronodownloader.run.run`, as the command line does, under the rich
progress display. The ui writes no JSON summary. Exit codes are those of the
chosen command: 0 when no work failed or stayed partial (and for a dry run
or a quit), 1 when one did, 2 for usage and configuration errors, 130 on
Ctrl+C at a prompt or during the run. Log records at WARNING and above go to
stderr (above the progress display); a run that is not a dry run also logs
INFO and above to the settings' log file.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TextIO

from rich.console import Console

from chronodownloader import run as core
from chronodownloader.net.credentials import redact_text
from chronodownloader.options.prompts import QuestionaryPrompter
from chronodownloader.options.wizard import (
    ClipboardRunner,
    Decision,
    Outcome,
    Prompter,
    State,
    Wizard,
    command_line,
    copy_to_clipboard,
)
from chronodownloader.process.contract import ExitCode, exit_code_for
from chronodownloader.process.log_setup import configure_logging
from chronodownloader.settings import SettingsError, load
from chronodownloader.spec import (
    DOWNLOAD_OPTIONS,
    FETCH_OPTIONS,
    SEARCH_OPTIONS,
    VERIFY_OPTIONS,
    resolve_download,
    resolve_fetch,
    resolve_search,
    resolve_verify,
)
from chronodownloader.ui.progress import RichProgress, plan_view, summary_view
from chronodownloader.ui.review import build_review
from chronodownloader.ui.session import (
    CommandSwitch,
    Flow,
    Session,
    build_wizard,
    new_state,
)
from chronodownloader.ui.steps import (
    download_steps,
    fetch_steps,
    search_steps,
    verify_steps,
)

if TYPE_CHECKING:
    from chronodownloader._runtime import Wiring

__all__ = [
    "FIRST",
    "FLOWS",
    "NOT_INTERACTIVE",
    "execute",
    "run_app",
    "run_flow",
    "wizard_for",
]

NOT_INTERACTIVE: Final = (
    "chronodownloader ui needs an interactive terminal (stdin and stdout). "
    "For scripts and agents, run the commands with flags; see "
    "'chronodownloader --help'."
)

FLOWS: Final[Mapping[str, Flow]] = {
    "download": Flow(
        "download",
        "search, judge and download the works of a CSV",
        DOWNLOAD_OPTIONS,
        download_steps,
        resolve_download,
    ),
    "fetch": Flow(
        "fetch",
        "download a known item without search",
        FETCH_OPTIONS,
        fetch_steps,
        resolve_fetch,
    ),
    "search": Flow(
        "search",
        "search and judge without downloading",
        SEARCH_OPTIONS,
        search_steps,
        resolve_search,
    ),
    "verify": Flow(
        "verify",
        "check the recorded files of an output root",
        VERIFY_OPTIONS,
        verify_steps,
        resolve_verify,
    ),
}
"""The commands the guided run offers, in menu order."""

FIRST: Final = "download"

LOGGER_NAME: Final = "chronodownloader"
"""The package's top-level logger (the ui does not import cli/)."""

_TITLES: Final = {name: flow.title for name, flow in FLOWS.items()}


def execute(args: argparse.Namespace, summary: object) -> int:
    """Run the guided UI for the parsed ``ui`` arguments; return the exit code.

    *summary* is the dispatcher's summary line, which the ui never writes.
    """
    settings_path: Path | None = getattr(args, "settings", None)
    return run_app(settings_path)


def _interactive() -> bool:
    streams = (sys.stdin, sys.stdout)
    return all(stream is not None and stream.isatty() for stream in streams)


def _say(console: Console, text: str, style: str | None = None) -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True, style=style)


def run_app(
    settings_path: Path | None = None,
    *,
    prompter: Prompter | None = None,
    console: Console | None = None,
    stdout: TextIO | None = None,
    clipboard: ClipboardRunner | None = None,
    wiring: Wiring | None = None,
    require_tty: bool = True,
) -> int:
    """Run the wizard, then the chosen command, and return the exit code.

    *console* shows messages, the run screen and the summary (default:
    stderr); *stdout* receives a copied command; *clipboard* runs the
    clipboard program; *wiring* replaces the run's providers, judge and
    session (tests). ``require_tty=False`` skips the terminal check.
    """
    if require_tty and not _interactive():
        sys.stderr.write(NOT_INTERACTIVE + "\n")
        return int(ExitCode.USAGE)
    screen = console if console is not None else Console(stderr=True)
    configure_logging(
        logger_name=LOGGER_NAME, level=logging.WARNING, redact=redact_text
    )
    try:
        try:
            settings = load(settings_path, DOWNLOAD_OPTIONS)
        except SettingsError as exc:
            _say(screen, f"Error: {exc}", "red")
            return int(ExitCode.USAGE)
        session = Session(settings, settings_path)
        flow, outcome = _wizard_outcome(
            session,
            prompter if prompter is not None else QuestionaryPrompter(),
            screen.width,
        )
        if outcome.action is Decision.QUIT:
            return int(ExitCode.OK)
        if outcome.action is Decision.COPY and outcome.command is not None:
            _copy(outcome.command, screen, stdout or sys.stdout, clipboard)
            return int(ExitCode.OK)
        return run_flow(flow, session, outcome.answers, screen, wiring=wiring)
    except KeyboardInterrupt:
        _say(screen, "Interrupted by the user.", "yellow")
        return int(ExitCode.INTERRUPTED)


def _wizard_outcome(
    session: Session, prompter: Prompter, width: int
) -> tuple[Flow, Outcome]:
    """Run the wizard, rebuilding it for each command the user switches to."""
    flow = FLOWS[FIRST]
    chosen = False
    while True:
        try:
            return flow, wizard_for(flow, session, prompter, width, chosen).run()
        except CommandSwitch as switch:
            flow = FLOWS[switch.command]
            chosen = True


def wizard_for(
    flow: Flow, session: Session, prompter: Prompter, width: int, chosen: bool
) -> Wizard:
    """Return the wizard of *flow* with the command step and the review."""
    return build_wizard(
        flow,
        session,
        prompter,
        titles=_TITLES,
        review=lambda state: build_review(flow, state, session),
        width=width,
        chosen=chosen,
    )


def _copy(
    words: tuple[str, ...],
    console: Console,
    stdout: TextIO,
    clipboard: ClipboardRunner | None,
) -> None:
    line = command_line(words)
    stdout.write(line + "\n")
    stdout.flush()
    if copy_to_clipboard(line, runner=clipboard):
        _say(console, "Copied the command to the clipboard.")
    else:
        _say(console, "Clipboard unavailable; copy the command above.")


def run_flow(
    flow: Flow,
    session: Session,
    answers: Mapping[str, Any],
    console: Console,
    *,
    wiring: Wiring | None = None,
) -> int:
    """Resolve *answers*, plan and run *flow*'s command; return the exit code.

    A dry run shows the plan; a run shows the progress display, then the
    summary. Ctrl+C propagates after the run released its leases.
    """
    state = State(flow.table, new_state(session, flow.table).defaults, answers)
    try:
        resolution = flow.resolve(state, session)
        run_plan = core.plan(
            resolution.spec, session.settings, sources=resolution.sources
        )
    except (ValueError, SettingsError) as exc:
        _say(console, f"Error: {exc}", "red")
        return int(ExitCode.USAGE)
    except core.PlanError as exc:
        _say(console, f"Error: {exc}", "red")
        return int(exc.exit_code)
    for warning in run_plan.warnings:
        _say(console, f"Warning: {warning}", "yellow")
    if state.value("dry_run"):
        console.print(plan_view(run_plan))
        return int(ExitCode.OK)
    configure_logging(
        logger_name=LOGGER_NAME,
        level=logging.WARNING,
        log_file=session.settings.log_file(),
        file_level=logging.INFO,
        redact=redact_text,
    )
    try:
        with RichProgress(console) as progress:
            result = core.run(run_plan, progress, wiring=wiring)
    except core.PlanError as exc:
        _say(console, f"Error: {exc}", "red")
        return int(exc.exit_code)
    console.print(summary_view(result, progress))
    counts = result.counts
    return int(exit_code_for(failed=counts.failed + counts.partial))
