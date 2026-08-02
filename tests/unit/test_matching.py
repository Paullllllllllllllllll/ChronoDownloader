"""Unit tests for api.matching module."""

from __future__ import annotations

import difflib

from api.matching import (
    creator_score,
    normalize_text,
    simple_ratio,
    strip_accents,
    title_score,
    token_set_ratio,
)


class TestStripAccents:
    """Tests for strip_accents function."""

    def test_removes_accents(self) -> None:
        """Test removal of accent marks."""
        assert strip_accents("café") == "cafe"
        assert strip_accents("résumé") == "resume"
        assert strip_accents("naïve") == "naive"

    def test_preserves_base_characters(self) -> None:
        """Test that base characters are preserved."""
        assert strip_accents("hello") == "hello"
        assert strip_accents("HELLO") == "HELLO"

    def test_handles_various_diacritics(self) -> None:
        """Test handling of various diacritical marks."""
        assert strip_accents("ñ") == "n"
        assert strip_accents("ü") == "u"
        assert strip_accents("ç") == "c"
        assert strip_accents("ø") == "ø"  # Not a combining character

    def test_empty_string(self) -> None:
        """Test with empty string."""
        assert strip_accents("") == ""

    def test_none_value(self) -> None:
        """Test with None value."""
        assert strip_accents(None) == ""

    def test_mixed_text(self) -> None:
        """Test with mixed accented and non-accented text."""
        assert (
            strip_accents("L'art de la cuisine française")
            == "L'art de la cuisine francaise"
        )


class TestNormalizeText:
    """Tests for normalize_text function."""

    def test_lowercase(self) -> None:
        """Test lowercase conversion."""
        assert normalize_text("HELLO WORLD") == "hello world"

    def test_removes_punctuation(self) -> None:
        """Test removal of punctuation."""
        assert normalize_text("Hello, World!") == "hello world"

    def test_collapses_whitespace(self) -> None:
        """Test collapsing of multiple spaces."""
        assert normalize_text("hello    world") == "hello world"

    def test_strips_leading_trailing(self) -> None:
        """Test stripping of leading/trailing whitespace."""
        assert normalize_text("  hello world  ") == "hello world"

    def test_removes_accents(self) -> None:
        """Test removal of accents."""
        assert normalize_text("Café Résumé") == "cafe resume"

    def test_handles_tabs_newlines(self) -> None:
        """Test handling of tabs and newlines."""
        assert normalize_text("hello\tworld\n") == "hello world"

    def test_empty_string(self) -> None:
        """Test with empty string."""
        assert normalize_text("") == ""

    def test_none_value(self) -> None:
        """Test with None value."""
        assert normalize_text(None) == ""

    def test_only_punctuation(self) -> None:
        """Test string with only punctuation."""
        assert normalize_text("!@#$%^&*()") == ""

    def test_transliterates_non_decomposable_letters(self) -> None:
        """ß, ø, æ, œ, ł do not split words into fragments.

        NFKD leaves these letters intact, so the ASCII-only filter used to
        replace them with a space ("Bønder" -> "b nder"), sinking title
        scores below the selection gate for Nordic/German/Polish titles.
        """
        assert normalize_text("Kogebog for Bønder") == "kogebog for bonder"
        assert normalize_text("Straße") == "strasse"
        assert normalize_text("Encyclopædia") == "encyclopaedia"
        assert normalize_text("Œuvres complètes") == "oeuvres completes"
        assert normalize_text("Kucharz doskonały") == "kucharz doskonaly"


