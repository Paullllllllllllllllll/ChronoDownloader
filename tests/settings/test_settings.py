"""Tests for settings.py: the packaged example, typed blocks and file lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from chronodownloader import settings as settings_module
from chronodownloader.options.options import Kind, Option, OptionTable
from chronodownloader.options.standard_options import (
    concurrency_option,
    dry_run_option,
    positive_int,
)
from chronodownloader.settings import (
    EXAMPLE_SETTINGS_PATH,
    PROVIDER_DEFAULTS,
    PROVIDER_KEYS,
    AnnasArchiveSettings,
    AnnasQuota,
    Coordination,
    Decisions,
    GoogleBooksSettings,
    JudgeNetwork,
    JudgeThresholds,
    Naming,
    NetworkSettings,
    PerWorkLimits,
    Settings,
    SettingsError,
    TitlePages,
    TotalLimits,
    load,
    provider_key,
)

DOWNLOAD_TABLE = OptionTable(
    [
        Option("output", "--output", "output root", type=Path),
        Option(
            "matching",
            "--matching",
            "matching mode",
            choices=("judge", "fuzzy", "hybrid"),
            default="judge",
        ),
        Option(
            "edition_policy",
            "--edition-policy",
            "editions accepted",
            choices=("any_edition", "same_edition", "any_language"),
            default="any_edition",
        ),
        Option(
            "providers", "--providers", "providers", kind=Kind.LIST, type=provider_key
        ),
        Option("max_candidates", "--max-candidates", "candidates", type=positive_int),
        Option("search_timeout", "--search-timeout", "timeout", type=float),
        Option("format", "--format", "format", choices=("images", "pdf", "both")),
        Option("max_pages", "--max-pages", "page limit", type=int, default=0),
        Option("include_metadata", "--include-metadata", "metadata", kind=Kind.SWITCH),
        concurrency_option(3),
        dry_run_option("run.dry_run"),
    ]
)
"""A stand-in for the download table of spec.py with the settable options."""

EXAMPLE_PROVIDERS = (
    "mdz",
    "bnf_gallica",
    "e_rara",
    "internet_archive",
    "wellcome",
    "europeana",
    "ddb",
    "polona",
)


@pytest.fixture
def settings_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the repository settings file and the example into ``tmp_path``."""
    monkeypatch.setattr(
        settings_module, "DEFAULT_SETTINGS_PATH", tmp_path / "settings.yaml"
    )
    monkeypatch.setattr(
        settings_module, "EXAMPLE_SETTINGS_PATH", tmp_path / "settings.example.yaml"
    )
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _load_text(directory: Path, text: str) -> Settings:
    _write(directory / "settings.yaml", text)
    return load(None, DOWNLOAD_TABLE)


def test_packaged_example_loads_and_matches_code_values() -> None:
    loaded = load(EXAMPLE_SETTINGS_PATH, DOWNLOAD_TABLE)
    assert loaded.source == EXAMPLE_SETTINGS_PATH
    assert dict(loaded.defaults) == {"providers": EXAMPLE_PROVIDERS}
    expected = Settings(defaults=loaded.defaults, source=EXAMPLE_SETTINGS_PATH)
    assert loaded == expected


def test_example_names_every_provider_key() -> None:
    loaded = load(EXAMPLE_SETTINGS_PATH, DOWNLOAD_TABLE)
    assert tuple(loaded.providers) == PROVIDER_KEYS
    assert len(PROVIDER_KEYS) == 17
    text = EXAMPLE_SETTINGS_PATH.read_text(encoding="utf-8")
    for key in PROVIDER_KEYS:
        assert f"\n  {key}:" in text


