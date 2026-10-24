"""Package structure: acyclic imports, silent core, separate front ends.

Only ``cli/`` and ``ui/`` print, prompt or exit; ``options/prompts.py`` is
the one exemption, and only ``ui/`` may import it; ``__main__`` may end the
process with ``raise SystemExit``. Exiting covers ``sys.exit``, ``os._exit``
(also imported by name) and ``raise SystemExit``. Providers never import
``output/`` or ``matching/``; ``coord.py`` and ``net/`` never import
``providers/``. The rules on packages that do not exist yet hold vacuously.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

import chronodownloader

PACKAGE = "chronodownloader"
PACKAGE_DIR = Path(chronodownloader.__file__).resolve().parent
FRONT_ENDS = (f"{PACKAGE}.cli", f"{PACKAGE}.ui")
ENTRY = f"{PACKAGE}.__main__"
PROMPTS = f"{PACKAGE}.options.prompts"
CONSOLE_CALLS = frozenset({"print", "input"})
EXIT_FUNCTIONS = frozenset({("sys", "exit"), ("os", "_exit")})
ENTRY_EXIT = "raise SystemExit"
"""The entry point ends the process with ``raise SystemExit(main())``."""
CONSOLE_MODULES = frozenset(
    {"tqdm", "colorama", "rich", "questionary", "prompt_toolkit"}
)
MSVCRT_CONSOLE_FUNCTIONS = frozenset(
    {
        "getch",
        "getwch",
        "getche",
        "getwche",
        "kbhit",
        "putch",
        "putwch",
        "ungetch",
        "ungetwch",
    }
)
FORBIDDEN_IMPORTS: Mapping[str, tuple[str, ...]] = {
    f"{PACKAGE}.providers": (f"{PACKAGE}.output", f"{PACKAGE}.matching"),
    f"{PACKAGE}.coord": (f"{PACKAGE}.providers",),
    f"{PACKAGE}.net": (f"{PACKAGE}.providers",),
}
"""Packages or modules and the packages they must not import."""


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _modules() -> dict[str, Path]:
    return {
        _module_name(path): path
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


MODULES = _modules()


def _within(name: str, root: str) -> bool:
    return name == root or name.startswith(root + ".")


def _tree(name: str) -> ast.Module:
    return ast.parse(MODULES[name].read_text(encoding="utf-8"), str(MODULES[name]))


def _is_package(name: str) -> bool:
    return MODULES[name].name == "__init__.py"


def _package_of(name: str) -> str:
    return name if _is_package(name) else name.rpartition(".")[0]


def _imported_names(name: str) -> Iterator[tuple[str, int]]:
    """Yield every absolute name imported by module *name*, with its line."""
    return _imports_in(_tree(name), _package_of(name))


def _imports_in(tree: ast.Module, package: str) -> Iterator[tuple[str, int]]:
    """Yield every absolute name imported in *tree*, with its line.

    Imports inside functions and ``TYPE_CHECKING`` blocks count. For
    ``from X import y`` both ``X`` and ``X.y`` are yielded.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[: len(base_parts) - node.level + 1]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            yield base, node.lineno
            for alias in node.names:
                yield f"{base}.{alias.name}", node.lineno


def _internal_target(imported: str) -> str | None:
    """Return the project module that *imported* names, if any."""
    parts = imported.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in MODULES:
            return candidate
        parts.pop()
    return None


def _module_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {name: set() for name in MODULES}
    for name in MODULES:
        for imported, _line in _imported_names(name):
            target = _internal_target(imported)
            if target is not None and target != name:
                graph[name].add(target)
    return graph


def _unit(name: str) -> str | None:
    """Return the top-level subpackage or module of *name* (None for the root)."""
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else None


def _unit_graph(graph: Mapping[str, set[str]]) -> dict[str, set[str]]:
    units: dict[str, set[str]] = {}
    for source, targets in graph.items():
        source_unit = _unit(source)
        if source_unit is None:
            continue
        units.setdefault(source_unit, set())
        for target in targets:
            target_unit = _unit(target)
            if target_unit is not None and target_unit != source_unit:
                units[source_unit].add(target_unit)
    return units


def _cycles(graph: Mapping[str, set[str]]) -> list[list[str]]:
    """Return one cycle per strongly connected component with a cycle."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph.get(node, ())):
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return components


def _in_front_end(name: str) -> bool:
    return any(_within(name, root) for root in FRONT_ENDS)


def _console_uses(name: str) -> Iterator[str]:
    return _console_uses_in(_tree(name), _package_of(name))


def _exit_aliases(tree: ast.Module) -> dict[str, str]:
    """Return the local names that ``from sys import exit`` and ``from os
    import _exit`` bind, with the function each names."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if (node.module, alias.name) in EXIT_FUNCTIONS:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _raises_system_exit(node: ast.Raise) -> bool:
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return isinstance(exc, ast.Name) and exc.id == "SystemExit"