class TestNonLatinScriptNormalization:
    """A title in a non-Latin script must not be erased by the ASCII filter.

    Pre-fix, normalize_text's ASCII-only filter erased Cyrillic/Greek titles
    entirely, so simple_ratio/token_set_ratio both short-circuited to 0 even
    for byte-identical titles and no such record could ever clear the
    selection gate.
    """

    def test_cyrillic_title_normalizes_and_scores_above_gate(self) -> None:
        """Identical Cyrillic titles normalize non-empty and score at 100."""
        assert normalize_text("Домострой") != ""
        assert simple_ratio("Домострой", "Домострой") == 100
        assert token_set_ratio("Домострой", "Домострой") == 100
        # The genuinely punctuation-only fallback case is unaffected.
        assert normalize_text("!@#$%^&*()") == ""

    def test_a_surviving_ascii_token_does_not_defeat_the_fallback(self) -> None:
        """A year or volume number must not strip the script off a title.

        The fallback used to be gated on the ASCII pass coming back empty, so
        one surviving token was enough to keep the mutilated form: a Cyrillic
        title carrying its year normalized to just that year and scored 0
        against its own query.
        """
        # The breve on "й" is a combining mark, so strip_accents folds it to
        # "и" exactly as it folds Latin diacritics.
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
        """Only scripts the ASCII filter cannot represent take the fallback."""
        assert normalize_text("Cookery for Bønder, 1861") == "cookery for bonder 1861"
        assert normalize_text("Œuvres, vol. 2") == "oeuvres vol 2"
        assert normalize_text("1861") == "1861"

    def test_mixed_script_titles_keep_both_halves(self) -> None:
        assert normalize_text("Подарокъ / Le Cadeau") == "подарокъ le cadeau"


class TestSimpleRatio:
    """Tests for simple_ratio function."""

    def test_identical_strings(self) -> None:
        """Test ratio for identical strings."""
        assert simple_ratio("hello world", "hello world") == 100

    def test_completely_different(self) -> None:
        """Test ratio for completely different strings."""
        # May not be exactly 0 due to algorithm
        assert simple_ratio("abc", "xyz") < 50

    def test_similar_strings(self) -> None:
        """Test ratio for similar strings."""
        score = simple_ratio("hello world", "hello worlds")
        assert 80 <= score <= 100

    def test_case_insensitive(self) -> None:
        """Test case insensitivity."""
        assert simple_ratio("Hello", "hello") == 100

    def test_ignores_punctuation(self) -> None:
        """Test that punctuation is ignored."""
        assert simple_ratio("hello, world!", "hello world") == 100

    def test_empty_strings(self) -> None:
        """Test with empty strings."""
        assert simple_ratio("", "hello") == 0
        assert simple_ratio("hello", "") == 0
        assert simple_ratio("", "") == 0


class TestTokenSetRatio:
    """Tests for token_set_ratio function."""

    def test_identical_strings(self) -> None:
        """Test ratio for identical strings."""
        assert token_set_ratio("hello world", "hello world") == 100

    def test_different_word_order(self) -> None:
        """Test that word order doesn't affect score."""
        score = token_set_ratio("hello world", "world hello")
        assert score == 100

    def test_subset_match(self) -> None:
        """A pure token subset scores 100 under set semantics."""
        assert token_set_ratio("hello world test", "hello world") == 100

    def test_completely_different(self) -> None:
        """Test completely different strings."""
        assert token_set_ratio("abc def", "xyz uvw") < 50

    def test_empty_strings(self) -> None:
        """Test with empty strings."""
        assert token_set_ratio("", "hello world") == 0
        assert token_set_ratio("hello world", "") == 0


class TestTokenSetSemantics:
    """The scorer must implement token SET, not token SORT, semantics.

    The CSV query column is ``short_title``, so the query is normally an exact
    token subset of the full imprint title a catalog returns. Under the former
    sort-only implementation those pairs scored in the low thirties -- beneath
    every ``min_title_score`` ``config.example.json`` ships (30-50) -- and the
    correct edition was discarded as a non-match.
    """

    SHORT_AND_FULL_TITLES = [
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
    ]

    def test_short_title_is_a_perfect_match_for_its_full_imprint(self) -> None:
        for short, full in self.SHORT_AND_FULL_TITLES:
            assert token_set_ratio(short, full) == 100, short
            assert title_score(short, full) == 100, short

    def test_set_semantics_are_symmetric(self) -> None:
        for short, full in self.SHORT_AND_FULL_TITLES:
            assert token_set_ratio(full, short) == 100, short

    def test_disjoint_titles_stay_far_below_the_gate(self) -> None:
        """Set semantics must not inflate unrelated titles."""
        assert token_set_ratio("abc def", "xyz uvw") < 50
        assert title_score("The Art of Cooking", "History of France") < 50
        assert title_score("Le Cuisinier francois", "A Treatise on Naval Gunnery") < 35

    def test_no_shared_tokens_reduces_to_the_sort_ratio(self) -> None:
        """An empty intersection scores exactly as the sort-only version did."""
        for a, b in (("Almanach", "N/A"), ("abc def", "xyz uvw")):
            sorted_a = " ".join(sorted(normalize_text(a).split()))
            sorted_b = " ".join(sorted(normalize_text(b).split()))
            expected = int(
                round(difflib.SequenceMatcher(None, sorted_a, sorted_b).ratio() * 100)
            )
            assert token_set_ratio(a, b) == expected