def test_every_block_parses(settings_paths: Path) -> None:
    value = _load_text(
        settings_paths,
        "api_keys:\n  europeana: MY_EUROPEANA\n  openrouter: ''\n"
        "judge:\n  model: other/model\n  retry_after_minutes: 5\n"
        "  thresholds:\n    same_work_accept: 0.8\n    text_accept: 85\n"
        "  network:\n    timeout_s: 120\n    slots: 2\n"
        "decisions:\n  model: gpt-6-luna-decisions\n  provider: openai\n"
        "  endpoint: https://proxy.example/v1/decisions\n  image_max_px: 512\n"
        "  pages_per_request: 10\n  profile: culinary\n  timeout_s: 30\n"
        "title_pages:\n  model: google/gemini-4-flash\n"
        "  endpoint: https://proxy.example/chat\n  image_max_px: 1200\n"
        "  effort: minimal\n  timeout_s: 60\n"
        "coordination:\n  default_slots: 3\n  slot_ttl_s: 90\n"
        "providers:\n"
        "  google_books:\n    prefer: epub\n    max_files: 1000\n"
        "  annas_archive:\n    base_url: https://mirror.example/\n"
        "    quota:\n      daily_limit: 10\n"
        "  sbb_digital:\n    max_pages: 20\n    slots: 2\n"
        "    network:\n      impersonate: chrome\n      verify_ssl: false\n"
        "download_limits:\n  total:\n    pdfs_gb: 0\n"
        "  per_work:\n    metadata_mb: 10\n  on_exceed: skip\n"
        "naming:\n  include_year_in_work_dir: true\n"
        "state_dir: ~/cd_state\n"
        "defaults:\n  output: out\n  matching: fuzzy\n  max_pages: 20\n",
    )
    assert value.api_key_env("europeana") == "MY_EUROPEANA"
    assert value.api_key_env("openrouter") == "OPENROUTER_API_KEY"
    assert value.judge.model == "other/model"
    assert value.judge.retry_after_minutes == 5.0
    assert value.judge.thresholds == JudgeThresholds(
        same_work_accept=0.8, text_accept=85.0
    )
    assert value.judge.network == JudgeNetwork(timeout_s=120.0, slots=2)
    assert value.decisions == Decisions(
        model="gpt-6-luna-decisions",
        provider="openai",
        endpoint="https://proxy.example/v1/decisions",
        image_max_px=512,
        pages_per_request=10,
        profile="culinary",
        timeout_s=30.0,
    )
    assert value.api_key_env("openai") == "OPENAI_API_KEY"
    assert value.title_pages == TitlePages(
        model="google/gemini-4-flash",
        endpoint="https://proxy.example/chat",
        image_max_px=1200,
        effort="minimal",
        timeout_s=60.0,
    )
    assert value.coordination == Coordination(default_slots=3, slot_ttl_s=90.0)
    books = value.providers["google_books"]
    assert isinstance(books, GoogleBooksSettings)
    assert (books.prefer, books.max_files, books.max_pages) == ("epub", 1000, 50)
    annas = value.providers["annas_archive"]
    assert isinstance(annas, AnnasArchiveSettings)
    assert annas.base_url == "https://mirror.example/"
    assert annas.quota == AnnasQuota(daily_limit=10)
    sbb = value.providers["sbb_digital"]
    assert (sbb.max_pages, sbb.slots) == (20, 2)
    assert sbb.network.impersonate == "chrome"
    assert sbb.network.verify_ssl is False
    assert sbb.network.timeout_s == 40.0
    limits = value.download_limits
    assert limits.total == TotalLimits(pdfs_gb=0.0)
    assert limits.per_work == PerWorkLimits(metadata_mb=10.0)
    assert limits.on_exceed == "skip"
    assert value.naming == Naming(include_year_in_work_dir=True)
    assert value.state_directory() == Path.home() / "cd_state"
    assert dict(value.defaults) == {
        "output": Path("out"),
        "matching": "fuzzy",
        "max_pages": 20,
    }


def test_missing_provider_keys_keep_the_built_in_profile(
    settings_paths: Path,
) -> None:
    value = _load_text(
        settings_paths,
        "providers:\n  mdz:\n    network:\n      delay_ms: 100\n"
        "  loc:\n    network:\n      headers: {X-Test: one}\n"
        "  british_library:\n",
    )
    mdz = value.providers["mdz"].network
    assert (mdz.delay_ms, mdz.jitter_ms, mdz.max_attempts) == (100, 150, 25)
    assert dict(value.providers["loc"].network.headers) == {"X-Test": "one"}
    assert value.providers["british_library"] == PROVIDER_DEFAULTS["british_library"]
    assert value.providers["bnf_gallica"] == PROVIDER_DEFAULTS["bnf_gallica"]
    assert value.slots("mdz") == 1
    assert value.slots("loc") == value.coordination.default_slots == 2
    with pytest.raises(SettingsError, match="unknown provider"):
        value.slots("gallica")


def test_an_explicit_null_switches_a_text_threshold_off(
    settings_paths: Path,
) -> None:
    value = _load_text(
        settings_paths,
        "judge:\n  thresholds:\n    text_accept: 115\n    text_prefilter: null\n",
    )
    thresholds = value.judge.thresholds
    assert (thresholds.text_accept, thresholds.text_prefilter) == (115.0, None)
    assert thresholds.text_reject == JudgeThresholds().text_reject


