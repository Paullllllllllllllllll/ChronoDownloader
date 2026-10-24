"""Argument parsing: the top-level parser and one parser per command."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, NoReturn

from chronodownloader.options.options import HelpFormatter, OptionTable
from chronodownloader.spec import (
    DOWNLOAD_OPTIONS,
    DROP_OPTIONS,
    FETCH_OPTIONS,
    INSPECT_OPTIONS,
    PROVIDERS_OPTIONS,
    SEARCH_OPTIONS,
    STATUS_OPTIONS,
    UI_OPTIONS,
    VERIFY_OPTIONS,
)

__all__ = ["SUBCOMMANDS", "Parser", "UsageError", "build_parser"]

PROG = "chronodownloader"

DESCRIPTION = (
    "Discover and download digitized historical sources from 17 digital "
    "libraries: search the providers, judge the candidates and download the "
    "preferred copy into an output root with a SQLite store."
)

TOP_EPILOG = "Run 'chronodownloader <command> --help' for the options of a command."

PRECEDENCE = """\
Precedence: a flag wins over the settings defaults: block, which wins over the \
code default. --settings FILE reads another settings file instead of \
config/settings.yaml.

"""

INTERRUPTED = "  130  interrupted (Ctrl+C or Ctrl+Break)\n"

JSON_LINE = """
With --json, stdout carries exactly one JSON summary line on every exit; \
progress, warnings and errors go to stderr.
"""

OPEN_STATES = """\
  0    no work failed; works left needs_review, deferred or blocked are
       open, not failed, and counted in the summary; a dry run that planned
"""

DOWNLOAD_EPILOG = (
    PRECEDENCE
    + "Exit codes:\n"
    + OPEN_STATES
    + """\
  1    a work failed
  2    usage or configuration error: an invalid option or settings file, a
       missing works CSV, a store the tool refuses, or an unset judge key
       under --matching judge or hybrid (--matching fuzzy needs none)
"""
    + INTERRUPTED
    + JSON_LINE
    + """
Examples:
  Download the works of a CSV with the default judge:
    chronodownloader download --input works.csv --output out
  Without a judge key, from two providers, page images only:
    chronodownloader download --input works.csv --matching fuzzy
      --providers mdz,internet_archive --format images
  Plan a run without network requests and print the summary as JSON:
    chronodownloader download --input works.csv --dry-run --json
"""
)

FETCH_EPILOG = (
    PRECEDENCE
    + """\
Exit codes:
  0    the item was downloaded, or a dry run planned it
  1    the download failed
  2    usage or configuration error: an invalid option or settings file,
       neither --url nor --provider with --id, a needs_review work without
       --reason, or a store the tool refuses
"""
    + INTERRUPTED
    + JSON_LINE
    + """
Examples:
  Fetch a IIIF manifest for a work:
    chronodownloader fetch --entry-id E0001 --url https://example.org/manifest
  Close a needs_review work with a chosen candidate:
    chronodownloader fetch --entry-id E0001 --provider mdz --id bsb11111111
      --reason "same edition, title page checked"
"""
)

SEARCH_EPILOG = (
    PRECEDENCE
    + "Exit codes:\n"
    + OPEN_STATES
    + """\
  1    a work failed
  2    usage or configuration error: an invalid option or settings file,
       neither --input nor --title, or an unset judge key under --matching
       judge or hybrid (--matching fuzzy needs none)
"""
    + INTERRUPTED
    + JSON_LINE
    + """
Examples:
  Print the candidates of one title:
    chronodownloader search --title "Le cuisinier francois" --year 1651
  Store candidates and judgments for every work of a CSV:
    chronodownloader search --input works.csv --output out
"""
)

INSPECT_EPILOG = (
    PRECEDENCE
    + """\
Exit codes:
  0    every candidate was sampled and checked, or a dry run planned it
  1    a candidate could not be sampled or checked
  2    usage or configuration error: an invalid option or settings file,
       not exactly one of --entry-ids and --provider with --id, or an
       unset page-model key
