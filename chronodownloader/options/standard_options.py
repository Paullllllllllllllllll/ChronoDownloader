"""Standard option rows: the run options every tool declares alike.

The builders return :class:`~.options.Option` rows for run control
(``--force``, ``--concurrency``, ``--dry-run``, ``--json``, ``--settings``)
and off-by-default flags. Each tool passes its own defaults, help texts and
targets, and arranges the rows into its option tables; the rows themselves
are declared once here. The value converter the rows use is public, so a
tool's own rows convert values the same way.

Copied from ChronoTranscriber v1.0.0 ``common/standard_options.py`` on
19.10.2026, trimmed: removed the model block (``model_options`` and its
constants), the page-image block (``image_options``, ``dpi_value``,
``jpeg_quality``), the batch rows (``batch_option``, ``check_input_option``,
``batch_ids_option``, ``adopt_option``, ``abandon_option``,
``batch_cancel_options``), ``output_options``, ``render_options`` and the
converters only those rows used (``bounded_float``, ``nonempty_text``).
"""

from __future__ import annotations

from pathlib import Path

from .options import Kind, Option

__all__ = [
    "DRY_RUN_HELP",
    "JSON_HELP",
    "SETTINGS_HELP",
    "concurrency_option",
    "dry_run_option",
    "flag_option",
    "force_option",
    "json_option",
    "positive_int",
    "settings_option",
]

DRY_RUN_HELP = "plan without API calls or writes"
JSON_HELP = "print one JSON summary line on stdout"
SETTINGS_HELP = "alternate settings file"


def positive_int(value: str) -> int:
    """Convert an integer above zero."""
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("must be > 0")
    return parsed


def flag_option(
    name: str,
    help: str,
    *,
    group: str = "run",
    target: str | None = None,
    exclusive: str | None = None,
) -> Option:
    """Return an off-by-default ``store_true`` flag that no settings set."""
    return Option(
        name,
        "--" + name.replace("_", "-"),
        help,
        group=group,
        kind=Kind.FLAG,
        default=False,
        exclusive=exclusive,
        target=target,
        settable=False,
    )


def force_option(help: str, *, target: str = "run.force") -> Option:
    """Return ``--force``; the tool's help says what is redone."""
    return flag_option("force", help, target=target)


def concurrency_option(
    default: int,
    *,
    help: str = "parallel requests",
    target: str = "run.concurrency",
) -> Option:
    """Return ``--concurrency N`` with the tool's default."""
    return Option(
        "concurrency",
        "--concurrency",
        help,
        group="run",
        type=positive_int,
        metavar="N",
        default=default,
        target=target,
    )


def dry_run_option(target: str, help: str = DRY_RUN_HELP) -> Option:
    """Return ``--dry-run`` filling *target*."""
    return flag_option("dry_run", help, target=target)


def json_option(target: str, help: str = JSON_HELP) -> Option:
    """Return ``--json`` filling *target*."""
    return flag_option("json", help, target=target)


def settings_option(*, help: str = SETTINGS_HELP, target: str | None = None) -> Option:
    """Return ``--settings FILE``."""
    return Option(
        "settings",
        "--settings",
        help,
        group="run",
        type=Path,
        metavar="FILE",
        default_help="config/settings.yaml",
        target=target,
        settable=False,
    )
