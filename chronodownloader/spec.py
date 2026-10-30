"""The run specifications of the ten commands and the option tables behind them.

Each command has a module-level :class:`~chronodownloader.options.options.
OptionTable`, from which the CLI parser, the settings ``defaults:`` check and
the equivalent command line derive, and a frozen spec dataclass that
``resolve_<command>`` builds: a flag wins over a settings default, which wins
over the code default.

The ``defaults:`` block of the settings is validated against
:data:`DOWNLOAD_OPTIONS`. The other commands take the defaults of the options
they share with ``download`` by name, as long as their own row is settable:
``search`` takes the matching options but not ``output``; ``inspect`` takes
``providers`` and ``output``. The decision-model flags of ``classify`` and
``download`` take no default from ``defaults:``; the settings' ``decisions``
block fills them when the run is planned. ``--decisions`` is settable there.
The page-model flags of ``inspect`` and ``verify --pages`` fall back to
``judge.page_model``.

Selectors that only work together (``--provider`` with ``--id``) or exclude
a pair (``--url`` against ``--provider``/``--id``) are checked in the
resolvers; plain alternatives are argparse-exclusive (``search --input`` and
``--title``; ``drop --entry-ids``, ``--only`` and ``--all``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, get_args

from chronodownloader.events import WorkStatus
from chronodownloader.options.options import (
    Kind,
    Option,
    OptionTable,
    Resolved,
    Source,
)
from chronodownloader.options.standard_options import (
    concurrency_option,
    dry_run_option,
    flag_option,
    force_option,
    json_option,
    positive_int,
    settings_option,
)
from chronodownloader.settings import provider_key

__all__ = [
    "CLASSIFY_OPTIONS",
    "COMMANDS",
    "DECISION_MODES",
    "DECISION_PROVIDERS",
    "DEFAULT_ONLY",
    "DEFAULT_PROVIDERS",
    "DOWNLOAD_OPTIONS",
    "DROP_OPTIONS",
    "EDITION_POLICIES",
    "FETCH_OPTIONS",
    "FORMATS",
    "INSPECT_OPTIONS",
    "MATCHING_MODES",
    "ON_EXCEED",
    "PROVIDERS_OPTIONS",
    "SEARCH_OPTIONS",
    "STATUSES",
    "STATUS_OPTIONS",
    "TABLES",
    "UI_OPTIONS",
    "VERIFY_OPTIONS",
    "ClassifySpec",
    "CommandSpec",
    "DecisionModel",
    "DownloadControl",
    "DownloadDecisions",
    "DownloadSpec",
    "DropSpec",
    "FetchControl",
    "FetchItem",
    "FetchSpec",
    "FilesSpec",
    "InspectSpec",
    "MatchingSpec",
    "ProvidersSpec",
    "Resolution",
    "RunControl",
    "SearchQuery",
    "SearchSpec",
    "SizeLimits",
    "SpecError",
    "StatusSpec",
    "UiSpec",
    "VerifySpec",
    "WorkSelection",
    "command_of",
    "effective_spec",
    "resolve_classify",
    "resolve_download",
    "resolve_drop",
    "resolve_fetch",
    "resolve_inspect",
    "resolve_providers",
    "resolve_search",
    "resolve_status",
    "resolve_ui",
    "resolve_verify",
    "to_argv",
]

DEFAULT_PROVIDERS: tuple[str, ...] = (
    "mdz",
    "bnf_gallica",
    "e_rara",
    "internet_archive",
    "wellcome",
    "europeana",
    "ddb",
    "polona",
)
"""The providers searched, in preference order, when no settings name them;
the list of the packaged settings example."""

STATUSES: tuple[str, ...] = get_args(WorkStatus)
"""The stored statuses of a work."""

NEW = "new"
"""The ``--only`` value of a work without a store entry."""

DEFAULT_ONLY: tuple[str, ...] = (NEW, "partial", "failed", "deferred", "blocked")
"""The works a run takes by default; complete, no_match and needs_review
works are reopened by ``--research`` or ``fetch``."""

MATCHING_MODES = ("judge", "fuzzy", "hybrid")
EDITION_POLICIES = ("any_edition", "same_edition", "any_language")
FORMATS = ("images", "pdf", "both")
ON_EXCEED = ("stop", "skip")

DEFAULT_OUTPUT = Path("downloaded_works")
DEFAULT_MAX_CANDIDATES = 5
DEFAULT_SEARCH_TIMEOUT = 60.0
DEFAULT_CONCURRENCY = 3
DEFAULT_FIRST_PAGES = 10
ADHOC_ENTRY_ID = "adhoc"

COMMANDS = (
    "download",
    "fetch",
    "search",
    "inspect",
    "verify",
    "classify",
    "status",
    "drop",
    "providers",
    "ui",
)
"""The commands in the order the help lists them."""

DECISION_PROVIDERS = ("openai", "openrouter")
"""The ``--decision-provider`` values: the decision dialects."""

DECISION_MODES = ("off", "shadow")
"""The ``--decisions`` values; ``apply`` comes with the gate's veto."""


