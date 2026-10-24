"""Shared flow of the commands: resolve, plan, run, and the exit paths.

Exit codes: 0 when no work failed or stayed partial, 1 when one did or the
command crashed, 2 for usage and configuration errors, 130 on Ctrl+C or
Ctrl+Break. :func:`guarded` is the one boundary around parsing, handler
import and the command, so with ``--json`` exactly one JSON summary line
goes to stdout on every exit; everything else goes to stderr. The exception
is ``--help``: argparse prints the help on stdout and exits 0, with or
without ``--json``, and no summary is written. Log records at WARNING and
above go to stderr, at INFO and above to the settings' log file
(:func:`setup_logging`); a dry run writes no log file.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Mapping
from typing import Any

from chronodownloader import run as core
from chronodownloader.cli.console import print_error
from chronodownloader.cli.parser import UsageError
from chronodownloader.cli.reporter import StderrReporter
from chronodownloader.cli.summary import (
    Summary,
    count_fields,
    exit_code,
    report_fields,
    summary_fields,
)
from chronodownloader.net.credentials import redact_text
from chronodownloader.process.contract import ExitCode
from chronodownloader.process.log_setup import configure_logging
from chronodownloader.settings import Settings, SettingsError, load
from chronodownloader.spec import DOWNLOAD_OPTIONS, TABLES, Resolution, SpecError

__all__ = [
    "LOGGER_NAME",
    "execute_command",
    "guarded",
    "load_command_settings",
    "setup_logging",
]

logger = logging.getLogger(__name__)

LOGGER_NAME = "chronodownloader"
"""The package's top-level logger; every module logs below it."""

# Resolution is invariant in its spec, so each resolver's own spec type
# would not match Resolution[CommandSpec].
type Resolver = Callable[[Mapping[str, Any], Mapping[str, Any] | None], Resolution[Any]]


def guarded(summary: Summary, body: Callable[[], int]) -> int:
    """Run *body* and write the summary on every exit path.

    A parse error returns 2 after the usage text, a spec or settings error
    2, a :class:`~chronodownloader.run.PlanError` its own exit code, Ctrl+C
    130 and a crash 1. ``SystemExit`` (``--help``) passes through.
    """
    try:
        return body()
    except UsageError as exc:
        sys.stderr.write(exc.usage)
        print_error(f"Invalid arguments: {exc}")
        return summary.emit(ExitCode.USAGE)
    except (SpecError, SettingsError) as exc:
        print_error(f"Invalid arguments: {exc}")
        return summary.emit(ExitCode.USAGE)
    except core.PlanError as exc:
        print_error(str(exc))
        return summary.emit(exc.exit_code, items_total=exc.items_total)
    except KeyboardInterrupt:
        print_error("Interrupted by the user.")
        return summary.emit(ExitCode.INTERRUPTED)
    # The crash boundary of the process: whatever escapes the command is
    # logged with its traceback and still ends with the summary line and
    # exit code 1, which the agent contract requires on every exit path.
    except Exception as exc:
        logger.exception("Command failed: %s", exc)
        print_error(f"Unexpected error: {exc}")
        return summary.emit(ExitCode.FAILURE)


def setup_logging(settings: Settings | None = None) -> None:
    """Show the tool's warnings on stderr and log INFO to the settings' file.

    Without *settings* no file is written; a dry run passes none, so it
    writes nothing. Calling again replaces the handlers of the earlier call.
    """
    configure_logging(
        logger_name=LOGGER_NAME,
        level=logging.WARNING,
        log_file=settings.log_file() if settings is not None else None,
        file_level=logging.INFO,
        redact=redact_text,
    )


def load_command_settings(flags: Mapping[str, Any]) -> Settings:
    """Load the settings of a command once.

    The file is ``--settings``, else ``config/settings.yaml``, else the
    packaged example; ``defaults:`` is checked against the download table.
    A command without ``--settings`` (``drop``) reads the default file.

    Raises:
        SettingsError: The file is missing or invalid.
    """
    return load(flags.get("settings"), DOWNLOAD_OPTIONS)


def execute_command(
    args: argparse.Namespace,
    summary: Summary,
    command: str,
    resolve: Resolver,
    *,
    report: Callable[[core.RunResult, bool], None] | None = None,
    listing: bool = False,
) -> int:
    """Resolve, plan and run *command* from its parsed *args*.

    The plan's warnings go to stderr first. *report* gets the run's result
    and whether ``--json`` is on, before the summary is written; a dry run
    whose plan holds a report passes it the same way. A *listing* exits 0
    whatever its counts. Returns the exit code and writes *summary*; errors
    propagate to :func:`guarded`.
    """
    flags = TABLES[command].values(args)
    settings = load_command_settings(flags)
    # A dry run and the read-only listings (status, providers) write no log.
    if not flags.get("dry_run") and not listing:
        setup_logging(settings)
    resolution = resolve(flags, settings.defaults)
    run_plan = core.plan(resolution.spec, settings, sources=resolution.sources)
    reporter = StderrReporter()
    for warning in run_plan.warnings:
        reporter.warning(warning)
    json = bool(flags.get("json"))
    if flags.get("dry_run"):
        if report is not None and run_plan.report is not None:
            report(core.RunResult(run_plan.counts, report=run_plan.report), json)
        return summary.emit(
            ExitCode.OK,
            **summary_fields(run_plan.counts),
            **count_fields(command, run_plan.counts),
            **report_fields(run_plan.report),
        )
    result = core.run(run_plan, reporter)
    if report is not None:
        report(result, json)
    code = ExitCode.OK if listing else exit_code(result.counts)
    return summary.emit(
        code,
        **summary_fields(result.counts),
        **count_fields(command, result.counts),
        **report_fields(result.report),
    )
