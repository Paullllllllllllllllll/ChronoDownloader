"""Work-folder and object naming, ported from v1.30.3 with its expected names."""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

import pytest

from chronodownloader.output.paths import (
    METADATA_DIR,
    OBJECTS_DIR,
    PROVIDER_SLUGS,
    STAGING_DIR,
    WINDOWS_PATH_WARN,
    object_path,
    path_at_risk,
    provider_slug,
    sanitize_filename,
    staging_dir,
    to_snake_case,
    work_folder_name,
)
from chronodownloader.providers.base import FileKind, StagedFile, page_item
from chronodownloader.providers.bne import chunk_item
from chronodownloader.settings import PROVIDER_KEYS, Naming

STEM = "e_0001_the_art_of_cooking"


def staged(name: str, kind: FileKind, item: str) -> StagedFile:
    return StagedFile(name, kind, item, 10, "", "")


def final(name: str, kind: FileKind, item: str, provider: str = "mdz") -> str:
    return object_path(staged(name, kind, item), work_stem=STEM, provider=provider)


class TestToSnakeCase:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("HelloWorld", "helloworld"),
            ("Hello World", "hello_world"),
            ("Hello, World!", "hello_world"),
            ("Entry0001", "entry_0001"),
            ("E0001Test", "e_0001_test"),
            ("TheArtOfCooking", "theartofcooking"),
            ("already_snake_case", "already_snake_case"),
            ("", ""),
            (None, ""),
            ("foo@bar#baz", "foo_bar_baz"),
            ("foo___bar", "foo_bar"),
            ("_foo_bar_", "foo_bar"),
        ],
    )
    def test_v1_30_3_values(self, value: str | None, expected: str) -> None:
        assert to_snake_case(value) == expected

    def test_nfc_and_nfd_agree(self) -> None:
        nfc = unicodedata.normalize("NFC", "Küche")
        nfd = unicodedata.normalize("NFD", "Küche")
        assert nfc != nfd
        assert to_snake_case(nfc) == to_snake_case(nfd) == "kuche"

    def test_distinct_titles_do_not_collide(self) -> None:
        assert to_snake_case("Gebäck") == "geback"
        assert to_snake_case("Gebück") == "gebuck"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Straße", "strasse"),
            ("Kogebog for Bønder", "kogebog_for_bonder"),
            ("Encyclopædia", "encyclopaedia"),
            ("Kucharz doskonały", "kucharz_doskonaly"),
            ("The Art of Cooking", "the_art_of_cooking"),
            ("E0001", "e_0001"),
        ],
    )
    def test_non_decomposable_letters_are_transliterated(
        self, value: str, expected: str
    ) -> None:
        assert to_snake_case(value) == expected


class TestSanitizeFilename:
    def test_simple_and_extension(self) -> None:
        assert sanitize_filename("document.pdf") == "document.pdf"
        assert sanitize_filename("archive.tar.gz").endswith(".tar.gz")

    def test_removes_illegal_characters(self) -> None:
        result = sanitize_filename('file<>:"/\\|?*.txt')
        assert not set('<>:"/\\|?*') & set(result)

    def test_scrubs_the_extension(self) -> None:
        result = sanitize_filename("document.pdf?token=1")
        assert "?" not in result
        assert result.startswith("document")

    def test_max_length(self) -> None:
        assert len(sanitize_filename("a" * 200 + ".pdf", max_base_len=50)) <= 54

    def test_empty_and_illegal_only(self) -> None:
        assert sanitize_filename("") == "_untitled_"
        assert sanitize_filename('<>:"/\\|?*') == "_untitled_"

    def test_whitespace_collapses(self) -> None:
        assert "  " not in sanitize_filename("foo  bar   baz.txt")

    def test_dots_inside_the_title_stay_in_the_base(self) -> None:
        assert (
            sanitize_filename("Mr. Smith's Voyage, vol. 2.pdf")
            == "Mr_Smith's_Voyage,_vol_2.pdf"
        )

    def test_trailing_dot_and_space_do_not_survive(self) -> None:
        assert sanitize_filename("report. ") == "report"
        assert sanitize_filename("report.") == "report"
        assert not sanitize_filename("Mr. Smith. ").endswith((".", " "))

    def test_long_tail_after_a_dot_is_truncated(self) -> None:
        assert len(sanitize_filename("x." + "y" * 300, max_base_len=50)) == 50

    def test_extension_like_segment_only(self) -> None:
        assert sanitize_filename(".pdf") == "pdf"

    @pytest.mark.parametrize("reserved", ["con", "nul", "com1", "lpt3"])
    def test_reserved_names_are_guarded(self, reserved: str) -> None:
        assert sanitize_filename(reserved) == f"_{reserved}_"
        assert sanitize_filename(f"{reserved}.pdf") == f"_{reserved}_.pdf"