"""
    + INTERRUPTED
    + JSON_LINE
)

VERIFY_EPILOG = (
    PRECEDENCE
    + """\
Exit codes:
  0    every checked work passed, or a dry run reported
  1    a work failed the check
  2    usage or configuration error: an invalid option or settings file, a
       store the tool refuses, or an unset page-model key with --pages
"""
    + INTERRUPTED
    + JSON_LINE
)

STATUS_EPILOG = (
    PRECEDENCE
    + """\
Exit codes:
  0    the listing was written
  2    usage or configuration error: an invalid option or settings file,
       or a store the tool refuses
"""
    + INTERRUPTED
    + JSON_LINE
)

DROP_EPILOG = (
    """\
Exit codes:
  0    every selected work was dropped, or in a dry run would be
  1    a work was refused: another run holds it
  2    usage error: no store in the output root, a store the tool refuses,
       or not exactly one of --entry-ids, --only and --all
"""
    + INTERRUPTED
    + JSON_LINE
    + """
Examples:
  List what dropping the failed works would delete:
    chronodownloader drop --output out --only failed --dry-run
"""
)

PROVIDERS_EPILOG = (
    """\
Exit codes:
  0    the listing was written
  2    usage or configuration error: an invalid option or settings file
"""
    + INTERRUPTED
    + JSON_LINE
)

UI_DESCRIPTION = (
    "Guided run: choose a command, answer its steps with Back navigation, "
    "then review the values with the equivalent command and run it, copy the "
    "command or quit. Needs an interactive terminal."
)

UI_EPILOG = (
    "Exit codes are those of the chosen command. For scripts and agents, use "
    "the commands with flags."
)


class UsageError(Exception):
    """The command line is invalid; *usage* is the parser's usage text."""

    def __init__(self, message: str, *, prog: str, usage: str) -> None:
        super().__init__(message)
        self.prog = prog
        self.usage = usage


class Parser(argparse.ArgumentParser):
    """An argument parser that raises :class:`UsageError` instead of exiting.

    Subcommand parsers created from it share the class, so every parse
    error reaches the caller, which can still write the JSON summary. No
    parser takes abbreviated flags, so the summary can read ``--json`` from
    the words of the command line before parsing.
    """

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, prog=self.prog, usage=self.format_usage())


# The action ``add_subparsers`` returns; its class is private to argparse.
Commands = Any


def _add_command(  # noqa: PLR0913 - one keyword per parser text
    commands: Commands,
    name: str,
    table: OptionTable,
    *,
    help: str,
    description: str,
    epilog: str,
    usage: str | None = None,
) -> argparse.ArgumentParser:
    """Add the subcommand *name* with the arguments of *table*."""
    parser: argparse.ArgumentParser = commands.add_parser(
        name,
        help=help,
        description=description,
        epilog=epilog,
        usage=usage,
        formatter_class=HelpFormatter,
        allow_abbrev=False,
    )
    table.add_arguments(parser)
    return parser


def add_download_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the download subcommand."""
    return _add_command(
        commands,
        "download",
        DOWNLOAD_OPTIONS,
        help="search, judge and download the works of a CSV",
        description=(
            "Search the providers for every work of a works CSV, judge the "
            "candidates and download the preferred copy into the output root. "
            "The store chronodownloader.sqlite in the output root records "
            "every work, so a rerun takes up where the last one stopped."
        ),
        epilog=DOWNLOAD_EPILOG,
        usage="%(prog)s --input FILE [options]",
    )


def add_fetch_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the fetch subcommand."""
    return _add_command(
        commands,
        "fetch",
        FETCH_OPTIONS,
        help="download a known item without search",
        description=(
            "Download one known item, named by its URL or by provider and "
            "identifier, into a work of the output root, without search or "
            "judgment. Fetch also closes a needs_review work; --reason is "
            "then required."
        ),
        epilog=FETCH_EPILOG,
        usage=(
            "%(prog)s --entry-id ID\n"
            "                              (--url URL | --provider KEY --id ID) "
            "[options]"
        ),
    )


