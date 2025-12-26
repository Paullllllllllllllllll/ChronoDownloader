"""Unit tests for api.matching module."""
from __future__ import annotations

import pytest

from api.matching import (
    combined_match_score,
    creator_score,
    normalize_text,
    parse_year,
    simple_ratio,
    strip_accents,
    title_score,
    token_set_ratio,
)


class TestStripAccents:
    """Tests for strip_accents function."""
    
    def test_removes_accents(self):
        """Test removal of accent marks."""
        assert strip_accents("café") == "cafe"
        assert strip_accents("résumé") == "resume"
        assert strip_accents("naïve") == "naive"
    
    def test_preserves_base_characters(self):
        """Test that base characters are preserved."""
        assert strip_accents("hello") == "hello"
        assert strip_accents("HELLO") == "HELLO"
    
    def test_handles_various_diacritics(self):
        """Test handling of various diacritical marks."""
        assert strip_accents("ñ") == "n"
        assert strip_accents("ü") == "u"
        assert strip_accents("ç") == "c"
        assert strip_accents("ø") == "ø"  # Not a combining character
    
    def test_empty_string(self):
        """Test with empty string."""
        assert strip_accents("") == ""
    
    def test_none_value(self):
        """Test with None value."""
        assert strip_accents(None) == ""
    
    def test_mixed_text(self):
        """Test with mixed accented and non-accented text."""
        assert strip_accents("L'art de la cuisine française") == "L'art de la cuisine francaise"


class TestNormalizeText:
    """Tests for normalize_text function."""
    
    def test_lowercase(self):
        """Test lowercase conversion."""
        assert normalize_text("HELLO WORLD") == "hello world"
    
    def test_removes_punctuation(self):
        """Test removal of punctuation."""
        assert normalize_text("Hello, World!") == "hello world"
    
    def test_collapses_whitespace(self):
        """Test collapsing of multiple spaces."""
        assert normalize_text("hello    world") == "hello world"
    
    def test_strips_leading_trailing(self):
        """Test stripping of leading/trailing whitespace."""
        assert normalize_text("  hello world  ") == "hello world"
    
    def test_removes_accents(self):
        """Test removal of accents."""
        assert normalize_text("Café Résumé") == "cafe resume"
    
    def test_handles_tabs_newlines(self):
        """Test handling of tabs and newlines."""
        assert normalize_text("hello\tworld\n") == "hello world"
    
    def test_empty_string(self):
        """Test with empty string."""
        assert normalize_text("") == ""
    
    def test_none_value(self):
        """Test with None value."""
        assert normalize_text(None) == ""
    
    def test_only_punctuation(self):
        """Test string with only punctuation."""
        assert normalize_text("!@#$%^&*()") == ""


class TestSimpleRatio:
    """Tests for simple_ratio function."""
    
    def test_identical_strings(self):
        """Test ratio for identical strings."""
        assert simple_ratio("hello world", "hello world") == 100
    
    def test_completely_different(self):
        """Test ratio for completely different strings."""
        # May not be exactly 0 due to algorithm
        assert simple_ratio("abc", "xyz") < 50
    
    def test_similar_strings(self):
        """Test ratio for similar strings."""
        score = simple_ratio("hello world", "hello worlds")
        assert 80 <= score <= 100
    
    def test_case_insensitive(self):
        """Test case insensitivity."""
        assert simple_ratio("Hello", "hello") == 100
    
    def test_ignores_punctuation(self):
        """Test that punctuation is ignored."""
        assert simple_ratio("hello, world!", "hello world") == 100
    
    def test_empty_strings(self):
        """Test with empty strings."""
        assert simple_ratio("", "hello") == 0
        assert simple_ratio("hello", "") == 0
        assert simple_ratio("", "") == 0


class TestTokenSetRatio:
    """Tests for token_set_ratio function."""
    
    def test_identical_strings(self):
        """Test ratio for identical strings."""
        assert token_set_ratio("hello world", "hello world") == 100
    
    def test_different_word_order(self):
        """Test that word order doesn't affect score."""
        score = token_set_ratio("hello world", "world hello")
        assert score == 100
    
    def test_subset_match(self):
        """Test matching with subset of words."""
        score = token_set_ratio("hello world test", "hello world")
        assert score >= 80
    
    def test_completely_different(self):
        """Test completely different strings."""
        assert token_set_ratio("abc def", "xyz uvw") < 50
    
    def test_empty_strings(self):
        """Test with empty strings."""
        assert token_set_ratio("", "hello world") == 0
        assert token_set_ratio("hello world", "") == 0


class TestTitleScore:
    """Tests for title_score function."""
    
    def test_exact_match(self):
        """Test exact title match."""
        assert title_score("The Art of Cooking", "The Art of Cooking") == 100
    
    def test_similar_titles(self):
        """Test similar titles."""
        score = title_score("The Art of Cooking", "Art of Cooking")
        assert score >= 80
    
    def test_different_titles(self):
        """Test different titles."""
        score = title_score("The Art of Cooking", "History of France")
        assert score < 50
    
    def test_simple_method(self):
        """Test with simple ratio method."""
        score = title_score("hello world", "hello world", method="simple")
        assert score == 100
    
    def test_token_set_method(self):
        """Test with token_set ratio method."""
        score = title_score("world hello", "hello world", method="token_set")
        assert score == 100
    
    def test_default_method_is_token_set(self):
        """Test that default method is token_set."""
        # Word order shouldn't matter with token_set
        score = title_score("world hello", "hello world")
        assert score == 100


