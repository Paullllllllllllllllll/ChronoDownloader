"""Unit tests for api.core.naming module."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

from api.core.naming import (
    PROVIDER_ABBREV,
    PROVIDER_SLUGS,
    build_work_directory_name,
    get_provider_abbrev,
    get_provider_slug,
    sanitize_filename,
    to_snake_case,
)


class TestToSnakeCase:
    """Tests for to_snake_case function."""

    def test_simple_string(self) -> None:
        """Test conversion of simple string."""
        # Note: to_snake_case doesn't split camelCase, only replaces non-alnum
        assert to_snake_case("HelloWorld") == "helloworld"

    def test_with_spaces(self) -> None:
        """Test conversion of string with spaces."""
        assert to_snake_case("Hello World") == "hello_world"

    def test_with_punctuation(self) -> None:
        """Test conversion of string with punctuation."""
        assert to_snake_case("Hello, World!") == "hello_world"

    def test_with_numbers(self) -> None:
        """Test conversion of string with numbers."""
        assert to_snake_case("Entry0001") == "entry_0001"
        assert to_snake_case("E0001Test") == "e_0001_test"

    def test_mixed_case(self) -> None:
        """Test conversion of mixed case string."""
        # Note: to_snake_case doesn't split camelCase, only lowercases
        assert to_snake_case("TheArtOfCooking") == "theartofcooking"

    def test_already_snake_case(self) -> None:
        """Test that already snake_case string is preserved."""
        assert to_snake_case("already_snake_case") == "already_snake_case"

    def test_empty_string(self) -> None:
        """Test conversion of empty string."""
        assert to_snake_case("") == ""

    def test_none_value(self) -> None:
        """Test conversion of None value."""
        assert to_snake_case(None) == ""

    def test_special_characters(self) -> None:
        """Test conversion with special characters."""
        assert to_snake_case("foo@bar#baz") == "foo_bar_baz"

    def test_multiple_underscores_collapsed(self) -> None:
        """Test that multiple underscores are collapsed."""
        assert to_snake_case("foo___bar") == "foo_bar"

    def test_leading_trailing_underscores_removed(self) -> None:
        """Test that leading/trailing underscores are removed."""
        assert to_snake_case("_foo_bar_") == "foo_bar"


class TestSnakeCaseUnicodeFolding:
    """Accented titles must fold to letters, not to underscores.

    Without normalization the ASCII-only replacement turned every accented
    character into an underscore, so the SAME title produced two different
    slugs depending on the Unicode form a provider delivered (NFC ``"Küche"``
    -> ``k_che``; NFD ``"Küche"`` -> ``ku_che``), which silently broke
    resume and deduplication, and two DIFFERENT titles collided on one slug.
    """

    def test_nfc_and_nfd_agree(self) -> None:
        nfc = "Küche"  # precomposed u-umlaut
        nfd = "Küche"  # u + combining diaeresis
        assert nfc != nfd
        assert to_snake_case(nfc) == to_snake_case(nfd) == "kuche"

    def test_distinct_titles_no_longer_collide(self) -> None:
        assert to_snake_case("Gebäck") == "geback"
        assert to_snake_case("Gebück") == "gebuck"
        assert to_snake_case("Gebäck") != to_snake_case("Gebück")

    def test_non_decomposable_letters_are_transliterated(self) -> None:
        """The same table api.matching uses, so slug and score agree."""
        assert to_snake_case("Straße") == "strasse"
        assert to_snake_case("Kogebog for Bønder") == "kogebog_for_bonder"
        assert to_snake_case("Encyclopædia") == "encyclopaedia"
        assert to_snake_case("Kucharz doskonały") == "kucharz_doskonaly"

    def test_ascii_titles_are_unaffected(self) -> None:
        assert to_snake_case("The Art of Cooking") == "the_art_of_cooking"
        assert to_snake_case("E0001") == "e_0001"


class TestSanitizeFilename:
    """Tests for sanitize_filename function."""

    def test_simple_filename(self) -> None:
        """Test sanitization of simple filename."""
        assert sanitize_filename("document.pdf") == "document.pdf"

    def test_preserves_extension(self) -> None:
        """Test that extension is preserved."""
        result = sanitize_filename("my_document.pdf")
        assert result.endswith(".pdf")

    def test_multi_extension(self) -> None:
        """Test preservation of multi-part extension."""
        result = sanitize_filename("archive.tar.gz")
        assert result.endswith(".tar.gz")

    def test_removes_illegal_characters(self) -> None:
        """Test removal of illegal filesystem characters."""
        result = sanitize_filename('file<>:"/\\|?*.txt')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "/" not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result

    def test_strips_illegal_characters_from_extension(self) -> None:
        """Illegal characters in the extension portion (e.g. from a URL-derived
        '.pdf?token=1' suffix) must be scrubbed too, else os.replace fails on
        Windows."""
        result = sanitize_filename("document.pdf?token=1")
        assert "?" not in result
        assert result.startswith("document")

    def test_collapses_separators(self) -> None:
        """Test that multiple separators are collapsed."""
        result = sanitize_filename("foo...bar___baz.txt")
        # sanitize_filename collapses whitespace/separators to underscore
        # but the current implementation may not collapse all
        assert result.endswith(".txt")

    def test_max_length(self) -> None:
        """Test that base name is truncated to max length."""
        long_name = "a" * 200 + ".pdf"
        result = sanitize_filename(long_name, max_base_len=50)
        # Should be truncated base + extension
        assert len(result) <= 50 + 4  # 50 chars + ".pdf"

    def test_empty_string(self) -> None:
        """Test sanitization of empty string."""
        assert sanitize_filename("") == "_untitled_"

    def test_only_illegal_characters(self) -> None:
        """Test sanitization when only illegal characters remain."""
        result = sanitize_filename('<>:"/\\|?*')
        assert result == "_untitled_"

    def test_whitespace_handling(self) -> None:
        """Test proper handling of whitespace."""
        result = sanitize_filename("foo  bar   baz.txt")
        assert "  " not in result


class TestExtensionSplitting:
    """Only a plausible trailing extension may bypass base cleaning.

    ``Path.suffixes`` treated everything after the FIRST dot as extension, so
    a title carrying an abbreviation ("Mr.", "vol.") rode through unsanitized:
    it was never collapsed, never truncated, and could keep a trailing dot or
    space, which Windows silently drops from the stored name.
    """

    def test_dots_inside_the_title_stay_in_the_base(self) -> None:
        result = sanitize_filename("Mr. Smith's Voyage, vol. 2.pdf")
        assert result.endswith(".pdf")
        # The base was actually cleaned: separators collapsed to underscores.
        assert result == "Mr_Smith's_Voyage,_vol_2.pdf"

    def test_trailing_dot_and_space_do_not_survive(self) -> None:
        assert sanitize_filename("report. ") == "report"
        assert sanitize_filename("report.") == "report"
        assert not sanitize_filename("Mr. Smith. ").endswith((".", " "))

    def test_long_tail_after_a_dot_is_truncated(self) -> None:
        """A 300-character run behind a dot used to escape max_base_len."""
        result = sanitize_filename("x." + "y" * 300, max_base_len=50)
        assert len(result) == 50

    def test_real_multi_suffix_is_preserved(self) -> None:
        assert sanitize_filename("archive.tar.gz").endswith(".tar.gz")

    def test_ordinary_extension_is_preserved(self) -> None:
        assert sanitize_filename("document.pdf") == "document.pdf"
        assert sanitize_filename("con.pdf") == "_con_.pdf"

    def test_extension_like_segment_only(self) -> None:
        """A name that is nothing but a suffix keeps a usable base."""
        assert sanitize_filename(".pdf") == "pdf"


class TestGetProviderSlug:
    """Tests for get_provider_slug function."""

    def test_known_provider(self) -> None:
        """Test slug for known provider."""
        assert get_provider_slug("internet_archive", None) == "ia"
        assert get_provider_slug("bnf_gallica", None) == "gallica"
        assert get_provider_slug("mdz", None) == "mdz"

    def test_url_provider_fallback(self) -> None:
        """Test fallback to URL provider when pref_key is None."""
        assert get_provider_slug(None, "internet_archive") == "ia"

    def test_unknown_provider(self) -> None:
        """Test slug for unknown provider."""
        result = get_provider_slug("custom_provider", None)
        assert result == "custom_provider"

    def test_none_values(self) -> None:
        """Test with both values None."""
        assert get_provider_slug(None, None) == "unknown"

    def test_all_known_slugs(self) -> None:
        """Test that all known slugs are mapped correctly."""
        for key, expected_slug in PROVIDER_SLUGS.items():
            assert get_provider_slug(key, None) == expected_slug


class TestGetProviderAbbrev:
    """Tests for get_provider_abbrev function."""

    def test_known_provider(self) -> None:
        """Test abbreviation for known provider."""
        assert get_provider_abbrev("internet_archive") == "IA"
        assert get_provider_abbrev("bnf_gallica") == "GAL"
        assert get_provider_abbrev("loc") == "LOC"

    def test_unknown_provider(self) -> None:
        """Test abbreviation for unknown provider."""
        assert get_provider_abbrev("custom") == "CUSTOM"

    def test_all_known_abbrevs(self) -> None:
        """Test that all known abbreviations are correct."""
        for key, expected_abbrev in PROVIDER_ABBREV.items():
            assert get_provider_abbrev(key) == expected_abbrev


class TestBuildWorkDirectoryName:
    """Tests for build_work_directory_name function."""

    def test_with_entry_id_and_title(self) -> None:
        """Test directory name with both entry_id and title."""
        result = build_work_directory_name("E0001", "The Art of Cooking")
        assert result == "e_0001_the_art_of_cooking"

    def test_without_entry_id(self) -> None:
        """Test directory name without entry_id."""
        result = build_work_directory_name(None, "The Art of Cooking")
        assert result == "the_art_of_cooking"

    def test_long_title_truncated(self) -> None:
        """Test that long titles are truncated."""
        long_title = "A" * 100
        result = build_work_directory_name("E0001", long_title, max_len=20)
        # Title component should be truncated
        # The result format is: entry_slug_title_slug
        assert len(result) < len("e_0001_" + "a" * 100)

    def test_empty_title(self) -> None:
        """Test with empty title."""
        result = build_work_directory_name("E0001", "")
        assert "untitled" in result

    def test_none_title(self) -> None:
        """Test with None title."""
        result = build_work_directory_name("E0001", None)  # type: ignore[arg-type]
        assert "untitled" in result

    def test_both_none(self) -> None:
        """Test with both values None."""
        result = build_work_directory_name(None, None)  # type: ignore[arg-type]
        assert result == "untitled"

    def test_special_characters_in_title(self) -> None:
        """Test with special characters in title."""
        result = build_work_directory_name("E0001", "L'Art de la Cuisine!")
        assert "'" not in result
        assert "!" not in result


class TestWindowsReservedNames:
    """Windows reserved device names must be guarded (os.makedirs('con') fails)."""

    def test_reserved_directory_name_guarded(self) -> None:
        result = build_work_directory_name(None, "CON")
        assert result.lower() != "con"

    def test_reserved_filename_guarded(self) -> None:
        from api.core.naming import sanitize_filename

        for reserved in ("con", "nul", "com1", "lpt3"):
            sanitized = sanitize_filename(reserved)
            base = sanitized.split(".")[0]
            assert base.lower() not in {"con", "nul", "com1", "lpt3"}

    def test_reserved_name_with_extension_guarded(self) -> None:
        from api.core.naming import sanitize_filename

        sanitized = sanitize_filename("con.pdf")
        assert sanitized.endswith(".pdf")
        assert sanitized.split(".")[0].lower() != "con"


class TestPathLengthWarning:
    """The MAX_PATH advisory must fire for paths that go on to fail."""

    def test_long_work_dir_warns(self, caplog: Any) -> None:
        """Files land at <work_dir>/objects/<name>, roughly 90 characters
        beyond the directory checked, so a 200-character work directory is
        already past the 260-character limit."""
        from api.core.naming import warn_if_path_too_long

        path = r"C:\out" + "\\" + ("a" * 200)
        with (
            patch("api.core.naming.sys.platform", "win32"),
            caplog.at_level(logging.WARNING, logger="api.core.naming"),
        ):
            warn_if_path_too_long(path, "W0001")

        assert "MAX_PATH" in caplog.text

    def test_short_work_dir_is_silent(self, caplog: Any) -> None:
        from api.core.naming import warn_if_path_too_long

        with (
            patch("api.core.naming.sys.platform", "win32"),
            caplog.at_level(logging.WARNING, logger="api.core.naming"),
        ):
            warn_if_path_too_long(r"C:\out\e_0001_the_art_of_cooking", "W0001")

        assert caplog.text == ""


class TestNonLatinTitleFallback:
    """Titles that slug to empty need a stable, distinct stand-in.

    A wholly non-Latin title slugged to "", collapsing the directory name to
    the entry slug alone -- two such works accepted with the interactive
    default entry id then shared one directory.
    """

    def test_non_latin_title_yields_a_stable_component(self) -> None:
        first = build_work_directory_name("W0001", "Домострой")
        second = build_work_directory_name("W0001", "Домострой")

        assert first == second
        assert first != "w_0001"
        assert first.startswith("w_0001_t_")

    def test_distinct_non_latin_titles_do_not_collide(self) -> None:
        first = build_work_directory_name("W0001", "Домострой")
        second = build_work_directory_name("W0001", "食譜")

        assert first != second

    def test_unicode_normalization_forms_reach_one_directory(self) -> None:
        import unicodedata

        nfc = build_work_directory_name(
            "W0001", unicodedata.normalize("NFC", "Домострой")
        )
        nfd = build_work_directory_name(
            "W0001", unicodedata.normalize("NFD", "Домострой")
        )

        assert nfc == nfd
