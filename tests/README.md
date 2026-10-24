# ChronoDownloader Test Suite

Pytest configuration (test paths, markers, options) lives in
`[tool.pytest.ini_options]` in `pyproject.toml`; pytest comes with the `dev`
dependency group.

## Structure

The folders mirror the `chronodownloader` package:

```text
tests/
├── conftest.py   # suite-wide: log folder per test, logger handlers restored
├── cli/          # help goldens, agent contract, README option check
├── core/         # architecture, coordination, SQLite helper, spec, inputs, budget
├── fixtures/     # recorded provider responses, jev_eval matching fixture
├── iiif/         # manifest parsing, renderings, page download, completeness
├── matching/     # fuzzy scores, judge client, policy, regression gate
├── net/          # paced requests, downloads, payload checks, impersonation
├── options/      # option tables, wizard, prompts, settings loader
├── output/       # store, leases, paths, index.csv
├── process/      # exit codes, heartbeat, interrupt, log setup
├── providers/    # one module per library, registry, page sampling
├── run/          # end-to-end commands against fake providers (support.py)
├── settings/     # settings file and its errors
├── support/      # hermetic helpers and the scripted prompter
└── ui/           # wizard parity and scripted guided sessions
```

## Running Tests

```bash
uv run pytest                                            # full suite
uv run --with pytest-xdist pytest -q -n auto             # in parallel
uv run pytest tests/providers/test_mdz.py                # one module
uv run pytest -m "not slow"                              # skip slow tests
```

No test touches the network: HTTP goes to a local test server or to recorded
responses under `tests/fixtures/providers/`. The `live` marker is reserved for
opt-in tests against the real libraries and is deselected by default.