def _console_uses_in(tree: ast.Module, package: str) -> Iterator[str]:
    aliases = _exit_aliases(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "msvcrt"
            and node.attr in MSVCRT_CONSOLE_FUNCTIONS
        ):
            yield f"msvcrt.{node.attr} at line {node.lineno}"
        if isinstance(node, ast.Raise) and _raises_system_exit(node):
            yield f"raise SystemExit at line {node.lineno}"
        if isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if (node.module, alias.name) in EXIT_FUNCTIONS:
                    yield f"import of {node.module}.{alias.name} at line {node.lineno}"
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in CONSOLE_CALLS:
                yield f"{func.id}() at line {node.lineno}"
            elif isinstance(func, ast.Name) and func.id in aliases:
                yield f"{aliases[func.id]}() at line {node.lineno}"
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in EXIT_FUNCTIONS
            ):
                yield f"{func.value.id}.{func.attr}() at line {node.lineno}"
    for imported, line in _imports_in(tree, package):
        root, _, member = imported.partition(".")
        if root in CONSOLE_MODULES or (
            root == "msvcrt" and member in MSVCRT_CONSOLE_FUNCTIONS
        ):
            yield f"import of {imported} at line {line}"


def test_the_graph_sees_every_module() -> None:
    assert f"{PACKAGE}.run" in MODULES
    assert f"{PACKAGE}.cli.download" in MODULES
    assert _module_graph()[f"{PACKAGE}.run"]


def test_module_import_graph_is_acyclic() -> None:
    assert _cycles(_module_graph()) == []


def test_subpackage_import_graph_is_acyclic() -> None:
    assert _cycles(_unit_graph(_module_graph())) == []


def test_core_does_not_import_the_front_ends() -> None:
    offenders = sorted(
        f"{source} -> {target}"
        for source, targets in _module_graph().items()
        if not _in_front_end(source) and source != ENTRY
        for target in targets
        if _in_front_end(target)
    )
    assert offenders == []


def _cross_front_end_imports(
    modules: Mapping[str, tuple[ast.Module, str]],
) -> list[str]:
    """Return the imports between cli and ui, and from ui of the entry module.

    *modules* maps module names to their parsed tree and package; imports
    inside functions and ``TYPE_CHECKING`` blocks count.
    """
    cli, ui = FRONT_ENDS
    forbidden = {cli: (ui,), ui: (cli, ENTRY)}
    offenders: list[str] = []
    for source, (tree, package) in sorted(modules.items()):
        for own, others in forbidden.items():
            if not _within(source, own):
                continue
            offenders.extend(
                f"{source} -> {imported} at line {line}"
                for imported, line in _imports_in(tree, package)
                if any(_within(imported, other) for other in others)
            )
    return sorted(set(offenders))


def _all_modules() -> dict[str, tuple[ast.Module, str]]:
    return {name: (_tree(name), _package_of(name)) for name in MODULES}


def test_front_ends_do_not_import_each_other() -> None:
    assert _cross_front_end_imports(_all_modules()) == []


def test_front_end_rule_flags_a_ui_module_importing_the_cli() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from ..cli import reporter\n"
        "if TYPE_CHECKING:\n"
        "    from chronodownloader.__main__ import main\n"
        "def show() -> None:\n"
        "    import chronodownloader.cli.command\n"
    )
    cli_source = "from chronodownloader.ui import progress\n"
    clean = "from chronodownloader.run import plan\nimport rich\n"
    modules = {
        f"{PACKAGE}.ui.screen": (ast.parse(source), f"{PACKAGE}.ui"),
        f"{PACKAGE}.cli.show": (ast.parse(cli_source), f"{PACKAGE}.cli"),
        f"{PACKAGE}.ui.clean": (ast.parse(clean), f"{PACKAGE}.ui"),
    }
    assert _cross_front_end_imports(modules) == [
        f"{PACKAGE}.cli.show -> {PACKAGE}.ui at line 1",
        f"{PACKAGE}.cli.show -> {PACKAGE}.ui.progress at line 1",
        f"{PACKAGE}.ui.screen -> {PACKAGE}.__main__ at line 4",
        f"{PACKAGE}.ui.screen -> {PACKAGE}.__main__.main at line 4",
        f"{PACKAGE}.ui.screen -> {PACKAGE}.cli at line 2",
        f"{PACKAGE}.ui.screen -> {PACKAGE}.cli.command at line 6",
        f"{PACKAGE}.ui.screen -> {PACKAGE}.cli.reporter at line 2",
    ]


def test_cycle_finder_reports_a_cycle() -> None:
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}
    assert _cycles(graph) == [["a", "b", "c"]]


def test_console_rule_flags_msvcrt_console_functions_only() -> None:
    console = ast.parse("import msvcrt\nmsvcrt.getch()\n")
    locking = ast.parse("import msvcrt\nmsvcrt.locking(fd, msvcrt.LK_LOCK, 1)\n")
    assert list(_console_uses_in(console, PACKAGE)) == ["msvcrt.getch at line 2"]
    assert list(_console_uses_in(locking, PACKAGE)) == []


