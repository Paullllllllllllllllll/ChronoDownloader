"""Interface-level tests for the refactored deep modules.

Asserts that each package publishes the documented public surface
and that every exported name resolves to a callable, class, module,
constant, or frozen collection. These tests guard against accidental
interface regressions during future refactors.
"""
from __future__ import annotations

import importlib
from types import ModuleType


def _import_and_check(module_name: str, expected_surface: list[str]) -> ModuleType:
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "__all__"), f"{module_name} must declare __all__"
    actual = set(mod.__all__)
    expected = set(expected_surface)
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"{module_name} missing from __all__: {sorted(missing)}"
    assert not extra, f"{module_name} has unexpected __all__ entries: {sorted(extra)}"
    for name in expected_surface:
        attr = getattr(mod, name, None)
        assert attr is not None, f"{module_name}.{name} resolved to None"
    return mod


def test_api_core_package_surface() -> None:
    _import_and_check(
        "api.core",
        ["config", "network", "context", "naming", "budget", "download"],
    )


def test_api_iiif_package_surface() -> None:
    _import_and_check(
        "api.iiif",
        [
            "extract_image_service_bases",
            "extract_direct_image_urls",
            "image_url_candidates",
            "download_one_from_service",
            "download_page_images",
            "download_iiif_manifest_and_images",
            "try_pdf_first_then_images",
            "download_iiif_renderings",
            "is_iiif_manifest_url",
            "detect_provider_from_url",
            "extract_item_id_from_url",
            "extract_manifest_metadata",
            "preview_manifest",
            "download_from_iiif_manifest",
            "is_direct_download_enabled",
            "get_direct_link_column",
            "get_naming_template",
            "resolve_file_stem",
            "IIIF_MANIFEST_PATTERNS",
        ],
    )


def test_api_providers_package_surface() -> None:
    _import_and_check("api.providers", ["PROVIDERS"])
    from api.providers import PROVIDERS

    # 17 providers
    assert len(PROVIDERS) == 17, f"Expected 17 providers, got {len(PROVIDERS)}"
    expected_keys = {
        "annas_archive", "bne", "bnf_gallica", "british_library", "ddb",
        "dpla", "e_rara", "europeana", "google_books", "hathitrust",
        "internet_archive", "loc", "mdz", "polona", "sbb_digital",
        "slub", "wellcome",
    }
    assert set(PROVIDERS.keys()) == expected_keys

    # Each value is a (search_fn, download_fn, display_name) tuple
    for key, value in PROVIDERS.items():
        assert isinstance(value, tuple) and len(value) == 3, (
            f"PROVIDERS[{key!r}] is not a 3-tuple"
        )
        search_fn, download_fn, display_name = value
        assert callable(search_fn), f"{key}: search_fn not callable"
        assert callable(download_fn), f"{key}: download_fn not callable"
        assert isinstance(display_name, str), f"{key}: display_name not str"


def test_main_cli_package_surface() -> None:
    _import_and_check("main.cli", ["main", "run_cli", "create_cli_parser"])


def test_main_ui_package_surface() -> None:
    _import_and_check(
        "main.ui",
        [
            "ConsoleUI",
            "DownloadConfiguration",
            "InteractiveWorkflow",
            "run_interactive",
            "run_interactive_session",
            "process_csv_batch_with_stats",
            "process_single_work",
            "get_general_config",
            "run_with_mode_detection",
        ],
    )


def test_main_orchestration_package_surface() -> None:
    _import_and_check(
        "main.orchestration",
        [
            "run_batch_downloads",
            "process_direct_iiif",
            "create_interactive_callbacks",
            "process_work",
            "search_and_select",
            "execute_download",
            "load_enabled_apis",
            "filter_enabled_providers_for_keys",
            "QUOTA_LIMITED_PROVIDERS",
            "DownloadTask",
            "DownloadScheduler",
            "get_parallel_download_config",
            "collect_candidates_all",
            "collect_candidates_sequential",
            "select_best_candidate",
        ],
    )


def test_main_state_package_surface() -> None:
    _import_and_check(
        "main.state",
        [
            "StateManager",
            "get_state_manager",
            "QuotaManager",
            "ProviderQuota",
            "get_quota_manager",
            "DeferredQueue",
            "DeferredItem",
            "get_deferred_queue",
            "BackgroundRetryScheduler",
            "get_background_scheduler",
            "start_background_scheduler",
            "stop_background_scheduler",
        ],
    )


def test_main_data_package_surface() -> None:
    _import_and_check(
        "main.data",
        [
            "ENTRY_ID_COL",
            "TITLE_COL",
            "CREATOR_COL",
            "STATUS_COL",
            "LINK_COL",
            "PROVIDER_COL",
            "TIMESTAMP_COL",
            "DIRECT_LINK_COL",
            "load_works_csv",
            "get_pending_works",
            "get_stats",
            "mark_success",
            "mark_failed",
            "mark_deferred",
            "build_index_row",
            "update_index_csv",
            "read_index_csv",
            "get_processed_work_ids",
            "compute_work_id",
            "compute_work_dir",
            "check_work_status",
            "create_work_json",
            "update_work_status",
            "format_candidates_for_json",
            "format_selected_for_json",
            "get_naming_config",
        ],
    )


def test_provider_connector_uniform_shape() -> None:
    """Every provider module exposes search_<key> and download_<key>_work.

    Guards against future provider additions forgetting to match the
    uniform shape the PROVIDERS registry assumes.
    """
    from api.providers import PROVIDERS

    for key, (search_fn, download_fn, _name) in PROVIDERS.items():
        assert search_fn.__module__.startswith("api.providers."), (
            f"{key}: search_fn lives outside api.providers (reverse dep?)"
        )
        assert download_fn.__module__.startswith("api.providers."), (
            f"{key}: download_fn lives outside api.providers (reverse dep?)"
        )


def test_no_provider_to_main_reverse_dependency() -> None:
    """Guard the architectural invariant: api/ must not import from main/.

    Walks every module under api/ and inspects its compiled imports for
    any `main` references. The Anna's Archive provider's lazy
    ``from main.quota_manager import get_quota_manager`` is the concrete
    case this guard is meant to prevent regressions of.
    """
    import ast
    from pathlib import Path

    api_root = Path(__file__).resolve().parent.parent.parent / "api"
    assert api_root.is_dir(), f"api/ not found at {api_root}"

    bad_imports: list[tuple[str, str]] = []
    for path in api_root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "main" or alias.name.startswith("main."):
                        bad_imports.append((str(path), alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == "main" or node.module.startswith("main.")
                ):
                    bad_imports.append((str(path), node.module))

    assert not bad_imports, (
        "api/ has reverse dependencies on main/: "
        + ", ".join(f"{p}: {m}" for p, m in bad_imports)
    )
