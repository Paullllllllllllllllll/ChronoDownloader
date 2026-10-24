"""Fakes for the end-to-end tests of the run core: providers, judge, events.

:class:`FakeProvider` answers searches from canned records and stages page
images (and, when offered, a PDF) without any request; it honors the
request's present items, page limit and budget like a real adapter, and
lists its first pages under :data:`PAGE_HOST`. :class:`StubJudge` answers
work questions from a table keyed by item and page checks from a table
keyed by the sought title. :func:`wiring` plugs both into
:class:`~chronodownloader._runtime.Wiring` with a session that serves the
page images; :func:`run_command` resolves, plans and runs a command with
them. The settings of :func:`settings_file` put the state folder into the
test's temporary directory and drop provider pacing.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chronodownloader import run as core
from chronodownloader._runtime import Wiring
from chronodownloader.events import (
    CandidateFound,
    CandidateJudged,
    Downloaded,
    StatusChanged,
    WorkStarted,
)
from chronodownloader.matching.answers import PageAnswer, WorkAnswer
from chronodownloader.net import Network
from chronodownloader.providers.base import (
    RENDERING_ITEM,
    Capabilities,
    Completeness,
    DownloadRequest,
    DownloadResult,
    PageRef,
    SearchQuery,
    SearchRecord,
    StagedFile,
    UnsupportedCapability,
    page_item,
    source_id_of,
)
from chronodownloader.run import RunResult
from chronodownloader.settings import Settings
from tests.iiif.support import JPEG, PDF
from tests.providers.routed import RoutedSession, jpeg_route

PROVIDERS = ("mdz", "internet_archive")
"""The provider keys the fakes stand in for, in preference order."""

PAGE_HOST = "https://pages.fake.test/"
"""Where :meth:`FakeProvider.pages` points; the default session serves it."""


def settings_file(tmp_path: Path, providers: Sequence[str] = PROVIDERS) -> Path:
    """Write test settings: state folder under *tmp_path*, no pacing, and
    *providers* as ``defaults.providers``."""
    lines = [
        f"state_dir: '{(tmp_path / 'state').as_posix()}'",
        "providers:",
    ]
    for key in PROVIDERS:
        lines += [
            f"  {key}:",
            "    slots: 4",
            "    network: {delay_ms: 0, jitter_ms: 0, max_attempts: 1}",
        ]
    lines += ["defaults:", f"  providers: [{', '.join(providers)}]"]
    path = tmp_path / "settings.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_settings(tmp_path: Path) -> Settings:
    """Return settings equal to :func:`settings_file`'s, without a file."""
    return Settings.from_mapping(
        {
            "state_dir": (tmp_path / "state").as_posix(),
            "providers": {
                key: {"slots": 4, "network": {"delay_ms": 0, "jitter_ms": 0}}
                for key in PROVIDERS
            },
        }
    )


def write_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> Path:
    """Write *rows* as a works CSV with the union of their columns."""
    columns: list[str] = []
    for row in rows:
        columns += [name for name in row if name not in columns]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def run_command(
    resolve: Callable[..., Any],
    settings: Settings,
    flags: Mapping[str, Any],
    providers: Iterable[FakeProvider] = (),
    judge: StubJudge | None = None,
    events: Recorder | None = None,
) -> RunResult:
    """Resolve *flags*, plan and run the command with the fakes."""
    resolution = resolve(dict(flags), settings.defaults)
    run_plan = core.plan(resolution.spec, settings, sources=resolution.sources)
    return core.run(run_plan, events or Recorder(), wiring=wiring(providers, judge))


def record(provider: str, source_id: str, title: str, **fields: Any) -> SearchRecord:
    """Return a search record of *provider*."""
    return SearchRecord(provider=provider, source_id=source_id, title=title, **fields)