class TestProviderSlug:
    def test_v1_30_3_slugs(self) -> None:
        assert provider_slug("internet_archive") == "ia"
        assert provider_slug("bnf_gallica") == "gallica"
        assert provider_slug("mdz") == "mdz"

    def test_every_provider_key_has_a_slug(self) -> None:
        assert set(PROVIDER_SLUGS) == set(PROVIDER_KEYS)

    def test_the_v1_30_3_gallica_key_is_refused_with_a_hint(self) -> None:
        with pytest.raises(ValueError, match="bnf_gallica"):
            provider_slug("gallica")

    def test_unknown_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            provider_slug("custom_provider")


class TestWorkFolderName:
    def test_entry_id_and_title(self) -> None:
        assert work_folder_name("E0001", "The Art of Cooking") == STEM

    def test_without_entry_id(self) -> None:
        assert work_folder_name(None, "The Art of Cooking") == "the_art_of_cooking"

    def test_missing_title(self) -> None:
        assert work_folder_name("E0001", "") == "e_0001_untitled"
        assert work_folder_name("E0001", None) == "e_0001_untitled"
        assert work_folder_name(None, None) == "untitled"

    def test_special_characters(self) -> None:
        assert work_folder_name("E0001", "L'Art de la Cuisine!") == (
            "e_0001_l_art_de_la_cuisine"
        )

    def test_long_title_is_cut_to_the_slug_limit(self) -> None:
        assert work_folder_name("E0001", "A" * 100) == "e_0001_" + "a" * 80
        short = Naming(title_slug_max_len=20)
        assert work_folder_name("E0001", "A" * 100, naming=short) == (
            "e_0001_" + "a" * 20
        )

    def test_creator_and_year_only_when_enabled(self) -> None:
        both = Naming(include_creator_in_work_dir=True, include_year_in_work_dir=True)
        args = ("E0001", "Le Cuisinier françois")
        assert work_folder_name(*args, creator="La Varenne", year=1651) == (
            "e_0001_le_cuisinier_francois"
        )
        assert work_folder_name(
            *args, creator="La Varenne", year=1651, naming=both
        ) == ("e_0001_le_cuisinier_francois_la_varenne_1651")
        assert work_folder_name(*args, year="1651", naming=both) == (
            "e_0001_le_cuisinier_francois_1651"
        )

    def test_reserved_name_is_guarded(self) -> None:
        assert work_folder_name(None, "CON") == "_con_"

    def test_non_latin_title_gets_a_stable_digest(self) -> None:
        first = work_folder_name("W0001", "Домострой")
        digest = hashlib.sha1("Домострой".encode()).hexdigest()[:8]
        assert first == f"w_0001_t_{digest}"
        assert first == work_folder_name("W0001", "Домострой")
        assert first != work_folder_name("W0001", "食譜")

    def test_normalization_forms_reach_one_folder(self) -> None:
        nfc = work_folder_name("W0001", unicodedata.normalize("NFC", "Домострой"))
        nfd = work_folder_name("W0001", unicodedata.normalize("NFD", "Домострой"))
        assert nfc == nfd


class TestPageImages:
    def test_page_number_is_the_sequence(self) -> None:
        name = object_path(
            staged("p00003.jpg", "image", page_item(3)),
            work_stem="my_work",
            provider="mdz",
        )
        assert name == "objects/my_work_mdz_image_003.jpg"

    def test_a_missing_page_leaves_a_gap(self) -> None:
        names = [
            object_path(
                staged(f"p{i:05d}.jpg", "image", page_item(i)),
                work_stem="my_work",
                provider="mdz",
            )
            for i in (1, 3)
        ]
        assert names == [
            "objects/my_work_mdz_image_001.jpg",
            "objects/my_work_mdz_image_003.jpg",
        ]

    def test_extension_comes_from_the_staged_name(self) -> None:
        assert final("p01234.tif", "image", "p01234", "bnf_gallica") == (
            f"objects/{STEM}_gallica_image_1234.tif"
        )

    def test_rejects_an_image_that_is_not_a_page(self) -> None:
        with pytest.raises(ValueError, match="page item"):
            final("cover.jpg", "image", "cover")


