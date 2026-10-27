"""Console entry point: dispatch the command, or print help when none is given.

Parsing, the import of the command's handler and the command run inside one
boundary (:func:`chronodownloader.cli.command.guarded`), so an interrupt or
a failed import still writes the summary line. Handlers are imported only
for the command that runs. On Windows, Ctrl+Break (``SIGBREAK``) is handled
like Ctrl+C (:mod:`chronodownloader.process.interrupt`), so an interrupted
run releases its leases and records its end. Warnings are logged to stderr
from the start; the command adds the log file once its settings are loaded.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from chronodownloader.cli.command import guarded, setup_logging
from chronodownloader.cli.parser import build_parser
from chronodownloader.cli.summary import Summary, summary_for
from chronodownloader.process.interrupt import install_break_handler
from chronodownloader.spec import COMMANDS

# Command -> (module, function) that runs it; the guided run lives in ui/,
# which cli/ never imports.
_HANDLERS: dict[str, tuple[str, str]] = {
    **{name: (f"chronodownloader.cli.{name}", "execute") for name in COMMANDS},
    "ui": ("chronodownloader.ui.app", "execute"),
}


def _dispatch(args: list[str], summary: Summary) -> int:
    """Parse *args*, import the command's handler and run it."""
    parser = build_parser()
    namespace = parser.parse_args(args)
    handler = _HANDLERS.get(namespace.command or "")
    if handler is None:
        parser.print_help()
        return 0
    module_name, function = handler
    execute = getattr(importlib.import_module(module_name), function)
    return int(execute(namespace, summary))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command in *argv* (``sys.argv[1:]`` when None)."""
    install_break_handler()
    setup_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        build_parser().print_help()
        return 0
    summary = summary_for(args)
    return guarded(summary, lambda: _dispatch(args, summary))


if __name__ == "__main__":
    raise SystemExit(main())
