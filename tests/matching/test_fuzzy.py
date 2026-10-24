"""The text score: v1.30.3 values on the jev_eval fixture and the ported tests.

The assertions of the scorer and year-penalty classes are those of v1.30.3
``tests/unit/test_matching.py``, ``test_matching_semantics.py`` and
``test_year_tolerance.py``, adapted to the v1.0.0 API (records instead of
``SearchResult``, the candidate year as an integer, the tolerance as an
argument instead of configuration).
"""

from __future__ import annotations

import difflib

import pytest

from chronodownloader.matching.fuzzy import (
    CREATOR_WEIGHT,
    MAX_YEAR_PENALTY,
    YEAR_PENALTY_PER_YEAR,
    YEAR_TOLERANCE,
    candidate_year,
    creator_score,
    normalize_text,
    score,
    title_score,
    token_set_ratio,
    year_penalty,
)
from chronodownloader.providers._query import parse_year
from chronodownloader.providers.base import SearchQuery, SearchRecord

from .jev_fixture import pairs, query, record

HARVEST_YEAR_TOLERANCE = 2
"""``selection.year_tolerance`` of the machine config the fixture was
harvested with; the fixture's ``year_penalty_if_year_given`` used it."""


def _record(
    title: str = "Le Cuisinier francois",
    *,
    creators: tuple[str, ...] = (),
    date: str | None = None,
    year: int | None = None,
    item_url: str | None = None,
    iiif_manifest: str | None = None,
) -> SearchRecord:
    return SearchRecord(
        provider="p1",
        source_id="s1",
        title=title,
        creators=creators,
        date=date,
        year=year,
        item_url=item_url,
        iiif_manifest=iiif_manifest,
    )


def test_constants_are_the_v1_30_3_defaults() -> None:
    assert CREATOR_WEIGHT == 0.2
    assert YEAR_TOLERANCE == 15
    assert (YEAR_PENALTY_PER_YEAR, MAX_YEAR_PENALTY) == (0.5, 25.0)


class TestFixtureScores:
    """Every stored v1.30.3 score is recomputed from the pair's fields."""

    def test_every_pair_reproduces_its_stored_scores(self) -> None:
        mismatches = []
        for pair in pairs():
            stored = pair["text_score"]["cd_scores"]
            got = score(query(pair, with_year=False), record(pair))
            computed = {
                "score": got.title,
                "creator_score": got.creator,
                "creator_bonus": got.creator_bonus,
                "boost": got.boost,
                "year_penalty": got.year_penalty,
                "total": got.total,
            }
            expected = {key: stored[key] for key in computed}
            if computed != expected:
                mismatches.append((pair["pair_id"], computed, expected))
        assert len(pairs()) == 294
        assert mismatches == []

    def test_every_pair_reproduces_its_year_penalty(self) -> None:
        mismatches = [
            pair["pair_id"]
            for pair in pairs()
            if year_penalty(
                pair["work"]["year"],
                candidate_year(record(pair)),
                tolerance=HARVEST_YEAR_TOLERANCE,
            )
            != pair["text_score"]["year_penalty_if_year_given"]
        ]
        assert mismatches == []

    def test_candidate_years_agree_with_the_v1_30_3_reading(self) -> None:
        for pair in pairs():
            assert (
                parse_year(pair["candidate"]["date"])
                == (pair["candidate"]["candidate_year"])
            )

    def test_the_total_with_a_year_subtracts_the_penalty(self) -> None:
        for pair in pairs():
            stored = pair["text_score"]["cd_scores"]
            got = score(query(pair), record(pair))
            assert got.total == pytest.approx(stored["total"] - got.year_penalty)


class TestNormalizeText:
    def test_folds_case_punctuation_and_whitespace(self) -> None:
        assert normalize_text("HELLO WORLD") == "hello world"
        assert normalize_text("Hello, World!") == "hello world"
        assert normalize_text("hello    world") == "hello world"
        assert normalize_text("  hello world  ") == "hello world"
        assert normalize_text("Café Résumé") == "cafe resume"
        assert normalize_text("hello\tworld\n") == "hello world"

    def test_empty_inputs(self) -> None:
        assert normalize_text("") == ""
        assert normalize_text(None) == ""
        assert normalize_text("!@#$%^&*()") == ""

    def test_transliterates_non_decomposable_letters(self) -> None:
        assert normalize_text("Kogebog for Bønder") == "kogebog for bonder"
        assert normalize_text("Straße") == "strasse"
        assert normalize_text("Encyclopædia") == "encyclopaedia"
        assert normalize_text("Œuvres complètes") == "oeuvres completes"
        assert normalize_text("Kucharz doskonały") == "kucharz doskonaly"

    def test_non_latin_titles_keep_their_script(self) -> None:
        assert normalize_text("Домострой") != ""
        assert token_set_ratio("Домострой", "Домострой") == 100
        assert normalize_text("Подарокъ молодымъ хозяйкамъ 1861") == (
            "подарокъ молодымъ хозяикамъ 1861"
        )
        assert normalize_text("Μαγειρική τέχνη τόμος 2") == "μαγειρικη τεχνη τομος 2"
        assert (
            title_score(
                "Подарокъ молодымъ хозяйкамъ", "Подарокъ молодымъ хозяйкамъ 1861"
            )
            > 80
        )

    def test_latin_titles_keep_the_ascii_form(self) -> None:
        assert normalize_text("Cookery for Bønder, 1861") == "cookery for bonder 1861"
        assert normalize_text("Œuvres, vol. 2") == "oeuvres vol 2"
        assert normalize_text("1861") == "1861"
        assert normalize_text("Подарокъ / Le Cadeau") == "подарокъ le cadeau"


