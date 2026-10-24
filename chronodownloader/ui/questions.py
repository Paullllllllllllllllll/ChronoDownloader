"""Wizard questions derived from option rows.

:func:`ask_option` asks one option the way its row declares it: a switch or
flag as Yes/No, a choice as a list, a list with choices as checkboxes, a
path with completion, anything else as text converted by the row. The
prompt text is the row's help up to its first clause; an empty answer
keeps the row's "empty" meaning (all rows, none, the settings value).
:func:`option_step` turns a run of options into one wizard step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Final

from chronodownloader.options.options import Kind, Option
from chronodownloader.options.wizard import (
    DONE,
    NO_DEFAULT,
    Choice,
    Nav,
    State,
    Step,
    StepContext,
    Validator,
)
from chronodownloader.settings import Settings
from chronodownloader.ui.session import absolute

__all__ = [
    "YES_NO",
    "Question",
    "ask_option",
    "existing_file",
    "is_size_limit",
    "limit_text",
    "option_step",
    "prompt_text",
    "required",
]

YES_NO: Final = (Choice(True, "Yes"), Choice(False, "No"))

_DOWNLOAD_LIMITS: Final = "download_limits."

Question = Callable[[], Nav | None]
"""One question of a step: DONE, BACK, or None when skipped."""


def prompt_text(option: Option) -> str:
    """Return the row's help up to its first clause, capitalized."""
    text = option.help.split(";")[0].split(": ")[0].strip()
    return text[:1].upper() + text[1:]


def is_size_limit(option: Option) -> bool:
    """Whether *option* falls back to a value of ``download_limits``."""
    return (option.default_help or "").startswith(_DOWNLOAD_LIMITS)


def limit_text(settings: Settings, option: Option) -> str:
    """Return the ``download_limits`` value a size-limit *option* falls back
    to, such as ``100`` or ``stop``."""
    value: Any = settings.download_limits
    path = (option.default_help or "").removeprefix(_DOWNLOAD_LIMITS)
    for part in path.split("."):
        value = getattr(value, part)
    return f"{value:g}" if isinstance(value, float) else str(value)


def required(text: str) -> str | None:
    """Refuse an empty answer."""
    return "enter a value" if not text.strip() else None


def existing_file(text: str) -> str | None:
    """Refuse an empty answer or one that names no file."""
    if not text.strip():
        return "enter a file"
    if not absolute(text).is_file():
        return "no such file"
    return None


def _both(first: Validator, second: Validator | None) -> Validator:
    if second is None:
        return first

    def check(text: str) -> str | None:
        return first(text) or second(text)

    return check


def _empty(option: Option) -> Any:
    """Return what an empty answer sets, or NO_DEFAULT when it is invalid."""
    if option.kind is Kind.LIST:
        return () if option.default == () else NO_DEFAULT
    return None if option.default is None else NO_DEFAULT


def _hint(option: Option, empty: Any, empty_hint: str | None) -> str:
    notes: list[str] = []
    if option.kind is Kind.LIST:
        notes.append("comma-separated")
    if empty is not NO_DEFAULT:
        notes.append(f"empty = {empty_hint or option.default_help or 'none'}")
    return f" ({'; '.join(notes)})" if notes else ""


def _yes_no_text(option: Option) -> str:
    """Return ``Dry run: plan without network requests or writes?``."""
    name = option.name.replace("_", " ")
    return f"{name[:1].upper()}{name[1:]}: {option.help.split(';')[0].strip()}?"


def ask_option(
    ctx: StepContext,
    name: str,
    *,
    message: str | None = None,
    validate: Validator | None = None,
    choices: Sequence[Choice[Any]] | None = None,
    empty_ok: bool = True,
    empty_hint: str | None = None,
) -> Nav:
    """Ask option *name* the way its row declares it.

    *message* replaces the prompt text; *choices* replaces the row's
    choices (or offers values for a row without any); *validate* checks text
    answers beside the row's conversion; without *empty_ok* an empty text
    answer is refused; *empty_hint* says what an empty answer stands for.
    A path answer the user typed is made absolute.
    """
    option = ctx.state.table[name]
    text = message or prompt_text(option)
    if option.kind in (Kind.SWITCH, Kind.FLAG):
        return ctx.select_option(name, message or _yes_no_text(option), YES_NO)
    if option.kind is Kind.LIST and option.choices:
        offered = [Choice(choice, str(choice)) for choice in option.choices]
        return ctx.checkbox_option(name, f"{text}:", offered)
    if choices is not None or option.choices:
        offered = list(choices or [Choice(c, str(c)) for c in option.choices or ()])
        return ctx.select_option(name, f"{text}:", offered)
    empty = _empty(option) if empty_ok else NO_DEFAULT
    if empty is NO_DEFAULT:
        validate = _both(required, validate)
    message = f"{text}{_hint(option, empty, empty_hint)}:"
    if option.type is Path:
        result = ctx.path_option(name, message, validate=validate, empty=empty)
        value = ctx.state.value(name)
        if result is DONE and ctx.state.is_answered(name) and value is not None:
            ctx.state.set(name, absolute(value))
        return result
    return ctx.text_option(name, message, validate=validate, empty=empty)


def option_step(
    name: str,
    title: str,
    names: Sequence[str],
    *,
    applies: Callable[[State], bool] | None = None,
    extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> Step:
    """Return a step that asks *names* in order, with Back between them.

    *extra* holds keyword arguments of :func:`ask_option` per option.
    """
    given = extra or {}

    def ask(ctx: StepContext) -> Nav:
        questions: list[Question] = [
            partial(ask_option, ctx, option, **given.get(option, {}))
            for option in names
        ]
        return StepContext.sequence(questions)

    if applies is None:
        return Step(name, title, ask=ask, sets=tuple(names))
    return Step(name, title, ask=ask, sets=tuple(names), applies=applies)