class TestRenderings:
    @pytest.mark.parametrize(
        ("name", "provider", "expected"),
        [
            ("rendering.pdf", "internet_archive", f"{STEM}_ia.pdf"),
            ("rendering.epub", "annas_archive", f"{STEM}_annas.epub"),
        ],
    )
    def test_single_rendering_has_no_suffix(
        self, name: str, provider: str, expected: str
    ) -> None:
        assert final(name, "pdf", "rendering", provider) == f"objects/{expected}"

    def test_sbb_files_are_numbered_by_index(self) -> None:
        names = [
            final(f"rendering_{i:05d}.pdf", "pdf", f"rendering_{i:05d}", "sbb_digital")
            for i in (1, 2, 12)
        ]
        assert names == [
            f"objects/{STEM}_sbb.pdf",
            f"objects/{STEM}_sbb_2.pdf",
            f"objects/{STEM}_sbb_12.pdf",
        ]

    def test_bne_ranges_are_numbered_by_range(self) -> None:
        items = [chunk_item(1, 25), chunk_item(26, 50), chunk_item(51, 60)]
        names = [final(f"{item}.pdf", "pdf", item, "bne") for item in items]
        assert names == [
            f"objects/{STEM}_bne.pdf",
            f"objects/{STEM}_bne_2.pdf",
            f"objects/{STEM}_bne_3.pdf",
        ]

    @pytest.mark.parametrize("item", ["rendering_00000", "rendering_p00000_00025", "x"])
    def test_rejects_other_items(self, item: str) -> None:
        with pytest.raises(ValueError, match="rendering item"):
            final(f"{item}.pdf", "pdf", item)


class TestMetadata:
    @pytest.mark.parametrize(
        ("item", "provider", "expected"),
        [
            ("manifest", "mdz", f"{STEM}_mdz_manifest.json"),
            ("ia_metadata", "internet_archive", f"{STEM}_ia_ia_metadata.json"),
            ("md5_page", "annas_archive", f"{STEM}_annas_md5_page.json"),
            ("Search Meta", "europeana", f"{STEM}_europeana_search_meta.json"),
        ],
    )
    def test_named_by_item(self, item: str, provider: str, expected: str) -> None:
        name = f"metadata/{item}.json"
        assert final(name, "metadata", item, provider) == f"metadata/{expected}"

    def test_preview_goes_to_metadata(self) -> None:
        assert final("metadata/preview.jpg", "metadata", "preview", "dpla") == (
            f"metadata/{STEM}_dpla_preview.jpg"
        )

    def test_long_stem_keeps_the_item(self) -> None:
        stem = work_folder_name("E0001", "word " * 40)
        names = {
            object_path(
                staged(f"metadata/{item}.json", "metadata", item),
                work_stem=stem,
                provider="loc",
            )
            for item in ("item", "manifest")
        }
        assert len(names) == 2
        for name in names:
            base = name.removeprefix("metadata/").rsplit("_", 1)[0]
            assert len(base) <= 100

    def test_rejects_an_empty_item(self) -> None:
        with pytest.raises(ValueError, match="item name"):
            final("metadata/_.json", "metadata", "_")


class TestLongTitles:
    def test_object_stem_is_cut_to_fifty(self) -> None:
        stem = work_folder_name("E0001", "The Art of Cooking " * 10)
        name = object_path(
            staged("rendering.pdf", "pdf", "rendering"),
            work_stem=stem,
            provider="internet_archive",
        )
        object_stem = stem[:50].rstrip("_")
        assert name == f"objects/{object_stem}_ia.pdf"
        assert len(name.removeprefix("objects/").split("_ia")[0]) <= 50

    def test_cut_stem_drops_a_trailing_underscore(self) -> None:
        stem = "a" * 49 + "_bcd"
        name = object_path(
            staged("p00001.jpg", "image", "p00001"), work_stem=stem, provider="mdz"
        )
        assert name == f"objects/{'a' * 49}_mdz_image_001.jpg"


class TestStagedFileChecks:
    def test_rejects_a_name_without_extension(self) -> None:
        with pytest.raises(ValueError, match="extension"):
            final("p00001", "image", "p00001")

    def test_rejects_an_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            final("p00001.jpg", "image", "p00001", "gallica")


class TestLayout:
    def test_folder_constants(self) -> None:
        assert OBJECTS_DIR == "objects"
        assert METADATA_DIR == "metadata"
        assert STAGING_DIR == "objects/.staging"

    def test_staging_dir_per_generation(self, tmp_path: Path) -> None:
        assert staging_dir(tmp_path, 3) == tmp_path / "objects" / ".staging" / "3"

    def test_staging_dir_rejects_a_negative_generation(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="generation"):
            staging_dir(tmp_path, -1)


class TestPathAtRisk:
    def test_long_work_folder_is_at_risk_on_windows(self) -> None:
        assert path_at_risk("C:\\out\\" + "a" * 200, windows=True)

    def test_short_work_folder_is_not(self) -> None:
        assert not path_at_risk(r"C:\out\e_0001_the_art_of_cooking", windows=True)

    def test_limit_is_exclusive(self) -> None:
        assert not path_at_risk("a" * WINDOWS_PATH_WARN, windows=True)
        assert path_at_risk("a" * (WINDOWS_PATH_WARN + 1), windows=True)

    def test_never_at_risk_elsewhere(self) -> None:
        assert not path_at_risk("/out/" + "a" * 300, windows=False)

    def test_accepts_paths(self) -> None:
        assert path_at_risk(Path("C:/out") / ("a" * 200), windows=True)
