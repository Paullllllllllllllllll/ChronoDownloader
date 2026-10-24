"""The decision model's answers as the matching modules share them.

A :class:`WorkAnswer` answers, for one candidate, the questions
``same_work`` (a probability) and ``match`` (the five-way relation with its
probabilities). A :class:`PageAnswer` answers the page check over the first
pages of one candidate. ``model`` is the dated model id the endpoint
reports, ``requested_model`` the id the request named.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "PAGE_TYPES",
    "RELATIONS",
    "PageAnswer",
    "PageType",
    "Relation",
    "WorkAnswer",
]

type Relation = Literal[
    "same_edition",
    "other_edition",
    "translation_or_adaptation",
    "different_work",
    "insufficient",
]

RELATIONS: Final[tuple[Relation, ...]] = (
    "same_edition",
    "other_edition",
    "translation_or_adaptation",
    "different_work",
    "insufficient",
)

type PageType = Literal[
    "cover_or_binding",
    "blank",
    "half_title",
    "title_page",
    "frontispiece_or_plate",
    "front_matter",
    "table_of_contents",
    "recipe_text",
    "other_body_text",
    "index",
    "other",
]

PAGE_TYPES: Final[tuple[PageType, ...]] = (
    "cover_or_binding",
    "blank",
    "half_title",
    "title_page",
    "frontispiece_or_plate",
    "front_matter",
    "table_of_contents",
    "recipe_text",
    "other_body_text",
    "index",
    "other",
)


def _frozen(values: Mapping[str, float]) -> Mapping[str, float]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class WorkAnswer:
    """The decision model's answer on one candidate."""

    same_work: float
    relation: Relation
    relation_probabilities: Mapping[str, float] = field(
        default_factory=dict, hash=False
    )
    confidence: float | None = None
    model: str = ""
    requested_model: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.same_work <= 1.0:
            raise ValueError(f"same_work must lie in [0, 1], got {self.same_work}")
        if self.relation not in RELATIONS:
            raise ValueError(f"unknown relation {self.relation!r}")
        object.__setattr__(
            self, "relation_probabilities", _frozen(self.relation_probabilities)
        )


@dataclass(frozen=True)
class PageAnswer:
    """The decision model's page check over the first pages of a candidate.

    ``title_page`` is the probability that one of the pages is the sought
    work's title page, ``imprint_year_matches`` that its imprint year agrees
    with the sought year; ``title_page_type`` is the kind of page that best
    shows the title, with its probabilities.
    """

    title_page: float
    imprint_year_matches: float | None
    title_page_type: PageType
    page_type_probabilities: Mapping[str, float] = field(
        default_factory=dict, hash=False
    )
    pages: int = 0
    model: str = ""
    requested_model: str = ""

    def __post_init__(self) -> None:
        if self.title_page_type not in PAGE_TYPES:
            raise ValueError(f"unknown page type {self.title_page_type!r}")
        object.__setattr__(
            self, "page_type_probabilities", _frozen(self.page_type_probabilities)
        )