class TestTokenSetRatio:
    SHORT_AND_FULL_TITLES = (
        (
            "Ein new Kochbuch",
            "Ein new Kochbuch, Das ist Ein grundtliche beschreibung wie man "
            "recht vnd wol kochen soll",
        ),
        (
            "Le cuisinier françois",
            "Le cuisinier françois, enseignant la maniere de bien apprester et "
            "assaisonner toutes sortes de viandes",
        ),
        (
            "The Art of Cookery",
            "The Art of Cookery, Made Plain and Easy; Which Far Exceeds Any "
            "Thing of the Kind Ever Yet Published",
        ),
    )

    def test_basic_values(self) -> None:
        assert token_set_ratio("hello world", "hello world") == 100
        assert token_set_ratio("hello world", "world hello") == 100
        assert token_set_ratio("hello world test", "hello world") == 100
        assert token_set_ratio("abc def", "xyz uvw") < 50
        assert token_set_ratio("", "hello world") == 0
        assert token_set_ratio("hello world", "") == 0

    def test_short_title_is_a_perfect_match_for_its_full_imprint(self) -> None:
        for short, full in self.SHORT_AND_FULL_TITLES:
            assert token_set_ratio(short, full) == 100, short
            assert token_set_ratio(full, short) == 100, short
            assert title_score(short, full) == 100, short

    def test_disjoint_titles_stay_low(self) -> None:
        assert title_score("The Art of Cooking", "History of France") < 50
        assert title_score("Le Cuisinier francois", "A Treatise on Naval Gunnery") < 35

    def test_no_shared_tokens_reduces_to_the_sort_ratio(self) -> None:
        for a, b in (("Almanach", "N/A"), ("abc def", "xyz uvw")):
            sorted_a = " ".join(sorted(normalize_text(a).split()))
            sorted_b = " ".join(sorted(normalize_text(b).split()))
            expected = int(
                round(difflib.SequenceMatcher(None, sorted_a, sorted_b).ratio() * 100)
            )
            assert token_set_ratio(a, b) == expected

    def test_title_scores(self) -> None:
        assert title_score("The Art of Cooking", "The Art of Cooking") == 100
        assert title_score("The Art of Cooking", "Art of Cooking") >= 80
        assert title_score("world hello", "hello world") == 100


class TestCreatorScore:
    def test_values(self) -> None:
        assert creator_score("John Smith", ["John Smith"]) == 100
        assert creator_score("John Smith", ["Jane Doe", "John Smith", "Bob"]) == 100
        assert 50 <= creator_score("John Smith", ["J. Smith"]) <= 100
        assert creator_score("John Smith", ["Jane Doe", "Bob Jones"]) < 50
        assert creator_score("Auguste Escoffier", ["Escoffier, Auguste"]) == 100

    def test_empty_sides_score_zero(self) -> None:
        assert creator_score(None, ["John Smith"]) == 0
        assert creator_score("", ["John Smith"]) == 0
        assert creator_score("John Smith", None) == 0
        assert creator_score("John Smith", []) == 0