def test_a_null_title_model_turns_the_reading_off(settings_paths: Path) -> None:
    value = _load_text(settings_paths, "title_pages:\n  model: null\n")
    assert value.title_pages == TitlePages(model=None)
    assert _load_text(settings_paths, "title_pages: {}\n").title_pages == TitlePages()


def test_code_profile_values_fill_unset_network_keys() -> None:
    assert PROVIDER_DEFAULTS["bne"].network == NetworkSettings(
        delay_ms=4000,
        jitter_ms=1000,
        base_backoff_s=2.0,
        backoff_multiplier=1.8,
        max_backoff_s=120.0,
        timeout_s=60.0,
        impersonate="chrome",
    )
    assert NetworkSettings().max_attempts == 5


def test_judge_network_converts_to_a_provider_network_block() -> None:
    judge = JudgeNetwork(
        delay_ms=100,
        jitter_ms=50,
        max_attempts=4,
        base_backoff_s=2.0,
        backoff_multiplier=3.0,
        max_backoff_s=9.0,
        timeout_s=120.0,
        slots=2,
    )
    assert judge.network_settings() == NetworkSettings(
        delay_ms=100,
        jitter_ms=50,
        max_attempts=4,
        base_backoff_s=2.0,
        backoff_multiplier=3.0,
        max_backoff_s=9.0,
        timeout_s=120.0,
    )
    converted = JudgeNetwork().network_settings()
    assert converted.verify_ssl is True
    assert converted.ssl_error_policy == "fail"
    assert converted.impersonate is None
    assert converted.circuit_breaker_enabled is True
    assert (
        converted.circuit_breaker_threshold
        == NetworkSettings().circuit_breaker_threshold
    )
    assert converted.timeout_s == 300.0


def test_settings_flag_beats_the_repository_file_and_the_example(
    settings_paths: Path,
) -> None:
    _write(
        settings_paths / "settings.example.yaml", "naming:\n  title_slug_max_len: 5\n"
    )
    assert load(None, DOWNLOAD_TABLE).naming.title_slug_max_len == 5
    _write(settings_paths / "settings.yaml", "naming:\n  title_slug_max_len: 7\n")
    assert load(None, DOWNLOAD_TABLE).naming.title_slug_max_len == 7
    other = _write(settings_paths / "other.yaml", "naming:\n  title_slug_max_len: 9\n")
    loaded = load(other, DOWNLOAD_TABLE)
    assert (loaded.naming.title_slug_max_len, loaded.source) == (9, other)


def test_without_any_file_the_code_values_apply(settings_paths: Path) -> None:
    assert load(None, DOWNLOAD_TABLE) == Settings()


def test_a_missing_settings_flag_file_is_an_error(settings_paths: Path) -> None:
    with pytest.raises(SettingsError, match="missing.yaml"):
        load(settings_paths / "missing.yaml", DOWNLOAD_TABLE)


@pytest.mark.parametrize(
    "text",
    [
        "unknown: 1\n",
        "config:\n  providers: {}\n",
        "api_keys:\n  anthropic: ANTHROPIC_API_KEY\n",
        "judge:\n  temperature: 0\n",
        "decisions:\n  thresholds: {}\n",
        "title_pages:\n  author: true\n",
        "judge:\n  thresholds:\n    min_title_score: 35\n",
        "judge:\n  network:\n    headers: {}\n",
        "coordination:\n  slots: 2\n",
        "providers:\n  bl: {}\n",
        "providers:\n  mdz:\n    min_title_score: 30\n",
        "providers:\n  mdz:\n    free_only: true\n",
        "providers:\n  wellcome:\n    max_images: 0\n",
        "providers:\n  mdz:\n    network:\n      retries: 3\n",
        "providers:\n  annas_archive:\n    quota:\n      limit: 3\n",
        "download_limits:\n  total:\n    images_mb: 3\n",
        "download_limits:\n  per_work:\n    metadata_gb: 1\n",
        "naming:\n  template: x\n",
        "defaults:\n  provider_hierarchy: [mdz]\n",
    ],
)
def test_unknown_keys_at_every_level_are_errors(
    settings_paths: Path, text: str
) -> None:
    with pytest.raises(SettingsError, match="settings.yaml"):
        _load_text(settings_paths, text)