def add_search_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the search subcommand."""
    return _add_command(
        commands,
        "search",
        SEARCH_OPTIONS,
        help="search and judge without downloading",
        description=(
            "Search the providers and judge the candidates without "
            "downloading. Without --output the results are printed as a "
            "table; with --output DIR the candidates, judgments and "
            "resulting statuses are written to that output root."
        ),
        epilog=SEARCH_EPILOG,
        usage="%(prog)s (--input FILE | --title TEXT) [options]",
    )


def add_inspect_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the inspect subcommand."""
    return _add_command(
        commands,
        "inspect",
        INSPECT_OPTIONS,
        help="sample the first pages of candidates and check them",
        description=(
            "Sample the first pages of a candidate at reduced size and check "
            "them with the page model: a title page among the first pages and "
            "the imprint year. fetch and drop remove the sampled pages of "
            "their works."
        ),
        epilog=INSPECT_EPILOG,
        usage=(
            "%(prog)s\n"
            "       (--entry-ids LIST --output DIR | --provider KEY --id ID) "
            "[options]"
        ),
    )


def add_verify_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the verify subcommand."""
    return _add_command(
        commands,
        "verify",
        VERIFY_OPTIONS,
        help="check the downloaded works of an output root",
        description=(
            "Check the files of the stored works against the store and "
            "update their statuses; with --pages also check the title page "
            "and the imprint year with the page model."
        ),
        epilog=VERIFY_EPILOG,
    )


def add_status_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the status subcommand."""
    return _add_command(
        commands,
        "status",
        STATUS_OPTIONS,
        help="list the works of an output root by status",
        description=(
            "List the works of an output root with their statuses, the review "
            "queue and leftover pages of inspect; with --input, the rows of "
            "the works CSV without a store entry count as new."
        ),
        epilog=STATUS_EPILOG,
    )


def add_drop_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the drop subcommand."""
    return _add_command(
        commands,
        "drop",
        DROP_OPTIONS,
        help="remove works from the store, to process them from scratch",
        description=(
            "Delete the selected works from the store of an output root and "
            "clear their pages sampled by inspect, so the next run processes "
            "them from scratch."
        ),
        epilog=DROP_EPILOG,
        usage=(
            "%(prog)s --output DIR\n"
            "                             (--entry-ids LIST | --only LIST | --all) "
            "[options]"
        ),
    )


def add_providers_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the providers subcommand."""
    return _add_command(
        commands,
        "providers",
        PROVIDERS_OPTIONS,
        help="list the providers with their capabilities and state",
        description=(
            "List the 17 provider keys with their capabilities, key status, "
            "circuit-breaker state and quota."
        ),
        epilog=PROVIDERS_EPILOG,
    )


def add_ui_command(commands: Commands) -> argparse.ArgumentParser:
    """Add the ui subcommand."""
    return _add_command(
        commands,
        "ui",
        UI_OPTIONS,
        help="guided run in an interactive terminal",
        description=UI_DESCRIPTION,
        epilog=UI_EPILOG,
    )


SUBCOMMANDS: tuple[Callable[[Commands], argparse.ArgumentParser], ...] = (
    add_download_command,
    add_fetch_command,
    add_search_command,
    add_inspect_command,
    add_verify_command,
    add_status_command,
    add_drop_command,
    add_providers_command,
    add_ui_command,
)


def build_parser() -> Parser:
    """Return the top-level parser with every subcommand."""
    parser = Parser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=TOP_EPILOG,
        formatter_class=HelpFormatter,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(
        dest="command", metavar="COMMAND", title="commands"
    )
    for add_command in SUBCOMMANDS:
        add_command(commands)
    return parser
