"""End to end: a run leaves out a provider whose searches keep timing out."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from chronodownloader import run as core
from chronodownloader.coord import read_snapshot
from chronodownloader.output.store import read_work_status
from chronodownloader.settings import PROVIDER_DEFAULTS, Settings
from chronodownloader.spec import resolve_download, resolve_search
from tests.providers.routed import RoutedSession, jpeg_route, json_route
from tests.run.support import (
    PAGE_HOST,
    PROVIDERS,
    FakeProvider,
    Recorder,
    StubJudge,
    record,
    wiring,
    write_csv,
)

TIMEOUT = 0.6
"""The run's search timeout; a request to :data:`SLOW` outlasts it three times.
Half of it (0.3 s) leaves room for the thread start and the coordination writes
before the request, also on a loaded machine."""

SLOW = "https://slow.fake.test/search"
FAST = "https://fast.fake.test/search"

SKIPPED = "mdz left out of this run's searches until"

COOK = record("mdz", "bsb1", "Le cuisinier françois")
COOK_IA = record("internet_archive", "ia1", "Le cuisinier francois")


class Session(RoutedSession):
    """Serves page images and the search URLs; :data:`SLOW` answers late."""

    def __init__(self) -> None:
        super().__init__(
            {PAGE_HOST: jpeg_route(), SLOW: json_route({}), FAST: json_route({})}
        )

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        if url.startswith(SLOW):
            time.sleep(3 * TIMEOUT)
        return super().request(method, url, **kwargs)


@dataclasses.dataclass
class Stalling(FakeProvider):
    """Asks :data:`SLOW` for titles containing ``slow`` and finds nothing."""

    def search(self, query: Any, *, network: Any, limit: int) -> list[Any]:
        if "slow" not in query.title.lower():
            return super().search(query, network=network, limit=limit)
        self.searches.append(query.title)
        network.request(self.key, "GET", SLOW)
        return []


@dataclasses.dataclass
class Queued(FakeProvider):
    """Asks :data:`FAST` twice per search, so the second waits for pacing."""

    def search(self, query: Any, *, network: Any, limit: int) -> list[Any]:
        self.searches.append(query.title)
        for _ in range(2):
            network.request(self.key, "GET", FAST)
        return []


@dataclasses.dataclass
class Hooked(FakeProvider):
    """Calls *hook* with the title of every search."""

    hook: Callable[[str], None] = lambda _title: None

    def search(self, query: Any, *, network: Any, limit: int) -> list[Any]:
        self.hook(query.title)
        return super().search(query, network=network, limit=limit)


class Clock:
    """The wall clock, moved forward by hand."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def tuned(
    settings: Settings,
    *,
    floor: float = 0.0,
    cooldown: float = 300.0,
    delay_ms: int = 0,
) -> Settings:
    blocks = dict(settings.providers)
    mdz = blocks["mdz"]
    network = dataclasses.replace(
        mdz.network, circuit_breaker_cooldown_s=cooldown, delay_ms=delay_ms
    )
    blocks["mdz"] = dataclasses.replace(
        mdz, min_search_timeout_s=floor, network=network
    )
    return dataclasses.replace(settings, providers=blocks)


def works(tmp_path: Path, *titles: str) -> Path:
    rows = [
        {"entry_id": f"E{index}", "title": title}
        for index, title in enumerate(titles, start=1)
    ]
    return write_csv(tmp_path / "works.csv", rows)


def download(
    settings: Settings,
    csv: Path,
    out: Path,
    providers: list[FakeProvider],
    *,
    clock: Clock | None = None,
    judge: StubJudge | None = None,
) -> Recorder:
    given = {
        "input": csv,
        "output": out,
        "providers": PROVIDERS,
        "search_timeout": TIMEOUT,
        "concurrency": 1,
    }
    resolution = resolve_download(given, settings.defaults)
    plan = core.plan(resolution.spec, settings, sources=resolution.sources)
    session = Session()
    wired = wiring(providers, judge, session_factory=lambda: session)
    if clock is not None:
        wired = dataclasses.replace(wired, clock=clock)
    events = Recorder()
    core.run(plan, events, wiring=wired)
    return events


def skips(events: Recorder) -> list[str]:
    return [warning for warning in events.warnings if SKIPPED in warning]


