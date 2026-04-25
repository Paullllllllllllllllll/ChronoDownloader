# ChronoDownloader Test Suite

This directory contains the comprehensive test suite for ChronoDownloader.

## Test Structure

```
tests/
├── conftest.py              # Shared fixtures and test configuration
├── pytest.ini               # Pytest configuration (in project root)
├── requirements-dev.txt     # Test dependencies (in project root)
├── unit/                    # Unit tests (fast, isolated)
│   ├── test_budget.py       # Download budget tracking tests
│   ├── test_config.py       # Configuration management tests
│   ├── test_context.py      # Thread-local context tests
│   ├── test_download_scheduler.py  # Parallel download scheduler tests
│   ├── test_matching.py     # Fuzzy matching algorithm tests
│   ├── test_model.py        # Data model tests (SearchResult, etc.)
│   ├── test_naming.py       # Filename sanitization tests
│   ├── test_unified_csv.py  # CSV operations tests
│   └── test_utils.py        # Utility function tests
└── integration/             # Integration tests (mocked APIs)
    ├── test_pipeline.py     # Pipeline workflow tests
    ├── test_providers.py    # Provider API tests
    └── test_selection.py    # Candidate selection tests
```

## Running Tests

### Install Test Dependencies

```bash
# Using pip
pip install -r requirements-dev.txt

# Or install individually
pip install pytest pytest-cov pytest-mock
```

### Run All Tests

```bash
# From project root
pytest tests/ -v

# With coverage report
pytest tests/ --cov=api --cov=main --cov-report=html
```

### Run Specific Test Categories

```bash
# Unit tests only
pytest tests/unit/ -v

# Integration tests only
pytest tests/integration/ -v

# Specific test file
pytest tests/unit/test_matching.py -v

# Specific test class
pytest tests/unit/test_matching.py::TestTitleScore -v

# Specific test
pytest tests/unit/test_matching.py::TestTitleScore::test_exact_match -v
```

### Run with Markers

```bash
# Skip slow tests
pytest tests/ -v -m "not slow"

# Skip network tests
pytest tests/ -v -m "not network"
```

## Test Coverage

The test suite covers the following modules:

### Core API (`api/core/`)
- **budget.py** - Download budget tracking and limits
- **config.py** - Configuration loading and caching
- **context.py** - Thread-local context management
- **naming.py** - Filename sanitization and standardization

### API Utilities (`api/`)
- **matching.py** - Fuzzy text matching for title/creator comparison
- **model.py** - SearchResult dataclass and conversion utilities
- **utils.py** - File download, JSON saving, IIIF rendering downloads

### Main Pipeline (`main/`)
- **unified_csv.py** - CSV loading, status tracking, thread-safe updates
- **download_scheduler.py** - Parallel download management
- **selection.py** - Candidate collection and best-match selection
- **pipeline.py** - Work directory management, index CSV updates

## Writing New Tests

### Fixtures

Common fixtures are defined in `conftest.py`:

```python
# Temporary directories
def temp_dir() -> str: ...
def temp_output_dir(temp_dir) -> str: ...

# Configuration
def sample_config() -> Dict[str, Any]: ...
def config_file(temp_dir, sample_config) -> str: ...
def mock_config(sample_config): ...

# Test data
def sample_csv_data() -> pd.DataFrame: ...
def sample_csv_file(temp_dir, sample_csv_data) -> str: ...
def sample_search_result() -> SearchResult: ...

# Mock responses
def mock_ia_search_response() -> Dict[str, Any]: ...
def mock_gallica_search_response() -> Dict[str, Any]: ...
```

### Test Patterns

```python
# Unit test example
class TestMyFunction:
    def test_basic_case(self):
        result = my_function("input")
        assert result == "expected"
    
    def test_edge_case(self):
        result = my_function("")
        assert result is None

# Integration test with mocks
class TestProviderSearch:
    def test_search_returns_results(self, mock_response):
        with patch("api.provider_api.make_request", return_value=mock_response):
            results = search_provider("query")
            assert len(results) >= 1
```

## Test Markers

Available pytest markers (defined in `pytest.ini`):

- `@pytest.mark.unit` - Unit tests (fast, no external dependencies)
- `@pytest.mark.integration` - Integration tests (may use mocks)
- `@pytest.mark.slow` - Slow running tests
- `@pytest.mark.network` - Tests requiring network access

## Continuous Integration

The test suite is designed to run in CI environments:

```yaml
# Example GitHub Actions
- name: Run tests
  run: |
    pip install -r requirements-dev.txt
    pytest tests/ -v --tb=short
```

## Test Results Summary

Current test status: **305 tests passing**

| Category | Tests | Status |
|----------|-------|--------|
| Unit Tests | 253 | ✅ Pass |
| Integration Tests | 52 | ✅ Pass |
| **Total** | **305** | ✅ **All Pass** |