class TestCreatorScore:
    """Tests for creator_score function."""
    
    def test_exact_match(self):
        """Test exact creator match."""
        assert creator_score("John Smith", ["John Smith"]) == 100
    
    def test_best_match_selected(self):
        """Test that best match is selected from multiple creators."""
        score = creator_score("John Smith", ["Jane Doe", "John Smith", "Bob"])
        assert score == 100
    
    def test_partial_match(self):
        """Test partial name match."""
        score = creator_score("John Smith", ["J. Smith"])
        assert 50 <= score <= 100
    
    def test_no_match(self):
        """Test when no match found."""
        score = creator_score("John Smith", ["Jane Doe", "Bob Jones"])
        assert score < 50
    
    def test_none_query_creator(self):
        """Test with None query creator."""
        assert creator_score(None, ["John Smith"]) == 0
    
    def test_empty_query_creator(self):
        """Test with empty query creator."""
        assert creator_score("", ["John Smith"]) == 0
    
    def test_none_creators_list(self):
        """Test with None creators list."""
        assert creator_score("John Smith", None) == 0
    
    def test_empty_creators_list(self):
        """Test with empty creators list."""
        assert creator_score("John Smith", []) == 0


class TestParseYear:
    """Tests for parse_year function."""
    
    def test_simple_year(self):
        """Test parsing simple year."""
        assert parse_year("1850") == 1850
    
    def test_year_in_text(self):
        """Test parsing year from text."""
        assert parse_year("Published in 1850") == 1850
    
    def test_year_with_range(self):
        """Test parsing first year from range."""
        assert parse_year("1850-1860") == 1850
    
    def test_none_value(self):
        """Test with None value."""
        assert parse_year(None) is None
    
    def test_empty_string(self):
        """Test with empty string."""
        assert parse_year("") is None
    
    def test_no_year(self):
        """Test with no year in text."""
        assert parse_year("no year here") is None
    
    def test_short_number(self):
        """Test that short numbers are not parsed as years."""
        assert parse_year("123") is None
    
    def test_multiple_years(self):
        """Test that first year is returned."""
        assert parse_year("From 1850 to 1900") == 1850


class TestCombinedMatchScore:
    """Tests for combined_match_score function."""
    
    def test_title_only(self):
        """Test scoring with title only."""
        score = combined_match_score(
            query_title="The Art of Cooking",
            item_title="The Art of Cooking"
        )
        # With default creator_weight=0.2 and no creator, title contributes 80%
        # 100 * 0.8 + 0 * 0.2 = 80
        assert score == 80.0
    
    def test_with_creator(self):
        """Test scoring with title and creator."""
        score = combined_match_score(
            query_title="The Art of Cooking",
            item_title="The Art of Cooking",
            query_creator="John Smith",
            creators=["John Smith"]
        )
        assert score == 100
    
    def test_creator_weight_default(self):
        """Test default creator weight."""
        # Perfect title, no creator match
        score = combined_match_score(
            query_title="The Art of Cooking",
            item_title="The Art of Cooking",
            query_creator="John Smith",
            creators=["Jane Doe"],
            creator_weight=0.2
        )
        # Title contributes 80%, creator contributes 20%
        # 100 * 0.8 + 0 * 0.2 = 80
        assert 75 <= score <= 85
    
    def test_creator_weight_zero(self):
        """Test with zero creator weight."""
        score = combined_match_score(
            query_title="The Art of Cooking",
            item_title="The Art of Cooking",
            query_creator="John Smith",
            creators=["Jane Doe"],
            creator_weight=0.0
        )
        # Only title matters
        assert score == 100
    
    def test_creator_weight_one(self):
        """Test with full creator weight."""
        score = combined_match_score(
            query_title="Different Title",
            item_title="The Art of Cooking",
            query_creator="John Smith",
            creators=["John Smith"],
            creator_weight=1.0
        )
        # Only creator matters
        assert score == 100
    
    def test_creator_weight_clamped(self):
        """Test that creator weight is clamped to [0, 1]."""
        score1 = combined_match_score(
            query_title="Title",
            item_title="Title",
            creator_weight=-0.5
        )
        score2 = combined_match_score(
            query_title="Title",
            item_title="Title",
            creator_weight=1.5
        )
        # Both should still compute valid scores
        assert 0 <= score1 <= 100
        assert 0 <= score2 <= 100
    
    def test_simple_method(self):
        """Test with simple matching method."""
        score = combined_match_score(
            query_title="hello world",
            item_title="hello world",
            method="simple"
        )
        # With default creator_weight=0.2 and no creator: 100 * 0.8 = 80
        assert score == 80.0