@dataclass
class FakeProvider:
    """A provider without network: canned records, staged page images.

    *records* are returned for a query whose title contains the key of the
    mapping (case-insensitive); *page_counts* gives each item's page count
    (default 3); *failing* names page items whose download fails;
    *pdf* lists the items that offer a rendering. ``searches``,
    ``downloads`` and ``sampled`` (item, pages asked) record the calls.
    """

    key: str
    records: Mapping[str, Sequence[SearchRecord]] = field(default_factory=dict)
    page_counts: Mapping[str, int] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    pdf: set[str] = field(default_factory=set)
    capabilities: Capabilities = field(default_factory=Capabilities)
    search_error: Exception | None = None
    searches: list[str] = field(default_factory=list)
    downloads: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    sampled: list[tuple[str, int]] = field(default_factory=list)

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        self.searches.append(query.title)
        if self.search_error is not None:
            raise self.search_error
        found: list[SearchRecord] = []
        for needle, records in self.records.items():
            if needle.lower() in query.title.lower():
                found += records
        return found[:limit]

    def _stage(
        self, staging_dir: Path, name: str, data: bytes, kind: Any, item: str
    ) -> StagedFile:
        path = staging_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        content_type = "application/pdf" if kind == "pdf" else "image/jpeg"
        return StagedFile(name, kind, item, len(data), content_type, f"fake://{name}")

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        if not self.capabilities.downloads:
            raise UnsupportedCapability(self.key, "download")
        source_id = source_id_of(record_or_id)
        self.downloads.append((source_id, request.present))
        staging_dir.mkdir(parents=True, exist_ok=True)
        files: list[StagedFile] = []
        stopped = None
        parts: list[Completeness] = []
        offers_pdf = source_id in self.pdf and request.format != "images"
        if offers_pdf:
            rendering = (RENDERING_ITEM,)
            present: tuple[str, ...] = ()
            if RENDERING_ITEM in request.present:
                present = rendering
            elif request.budget is None or request.budget.allows("pdf"):
                staged = self._stage(
                    staging_dir, "rendering.pdf", PDF, "pdf", RENDERING_ITEM
                )
                files.append(staged)
                if request.budget is not None:
                    request.budget.charge("pdf", staged.size)
                present = rendering
            else:
                stopped = "budget"
            parts.append(Completeness("pdf", rendering, present, available=1))
        if not offers_pdf or request.format == "both":
            count = self.page_counts.get(source_id, 3)
            limit = request.max_pages or count
            expected = tuple(
                page_item(index) for index in range(1, min(count, limit) + 1)
            )
            got: list[str] = []
            causes: dict[str, str] = {}
            for item in expected:
                if item in request.present:
                    got.append(item)
                    continue
                if request.budget is not None and not request.budget.allows("image"):
                    stopped = "budget"
                    break
                if item in self.failing:
                    causes[item] = "transient"
                    continue
                staged = self._stage(staging_dir, f"{item}.jpg", JPEG, "image", item)
                files.append(staged)
                if request.budget is not None:
                    request.budget.charge("image", staged.size)
                got.append(item)
            parts.append(
                Completeness(
                    "images",
                    expected,
                    tuple(got),
                    page_limit=request.max_pages or None,
                    available=count,
                    causes=causes,
                )
            )
        return DownloadResult(
            self.key,
            source_id,
            request.format,
            tuple(parts),
            tuple(files),
            stopped=stopped,
        )

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the item's first pages under :data:`PAGE_HOST`, which the
        session of :func:`wiring` serves."""
        if not self.capabilities.downloads:
            raise UnsupportedCapability(self.key, "pages")
        source_id = source_id_of(record_or_id)
        self.sampled.append((source_id, first_n))
        count = min(self.page_counts.get(source_id, 3), first_n)
        return [
            PageRef(
                page_item(index),
                index,
                (f"{PAGE_HOST}{self.key}/{source_id}/{index}.jpg",),
            )
            for index in range(1, count + 1)
        ]


@dataclass
class StubJudge:
    """Answers ``judge_work`` from *answers*, keyed by ``provider:source_id``.

    A value is a ``same_work`` probability (relation ``same_edition``), a
    :class:`WorkAnswer`, or an exception to raise; unknown items get
    *default*. ``calls`` records the items asked about. ``judge_pages``
    answers from *pages*, keyed by the sought title, in the same way (a
    ``title_page`` probability, a :class:`PageAnswer` or an exception;
    *default_page* otherwise); ``page_calls`` records the sought title and
    the number of images.
    """

    answers: dict[str, float | WorkAnswer | Exception] = field(default_factory=dict)
    default: float = 0.05
    calls: list[str] = field(default_factory=list)
    pages: dict[str, float | PageAnswer | Exception] = field(default_factory=dict)
    default_page: float = 0.9
    page_calls: list[tuple[str, int]] = field(default_factory=list)

    def judge_pages(
        self, sought: SearchQuery, page_images: Sequence[bytes]
    ) -> PageAnswer:
        self.page_calls.append((sought.title, len(page_images)))
        value = self.pages.get(sought.title, self.default_page)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, PageAnswer):
            return value
        return PageAnswer(
            title_page=value,
            imprint_year_matches=None if sought.year is None else value,
            title_page_type="title_page" if value >= 0.5 else "other",
            pages=len(page_images),
            model="openai/gpt-6-luna-decisions-20261001",
            requested_model="openai/gpt-6-luna-decisions",
        )

    def judge_work(self, sought: SearchQuery, candidate: SearchRecord) -> WorkAnswer:
        key = f"{candidate.provider}:{candidate.source_id}"
        self.calls.append(key)
        value = self.answers.get(key, self.default)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, WorkAnswer):
            return value
        relation = "same_edition" if value >= 0.5 else "different_work"
        return WorkAnswer(
            same_work=value,
            relation=relation,
            relation_probabilities={relation: 0.95},
            model="typesafe/jev-1.14-20261001",
            requested_model="typesafe/jev-1.14",
        )


def wiring(
    providers: Iterable[FakeProvider],
    judge: StubJudge | None = None,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> Wiring:
    """Return a wiring with the fake *providers* and *judge*; the default
    session serves page images under :data:`PAGE_HOST` and nothing else."""
    factories = {provider.key: _factory(provider) for provider in providers}
    stub = judge or StubJudge()
    if session_factory is None:
        session_factory = _page_session(RoutedSession({PAGE_HOST: jpeg_route()}))
    return Wiring(
        providers=factories,
        judge=lambda _network, _settings: stub,
        session_factory=session_factory,
        sleep=lambda seconds: time.sleep(min(seconds, 0.02)),
    )


def _factory(provider: FakeProvider) -> Callable[[Settings], Any]:
    return lambda _settings: provider


def _page_session(session: RoutedSession) -> Callable[[], Any]:
    return lambda: session


@dataclass
class Recorder:
    """Run events recorded in order; *interrupt* raises Ctrl+C on an event.

    *interrupt* receives each event and returns True to raise
    ``KeyboardInterrupt`` from it, as a Ctrl+C on the main thread would.
    """

    events: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    interrupt: Callable[[Any], bool] | None = None

    def _note(self, event: Any) -> None:
        self.events.append(event)
        if self.interrupt is not None and self.interrupt(event):
            raise KeyboardInterrupt

    def work_started(self, event: WorkStarted) -> None:
        self._note(event)

    def candidate_found(self, event: CandidateFound) -> None:
        self._note(event)

    def candidate_judged(self, event: CandidateJudged) -> None:
        self._note(event)

    def downloaded(self, event: Downloaded) -> None:
        self._note(event)

    def status_changed(self, event: StatusChanged) -> None:
        self._note(event)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def statuses(self) -> dict[str, str]:
        """Return the last status of each work."""
        return {
            event.entry_id: event.status
            for event in self.events
            if isinstance(event, StatusChanged)
        }
