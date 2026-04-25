"""CLI entry point.

Wires :func:`run_with_mode_detection` to the parser, interactive handler,
and CLI dispatcher. Handles the standalone ``--quota-status`` and
``--cleanup-deferred`` commands before any mode detection runs.
"""
from __future__ import annotations

import logging
import sys

from main.ui.interactive import run_interactive
from main.ui.mode import run_with_mode_detection

from .commands.quota import cleanup_deferred_queue, show_quota_status
from .dispatch import run_cli
from .overrides import _looks_like_cli_invocation
from .parser import create_cli_parser


def main() -> None:
    """Main entry point supporting both interactive and CLI modes."""
    try:
        if "--quota-status" in sys.argv:
            show_quota_status()
            return

        if "--cleanup-deferred" in sys.argv:
            cleanup_deferred_queue()
            return

        if "--cli" not in sys.argv and _looks_like_cli_invocation(sys.argv[1:]):
            sys.argv.insert(1, "--cli")

        config, interactive_mode, args = run_with_mode_detection(
            interactive_handler=run_interactive,
            cli_handler=run_cli,
            parser_factory=create_cli_parser,
            script_name="downloader",
        )

        if args:
            if args.interactive:
                interactive_mode = True
            elif args.cli:
                interactive_mode = False

        if interactive_mode:
            run_interactive()
        else:
            if args is None:
                print("Error: CLI mode requires arguments. Use --interactive for guided setup.")
                sys.exit(1)
            run_cli(args, config)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
