"""CLI entry point.

Wires :func:`run_with_mode_detection` to the parser, interactive handler,
and CLI dispatcher, and enforces the CLI agent contract: deterministic exit
codes (0 success, 1 failures/partial, 2 usage/config error, 130 interrupt),
an optional one-line ``--json`` summary, and a non-TTY guard that refuses to
block on prompts when stdin is not a terminal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from main.ui.interactive import run_interactive
from main.ui.mode import run_with_mode_detection

from .commands.quota import cleanup_deferred_queue, show_quota_status
from .dispatch import run_cli
from .exit_codes import EXIT_INTERRUPT, EXIT_OK, EXIT_USAGE
from .overrides import _looks_like_cli_invocation
from .parser import create_cli_parser


def _emit_json_summary(summary: dict[str, object]) -> None:
    """Print one machine-readable JSON summary line on stdout."""
    print(json.dumps(summary, ensure_ascii=False))


def _run_verify_command(args: object) -> int:
    """Run ``--verify`` and return an exit code (1 if any work was partial)."""
    from .commands.verify import run_verify

    output_dir = getattr(args, "output_dir", "downloaded_works")
    stats = run_verify(output_dir)
    if getattr(args, "json_summary", False):
        _emit_json_summary({"command": "verify", **stats})
    return EXIT_OK if stats.get("partial", 0) == 0 else 1


def _apply_pre_config() -> argparse.Namespace:
    """Honor ``--config`` (and grab csv_file) for maintenance commands.

    The maintenance flags are handled before full mode detection; this
    pre-parse ensures a ``--config custom.json`` override still applies.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("csv_file", nargs="?", default=None)
    pre.add_argument("--config", default=None)
    pre_args, _ = pre.parse_known_args()
    if pre_args.config:
        os.environ["CHRONO_CONFIG_PATH"] = pre_args.config
    return pre_args


def _show_status(csv_file: str | None) -> None:
    """Display works-CSV progress plus quota/deferred status."""
    if csv_file and os.path.exists(csv_file):
        from main.data.works_csv import get_stats

        stats = get_stats(csv_file)
        print(f"\nWorks CSV: {os.path.abspath(csv_file)}")
        print(f"  * Total: {stats['total']}")
        print(f"  * Completed: {stats['completed']}")
        print(f"  * Failed: {stats['failed']}")
        print(f"  * Deferred: {stats.get('deferred', 0)}")
        print(f"  * Pending: {stats['pending']}")
    elif csv_file:
        print(f"CSV file not found: {csv_file}")
    show_quota_status()


def main() -> None:
    """Main entry point supporting both interactive and CLI modes."""
    try:
        if "--quota-status" in sys.argv:
            _apply_pre_config()
            show_quota_status()
            return

        if "--status" in sys.argv:
            pre_args = _apply_pre_config()
            _show_status(pre_args.csv_file)
            return

        if "--cleanup-deferred" in sys.argv:
            _apply_pre_config()
            cleanup_deferred_queue()
            return

        forces_cli = "--cli" in sys.argv or "--non-interactive" in sys.argv
        if not forces_cli and _looks_like_cli_invocation(sys.argv[1:]):
            sys.argv.insert(1, "--cli")

        config, interactive_mode, args = run_with_mode_detection(
            interactive_handler=run_interactive,
            cli_handler=run_cli,
            parser_factory=create_cli_parser,
            script_name="downloader",
        )

        if args:
            if getattr(args, "non_interactive", False) or args.cli:
                interactive_mode = False
            elif args.interactive:
                interactive_mode = True

        # --verify is a maintenance command; run it and exit regardless of mode.
        if args is not None and getattr(args, "verify", False):
            sys.exit(_run_verify_command(args))

        if interactive_mode:
            # Non-TTY guard: interactive mode would prompt, which cannot work
            # without a terminal. Fail clearly instead of blocking or faking
            # success.
            if not sys.stdin.isatty():
                print(
                    "Error: interactive mode requires a TTY. Re-run with --cli "
                    "(or --non-interactive) and the appropriate arguments.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_USAGE)
            run_interactive()
            return

        if args is None:
            print(
                "Error: CLI mode requires arguments. Use --interactive for "
                "guided setup.",
                file=sys.stderr,
            )
            sys.exit(EXIT_USAGE)

        code = run_cli(args, config)
        if code:
            sys.exit(code)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(EXIT_INTERRUPT)
    except SystemExit:
        raise
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
