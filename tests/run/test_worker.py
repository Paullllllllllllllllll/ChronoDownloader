"""Worker helpers: stored records reused, judgments stored and read back."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from types import MappingProxyType
from typing import Any

from chronodownloader._runtime import Services
from chronodownloader._worker import (
    MatchJob,
    answer_of,
    judgment_of,
    match_work,
    unmask,
)
from chronodownloader.matching.answers import WorkAnswer
from chronodownloader.net import Cancelled, Network
from chronodownloader.output.store import WorkInput
from chronodownloader.providers.base import SearchRecord
from chronodownloader.providers.registry import host_key
from chronodownloader.settings import Settings
from tests.providers.routed import RoutedSession, json_route
from tests.run.support import FakeProvider, wiring

MASKED = "https://api.europeana.eu/record/v2/9200/abc.json?wskey=***"


def test_unmask_drops_masked_credentials_from_urls_and_raw_fields() -> None:
    record = SearchRecord(
        provider="europeana",
        source_id="/9200/abc",
        title="Kochbuch",
        item_url=MASKED,
        iiif_manifest="https://iiif.europeana.eu/x/manifest?wskey=***&format=3",
        raw=MappingProxyType(
            {
                "link": MASKED,
                "nested": {"urls": [f"{MASKED}&profile=rich", "plain"]},
                "count": 3,
            }
        ),
    )

    clean = unmask(record)

    assert clean.item_url == "https://api.europeana.eu/record/v2/9200/abc.json"
    assert clean.iiif_manifest == "https://iiif.europeana.eu/x/manifest?format=3"
    assert clean.raw["link"] == "https://api.europeana.eu/record/v2/9200/abc.json"
    assert clean.raw["nested"] == {
        "urls": [
            "https://api.europeana.eu/record/v2/9200/abc.json?profile=rich",
            "plain",
        ]
    }
    assert clean.raw["count"] == 3
    assert "***" not in json.dumps(dict(clean.raw))


def test_a_stored_judgment_reads_back_only_for_its_model() -> None:
    answer = WorkAnswer(
        same_work=0.93,
        relation="other_edition",
        relation_probabilities={"other_edition": 0.8, "same_edition": 0.2},
        confidence=0.7,
        model="typesafe/jev-1.14-20261001",
        requested_model="typesafe/jev-1.14",
    )

    judgment = judgment_of(answer, "accept")

    assert judgment.decision == "accept"
    assert answer_of(judgment, "typesafe/jev-1.14") == answer
    assert answer_of(judgment, "typesafe/jev-2") is None
    assert answer_of(None, "typesafe/jev-1.14") is None


def test_the_run_network_resolves_hosts_through_the_registry(
    settings: Settings,
) -> None:
    with Services.open(settings, wiring=wiring([])) as services:
        assert services.network._host_key is host_key


@dataclasses.dataclass
class Overdue(FakeProvider):
    """Sends its request only after the deadline, then finds nothing."""

    cancelled: list[str] = dataclasses.field(default_factory=list)

    def search(self, query: Any, *, network: Network, limit: int) -> list[Any]:
        self.searches.append(f"{query.title} / {query.creator}")
        time.sleep(0.3)
        try:
            network.request("mdz", "GET", "https://api.fake.test/search")
        except Cancelled as exc:
            self.cancelled.append(str(exc))
        return []


def test_an_abandoned_search_sends_nothing_and_tries_no_other_variant(
    settings: Settings,
) -> None:
    mdz = Overdue("mdz")
    session = RoutedSession()
    work = WorkInput("E1", "Kochbuch", creator="Rumpolt")
    job = MatchJob(work, (), "judge", ("mdz",), 5, search_timeout=0.05)
    services = Services.open(
        settings, wiring=wiring([mdz], session_factory=lambda: session)
    )
    with services:
        match = match_work(services, job)
        for thread in threading.enumerate():
            if thread.name == "deadline-call":
                thread.join(5.0)

    assert match.timed_out == match.searched == ()
    assert match.failure is not None
    assert mdz.searches == ["Kochbuch / Rumpolt"]
    assert len(mdz.cancelled) == 1
    assert session.calls == []


@dataclasses.dataclass
class TitleOnly(FakeProvider):
    """Sends the title alone, as BNE does, and finds nothing."""

    def search(self, query: Any, *, network: Network, limit: int) -> list[Any]:
        self.searches.append(f"{query.title} / {query.creator}")
        network.request(
            "mdz", "GET", "https://api.fake.test/search", params={"q": query.title}
        )
        return []


def test_the_search_without_the_creator_repeats_no_request(
    settings: Settings,
) -> None:
    mdz = TitleOnly("mdz")
    session = RoutedSession({"https://api.fake.test/search": json_route({})})
    work = WorkInput("E1", "Kochbuch", creator="Rumpolt")
    job = MatchJob(work, (), "judge", ("mdz",), 5, search_timeout=0)
    services = Services.open(
        settings, wiring=wiring([mdz], session_factory=lambda: session)
    )
    with services:
        match_work(services, job)

    assert mdz.searches == ["Kochbuch / Rumpolt", "Kochbuch / None"]
    assert len(session.calls) == 1