class TestScore:
    def test_title_score_ignores_a_missing_creator(self) -> None:
        got = score(
            SearchQuery("The Art of Cooking", "Some Author"),
            _record("The Art of Cooking"),
        )
        assert got.title == 100.0

    def test_creator_is_a_bonus_never_a_penalty(self) -> None:
        q = SearchQuery("Ancient Recipes", "Jane Doe")
        with_creator = score(q, _record("Ancient Recipes", creators=("Jane Doe",)))
        without = score(q, _record("Ancient Recipes"))
        assert with_creator.title == without.title
        assert with_creator.total > without.total
        assert without.total >= without.title
        assert with_creator.creator_bonus == pytest.approx(20.0)

    def test_titleless_record_scores_zero(self) -> None:
        for title in ("Almanach", "Analecta", "Manual de arte"):
            assert 35 <= title_score(title, "N/A") <= 36
            assert score(SearchQuery(title), _record("")).title == 0.0

    def test_query_echo_scores_a_perfect_match(self) -> None:
        q = "Le Cuisinier royal et bourgeois"
        assert score(SearchQuery(q), _record(q)).title == 100.0

    def test_boost_for_manifest_and_item_url(self) -> None:
        got = score(
            SearchQuery("Le Cuisinier francois"),
            _record(item_url="https://x/i", iiif_manifest="https://x/m"),
        )
        assert got.boost == 3.5
        assert got.total == pytest.approx(103.5)

    def test_dormant_without_a_query_year(self) -> None:
        got = score(SearchQuery("Le Cuisinier francois"), _record(date="2019"))
        assert got.year_penalty == 0.0
        assert got.candidate_year == 2019
        assert got.total == pytest.approx(got.title + got.creator_bonus + got.boost)

    def test_penalty_is_subtracted_from_the_total(self) -> None:
        got = score(
            SearchQuery("Le Cuisinier francois", year=1651), _record(date="2019")
        )
        assert got.year_penalty == MAX_YEAR_PENALTY
        assert got.title == 100.0
        assert got.total == pytest.approx(
            got.title + got.creator_bonus + got.boost - MAX_YEAR_PENALTY
        )

    def test_record_year_wins_over_the_date(self) -> None:
        rec = _record(date="1651", year=1700)
        assert candidate_year(rec) == 1700
        assert candidate_year(_record(date="[ca. 1890]")) == 1890
        assert candidate_year(_record(date="s.d.")) is None

    def test_undated_candidate_never_outranks_a_matching_one(self) -> None:
        q = SearchQuery("Le Cuisinier francois", year=1651)
        dated = score(q, _record(date="1651"))
        undated = score(q, _record(date=None))
        assert undated.total <= dated.total
        assert undated.year_penalty == dated.year_penalty == 0.0

    def test_period_edition_outranks_a_reprint(self) -> None:
        q = SearchQuery("Le Cuisinier francois", year=1651)
        reprint = score(
            q,
            _record(
                date="2019",
                item_url="https://example.org/reprint",
                iiif_manifest="https://example.org/reprint/manifest.json",
            ),
        )
        period = score(q, _record(date="1651"))
        assert period.total > reprint.total


class TestYearPenalty:
    @pytest.mark.parametrize("candidate", [1640, 1645, 1650, 1655, 1660])
    def test_inside_window_is_free(self, candidate: int) -> None:
        assert year_penalty(1650, candidate, tolerance=10) == 0.0

    def test_boundary_is_inclusive(self) -> None:
        assert year_penalty(1650, 1660, tolerance=10) == 0.0
        assert year_penalty(1650, 1661, tolerance=10) == pytest.approx(
            YEAR_PENALTY_PER_YEAR
        )

    def test_grows_with_distance_beyond_the_window(self) -> None:
        penalties = [
            year_penalty(1650, 1650 + d, tolerance=10) for d in (11, 20, 30, 40)
        ]
        assert penalties == sorted(penalties)
        assert penalties[0] < penalties[-1]
        assert year_penalty(1650, 1670, tolerance=10) == pytest.approx(
            10 * YEAR_PENALTY_PER_YEAR
        )

    def test_is_capped(self) -> None:
        assert year_penalty(1651, 2019, tolerance=10) == MAX_YEAR_PENALTY
        assert year_penalty(1651, 2100, tolerance=10) == MAX_YEAR_PENALTY
        span = int(MAX_YEAR_PENALTY / YEAR_PENALTY_PER_YEAR)
        assert year_penalty(1650, 1650 + 10 + span - 1, tolerance=10) < MAX_YEAR_PENALTY
        assert year_penalty(1650, 1650 + 10 + span, tolerance=10) == MAX_YEAR_PENALTY

    def test_is_symmetric(self) -> None:
        assert year_penalty(1650, 1600, tolerance=5) == year_penalty(
            1650, 1700, tolerance=5
        )

    def test_zero_and_negative_tolerance(self) -> None:
        for tolerance in (0, -5):
            assert year_penalty(1651, 1651, tolerance=tolerance) == 0.0
            assert year_penalty(1651, 1652, tolerance=tolerance) == pytest.approx(
                YEAR_PENALTY_PER_YEAR
            )

    def test_default_tolerance_is_the_code_constant(self) -> None:
        assert year_penalty(1651, 1651 + YEAR_TOLERANCE) == 0.0
        assert year_penalty(1651, 1652 + YEAR_TOLERANCE) == YEAR_PENALTY_PER_YEAR

    @pytest.mark.parametrize(
        ("query_year", "date"),
        [
            (None, "2019"),
            (None, None),
            (1651, None),
            (1651, ""),
            (1651, "S. XVIII"),
            (1651, "18. Jh."),
            (1651, "s.d."),
            (1651, "MDCLI"),
        ],
    )
    def test_fails_open(self, query_year: int | None, date: str | None) -> None:
        got = score(
            SearchQuery("Le Cuisinier francois", year=query_year), _record(date=date)
        )
        assert got.year_penalty == 0.0