@pytest.mark.parametrize(
    "text",
    [
        "providers: [mdz]\n",
        "api_keys:\n  dpla: 5\n",
        "judge:\n  model: ''\n",
        "judge:\n  retry_after_minutes: soon\n",
        "judge:\n  thresholds:\n    same_work_accept: 1.5\n",
        "judge:\n  thresholds:\n    text_accept: 124\n",
        "judge:\n  network:\n    slots: 0\n",
        "decisions:\n  model: ''\n",
        "decisions:\n  provider: anthropic\n",
        "decisions:\n  profile: legal\n",
        "decisions:\n  pages_per_request: 401\n",
        "decisions:\n  image_max_px: 0\n",
        "title_pages:\n  model: ''\n",
        "title_pages:\n  endpoint: ''\n",
        "title_pages:\n  effort: extreme\n",
        "title_pages:\n  image_max_px: 0\n",
        "coordination:\n  default_slots: two\n",
        "coordination:\n  pacing_tolerance_ms: 2.5\n",
        "providers:\n  mdz: 3\n",
        "providers:\n  mdz:\n    max_pages: -1\n",
        "providers:\n  mdz:\n    slots: 0\n",
        "providers:\n  mdz:\n    network:\n      delay_ms: fast\n",
        "providers:\n  mdz:\n    network:\n      verify_ssl: 'no'\n",
        "providers:\n  mdz:\n    network:\n      ssl_error_policy: ignore\n",
        "providers:\n  mdz:\n    network:\n      headers: {Accept: 1}\n",
        "providers:\n  mdz:\n    network:\n      impersonate: 3\n",
        "providers:\n  google_books:\n    prefer: djvu\n",
        "providers:\n  annas_archive:\n    quota:\n      enabled: 1\n",
        "download_limits:\n  on_exceed: warn\n",
        "download_limits:\n  total: 5\n",
        "naming:\n  title_slug_max_len: 0\n",
        "state_dir: 5\n",
        "logs_dir: [a]\n",
        "defaults:\n  dry_run: true\n",
        "defaults:\n  concurrency: 0\n",
        "defaults:\n  format: djvu\n",
        "defaults: [providers]\n",
    ],
)
def test_wrong_types_and_values_are_errors(settings_paths: Path, text: str) -> None:
    with pytest.raises(SettingsError, match="settings.yaml"):
        _load_text(settings_paths, text)


def test_defaults_key_outside_the_table_is_an_error(settings_paths: Path) -> None:
    with pytest.raises(SettingsError, match="unknown option in defaults: 'first'"):
        _load_text(settings_paths, "defaults:\n  first: 10\n")


def test_defaults_providers_keep_their_order(settings_paths: Path) -> None:
    value = _load_text(settings_paths, "defaults:\n  providers: [polona, mdz]\n")
    assert value.defaults["providers"] == ("polona", "mdz")


@pytest.mark.parametrize(
    "text",
    [
        "providers:\n  gallica:\n    slots: 1\n",
        "defaults:\n  providers: [mdz, gallica]\n",
    ],
)
def test_gallica_is_rejected_with_a_hint(settings_paths: Path, text: str) -> None:
    with pytest.raises(SettingsError, match="bnf_gallica"):
        _load_text(settings_paths, text)


def test_provider_key_converter() -> None:
    assert provider_key(" mdz ") == "mdz"
    with pytest.raises(ValueError, match="bnf_gallica"):
        provider_key("gallica")
    with pytest.raises(ValueError, match="unknown provider 'bl'"):
        provider_key("bl")


def test_relative_paths_resolve_against_the_current_directory(
    settings_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = settings_paths / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    value = _load_text(settings_paths, "logs_dir: logs\n")
    assert value.log_file() == work / "logs" / "application.log"
    file_value = _load_text(settings_paths, "logs_dir: run.log\n")
    assert file_value.log_file() == work / "run.log"
    assert Settings().state_directory() == Path.home() / ".chronodownloader"


@pytest.mark.parametrize("text", ["state_dir: state\n", "state_dir: ./a/b\n"])
def test_a_relative_state_dir_is_an_error(settings_paths: Path, text: str) -> None:
    with pytest.raises(SettingsError, match="state_dir: must be an absolute path"):
        _load_text(settings_paths, text)


def test_an_absolute_state_dir_is_kept(settings_paths: Path) -> None:
    target = settings_paths / "machine state"
    value = _load_text(settings_paths, f"state_dir: '{target.as_posix()}'\n")
    assert value.state_directory() == target


def test_a_relative_state_dir_given_in_code_is_refused() -> None:
    with pytest.raises(SettingsError, match="state_dir"):
        Settings(state_dir=Path("state")).state_directory()