def test_three_timeouts_in_a_row_leave_the_provider_out(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz = Stalling("mdz")
    ia = FakeProvider("internet_archive", records={"cuisinier": [COOK_IA]})
    titles = ("Slow 1", "Slow 2", "Slow 3", "Slow cuisinier 4", "Slow 5")
    judge = StubJudge({"internet_archive:ia1": 0.97})

    events = download(
        tuned(settings), works(tmp_path, *titles), out, [mdz, ia], judge=judge
    )

    assert mdz.searches == ["Slow 1", "Slow 2", "Slow 3"]
    assert len(ia.searches) == 5
    (warning,) = skips(events)
    assert "3 searches in a row passed their deadline" in warning
    assert "E5: mdz search left out after repeated timeouts" in events.warnings
    rows = {row["entry_id"]: row for row in read_work_status(out)}
    assert (rows["E4"]["status"], rows["E4"]["selected_provider"]) == (
        "complete",
        "internet_archive",
    )
    assert rows["E5"]["status"] == "failed"
    assert "search failed on 1 provider(s)" in rows["E5"]["status_reason"]


def test_a_deadline_passed_waiting_for_pacing_does_not_count(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = Queued("mdz"), FakeProvider("internet_archive")
    csv = works(tmp_path, *(f"Book {n}" for n in range(1, 6)))

    events = download(tuned(settings, delay_ms=1000), csv, out, [mdz, ia])

    assert len(mdz.searches) == 5
    assert sum("mdz search timed out" in w for w in events.warnings) == 5
    assert skips(events) == []


def test_a_search_in_time_resets_the_count(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    mdz, ia = Stalling("mdz"), FakeProvider("internet_archive")
    titles = ("Slow 1", "Slow 2", "Quick 3", "Slow 4", "Slow 5")

    events = download(tuned(settings), works(tmp_path, *titles), out, [mdz, ia])

    assert mdz.searches == list(titles)
    assert skips(events) == []


def test_a_provider_with_a_search_floor_is_never_left_out(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    assert PROVIDER_DEFAULTS["bnf_gallica"].min_search_timeout_s > 0
    mdz, ia = Stalling("mdz"), FakeProvider("internet_archive")
    csv = works(tmp_path, *(f"Slow book {n}" for n in range(1, 6)))

    events = download(tuned(settings, floor=TIMEOUT * 1.2), csv, out, [mdz, ia])

    assert len(mdz.searches) == 5
    assert sum("mdz search timed out" in w for w in events.warnings) == 5
    assert skips(events) == []


def test_the_provider_is_searched_again_after_the_cooldown(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    clock = Clock()

    def pass_the_cooldown(title: str) -> None:
        if title == "Wait 4":
            clock.offset += 31.0

    mdz = Stalling("mdz")
    ia = Hooked("internet_archive", hook=pass_the_cooldown)
    csv = works(tmp_path, "Slow 1", "Slow 2", "Slow 3", "Wait 4", "Slow 5")

    events = download(tuned(settings, cooldown=30.0), csv, out, [mdz, ia], clock=clock)

    assert mdz.searches == ["Slow 1", "Slow 2", "Slow 3", "Slow 5"]
    assert len(skips(events)) == 1


def test_downloads_from_a_left_out_provider_go_on(
    tmp_path: Path, settings: Settings, out: Path
) -> None:
    settings = tuned(settings)
    judge = StubJudge({"mdz:bsb1": 0.97})
    csv = works(tmp_path, "Slow 1", "Slow 2", "Slow 3", "Le cuisinier francois")
    searched = resolve_search(
        {"input": csv, "output": out, "providers": PROVIDERS, "entry_ids": ("E4",)},
        settings.defaults,
    )
    core.run(
        core.plan(searched.spec, settings, sources=searched.sources),
        Recorder(),
        wiring=wiring(
            [
                FakeProvider("mdz", records={"cuisinier": [COOK]}),
                FakeProvider("internet_archive"),
            ],
            judge,
        ),
    )
    mdz = Stalling("mdz", records={"cuisinier": [COOK]})

    events = download(
        settings, csv, out, [mdz, FakeProvider("internet_archive")], judge=judge
    )

    assert len(skips(events)) == 1
    assert [source for source, _present in mdz.downloads] == ["bsb1"]
    (row,) = read_work_status(out, ["E4"])
    assert (row["status"], row["selected_provider"]) == ("complete", "mdz")
    assert "mdz" not in read_snapshot(settings.state_directory()).breakers
