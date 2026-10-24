"""The README agrees with the option tables and the settings example.

The comparison runs against the Command Reference (one ``### <command>``
section per command with a flag table) and the first YAML block of the
Settings File section; the self-checks below test the comparison itself.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from chronodownloader.options.options import Kind, Option, OptionTable
from chronodownloader.spec import TABLES

README = Path(__file__).resolve().parents[2] / "README.md"
COMMANDS = "## Command Reference"
SETTINGS = "## Settings File"
FLAG_HEADER = ["Flag", "Description", "Default", "In `defaults:`"]
SETTABLE_TEXT = {True: "yes", False: "no"}


def _section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return lines[start:end]


def _subsections(lines: list[str]) -> dict[str, list[str]]:
    """Split a section at its ``### `` headings; deeper headings stay inside."""
    parts: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        if line.startswith("### "):
            current = parts.setdefault(line.removeprefix("### ").strip(), [])
        elif current is not None:
            current.append(line)
    return parts


def _cells(line: str) -> list[str]:
    inner = line.strip()[1:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", inner)]


def _tables(lines: list[str]) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in [*lines, ""]:
        if line.startswith("|"):
            current.append(_cells(line))
        elif current:
            tables.append([current[0], *current[2:]])
            current = []
    return tables


def default_text(option: Option) -> str:
    """Return the default that ``--help`` prints for *option*, or ''."""
    if option.default_help is not None:
        return option.default_help
    if option.default is None or option.kind is Kind.FLAG:
        return ""
    if option.kind is Kind.SWITCH:
        return "on" if option.default else "off"
    if option.kind is Kind.LIST:
        return ",".join(str(item) for item in option.default)
    return str(option.default)


def description(option: Option) -> str:
    """Return the ``--help`` line of *option* without its default."""
    text = option.help_text()
    default = default_text(option)
    return text.replace(f"; default: {default}", "") if default else text


def _argument(option: Option) -> str:
    if option.kind in (Kind.SWITCH, Kind.FLAG):
        return ""
    if option.metavar is not None:
        return option.metavar
    return "{" + ",".join(str(choice) for choice in option.choices or ()) + "}"


def _flag_spellings(option: Option) -> list[str]:
    argument = _argument(option)
    if option.kind is Kind.SWITCH:
        return [option.flag, option.negative_flag]
    return [f"{option.flag} {argument}" if argument else option.flag]


def _flags(option: Option) -> list[str]:
    if option.kind is Kind.SWITCH:
        return [option.flag, option.negative_flag]
    return [option.flag]


def _check_row(
    command: str,
    row: list[str],
    table: OptionTable,
    seen: Counter[str],
    problems: list[str],
) -> None:
    spans = re.findall(r"`([^`]*)`", row[0])
    flags = [span.split(" ")[0] for span in spans]
    seen.update(flags)
    by_flag = {option.flag: option for option in table}
    if not flags or flags[0] not in by_flag:
        problems.append(f"{command}: unknown flag row: {row[0]}")
        return
    option = by_flag[flags[0]]
    where = f"{command}: {option.flag}"
    spellings = _flag_spellings(option)
    if spans != spellings:
        problems.append(f"{where}: flag cell {spans} != {spellings}")
    if row[1] != description(option):
        problems.append(f"{where}: description {row[1]!r}")
    default = default_text(option)
    if row[2] != default:
        problems.append(f"{where}: default {row[2]!r} != {default!r}")
    if row[3] != SETTABLE_TEXT[option.settable]:
        problems.append(f"{where}: defaults column {row[3]!r}")


def _command_drift(command: str, lines: list[str], table: OptionTable) -> list[str]:
    problems: list[str] = []
    seen: Counter[str] = Counter()
    for header, *rows in _tables(lines):
        if header == FLAG_HEADER:
            for row in rows:
                _check_row(command, row, table, seen, problems)
    expected = {flag: option for option in table for flag in _flags(option)}
    for flag in expected:
        if seen[flag] != 1:
            problems.append(f"{command}: {flag}: {seen[flag]} README rows")
    problems.extend(
        f"{command}: {flag}: not an option" for flag in seen if flag not in expected
    )
    return problems


def table_drift(text: str) -> list[str]:
    """Return every disagreement between the Command Reference and the tables."""
    sections = _subsections(_section(text, COMMANDS))
    problems = [f"{name}: no section" for name in TABLES if name not in sections]
    for name, table in TABLES.items():
        problems.extend(_command_drift(name, sections.get(name, []), table))
    return problems


def settings_block(text: str) -> str:
    """Return the first YAML block of the Settings File section."""
    lines = _section(text, SETTINGS)
    start = lines.index("```yaml") + 1
    end = lines.index("```", start)
    return "\n".join(lines[start:end]) + "\n"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _row(option: Option) -> str:
    flags = ", ".join(f"`{spelling}`" for spelling in _flag_spellings(option))
    cells = [flags, description(option), default_text(option)]
    cells.append(SETTABLE_TEXT[option.settable])
    return "| " + " | ".join(cells) + " |"


def reference(tables: dict[str, OptionTable]) -> str:
    """Return a Command Reference that matches *tables*, for the self-checks."""
    lines = [COMMANDS, ""]
    for name, table in tables.items():
        lines += [f"### {name}", "", "| " + " | ".join(FLAG_HEADER) + " |"]
        lines.append("| --- | --- | --- | --- |")
        lines += [_row(option) for option in table]
        lines.append("")
    return "\n".join(lines) + "\n"


def test_readme_tables_match_the_option_tables() -> None:
    assert table_drift(_readme()) == []


def test_readme_settings_block_is_the_example_file() -> None:
    from chronodownloader.settings import EXAMPLE_SETTINGS_PATH

    example = EXAMPLE_SETTINGS_PATH.read_text(encoding="utf-8")
    assert settings_block(_readme()) == example


def test_default_text_is_what_help_prints() -> None:
    for table in TABLES.values():
        for option in table:
            default = default_text(option)
            if default:
                assert f"default: {default}" in option.help_text(), option.flag
            else:
                assert "default: " not in option.help_text(), option.flag


def test_a_generated_reference_has_no_drift() -> None:
    assert table_drift(reference(dict(TABLES))) == []


DRIFT = [
    pytest.param(
        "| `--concurrency N` | works processed in parallel | 3 | yes |",
        "| `--concurrency N` | works processed in parallel | 4 | yes |",
        "download: --concurrency: default",
        id="changed-default",
    ),
    pytest.param(
        "| `--research` | search and judge again; old candidates kept as history "
        "|  | no |\n",
        "",
        "download: --research: 0 README rows",
        id="missing-row",
    ),
    pytest.param(
        "| `--include-metadata`, `--no-include-metadata` |",
        "| `--include-metadata` |",
        "download: --no-include-metadata: 0 README rows",
        id="missing-no-form",
    ),
    pytest.param(
        "| `--all` | drop every work of the store |  | no |",
        "| `--all` | drop every work of the store |  | yes |",
        "drop: --all: defaults column",
        id="flipped-defaults-column",
    ),
]


@pytest.mark.parametrize(("old", "new", "message"), DRIFT)
def test_table_drift_is_reported(old: str, new: str, message: str) -> None:
    text = reference(dict(TABLES))
    assert old in text
    problems = table_drift(text.replace(old, new, 1))
    assert any(message in problem for problem in problems), problems