class SpecError(ValueError):
    """The options do not form a valid run specification."""


# -- value converters -------------------------------------------------------


def _text(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("must not be empty")
    return text


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError("must be >= 0")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("must be a number >= 0")
    return parsed


# -- specs ------------------------------------------------------------------


@dataclass(frozen=True)
class WorkSelection:
    """The works of the input CSV or the store that a run takes.

    ``entry_ids`` empty takes every work; ``limit`` None takes all of them.
    ``only`` names the statuses taken, ``new`` being a work without a store
    entry. ``research`` searches and judges again, keeping the old
    candidates as history.
    """

    entry_ids: tuple[str, ...]
    limit: int | None
    only: tuple[str, ...]
    research: bool


@dataclass(frozen=True)
class MatchingSpec:
    """How candidates are found and accepted.

    ``providers`` are searched in this order, which is also the preference
    order. ``search_timeout`` 0 disables the per-provider timeout.
    """

    mode: str
    edition_policy: str
    providers: tuple[str, ...]
    max_candidates: int
    search_timeout: float


@dataclass(frozen=True)
class FilesSpec:
    """What a download writes.

    ``format`` pdf takes the item's PDF when it offers one, else its page
    images; ``max_pages`` (0 for none) binds page images only.
    """

    format: str
    max_pages: int
    include_metadata: bool


@dataclass(frozen=True)
class SizeLimits:
    """Size limits of a run; None takes the settings' ``download_limits``."""

    total_images_gb: float | None = None
    total_pdfs_gb: float | None = None
    total_metadata_gb: float | None = None
    work_images_gb: float | None = None
    work_pdfs_gb: float | None = None
    work_metadata_mb: float | None = None
    on_exceed: str | None = None


@dataclass(frozen=True)
class DownloadControl:
    """Force, concurrency and the agent-contract switches of a download."""

    force: bool
    concurrency: int
    dry_run: bool
    json: bool


@dataclass(frozen=True)
class DownloadDecisions:
    """The decision-model steps after a download's work loop.

    ``mode`` ``shadow`` checks the downloaded and review-band works and
    records the answers only; ``classify`` classifies the downloaded works.
    ``model``, ``provider`` and ``endpoint`` None take the settings'
    ``decisions`` block.
    """

    mode: str
    classify: bool
    model: str | None
    provider: str | None
    endpoint: str | None


@dataclass(frozen=True)
class DownloadSpec:
    """One ``download`` run: the works of ``input`` into ``output``."""

    input: Path
    selection: WorkSelection
    matching: MatchingSpec
    files: FilesSpec
    limits: SizeLimits
    output: Path
    run: DownloadControl
    decision: DownloadDecisions


@dataclass(frozen=True)
class FetchItem:
    """The item to fetch: a ``url``, or a ``provider`` with an ``id``.

    ``title`` None takes the stored title; ``reason`` is recorded with the
    download.
    """

    entry_id: str
    title: str | None
    url: str | None
    provider: str | None
    id: str | None
    reason: str | None


@dataclass(frozen=True)
class FetchControl:
    """Force and the agent-contract switches of a fetch."""

    force: bool
    dry_run: bool
    json: bool


@dataclass(frozen=True)
class FetchSpec:
    """One ``fetch``: a known item into a work of ``output``."""

    item: FetchItem
    files: FilesSpec
    limits: SizeLimits
    output: Path
    run: FetchControl


@dataclass(frozen=True)
class RunControl:
    """The agent-contract switches."""

    dry_run: bool
    json: bool


@dataclass(frozen=True)
class SearchQuery:
    """The works CSV ``input``, or one ad hoc query by ``title``.

    ``entry_id`` names the ad hoc work in the output root.
    """

    input: Path | None
    title: str | None
    creator: str | None
    year: int | None
    entry_id: str


@dataclass(frozen=True)
class SearchSpec:
    """One ``search``; ``output`` None prints the results instead of storing."""

    query: SearchQuery
    selection: WorkSelection
    matching: MatchingSpec
    output: Path | None
    run: RunControl


@dataclass(frozen=True)
class InspectSpec:
    """One ``inspect``: the stored candidates of ``entry_ids``, or one item.

    ``providers`` None takes the candidates of every provider; ``output``
    None writes the pages to a scratch folder.
    """

    entry_ids: tuple[str, ...]
    provider: str | None
    id: str | None
    providers: tuple[str, ...] | None
    first: int
    output: Path | None
    run: RunControl
    decision: DecisionModel


@dataclass(frozen=True)
class VerifySpec:
    """One ``verify`` of the works in ``output``; ``entry_ids`` empty takes all."""

    entry_ids: tuple[str, ...]
    pages: bool
    first: int
    output: Path
    run: RunControl
    decision: DecisionModel


@dataclass(frozen=True)
class DecisionModel:
    """The decision model of a run; None takes the settings.

    ``classify`` takes the ``decisions`` block; the page check of
    ``inspect`` and ``verify --pages`` takes ``judge.page_model`` when all
    three are None.
    """

    model: str | None
    provider: str | None
    endpoint: str | None


@dataclass(frozen=True)
class ClassifySpec:
    """One ``classify`` of the works in ``output``; ``entry_ids`` empty takes all.

    ``force`` asks every page again, current labels included.
    """

    entry_ids: tuple[str, ...]
    force: bool
    decision: DecisionModel
    output: Path
    run: RunControl


@dataclass(frozen=True)
class StatusSpec:
    """One ``status`` listing of ``output``, against the works CSV ``input``."""

    input: Path | None
    entry_ids: tuple[str, ...]
    only: tuple[str, ...]
    output: Path
    json: bool


@dataclass(frozen=True)
class DropSpec:
    """One ``drop``: exactly one of ``entry_ids``, ``only`` and ``all``."""

    entry_ids: tuple[str, ...]
    only: tuple[str, ...]
    all: bool
    output: Path
    dry_run: bool
    json: bool


@dataclass(frozen=True)
class ProvidersSpec:
    """One ``providers`` listing."""

    json: bool


@dataclass(frozen=True)
class UiSpec:
    """The guided run; its only option, ``--settings``, is not part of a spec."""


type CommandSpec = (
    DownloadSpec
    | FetchSpec
    | SearchSpec
    | InspectSpec
    | VerifySpec
    | ClassifySpec
    | StatusSpec
    | DropSpec
    | ProvidersSpec
    | UiSpec
)


# -- option rows --------------------------------------------------------------

_GROUP_TITLES = {
    "works": "works",
    "query": "works CSV or ad hoc query",
    "item": "item",
    "candidates": "candidates",
    "matching": "search and matching",
    "download": "download",
    "limits": "size limits",
    "pages": "page check",
    "classify": "pages",
    "decision": "decision model",
    "drop": "works to drop (exactly one)",
    "output": "output",
    "run": "run control",
}

_INPUT = Option(
    "input",
    "--input",
    "works CSV with entry_id and title",
    group="works",
    type=Path,
    metavar="FILE",
    target="input",
    settable=False,
)
_ENTRY_IDS = Option(
    "entry_ids",
    "--entry-ids",
    "only these entry_id values",
    group="works",
    kind=Kind.LIST,
    type=_text,
    metavar="LIST",
    default=(),
    default_help="all rows",
    target="selection.entry_ids",
    settable=False,
)
_LIMIT = Option(
    "limit",
    "--limit",
    "at most N works after the filters",
    group="works",
    type=positive_int,
    metavar="N",
    default_help="all",
    target="selection.limit",
    settable=False,
)
_ONLY = Option(
    "only",
    "--only",
    "only works in these stored statuses",
    group="works",
    kind=Kind.LIST,
    choices=(NEW, *STATUSES),
    metavar="LIST",
    default=DEFAULT_ONLY,
    target="selection.only",
    settable=False,
)
_RESEARCH = flag_option(
    "research",
    "search and judge again; old candidates kept as history",
    group="works",
    target="selection.research",
)

_MATCHING = Option(
    "matching",
    "--matching",
    "judge needs the OpenRouter key; fuzzy needs none",
    group="matching",
    choices=MATCHING_MODES,
    default="judge",
    target="matching.mode",
)
_EDITION_POLICY = Option(
    "edition_policy",
    "--edition-policy",
    "editions accepted without review",
    group="matching",
    choices=EDITION_POLICIES,
    default="any_edition",
    target="matching.edition_policy",
)
_PROVIDERS = Option(
    "providers",
    "--providers",
    "providers to search, in preference order",
    group="matching",
    kind=Kind.LIST,
    type=provider_key,
    metavar="LIST",
    default=DEFAULT_PROVIDERS,
    default_help="defaults.providers",
    target="matching.providers",
)
_MAX_CANDIDATES = Option(
    "max_candidates",
    "--max-candidates",
    "candidates kept per provider",
    group="matching",
    type=positive_int,
    metavar="N",
    default=DEFAULT_MAX_CANDIDATES,
    target="matching.max_candidates",
)
_SEARCH_TIMEOUT = Option(
    "search_timeout",
    "--search-timeout",
    "per-provider search timeout; 0 disables",
    group="matching",
    type=_non_negative_float,
    metavar="SECONDS",
    default=DEFAULT_SEARCH_TIMEOUT,
    default_help="60",
    target="matching.search_timeout",
)

_FORMAT = Option(
    "format",
    "--format",
    (
        "what to download: pdf takes the item's PDF when it offers one, else "
        "its page images; images takes page images only; both takes both"
    ),
    group="download",
    choices=FORMATS,
    default="pdf",
    target="files.format",
)
_MAX_PAGES = Option(
    "max_pages",
    "--max-pages",
    (
        "page limit per work, binding page images only; the smaller of this "
        "and the provider's max_pages applies; 0 = none"
    ),
    group="download",
    type=_non_negative_int,
    metavar="N",
    default=0,
    target="files.max_pages",
)
_INCLUDE_METADATA = Option(
    "include_metadata",
    "--include-metadata",
    "save the metadata files",
    group="download",
    kind=Kind.SWITCH,
    default=True,
    target="files.include_metadata",
)
_FORCE = replace(
    force_option("re-download complete works; take over unstored folders"),
    group="download",
)


def _limit(name: str, help: str, unit: str, block: str) -> Option:
    return Option(
        name,
        "--" + name.replace("_", "-"),
        help,
        group="limits",
        type=_non_negative_float,
        metavar=unit,
        default_help=f"download_limits.{block}",
        target=f"limits.{name}",
        settable=False,
    )


_LIMITS = (
    _limit(
        "total_images_gb",
        "run size limit of page images in GB; 0 = none",
        "GB",
        "total.images_gb",
    ),
    _limit(
        "total_pdfs_gb", "run size limit of PDFs in GB; 0 = none", "GB", "total.pdfs_gb"
    ),
    _limit(
        "total_metadata_gb",
        "run size limit of metadata files in GB; 0 = none",
        "GB",
        "total.metadata_gb",
    ),
    _limit(
        "work_images_gb",
        "per-work size limit of page images in GB; 0 = none",
        "GB",
        "per_work.images_gb",
    ),
    _limit(
        "work_pdfs_gb",
        "per-work size limit of PDFs in GB; 0 = none",
        "GB",
        "per_work.pdfs_gb",
    ),
    _limit(
        "work_metadata_mb",
        "per-work size limit of metadata files in MB; 0 = none",
        "MB",
        "per_work.metadata_mb",
    ),
    Option(
        "on_exceed",
        "--on-exceed",
        "stop the run or skip the file at a limit",
        group="limits",
        choices=ON_EXCEED,
        default_help="download_limits.on_exceed",
        target="limits.on_exceed",
        settable=False,
    ),
)

_OUTPUT = Option(
    "output",
    "--output",
    "output root holding chronodownloader.sqlite",
    group="output",
    type=Path,
    metavar="DIR",
    default=DEFAULT_OUTPUT,
    target="output",
)

_DRY_RUN = dry_run_option("run.dry_run", "plan without network requests or writes")
_JSON = json_option("run.json")
_SETTINGS = settings_option()


def _output(help: str) -> Option:
    """Return the download ``--output`` row with another help text."""
    return replace(_OUTPUT, help=help)


def _decision(name: str, help: str, **extra: Any) -> Option:
    return Option(
        name,
        "--" + name.replace("_", "-"),
        help,
        group="decision",
        target=f"decision.{name.removeprefix('decision_')}",
        settable=False,
        **extra,
    )


_DECISION_ROWS = (
    _decision(
        "decision_model",
        "decision model id; vendor/model ids go to OpenRouter",
        type=_text,
        metavar="ID",
        default_help="decisions.model",
    ),
    _decision(
        "decision_provider",
        "dialect and key of the decision model",
        choices=DECISION_PROVIDERS,
        default_help="from the model id",
    ),
    _decision(
        "decision_endpoint",
        "Decisions endpoint URL",
        type=_text,
        metavar="URL",
        default_help="the provider's endpoint",
    ),
)
"""The decision-model rows of ``classify`` and ``download``."""

_PAGE_MODEL_ROWS = (
    replace(
        _DECISION_ROWS[0],
        help="page model id of the page check; vendor/model ids go to OpenRouter",
        group="pages",
        default_help="judge.page_model",
    ),
    replace(
        _DECISION_ROWS[1],
        help="dialect and key of the page model",
        group="pages",
    ),
    replace(
        _DECISION_ROWS[2],
        help="Decisions endpoint URL of the page model",
        group="pages",
        default_help="judge.endpoint; with another model, the provider's",
    ),
)
"""The page-model rows of ``inspect`` and ``verify --pages``."""

_PAGE_MODEL_NAMES = tuple(row.name for row in _PAGE_MODEL_ROWS)

_DECISIONS_MODE = Option(
    "decisions",
    "--decisions",
    (
        "shadow checks the downloaded and review-band works' first pages after "
        "the downloads and records the answers only; apply waits for a validated "
        "threshold"
    ),
    group="decision",
    choices=DECISION_MODES,
    default="off",
    target="decision.mode",
)
_CLASSIFY = flag_option(
    "classify",
    "classify the downloaded works' pages after the downloads",
    group="decision",
    target="decision.classify",
)


# -- tables -------------------------------------------------------------------

DOWNLOAD_OPTIONS = OptionTable(
    [
        _INPUT,
        _ENTRY_IDS,
        _LIMIT,
        _ONLY,
        _RESEARCH,
        _MATCHING,
        _EDITION_POLICY,
        _PROVIDERS,
        _MAX_CANDIDATES,
        _SEARCH_TIMEOUT,
        _FORMAT,
        _MAX_PAGES,
        _INCLUDE_METADATA,
        _FORCE,
        *_LIMITS,
        _DECISIONS_MODE,
        _CLASSIFY,
        *_DECISION_ROWS,
        _OUTPUT,
        concurrency_option(DEFAULT_CONCURRENCY, help="works processed in parallel"),
        _DRY_RUN,
        _JSON,
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)
"""The download options; the settings validate ``defaults:`` against them."""


def _item(name: str, flag: str, help: str, metavar: str, **extra: Any) -> Option:
    return Option(
        name,
        flag,
        help,
        group="item",
        type=extra.pop("type", _text),
        metavar=metavar,
        target=f"item.{name}",
        settable=False,
        **extra,
    )


FETCH_OPTIONS = OptionTable(
    [
        _item("entry_id", "--entry-id", "work the item belongs to", "ID"),
        _item(
            "title",
            "--title",
            "title of a work not yet in the store",
            "TEXT",
            default_help="stored title",
        ),
        _item(
            "url",
            "--url",
            "IIIF manifest or item URL; excludes --provider and --id",
            "URL",
        ),
        _item("provider", "--provider", "provider of --id", "KEY", type=provider_key),
        _item("id", "--id", "provider item identifier", "ID"),
        _item(
            "reason",
            "--reason",
            "reason recorded with the download; required when the work is needs_review",
            "TEXT",
        ),
        _FORMAT,
        _MAX_PAGES,
        _INCLUDE_METADATA,
        _FORCE,
        *_LIMITS,
        _output("output root"),
        _DRY_RUN,
        _JSON,
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)


def _query(name: str, flag: str, help: str, metavar: str, **extra: Any) -> Option:
    return Option(
        name,
        flag,
        help,
        group="query",
        type=extra.pop("type", _text),
        metavar=metavar,
        target=f"query.{name}",
        settable=False,
        **extra,
    )


SEARCH_OPTIONS = OptionTable(
    [
        _query(
            "input",
            "--input",
            "works CSV with entry_id and title",
            "FILE",
            type=Path,
            exclusive="query",
        ),
        _query("title", "--title", "ad hoc query", "TEXT", exclusive="query"),
        _query("creator", "--creator", "creator of the ad hoc query", "NAME"),
        _query("year", "--year", "year of the ad hoc query", "YEAR", type=int),
        _query(
            "entry_id",
            "--entry-id",
            "work id of an ad hoc query, used with --output",
            "ID",
            default=ADHOC_ENTRY_ID,
        ),
        _ENTRY_IDS,
        _LIMIT,
        _ONLY,
        _RESEARCH,
        _MATCHING,
        _EDITION_POLICY,
        _PROVIDERS,
        _MAX_CANDIDATES,
        _SEARCH_TIMEOUT,
        replace(
            _output("write the results into this output root"),
            default=None,
            default_help="print the results",
            settable=False,
        ),
        _DRY_RUN,
        _JSON,
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)

_STORE_ENTRY_IDS = replace(_ENTRY_IDS, target="entry_ids")

INSPECT_OPTIONS = OptionTable(
    [
        replace(
            _STORE_ENTRY_IDS,
            help="inspect the stored candidates of these works; needs --output",
            group="candidates",
            default_help=None,
        ),
        Option(
            "provider",
            "--provider",
            "provider of --id; inspects one item outside the store",
            group="candidates",
            type=provider_key,
            metavar="KEY",
            target="provider",
            settable=False,
        ),
        Option(
            "id",
            "--id",
            "provider item identifier; inspects one item outside the store",
            group="candidates",
            type=_text,
            metavar="ID",
            target="id",
            settable=False,
        ),
        replace(
            _PROVIDERS,
            help="only candidates of these providers",
            group="candidates",
            default=None,
            default_help="all",
            target="providers",
        ),
        Option(
            "first",
            "--first",
            "pages sampled per candidate",
            group="candidates",
            type=positive_int,
            metavar="N",
            default=DEFAULT_FIRST_PAGES,
            target="first",
            settable=False,
        ),
        replace(
            _output(
                "store root; pages go to <output>/_inspect/<entry_id>/<provider>_<id>/"
            ),
            default=None,
            default_help="a scratch folder in %TEMP%\\chronodownloader\\inspect",
        ),
        *_PAGE_MODEL_ROWS,
        _DRY_RUN,
        _JSON,
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)

VERIFY_OPTIONS = OptionTable(
    [
        replace(_STORE_ENTRY_IDS, help="only these works", default_help="all works"),
        flag_option(
            "pages",
            "also check title page and imprint year with the page model",
            group="pages",
            target="pages",
        ),
        Option(
            "first",
            "--first",
            "pages sampled for the check; only with --pages",
            group="pages",
            type=positive_int,
            metavar="N",
            default=DEFAULT_FIRST_PAGES,
            target="first",
            settable=False,
        ),
        *(
            replace(row, help=row.help + "; only with --pages")
            for row in _PAGE_MODEL_ROWS
        ),
        _output("output root to verify"),
        dry_run_option("run.dry_run", "report without changing statuses"),
        _JSON,
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)


CLASSIFY_OPTIONS = OptionTable(
    [
        replace(_STORE_ENTRY_IDS, help="only these works", default_help="all works"),
        flag_option(
            "force",
            "ask every page again, current labels included",
            group="classify",
            target="force",
        ),
        *_DECISION_ROWS,
        _output("output root to classify"),
        dry_run_option(
            "run.dry_run", "list the pages to ask without requests or writes"
        ),
        _JSON,
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)

STATUS_OPTIONS = OptionTable(
    [
        replace(_INPUT, help="works CSV; rows without a store entry count as new"),
        _STORE_ENTRY_IDS,
        replace(
            _ONLY,
            help="only works in these statuses",
            default=(NEW, *STATUSES),
            default_help="all",
            target="only",
        ),
        _output("output root; statuses, review queue, _inspect leftovers"),
        json_option("json"),
        _SETTINGS,
    ],
    group_titles=_GROUP_TITLES,
)

DROP_OPTIONS = OptionTable(
    [
        replace(
            _STORE_ENTRY_IDS,
            help="drop these works",
            group="drop",
            default_help=None,
            exclusive="selection",
        ),
        replace(
            _ONLY,
            help="drop the works in these stored statuses",
            group="drop",
            choices=STATUSES,
            default=(),
            target="only",
            exclusive="selection",
        ),
        flag_option(
            "all",
            "drop every work of the store",
            group="drop",
            target="all",
            exclusive="selection",
        ),
        replace(
            _OUTPUT, help="output root", default=None, default_help=None, settable=False
        ),
        dry_run_option("dry_run", "list what would be dropped"),
        json_option("json"),
    ],
    group_titles=_GROUP_TITLES,
)

PROVIDERS_OPTIONS = OptionTable(
    [json_option("json"), _SETTINGS], group_titles=_GROUP_TITLES
)

UI_OPTIONS = OptionTable([_SETTINGS], group_titles=_GROUP_TITLES)

TABLES: Mapping[str, OptionTable] = {
    "download": DOWNLOAD_OPTIONS,
    "fetch": FETCH_OPTIONS,
    "search": SEARCH_OPTIONS,
    "inspect": INSPECT_OPTIONS,
    "verify": VERIFY_OPTIONS,
    "classify": CLASSIFY_OPTIONS,
    "status": STATUS_OPTIONS,
    "drop": DROP_OPTIONS,
    "providers": PROVIDERS_OPTIONS,
    "ui": UI_OPTIONS,
}
"""The option table of each command, in help order."""

_SPECS: Mapping[type, str] = {
    DownloadSpec: "download",
    FetchSpec: "fetch",
    SearchSpec: "search",
    InspectSpec: "inspect",
    VerifySpec: "verify",
    ClassifySpec: "classify",
    StatusSpec: "status",
    DropSpec: "drop",
    ProvidersSpec: "providers",
    UiSpec: "ui",
}


def command_of(spec: CommandSpec) -> str:
    """Return the name of the command *spec* belongs to."""
    return _SPECS[type(spec)]


# -- resolution ---------------------------------------------------------------


@dataclass(frozen=True)
class Resolution[S]:
    """The effective spec and the source of each field (dotted path)."""

    spec: S
    sources: Mapping[str, Source]


def _applicable(
    table: OptionTable, defaults: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return the settings defaults that set settable options of *table*."""
    return {
        name: value
        for name, value in (defaults or {}).items()
        if name in table and table[name].settable
    }


def _resolve_table(
    table: OptionTable, flags: Mapping[str, Any], defaults: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Source]]:
    unknown = set(flags) - set(table.names)
    if unknown:
        raise SpecError(f"unknown options: {', '.join(sorted(unknown))}")
    resolved: dict[str, Resolved] = table.resolve(flags, _applicable(table, defaults))
    return table.by_targets(resolved)


def _part(values: Mapping[str, Any], name: str) -> dict[str, Any]:
    prefix = name + "."
    return {
        key.removeprefix(prefix): value
        for key, value in values.items()
        if key.startswith(prefix)
    }


def _require(value: Any, message: str) -> None:
    if value is None:
        raise SpecError(message)


def _check_lists(values: Mapping[str, Any], *paths: str) -> None:
    """Refuse an empty list that would select nothing (``--only ''``)."""
    for path in paths:
        if values[path] == ():
            flag = path.rpartition(".")[2].replace("_", "-")
            raise SpecError(f"--{flag} needs at least one value")


def _selection(values: Mapping[str, Any]) -> WorkSelection:
    return WorkSelection(**_part(values, "selection"))


def _matching(values: Mapping[str, Any]) -> MatchingSpec:
    return MatchingSpec(**_part(values, "matching"))


def resolve_download(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[DownloadSpec]:
    """Resolve the download options.

    ``flags`` are converted values keyed by option name, as
    ``DOWNLOAD_OPTIONS.values`` returns them; ``defaults`` is a checked
    settings ``defaults:`` block.

    Raises:
        SpecError: No ``--input``, or an empty ``--only`` or ``--providers``.
    """
    values, sources = _resolve_table(DOWNLOAD_OPTIONS, flags, defaults)
    _require(values["input"], "a works CSV is required (--input FILE)")
    _check_lists(values, "selection.only", "matching.providers")
    spec = DownloadSpec(
        input=values["input"],
        selection=_selection(values),
        matching=_matching(values),
        files=FilesSpec(**_part(values, "files")),
        limits=SizeLimits(**_part(values, "limits")),
        output=values["output"],
        run=DownloadControl(**_part(values, "run")),
        decision=DownloadDecisions(**_part(values, "decision")),
    )
    return Resolution(spec, sources)


def resolve_fetch(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[FetchSpec]:
    """Resolve the fetch options.

    Raises:
        SpecError: No ``--entry-id``; not exactly one of ``--url`` and
            ``--provider`` with ``--id``.
    """
    values, sources = _resolve_table(FETCH_OPTIONS, flags, defaults)
    item = FetchItem(**_part(values, "item"))
    _require(item.entry_id, "the work is required (--entry-id ID)")
    pair = (item.provider is not None, item.id is not None)
    if item.url is not None and any(pair):
        raise SpecError("--url excludes --provider and --id")
    if item.url is None and not all(pair):
        raise SpecError("name the item with --url URL, or --provider KEY with --id ID")
    spec = FetchSpec(
        item=item,
        files=FilesSpec(**_part(values, "files")),
        limits=SizeLimits(**_part(values, "limits")),
        output=values["output"],
        run=FetchControl(**_part(values, "run")),
    )
    return Resolution(spec, sources)


def resolve_search(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[SearchSpec]:
    """Resolve the search options; ``defaults.output`` does not apply.

    Raises:
        SpecError: Neither ``--input`` nor ``--title``; ``--creator``,
            ``--year`` or ``--entry-id`` without ``--title``;
            ``--entry-ids`` or ``--limit`` without ``--input``; an empty
            ``--only`` or ``--providers``.
    """
    values, sources = _resolve_table(SEARCH_OPTIONS, flags, defaults)
    query = SearchQuery(**_part(values, "query"))
    if query.input is None and query.title is None:
        raise SpecError("give a works CSV (--input FILE) or a query (--title TEXT)")
    if query.title is None:
        _given_only_with(flags, ("creator", "year", "entry_id"), "--title")
    if query.input is None:
        _given_only_with(flags, ("entry_ids", "limit"), "--input")
    _check_lists(values, "selection.only", "matching.providers")
    spec = SearchSpec(
        query=query,
        selection=_selection(values),
        matching=_matching(values),
        output=values["output"],
        run=RunControl(**_part(values, "run")),
    )
    return Resolution(spec, sources)


def _given_only_with(flags: Mapping[str, Any], names: Sequence[str], need: str) -> None:
    for name in names:
        if name in flags:
            raise SpecError(f"--{name.replace('_', '-')} needs {need}")


def resolve_inspect(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[InspectSpec]:
    """Resolve the inspect options.

    Raises:
        SpecError: Not exactly one of ``--entry-ids`` and ``--provider``
            with ``--id``; ``--entry-ids`` without an output root; an empty
            ``--providers``.
    """
    values, sources = _resolve_table(INSPECT_OPTIONS, flags, defaults)
    stored = bool(values["entry_ids"])
    pair = (values["provider"] is not None, values["id"] is not None)
    if stored == any(pair) or (any(pair) and not all(pair)):
        raise SpecError(
            "give --entry-ids LIST, or --provider KEY with --id ID, not both"
        )
    if stored and values["output"] is None:
        raise SpecError("--entry-ids needs the output root (--output DIR)")
    _check_lists(values, "providers")
    spec = InspectSpec(
        entry_ids=values["entry_ids"],
        provider=values["provider"],
        id=values["id"],
        providers=values["providers"],
        first=values["first"],
        output=values["output"],
        run=RunControl(**_part(values, "run")),
        decision=DecisionModel(**_part(values, "decision")),
    )
    return Resolution(spec, sources)


def resolve_verify(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[VerifySpec]:
    """Resolve the verify options.

    Raises:
        SpecError: ``--first`` or a page-model flag without ``--pages``.
    """
    values, sources = _resolve_table(VERIFY_OPTIONS, flags, defaults)
    if not values["pages"]:
        _given_only_with(flags, ("first", *_PAGE_MODEL_NAMES), "--pages")
    spec = VerifySpec(
        entry_ids=values["entry_ids"],
        pages=values["pages"],
        first=values["first"],
        output=values["output"],
        run=RunControl(**_part(values, "run")),
        decision=DecisionModel(**_part(values, "decision")),
    )
    return Resolution(spec, sources)


def resolve_classify(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[ClassifySpec]:
    """Resolve the classify options; the decision flags left unset stay None."""
    values, sources = _resolve_table(CLASSIFY_OPTIONS, flags, defaults)
    spec = ClassifySpec(
        entry_ids=values["entry_ids"],
        force=values["force"],
        decision=DecisionModel(**_part(values, "decision")),
        output=values["output"],
        run=RunControl(**_part(values, "run")),
    )
    return Resolution(spec, sources)


def resolve_status(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[StatusSpec]:
    """Resolve the status options.

    Raises:
        SpecError: An empty ``--only``.
    """
    values, sources = _resolve_table(STATUS_OPTIONS, flags, defaults)
    _check_lists(values, "only")
    return Resolution(StatusSpec(**values), sources)


def resolve_drop(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[DropSpec]:
    """Resolve the drop options; no option takes a settings default.

    Raises:
        SpecError: No ``--output``, or not exactly one of ``--entry-ids``,
            ``--only`` and ``--all``.
    """
    values, sources = _resolve_table(DROP_OPTIONS, flags, None)
    _require(values["output"], "the output root is required (--output DIR)")
    chosen = (bool(values["entry_ids"]), bool(values["only"]), values["all"])
    if sum(chosen) != 1:
        raise SpecError("give exactly one of --entry-ids, --only and --all")
    return Resolution(DropSpec(**values), sources)


def resolve_providers(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[ProvidersSpec]:
    """Resolve the providers options."""
    values, sources = _resolve_table(PROVIDERS_OPTIONS, flags, defaults)
    return Resolution(ProvidersSpec(**values), sources)


def resolve_ui(
    flags: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
) -> Resolution[UiSpec]:
    """Resolve the ui options; ``--settings`` is read by the caller."""
    _resolve_table(UI_OPTIONS, flags, defaults)
    return Resolution(UiSpec(), {})


# -- views --------------------------------------------------------------------


def effective_spec(
    spec: CommandSpec, sources: Mapping[str, Source] | None = None
) -> dict[str, dict[str, Any]]:
    """Return every field of *spec* (dotted path) with its value and source.

    Values are JSON values; fields missing from *sources* were given by the
    caller and count as flags.
    """
    known = sources or {}
    return {
        path: {
            "value": _jsonable(value),
            "source": known.get(path, Source.FLAG).value,
        }
        for path, value in _flatten(spec).items()
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        path = f"{prefix}{f.name}"
        if is_dataclass(value):
            flat.update(_flatten(value, path + "."))
        else:
            flat[path] = value
    return flat


_UNSET = object()


def _spell(given: dict[str, Any], option: Option, value: Any) -> None:
    """Add *option* with *value* to *given*, or raise when it has no spelling."""
    if value is not None and (option.kind is not Kind.FLAG or value):
        given[option.name] = value
        return
    raise SpecError(
        f"the settings default of {option.name} cannot be undone on the "
        f"command line: {option.flag} has no spelling for {value!r}"
    )


def to_argv(spec: CommandSpec, defaults: Mapping[str, Any] | None = None) -> list[str]:
    """Return arguments that reproduce *spec* with and without *defaults*.

    ``defaults`` is a checked settings ``defaults:`` block. Every value that
    differs from its code default is spelled out, and so is every value the
    defaults would change. ``--settings`` is never part of the result.

    Raises:
        SpecError: The defaults change a value that has no spelling, such
            as the scratch folder of ``inspect`` under a ``defaults.output``.
    """
    table = TABLES[command_of(spec)]
    applicable = _applicable(table, defaults)
    flat = _flatten(spec)
    targeted = [option for option in table if option.target is not None]
    given: dict[str, Any] = {}
    for option in targeted:
        value = flat[option.target or ""]
        if value != option.default:
            _spell(given, option, value)
    for option in targeted:
        if option.name in given:
            continue
        setting = applicable.get(option.name, _UNSET)
        value = flat[option.target or ""]
        if setting is not _UNSET and setting != value:
            _spell(given, option, value)
    return table.to_argv(given)
