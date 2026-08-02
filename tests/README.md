# ChronoDownloader Test Suite

Pytest-based test suite for ChronoDownloader. Pytest configuration
(test paths, markers, options) lives in `[tool.pytest.ini_options]` in
the project-root `pyproject.toml`; test dependencies install via the
`dev` extra.

## Structure

```
tests/
├── conftest.py     # Shared fixtures (temp dirs, config, CSV data,
│                   # SearchResult samples, mocked provider responses)
├── unit/           # Unit tests: fast, isolated; cover api/core/
│                   # (config, network, budget, context, naming,
│                   # download), matching, models, IIIF parsing, CLI
│                   # contract and subcommands, interactive UI,
│                   # orchestration, state/quota/deferred queue,
│                   # CSV and index I/O
├── integration/    # Integration tests with mocked APIs: pipeline,
│                   # providers, selection
└── staging/        # Live-network staging configs and CSVs for
    │               # manual multi-provider verification runs
    └── run_matrix.sh
```

## Running Tests

```bash
# Install with test dependencies
uv sync --extra dev

# Run the full suite
uv run pytest

# Unit or integration tests only
uv run pytest tests/unit
uv run pytest tests/integration

# A specific file, class, or test
uv run pytest tests/unit/test_matching.py
uv run pytest tests/unit/test_matching.py::TestTitleScore
uv run pytest tests/unit/test_matching.py::TestTitleScore::test_exact_match
```

## Markers

`pyproject.toml` declares `unit`, `integration`, `slow`, and `network`.
Only `slow` is applied in practice, on the few long-running tests; the
directory layout already separates unit from integration, and no test
touches the live network. To skip the long ones:

```bash
uv run pytest -m "not slow"
```

## Writing New Tests

Reuse the shared fixtures from `conftest.py` (temporary directories,
sample configuration, sample works CSV data, `SearchResult` samples,
and mocked provider search responses) rather than building ad hoc
setup. Unit tests go in `tests/unit/`, mocked-API workflow tests in
`tests/integration/`. Mock all HTTP traffic; live-provider checks
belong in `tests/staging/`, which is run by hand.
