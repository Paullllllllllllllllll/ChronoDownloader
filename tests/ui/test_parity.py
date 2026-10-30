"""Parity between the option tables, the CLI and the wizard.

Every option of a command's table must be an argument of its subcommand,
under the table's flag (and its ``--no-`` form for a switch). Every option of
the four guided commands must also be set by some scripted wizard session,
through a step that lists it, apart from the exemptions below. Each
session's printed command, parsed by the real parser and resolved with the
settings file it names, must resolve to the spec the session's answers
resolve to; it must also do so with an empty defaults block.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest

from chronodownloader.cli.parser import build_parser
from chronodownloader.options.options import Kind, Option, OptionTable
from chronodownloader.options.wizard import BACK, Decision, command_display
from chronodownloader.settings import load
from chronodownloader.spec import COMMANDS, DOWNLOAD_OPTIONS, TABLES
from chronodownloader.ui import review
from chronodownloader.ui.app import FLOWS
from chronodownloader.ui.session import Session, absolute
from tests.support.testing import KEEP
from tests.ui.support import WIDTH, Played, Workspace, play

EXEMPT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "json": "agent-contract output (one JSON line on stdout); no use in the UI",
        "settings": "given to the ui itself (ui --settings) and added to the command",
    }
)

WIZARD_COMMANDS: Final = ("download", "fetch", "search", "verify")
"""The commands the guided run offers."""

MANIFEST: Final = (
    "https://api.digitale-sammlungen.de/iiif/presentation/v2/bsb1/manifest"
)


def unreached(names: Collection[str], reached: Collection[str]) -> set[str]:
    """Return the options of *names* that no wizard answer reached."""
    return {name for name in names if name not in EXEMPT and name not in reached}


def _subparser(words: Sequence[str]) -> argparse.ArgumentParser:
    """Return the parser of the subcommand *words*."""
    parser: argparse.ArgumentParser = build_parser()
    for word in words:
        (commands,) = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        parser = commands.choices[word]
    return parser


def _arguments(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {
        action.dest: action
        for action in parser._actions
        if not isinstance(action, argparse._HelpAction | argparse._SubParsersAction)
    }


def test_every_command_has_a_table() -> None:
    assert list(TABLES) == list(COMMANDS)
    (commands,) = [
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert list(commands.choices) == list(COMMANDS)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_option_is_an_argument_of_the_subcommand(command: str) -> None:
    table = TABLES[command]
    arguments = _arguments(_subparser((command,)))
    assert set(arguments) == set(table.names)
    for option in table:
        strings = set(arguments[option.name].option_strings)
        expected = {option.flag}
        if option.kind is Kind.SWITCH:
            expected.add(option.negative_flag)
        assert strings == expected, option.name


@pytest.mark.parametrize("command", COMMANDS)
def test_a_shared_settable_option_converts_like_the_download_row(
    command: str,
) -> None:
    """A settings default checked by the download table fits every command."""
    for option in TABLES[command]:
        if option.settable:
            row = DOWNLOAD_OPTIONS[option.name]
            assert (option.kind, option.type, option.choices) == (
                row.kind,
                row.type,
                row.choices,
            ), f"{command}: {option.name}"


def test_an_option_missing_from_every_step_is_reported() -> None:
    names = set(DOWNLOAD_OPTIONS.names)
    extra = Option("extra", "--extra", "an option no step sets")
    table = OptionTable([*DOWNLOAD_OPTIONS, extra])
    assert unreached(names, names) == set()
    assert unreached(table.names, names) == {"extra"}
    assert unreached(names, names - {"format", "json"}) == {"format"}


def test_the_wizard_offers_the_four_commands_and_exempts_the_same_options() -> None:
    assert tuple(FLOWS) == WIZARD_COMMANDS
    assert dict(review.EXEMPT) == dict(EXEMPT)
    for name, flow in FLOWS.items():
        assert flow.table is TABLES[name]


# -- scripted sessions --------------------------------------------------------

LIMIT_ANSWERS: Final = [True, "1", "2", "0.5", "0.25", "0.5", "10", "skip"]
"""Own size limits with every value set."""

MATCHING_ANSWERS: Final = [
    "fuzzy",
    "same_edition",
    "internet_archive,mdz",
    "3",
    "30",
]


@dataclasses.dataclass(frozen=True)
class Script:
    """A wizard session: command, settings and the answers after the
    command step."""

    command: str
    session: Session
    answers: Sequence[Any]


def download_customized(ws: Workspace) -> Script:
    """A download with every option set."""
    answers = [
        str(ws.csv),
        "E1,E2",
        "1",
        ["new", "complete"],
        True,
        *MATCHING_ANSWERS,
        "both",
        "5",
        False,
        True,
        *LIMIT_ANSWERS,
        str(ws.out),
        "shadow",
        True,
        "gpt-5.5",
        "openai",
        "https://example.org/v1/decisions",
        "2",
        True,
        "copy",
    ]
    return Script("download", ws.session(), answers)


def download_over_defaults(ws: Workspace) -> Script:
    """A download that keeps settings defaults, which the command spells out."""
    defaults = {
        "matching": "fuzzy",
        "format": "images",
        "include_metadata": False,
        "output": ws.out,
        "decisions": "shadow",
        "concurrency": 4,
    }
    answers = [
        str(ws.csv),
        *[KEEP] * 9,
        *[KEEP] * 4,
        False,
        KEEP,
        *[KEEP] * 5,
        KEEP,
        KEEP,
        "copy",
    ]
    return Script("download", ws.session(defaults), answers)


def download_unnamed_settings(ws: Workspace) -> Script:
    """A download from a ui started without --settings, with Back once."""
    answers = [
        str(ws.csv),
        KEEP,
        KEEP,
        KEEP,
        BACK,
        KEEP,
        True,
        *[KEEP] * 5,
        *[KEEP] * 4,
        False,
        KEEP,
        KEEP,
        True,
        *[KEEP] * 3,
        KEEP,
        True,
        "copy",
    ]
    return Script("download", ws.session({"matching": "fuzzy"}, named=False), answers)


def fetch_by_provider(ws: Workspace) -> Script:
    """A fetch of a provider item with every option set."""
    answers = [
        "E9",
        "Le cuisinier",
        "provider",
        "mdz",
        "bsb1",
        "matches the title page",
        "images",
        "2",
        False,
        True,
        *LIMIT_ANSWERS,
        str(ws.out),
        True,
        "copy",
    ]
    return Script("fetch", ws.session(), answers)


def fetch_by_url(ws: Workspace) -> Script:
    """A fetch by manifest URL, after naming a provider item first."""
    answers = [
        "E1",
        KEEP,
        "provider",
        "mdz",
        "<",
        BACK,
        "url",
        MANIFEST,
        KEEP,
        *[KEEP] * 4,
        False,
        KEEP,
        KEEP,
        "copy",
    ]
    return Script("fetch", ws.session(), answers)


def search_csv(ws: Workspace) -> Script:
    """A search of the works CSV, stored into an output root."""
    answers = [
        "input",
        str(ws.csv),
        "E1",
        "1",
        ["failed"],
        True,
        *MATCHING_ANSWERS,
        True,
        str(ws.out),
        True,
        "copy",
    ]
    return Script("search", ws.session(), answers)


def search_adhoc(ws: Workspace) -> Script:
    """An ad hoc search, printed, after starting on the works CSV."""
    answers = [
        "input",
        str(ws.csv),
        "E1",
        "<",
        "<",
        "title",
        "Le cuisinier",
        "La Varenne",
        "1651",
        "lv1651",
        KEEP,
        KEEP,
        *[KEEP] * 5,
        KEEP,
        KEEP,
        "copy",
    ]
    return Script("search", ws.session(), answers)


def verify_pages(ws: Workspace) -> Script:
    """A verify with the page check."""
    answers = [
        str(ws.out),
        "E1",
        True,
        "3",
        "gpt-5.5",
        "openai",
        "https://example.org/v1/decisions",
        True,
        "copy",
    ]
    return Script("verify", ws.session(), answers)


def verify_pages_off(ws: Workspace) -> Script:
    """A verify whose page check is switched off again from the review."""
    answers = [
        KEEP,
        KEEP,
        True,
        "4",
        "gpt-5.5",
        KEEP,
        KEEP,
        KEEP,
        "edit",
        "pages",
        False,
        "copy",
    ]
    return Script("verify", ws.session(), answers)


SESSIONS: Final[Mapping[str, Callable[[Workspace], Script]]] = MappingProxyType(
    {
        "download_customized": download_customized,
        "download_over_defaults": download_over_defaults,
        "download_unnamed_settings": download_unnamed_settings,
        "fetch_by_provider": fetch_by_provider,
        "fetch_by_url": fetch_by_url,
        "search_csv": search_csv,
        "search_adhoc": search_adhoc,
        "verify_pages": verify_pages,
        "verify_pages_off": verify_pages_off,
    }
)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def play_script(script: Script) -> Played:
    return play(script.command, script.session, script.answers)


@pytest.mark.parametrize("command", WIZARD_COMMANDS)
def test_the_sessions_reach_every_option_from_their_steps(
    workspace: Workspace, command: str
) -> None:
    changed: dict[str, set[str]] = {}
    reached: dict[str, set[str]] = {}
    for build in SESSIONS.values():
        script = build(workspace)
        if script.command != command:
            continue
        played = play_script(script)
        for name, names in played.changed.items():
            changed.setdefault(name, set()).update(names)
        for name, names in played.reached.items():
            reached.setdefault(name, set()).update(names)
    flow = FLOWS[command]
    every = {name for names in reached.values() for name in names}
    assert unreached(flow.table.names, every) == set()
    for step in flow.steps(workspace.session()):
        assert changed.get(step.name, set()) <= set(step.sets), step.name
        assert set(step.sets) <= reached.get(step.name, set()), step.name


def command_spec(played: Played, *, empty: bool = False) -> Any:
    """Parse the printed command with the real parser and resolve it.

    The settings are those of the file the command names, else the
    session's; *empty* drops their defaults block.
    """
    flow, words = played.flow, played.command
    assert tuple(words[:2]) == ("chronodownloader", flow.name)
    args = build_parser().parse_args(list(words[1:]))
    flags = flow.table.values(args)
    path = flags.pop("settings", None)
    settings = played.session.settings
    if path is not None:
        settings = load(path, DOWNLOAD_OPTIONS)
    if empty:
        settings = dataclasses.replace(settings, defaults=MappingProxyType({}))
    return flow.resolver(flags, settings.defaults).spec


def check_command(played: Played) -> None:
    """The printed command yields the session's spec, with and without defaults."""
    spec = played.spec()
    words = played.command
    assert "--json" not in words
    path = played.session.settings_path
    if path is not None:
        assert words[-2:] == ("--settings", str(absolute(path)))
    else:
        assert "--settings" not in words
    assert command_spec(played) == spec, played.prompter.transcript
    assert command_spec(played, empty=True) == spec, played.prompter.transcript
    shown = command_display(list(words), width=WIDTH)
    lines = played.prompter.lines
    start = len(lines) - lines[::-1].index("Equivalent command:")
    assert lines[start : start + len(shown)] == shown


@pytest.mark.parametrize("name", list(SESSIONS))
def test_the_printed_command_yields_the_wizard_spec(
    workspace: Workspace, name: str
) -> None:
    played = play_script(SESSIONS[name](workspace))
    assert played.outcome.action is Decision.COPY
    check_command(played)


def _flag_value(words: Sequence[str], flag: str) -> str:
    return words[list(words).index(flag) + 1]


def test_settings_defaults_are_the_starting_values_and_are_spelled_out(
    workspace: Workspace,
) -> None:
    played = play_script(download_over_defaults(workspace))
    words = played.command
    assert _flag_value(words, "--matching") == "fuzzy"
    assert _flag_value(words, "--format") == "images"
    assert "--no-include-metadata" in words
    assert _flag_value(words, "--concurrency") == "4"
    assert _flag_value(words, "--decisions") == "shadow"
    assert played.outcome.answers == {"input": workspace.csv}
    assert "(s) = from settings defaults" in played.prompter.transcript
