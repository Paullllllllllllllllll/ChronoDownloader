"""The wizard steps of ``download``, ``fetch``, ``search`` and ``verify``.

Each step names the options of the command's table it may set and asks them
through :func:`chronodownloader.ui.questions.ask_option`, so the prompts,
choices and conversions come from the option rows. Where the command line
has alternatives, a step first asks which one: the works CSV or an ad hoc
query (``search``), a manifest URL or a provider item (``fetch``), the
settings' size limits or the run's own, printing or storing the results
(``search``). Steps that stop applying set their answers aside, so an
alternative left behind never reaches the command.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
from typing import Final

from chronodownloader.options.wizard import (
    BACK,
    DONE,
    Choice,
    Nav,
    State,
    Step,
    StepContext,
)
from chronodownloader.settings import PROVIDER_KEYS, DownloadLimits
from chronodownloader.ui.questions import (
    Question,
    ask_option,
    existing_file,
    limit_text,
    option_step,
)
from chronodownloader.ui.session import Session

__all__ = [
    "DECISION_MODEL",
    "LIMITS",
    "download_steps",
    "fetch_steps",
    "search_steps",
    "verify_steps",
]

LIMITS: Final = (
    "total_images_gb",
    "total_pdfs_gb",
    "total_metadata_gb",
    "work_images_gb",
    "work_pdfs_gb",
    "work_metadata_mb",
    "on_exceed",
)
"""The size-limit options, asked only when the run sets its own limits."""

MATCHING: Final = (
    "matching",
    "edition_policy",
    "providers",
    "max_candidates",
    "search_timeout",
)
FILES: Final = ("format", "max_pages", "include_metadata", "force")

_LIMITS_KEY: Final = "limits"
_QUERY_KEY: Final = "query"
_ITEM_KEY: Final = "item"
_STORE_KEY: Final = "store"

_PROVIDER_CHOICES: Final = tuple(Choice(key, key) for key in PROVIDER_KEYS)


def _providers_hint(ctx: StepContext) -> Nav | None:
    ctx.prompter.show(f"  provider keys: {', '.join(PROVIDER_KEYS)}")
    return None


def _skip(ask: Question, when: Callable[[], bool]) -> Question:
    """Return *ask*, skipped (None) while *when* holds."""
    return lambda: None if when() else ask()


_MODES: Final = (
    Choice("judge", "judge", note="decision model; needs the OpenRouter key"),
    Choice("fuzzy", "fuzzy", note="text score only; needs no key"),
    Choice("hybrid", "hybrid", note="text score, then the decision model"),
)


def _matching_step() -> Step:
    def ask(ctx: StepContext) -> Nav:
        questions: list[Question] = [
            partial(ask_option, ctx, "matching", message="Matching", choices=_MODES),
            partial(ask_option, ctx, "edition_policy"),
            partial(_providers_hint, ctx),
            partial(ask_option, ctx, "providers", empty_ok=False),
            partial(ask_option, ctx, "max_candidates"),
            partial(ask_option, ctx, "search_timeout"),
        ]
        return StepContext.sequence(questions)

    return Step("matching", "Search and matching", ask=ask, sets=MATCHING)


def _limits_note(limits: DownloadLimits) -> str:
    total, work = limits.total, limits.per_work
    return (
        f"run: images {total.images_gb:g} GB, PDFs {total.pdfs_gb:g} GB, "
        f"metadata {total.metadata_gb:g} GB; per work: images "
        f"{work.images_gb:g} GB, PDFs {work.pdfs_gb:g} GB, metadata "
        f"{work.metadata_mb:g} MB; at a limit: {limits.on_exceed}"
    )


def _limits_step(session: Session) -> Step:
    """Return the size-limit step: the settings' limits, or the run's own."""
    limits = session.settings.download_limits
    on_exceed: list[Choice[str | None]] = [
        Choice(None, "settings", note=f"download_limits.on_exceed: {limits.on_exceed}"),
        Choice("stop", "stop", note="stop the run"),
        Choice("skip", "skip", note="skip the file"),
    ]

    def ask(ctx: StepContext) -> Nav:
        state = ctx.state

        def gate() -> Nav:
            own = bool(state.data.get(_LIMITS_KEY)) or any(
                state.is_answered(name) for name in LIMITS
            )
            answer = ctx.prompter.select(
                "Size limits:",
                [
                    Choice(False, "Settings limits", note=_limits_note(limits)),
                    Choice(True, "Set limits for this run (0 = none)"),
                ],
                default=own,
            )
            if answer is BACK:
                return BACK
            state.data[_LIMITS_KEY] = answer
            if not answer:
                for name in LIMITS:
                    state.clear(name)
            return DONE

        def closed() -> bool:
            return not state.data.get(_LIMITS_KEY)

        questions: list[Question] = [gate]
        for name in LIMITS[:-1]:
            hint = f"settings, {limit_text(session.settings, state.table[name])}"
            question = partial(ask_option, ctx, name, empty_hint=hint)
            questions.append(_skip(question, closed))
        question = partial(ask_option, ctx, "on_exceed", choices=on_exceed)
        questions.append(_skip(question, closed))
        return StepContext.sequence(questions)

    return Step("limits", "Size limits", ask=ask, sets=LIMITS)


def _output_step() -> Step:
    return option_step("output", "Output", ("output",))


DECISION_MODEL: Final = ("decision_model", "decision_provider", "decision_endpoint")
"""The decision-model options of the post-phase and of the page check."""

_DIALECTS: Final = (
    Choice(None, "from the model id", note="vendor/model ids go to OpenRouter"),
    Choice("openai", "openai", note="needs the OpenAI key"),
    Choice("openrouter", "openrouter", note="needs the OpenRouter key"),
)


def _model_step(name: str, title: str, applies: Callable[[State], bool]) -> Step:
    return option_step(
        name,
        title,
        DECISION_MODEL,
        applies=applies,
        extra={"decision_provider": {"choices": _DIALECTS}},
    )


def _post_phase(state: State) -> bool:
    """Whether the download runs the gate or ``--classify`` after the loop."""
    return state.value("decisions") == "shadow" or bool(state.value("classify"))


def download_steps(session: Session) -> Sequence[Step]:
    """Return the steps of ``download``."""
    return (
        option_step(
            "works",
            "Works",
            ("input", "entry_ids", "limit", "only", "research"),
            extra={"input": {"validate": existing_file, "empty_ok": False}},
        ),
        _matching_step(),
        option_step("files", "Files", FILES),
        _limits_step(session),
        _output_step(),
        option_step("decisions", "After the downloads", ("decisions", "classify")),
        _model_step("decision_model", "Decision model", _post_phase),
        option_step("run", "Run", ("concurrency", "dry_run")),
    )


def _item_step() -> Step:
    """Return the item step of ``fetch``: a manifest URL or a provider item."""
    names = ("url", "provider", "id")

    def ask(ctx: StepContext) -> Nav:
        state = ctx.state

        def kind() -> Nav:
            current = state.data.get(_ITEM_KEY)
            if current is None:
                current = "url" if state.value("url") is not None else "provider"
            answer = ctx.prompter.select(
                "Item:",
                [
                    Choice("provider", "Provider and item id"),
                    Choice("url", "IIIF manifest URL"),
                ],
                default=current,
            )
            if answer is BACK:
                return BACK
            state.data[_ITEM_KEY] = answer
            for name in ("provider", "id") if answer == "url" else ("url",):
                state.clear(name)
            return DONE

        def by_url() -> bool:
            return state.data.get(_ITEM_KEY) == "url"

        def by_provider() -> bool:
            return not by_url()

        return StepContext.sequence(
            [
                kind,
                _skip(
                    partial(ask_option, ctx, "url", empty_ok=False),
                    by_provider,
                ),
                _skip(
                    partial(ask_option, ctx, "provider", choices=_PROVIDER_CHOICES),
                    by_url,
                ),
                _skip(
                    partial(ask_option, ctx, "id", empty_ok=False),
                    by_url,
                ),
            ]
        )

    return Step("item", "Item", ask=ask, sets=names)


def fetch_steps(session: Session) -> Sequence[Step]:
    """Return the steps of ``fetch``."""
    return (
        option_step(
            "work",
            "Work",
            ("entry_id", "title"),
            extra={"entry_id": {"empty_ok": False}},
        ),
        _item_step(),
        option_step("reason", "Reason", ("reason",)),
        option_step("files", "Files", FILES),
        _limits_step(session),
        _output_step(),
        option_step("run", "Run", ("dry_run",)),
    )


_ADHOC: Final = ("title", "creator", "year", "entry_id")


def _adhoc(state: State) -> bool:
    """Whether the search runs an ad hoc query instead of a works CSV."""
    chosen = state.data.get(_QUERY_KEY)
    if chosen is None:
        return state.value("title") is not None
    return bool(chosen == "title")


def _query_step() -> Step:
    """Return the query step of ``search``: a works CSV or an ad hoc query."""

    def ask(ctx: StepContext) -> Nav:
        state = ctx.state

        def kind() -> Nav:
            answer = ctx.prompter.select(
                "Search:",
                [
                    Choice("input", "Works CSV"),
                    Choice("title", "Ad hoc query (title, creator, year)"),
                ],
                default="title" if _adhoc(state) else "input",
            )
            if answer is BACK:
                return BACK
            state.data[_QUERY_KEY] = answer
            for name in _ADHOC if answer == "input" else ("input",):
                state.clear(name)
            return DONE

        def by_csv() -> bool:
            return not _adhoc(state)

        def by_query() -> bool:
            return _adhoc(state)

        questions: list[Question] = [
            kind,
            _skip(
                partial(
                    ask_option, ctx, "input", validate=existing_file, empty_ok=False
                ),
                by_query,
            ),
            _skip(
                partial(ask_option, ctx, "title", empty_ok=False),
                by_csv,
            ),
        ]
        questions += [
            _skip(partial(ask_option, ctx, name), by_csv) for name in _ADHOC[1:]
        ]
        return StepContext.sequence(questions)

    return Step("query", "Query", ask=ask, sets=("input", *_ADHOC))


def _search_output_step() -> Step:
    """Return the output step of ``search``: print, or store into a root."""

    def ask(ctx: StepContext) -> Nav:
        state = ctx.state

        def kind() -> Nav:
            stored = state.data.get(_STORE_KEY)
            if stored is None:
                stored = state.value("output") is not None
            answer = ctx.prompter.select(
                "Results:",
                [
                    Choice(False, "Print them"),
                    Choice(True, "Store them in an output root"),
                ],
                default=stored,
            )
            if answer is BACK:
                return BACK
            state.data[_STORE_KEY] = answer
            if not answer:
                state.clear("output")
            return DONE

        def printed() -> bool:
            return not state.data.get(_STORE_KEY)

        return StepContext.sequence(
            [kind, _skip(partial(ask_option, ctx, "output", empty_ok=False), printed)]
        )

    return Step("output", "Output", ask=ask, sets=("output",))


def search_steps(session: Session) -> Sequence[Step]:
    """Return the steps of ``search``."""
    return (
        _query_step(),
        option_step(
            "works",
            "Works",
            ("entry_ids", "limit"),
            applies=lambda state: not _adhoc(state),
        ),
        option_step("selection", "Selection", ("only", "research")),
        _matching_step(),
        _search_output_step(),
        option_step("run", "Run", ("dry_run",)),
    )


def verify_steps(session: Session) -> Sequence[Step]:
    """Return the steps of ``verify``."""
    return (
        _output_step(),
        option_step("works", "Works", ("entry_ids",)),
        option_step("pages", "Page check", ("pages",)),
        option_step(
            "first",
            "Pages sampled",
            ("first",),
            applies=lambda state: bool(state.value("pages")),
        ),
        _model_step(
            "page_model", "Page model", lambda state: bool(state.value("pages"))
        ),
        option_step("run", "Run", ("dry_run",)),
    )