class TestTitleScore:
    """Tests for title_score function."""

    def test_exact_match(self) -> None:
        """Test exact title match."""
        assert title_score("The Art of Cooking", "The Art of Cooking") == 100

    def test_similar_titles(self) -> None:
        """Test similar titles."""
        score = title_score("The Art of Cooking", "Art of Cooking")
        assert score >= 80

    def test_different_titles(self) -> None:
        """Test different titles."""
        score = title_score("The Art of Cooking", "History of France")
        assert score < 50

    def test_simple_method(self) -> None:
        """Test with simple ratio method."""
        score = title_score("hello world", "hello world", method="simple")
        assert score == 100

    def test_token_set_method(self) -> None:
        """Test with token_set ratio method."""
        score = title_score("world hello", "hello world", method="token_set")
        assert score == 100

    def test_default_method_is_token_set(self) -> None:
        """Test that default method is token_set."""
        # Word order shouldn't matter with token_set
        score = title_score("world hello", "hello world")
        assert score == 100


class TestCreatorScore:
    """Tests for creator_score function."""

    def test_exact_match(self) -> None:
        """Test exact creator match."""
        assert creator_score("John Smith", ["John Smith"]) == 100

    def test_best_match_selected(self) -> None:
        """Test that best match is selected from multiple creators."""
        score = creator_score("John Smith", ["Jane Doe", "John Smith", "Bob"])
        assert score == 100

    def test_partial_match(self) -> None:
        """Test partial name match."""
        score = creator_score("John Smith", ["J. Smith"])
        assert 50 <= score <= 100

    def test_no_match(self) -> None:
        """Test when no match found."""
        score = creator_score("John Smith", ["Jane Doe", "Bob Jones"])
        assert score < 50

    def test_none_query_creator(self) -> None:
        """Test with None query creator."""
        assert creator_score(None, ["John Smith"]) == 0

    def test_empty_query_creator(self) -> None:
        """Test with empty query creator."""
        assert creator_score("", ["John Smith"]) == 0

    def test_none_creators_list(self) -> None:
        """Test with None creators list."""
        assert creator_score("John Smith", None) == 0

    def test_empty_creators_list(self) -> None:
        """Test with empty creators list."""
        assert creator_score("John Smith", []) == 0


class TestInvertedCreatorNames:
    """Catalog records name a single author in inverted form."""

    def test_inverted_name_scores_as_one_creator(self) -> None:
        """Splitting "Escoffier, Auguste" on the comma made two people out of
        one and dropped the creator score from 100 to 69."""
        from api.matching import creator_score
        from api.model import convert_to_searchresult

        sr = convert_to_searchresult(
            "BnF Gallica", {"title": "T", "creator": "Escoffier, Auguste"}
        )

        assert sr.creators == ["Escoffier, Auguste"]
        assert creator_score("Auguste Escoffier", sr.creators) == 100

    def test_semicolon_list_still_splits(self) -> None:
        """The DC/MARC multi-value separator remains a separator."""
        from api.model import convert_to_searchresult

        sr = convert_to_searchresult(
            "BnF Gallica", {"title": "T", "creator": "Escoffier, Auguste; Gilbert, P."}
        )

        assert sr.creators == ["Escoffier, Auguste", "Gilbert, P."]
