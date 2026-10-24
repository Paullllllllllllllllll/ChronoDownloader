"""The review screen: every resolved value, a plan preview, the command.

The review resolves the answers as the command line would, shows one line
per option group with every option's value (marked when it comes from the
settings ``defaults:`` block) and previews the plan with
:func:`chronodownloader.run.plan`, which sends no request and writes
nothing. Run is withheld while the answers do not resolve or the plan
fails; the review names the reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from chronodownloader import run as core
from chronodownloader.options.options import Kind, Option, Source
from chronodownloader.options.wizard import Line, Part, Review, State
from chronodownloader.settings import Settings, SettingsError
from chronodownloader.ui.questions import is_size_limit, limit_text
from chronodownloader.ui.session import Flow, Session

__all__ = ["EXEMPT", "PLAN_LIMIT", "build_review", "plan_lines", "value_text"]

EXEMPT: Final[Mapping[str, str]] = {
    "json": "agent-contract output (one JSON line on stdout); no use in the UI",
    "settings": "given to the ui itself (ui --settings) and added to the command",
}
"""Options the wizard never asks, with the reason."""

PLAN_LIMIT: Final = 20
"""Works listed in the plan preview."""


def value_text(option: Option, value: Any, settings: Settings) -> str:
    """Return *value* of *option* as the review shows it.

    An unset value shows what it stands for: the settings' size limit, or
    the row's default text (``all rows``, ``print the results``).
    """
    if value is None:
        if is_size_limit(option):
            return f"{limit_text(settings, option)} (settings)"
        return option.default_help or "none"
    if option.kind in (Kind.SWITCH, Kind.FLAG):
        return "yes" if value else "no"
    if isinstance(value, tuple):
        if not value:
            return option.default_help or "none"
        return ",".join(str(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _lines(state: State, settings: Settings) -> list[Line]:
    resolved = state.resolved()
    groups: dict[str, list[Part | str]] = {}
    for option in state.table:
        if option.name in EXEMPT:
            continue
        entry = resolved[option.name]
        name = option.flag.removeprefix("--")
        text = f"{name} {value_text(option, entry.value, settings)}"
        groups.setdefault(option.group, []).append(
            Part(text, entry.source is Source.SETTINGS)
        )
    source = settings.source
    lines = [Line(group, parts) for group, parts in groups.items()]
    lines.append(Line("settings", [str(source) if source else "code defaults"]))
    return lines


def plan_lines(run_plan: core.RunPlan) -> list[str]:
    """Return the plan preview: counts, the works, the fetched item, warnings."""
    counts = run_plan.counts
    report = run_plan.report
    if isinstance(report, core.VerifyReport):
        head = f"Plan: {len(report.works)} work(s) to verify"
        entries = [f"  {work.entry_id}: {work.status}" for work in report.works]
    else:
        head = (
            f"Plan: {len(run_plan.works)} of {counts.total} work(s) to process, "
            f"{counts.skipped} skipped"
        )
        entries = [
            f"  {planned.work.entry_id}: {planned.status}  {planned.work.title}"
            for planned in run_plan.works
        ]
    lines = [head, *entries[:PLAN_LIMIT]]
    if len(entries) > PLAN_LIMIT:
        lines.append(f"  ... and {len(entries) - PLAN_LIMIT} more")
    target = run_plan.fetch
    if target is not None:
        lines.append(
            f"Item: {target.provider}:{target.item_id} into work "
            f"{target.planned.work.entry_id} ({target.stored_status or 'new'})"
        )
    lines += [f"Warning: {warning}" for warning in run_plan.warnings]
    return lines


def build_review(flow: Flow, state: State, session: Session) -> Review:
    """Return the review screen of the answers in *state*."""
    settings = session.settings
    try:
        resolution = flow.resolve(state, session)
    except ValueError as exc:
        return Review(lines=_lines(state, settings), withheld=str(exc))
    notes: list[str] = []
    withheld: str | None = None
    try:
        run_plan = core.plan(resolution.spec, settings, sources=resolution.sources)
    except (core.PlanError, SettingsError) as exc:
        notes.append(f"Plan error: {exc}")
        withheld = "the plan failed; edit a step"
    else:
        notes += plan_lines(run_plan)
    if state.value("dry_run"):
        notes.append(
            f"Dry run: Run shows the plan only ({flow.table['dry_run'].help})."
        )
    return Review(lines=_lines(state, settings), notes=notes, withheld=withheld)
