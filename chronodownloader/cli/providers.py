"""The providers command: list the 17 providers and their state.

One row per provider, in the settings order, goes to stdout, or to stderr
with ``--json``: IIIF, downloads or metadata only, image size cap, the API
key's environment variable and whether it is set, the circuit breaker and
the quota window from the coordination database. The command reads only and
exits 0.
"""

from __future__ import annotations

import argparse

from chronodownloader.cli.command import execute_command
from chronodownloader.cli.console import iso_time, render_table, write_report
from chronodownloader.cli.summary import Summary
from chronodownloader.run import ProvidersReport, ProviderState, RunResult
from chronodownloader.spec import resolve_providers

__all__ = ["execute", "render"]

HEADER = ("provider", "iiif", "downloads", "max_px", "key", "breaker", "quota", "note")


def _flag(value: bool) -> str:
    return "yes" if value else "no"


def _key(state: ProviderState) -> str:
    if state.api_key is None:
        return "missing" if state.key_set is False else "-"
    return f"{state.api_key} {'set' if state.key_set else 'unset'}"


def _breaker(state: ProviderState) -> str:
    if state.breaker == "closed":
        return "closed"
    cause = f", {state.breaker_cause}" if state.breaker_cause else ""
    return f"{state.breaker} until {iso_time(state.breaker_until)}{cause}"


def _quota(state: ProviderState) -> str:
    if state.quota_limit is None:
        return "-"
    left = f"{state.quota_remaining}/{state.quota_limit} left"
    if state.quota_reset_at is None:
        return left
    return f"{left}, resets {iso_time(state.quota_reset_at)}"


def render(report: ProvidersReport) -> str:
    """Return the providers of *report* as a table."""
    rows = [
        (
            state.key,
            _flag(state.iiif),
            "yes" if state.downloads else "metadata only",
            "-" if state.image_max_px is None else str(state.image_max_px),
            _key(state),
            _breaker(state),
            _quota(state),
            state.note or "",
        )
        for state in report.providers
    ]
    return render_table(HEADER, rows)


def _print(result: RunResult, json: bool) -> None:
    if isinstance(result.report, ProvidersReport):
        write_report(render(result.report), json=json)


def execute(args: argparse.Namespace, summary: Summary) -> int:
    """Run the parsed providers options and return the exit code."""
    return execute_command(
        args, summary, "providers", resolve_providers, report=_print, listing=True
    )