@pytest.mark.parametrize(
    "name",
    sorted(name for name in MODULES if not _in_front_end(name) and name != PROMPTS),
)
def test_core_module_has_no_console_io(name: str) -> None:
    uses = list(_console_uses(name))
    if name == ENTRY:
        uses = [use for use in uses if not use.startswith(ENTRY_EXIT)]
    assert uses == []


EXITS = [
    pytest.param("raise SystemExit\n", ["raise SystemExit at line 1"], id="bare"),
    pytest.param("raise SystemExit(2)\n", ["raise SystemExit at line 1"], id="call"),
    pytest.param(
        "from sys import exit\nexit(1)\n",
        ["import of sys.exit at line 1", "sys.exit() at line 2"],
        id="from-sys",
    ),
    pytest.param(
        "from sys import exit as leave\nleave(1)\n",
        ["import of sys.exit at line 1", "sys.exit() at line 2"],
        id="alias",
    ),
    pytest.param("import os\nos._exit(1)\n", ["os._exit() at line 2"], id="os-exit"),
    pytest.param(
        "from os import _exit\n_exit(1)\n",
        ["import of os._exit at line 1", "os._exit() at line 2"],
        id="from-os",
    ),
    pytest.param("import sys\nsys.exit()\n", ["sys.exit() at line 2"], id="sys-exit"),
    pytest.param("raise ValueError('x')\nexit_code = 1\n", [], id="clean"),
]


@pytest.mark.parametrize(("source", "expected"), EXITS)
def test_console_rule_flags_every_way_to_exit(source: str, expected: list[str]) -> None:
    assert sorted(_console_uses_in(ast.parse(source), PACKAGE)) == sorted(expected)


def _prompt_importers(modules: Mapping[str, tuple[ast.Module, str]]) -> list[str]:
    """Return the imports of the prompts module from outside ``ui/``."""
    ui = FRONT_ENDS[1]
    return sorted(
        {
            f"{source} -> {imported} at line {line}"
            for source, (tree, package) in modules.items()
            if source != PROMPTS and not _within(source, ui)
            for imported, line in _imports_in(tree, package)
            if _within(imported, PROMPTS)
        }
    )


def test_only_the_ui_imports_the_prompts() -> None:
    assert PROMPTS in MODULES
    assert _prompt_importers(_all_modules()) == []


def test_prompt_rule_flags_an_import_outside_the_ui() -> None:
    importer = "from chronodownloader.options import prompts\n"
    relative = "from .prompts import ask\n"
    modules = {
        f"{PACKAGE}.cli.wizard": (ast.parse(importer), f"{PACKAGE}.cli"),
        f"{PACKAGE}.options.wizard": (ast.parse(relative), f"{PACKAGE}.options"),
        f"{PACKAGE}.ui.steps": (ast.parse(importer), f"{PACKAGE}.ui"),
    }
    assert _prompt_importers(modules) == [
        f"{PACKAGE}.cli.wizard -> {PROMPTS} at line 1",
        f"{PACKAGE}.options.wizard -> {PROMPTS} at line 1",
        f"{PACKAGE}.options.wizard -> {PROMPTS}.ask at line 1",
    ]


def _forbidden_imports(modules: Mapping[str, tuple[ast.Module, str]]) -> list[str]:
    """Return the imports that :data:`FORBIDDEN_IMPORTS` rules out."""
    offenders: set[str] = set()
    for source, (tree, package) in modules.items():
        for owner, others in FORBIDDEN_IMPORTS.items():
            if not _within(source, owner):
                continue
            offenders.update(
                f"{source} -> {imported} at line {line}"
                for imported, line in _imports_in(tree, package)
                if any(_within(imported, other) for other in others)
            )
    return sorted(offenders)


def test_layer_rules_hold() -> None:
    assert _forbidden_imports(_all_modules()) == []


def test_layer_rules_flag_their_imports() -> None:
    provider = (
        "from chronodownloader.output.store import Store\n"
        "from ..matching import fuzzy\n"
        "from chronodownloader.net import request\n"
    )
    modules = {
        f"{PACKAGE}.providers.mdz": (ast.parse(provider), f"{PACKAGE}.providers"),
        f"{PACKAGE}.coord": (
            ast.parse("import chronodownloader.providers.base\n"),
            PACKAGE,
        ),
        f"{PACKAGE}.net.session": (
            ast.parse("from chronodownloader.providers import registry\n"),
            f"{PACKAGE}.net",
        ),
    }
    assert _forbidden_imports(modules) == [
        f"{PACKAGE}.coord -> {PACKAGE}.providers.base at line 1",
        f"{PACKAGE}.net.session -> {PACKAGE}.providers at line 1",
        f"{PACKAGE}.net.session -> {PACKAGE}.providers.registry at line 1",
        f"{PACKAGE}.providers.mdz -> {PACKAGE}.matching at line 2",
        f"{PACKAGE}.providers.mdz -> {PACKAGE}.matching.fuzzy at line 2",
        f"{PACKAGE}.providers.mdz -> {PACKAGE}.output.store at line 1",
        f"{PACKAGE}.providers.mdz -> {PACKAGE}.output.store.Store at line 1",
    ]
