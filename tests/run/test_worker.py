"""Worker helpers: stored records reused, judgments stored and read back."""

from __future__ import annotations

import json
from types import MappingProxyType

from chronodownloader._runtime import Services
from chronodownloader._worker import answer_of, judgment_of, unmask
from chronodownloader.matching.answers import WorkAnswer
from chronodownloader.providers.base import SearchRecord
from chronodownloader.providers.registry import host_key
from chronodownloader.settings import Settings
from tests.run.support import wiring

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
