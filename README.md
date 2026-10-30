# ChronoDownloader v1.4.1

ChronoDownloader finds and downloads digitized historical sources from 17 digital
libraries. Given a CSV of works, it searches the libraries, has a decision model
judge whether each catalog record describes the sought work, and downloads the
preferred copy as a PDF or as page images. Every output folder holds a SQLite
store that records each work's candidates, judgments, download attempts and files,
so an interrupted run resumes and several agents can work the same folder and the
same libraries at once. The commands serve scripts and agents; `chronodownloader
ui` walks you through a run in the terminal. The downloads feed
[ChronoTranscriber][transcriber] and [ChronoMiner][miner]. Please report bugs on
the [GitHub issue tracker][issues].

[transcriber]: https://github.com/Paullllllllllllllllll/ChronoTranscriber
[miner]: https://github.com/Paullllllllllllllllll/ChronoMiner
[issues]: https://github.com/Paullllllllllllllllll/ChronoDownloader/issues

## Overview

A work is one row of the works CSV, named by its `entry_id`. A `download` run
takes each work in turn: it searches the providers in preference order, scores
every candidate record against the work's title, creator and year, asks the
decision model whether the record describes the same work and in which relation
(same edition, other edition, translation), and decides the work. An accepted
work is downloaded from the preferred acceptable candidate into its own folder of
the output root; a work without an acceptable candidate ends `no_match`, and a
doubtful one `needs_review`, where an agent or a person settles it with `inspect`
and `fetch`. The store `chronodownloader.sqlite` in the output root is the only
status record, and `index.csv` beside it is rendered from the store after each
run. For `works.csv` and `--output out`, a run writes
`out/chronodownloader.sqlite`, `out/index.csv` and one folder per work, such as
`out/e_0001_le_cuisinier_francois/objects/`.

## Installation

ChronoDownloader needs Python 3.13 or later and [uv](https://docs.astral.sh/uv/).
Install it from a clone, since the settings file and the log folder are read from
the repository.

```bash
git clone https://github.com/Paullllllllllllllllll/ChronoDownloader.git
cd ChronoDownloader
uv sync                       # or uv sync --all-extras, with the dev group
mkdir config
cp chronodownloader/resources/settings.example.yaml config/settings.yaml
export OPENROUTER_API_KEY="sk-or-..."   # PowerShell: $env:OPENROUTER_API_KEY = "..."
```

Gallica and BNE refuse the requests client behind Cloudflare; their requests go
through [curl_cffi][curl_cffi] with a Chrome fingerprint (`network.impersonate`).
The decision model runs on OpenRouter and reads its key from
`OPENROUTER_API_KEY`; without that key, `--matching fuzzy` matches on text
alone. Europeana, DPLA, DDB and Google Books need their own keys (see
Providers); a provider whose key is unset is skipped with a warning. The settings
name each key's environment variable (`api_keys:`), never the key.

[curl_cffi]: https://github.com/lexiforest/curl_cffi

## Quick Start

```bash
uv run chronodownloader download --input works.csv --output out
uv run chronodownloader download --input works.csv --output out --dry-run --json
uv run chronodownloader search --title "Le cuisinier francois" --year 1651
uv run chronodownloader status --output out --only needs_review
uv run chronodownloader inspect --entry-ids E0001 --output out
uv run chronodownloader fetch --entry-id E0001 --provider mdz --id bsb11111111 \
  --output out --reason "same edition, title page checked"
uv run chronodownloader classify --output out
uv run chronodownloader providers
uv run chronodownloader ui
```

Bare `chronodownloader` prints the commands, `<command> --help` the options of
one. Relative paths resolve against the current directory; with
`uv --directory <clone> run ...` that is the clone, so pass absolute paths.

A works CSV needs the columns `entry_id` and `title`:

```text
entry_id,title,creator,year,language
E0001,Le cuisinier françois,La Varenne,1651,fre
E0002,Die süddeutsche Küche,Katharina Prato,1858,ger
```

## Command Reference

The tables list every option of each command with its `--help` wording and
default. Options without a stated default are off or unset until given. The last
column says whether the settings `defaults:` block can set the option; the key is
the flag without dashes, with underscores for hyphens (`edition_policy`). A list
option takes comma-separated values.

### download

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--input FILE` | works CSV with entry_id and title | | no |
| `--entry-ids LIST` | only these entry_id values | all rows | no |
| `--limit N` | at most N works after the filters | all | no |
| `--only LIST` | only works in these stored statuses; from new, complete, partial, failed, no_match, needs_review, deferred, blocked | new,partial,failed,deferred,blocked | no |
| `--research` | search and judge again; old candidates kept as history | | no |
| `--matching {judge,fuzzy,hybrid}` | judge needs the OpenRouter key; fuzzy needs none | judge | yes |
| `--edition-policy {any_edition,same_edition,any_language}` | editions accepted without review | any_edition | yes |
| `--providers LIST` | providers to search, in preference order | defaults.providers | yes |
| `--max-candidates N` | candidates kept per provider | 5 | yes |
| `--search-timeout SECONDS` | per-provider search timeout; 0 disables | 60 | yes |
| `--format {images,pdf,both}` | what to download: pdf takes the item's PDF when it offers one, else its page images; images takes page images only; both takes both | pdf | yes |
| `--max-pages N` | page limit per work, binding page images only; the smaller of this and the provider's max_pages applies; 0 = none | 0 | yes |
| `--include-metadata`, `--no-include-metadata` | save the metadata files | on | yes |
| `--force` | re-download complete works; take over unstored folders | | no |
| `--total-images-gb GB` | run size limit of page images in GB; 0 = none | download_limits.total.images_gb | no |
| `--total-pdfs-gb GB` | run size limit of PDFs in GB; 0 = none | download_limits.total.pdfs_gb | no |
| `--total-metadata-gb GB` | run size limit of metadata files in GB; 0 = none | download_limits.total.metadata_gb | no |
| `--work-images-gb GB` | per-work size limit of page images in GB; 0 = none | download_limits.per_work.images_gb | no |
| `--work-pdfs-gb GB` | per-work size limit of PDFs in GB; 0 = none | download_limits.per_work.pdfs_gb | no |
| `--work-metadata-mb MB` | per-work size limit of metadata files in MB; 0 = none | download_limits.per_work.metadata_mb | no |
| `--on-exceed {stop,skip}` | stop the run or skip the file at a limit | download_limits.on_exceed | no |
| `--decisions {off,shadow}` | shadow checks the downloaded and review-band works' first pages after the downloads and records the answers only; apply waits for a validated threshold | off | yes |
| `--classify` | classify the downloaded works' pages after the downloads | | no |
| `--decision-model ID` | decision model id; vendor/model ids go to OpenRouter | decisions.model | no |
| `--decision-provider {openai,openrouter}` | dialect and key of the decision model | from the model id | no |
| `--decision-endpoint URL` | Decisions endpoint URL | the provider's endpoint | no |
| `--output DIR` | output root holding chronodownloader.sqlite | downloaded_works | yes |
| `--concurrency N` | works processed in parallel | 3 | yes |
| `--dry-run` | plan without network requests or writes | | no |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`--input` is required. A run takes the works of the CSV, narrowed by
`--entry-ids`, whose status is among `--only`: `new` for a work without a store
entry or without an outcome, `blocked` for one another run holds, else its stored
status. `complete`, `no_match` and `needs_review` works are left alone by
default; `--research` reopens them and searches and judges them again, keeping
the old candidates as history, and `--force` reopens `complete` works and
downloads their files again. A `deferred` work waits until its `retry_after`. A
work whose title, creator, year or other input fields changed in the CSV since its
last run is searched and judged again, as under `--research`. `--limit` applies
last. Without `--research`, a rerun reuses the stored candidates and the stored
judgments of the configured model, so it sends no search and no judge request for
a work already matched. The CSV is never rewritten.

A work is processed in three steps: matching (search, text score, judgment), the
decision (see Matching), and for an accepted work the download. `--concurrency`
works run at once; the providers of one work are searched one after another,
each under `--search-timeout`. A download fetches what `--format` names and
counts every expected page or file: a work whose expected items are all present,
within the page limit, is `complete`; one with some of them is `partial`; one with
none is `failed`, with the cause recorded. When the preferred candidate yields no
file (it fails, or its provider holds only metadata), the next acceptable
candidate in preference order is downloaded, and the work records every attempt's
reason. A rerun downloads only the pages and files a work still lacks. A work that
holds the files of one copy and is matched to another goes to `needs_review`
unless `--force`, which deletes the other copy's files first. The size limits
count the bytes written in this run and per work; at a limit, `--on-exceed stop`
starts no further work and `skip` leaves the file out.

After the last download of the run and before `index.csv` is written, a
post-phase can check the works it reached; a dry run and a run a size limit
stopped skip it. `--decisions shadow` sends one gate request per work to the
decision model (`--decision-*` flags, else the settings' `decisions:` block,
with the key and dialect rules of `classify`). A work this run left `complete` or
`partial` is checked on the first 10 page images of its selected candidate, read
from disk; a work with a PDF only is recorded `unavailable`. A work this run left
`needs_review` in the review band, or held by the edition policy, is checked on
the first 10 pages of the candidate its reason names, sampled into its `inspect`
folder; metadata-only works and rejected links get no check. Pages go at 1,500
pixels on the longer side. The request sends the sought work and the numbered
pages and asks whether one page is the sought work's title page
(`title_page_of_sought`), whether the pages come from a copy of it (`same_work`),
what they show relative to it (`relation`: `same_edition`, `other_edition`,
`translation_or_adaptation`, `different_work`, `insufficient`) and each page's
type. A check is stored in `page_checks` under the work's lease, which leaves
the work's row as it was; one whose candidate, leading page files, work status
or sought work changed while it was asked is dropped as stale, and a work
another run holds is skipped as blocked. A stored answer is reused while the
model, dialect, endpoint, question version, profile, sought work, item and page
hashes all match, as a new row that names the check it copies. Shadow mode
records the answers and changes no status, decision, download order or exit
code; an unreadable page, a failed request, an outage or a store error counts
the check `failed`. Acting on the
answers (`--decisions apply`) waits until a holdout validates a threshold.
`--classify` labels and reads the title pages of the works this run downloaded,
as `classify` does.

### fetch

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--entry-id ID` | work the item belongs to | | no |
| `--title TEXT` | title of a work not yet in the store | stored title | no |
| `--url URL` | IIIF manifest or item URL; excludes --provider and --id | | no |
| `--provider KEY` | provider of --id | | no |
| `--id ID` | provider item identifier | | no |
| `--reason TEXT` | reason recorded with the download; required when the work is needs_review | | no |
| `--format {images,pdf,both}` | what to download: pdf takes the item's PDF when it offers one, else its page images; images takes page images only; both takes both | pdf | yes |
| `--max-pages N` | page limit per work, binding page images only; the smaller of this and the provider's max_pages applies; 0 = none | 0 | yes |
| `--include-metadata`, `--no-include-metadata` | save the metadata files | on | yes |
| `--force` | re-download complete works; take over unstored folders | | no |
| `--total-images-gb GB` | run size limit of page images in GB; 0 = none | download_limits.total.images_gb | no |
| `--total-pdfs-gb GB` | run size limit of PDFs in GB; 0 = none | download_limits.total.pdfs_gb | no |
| `--total-metadata-gb GB` | run size limit of metadata files in GB; 0 = none | download_limits.total.metadata_gb | no |
| `--work-images-gb GB` | per-work size limit of page images in GB; 0 = none | download_limits.per_work.images_gb | no |
| `--work-pdfs-gb GB` | per-work size limit of PDFs in GB; 0 = none | download_limits.per_work.pdfs_gb | no |
| `--work-metadata-mb MB` | per-work size limit of metadata files in MB; 0 = none | download_limits.per_work.metadata_mb | no |
| `--on-exceed {stop,skip}` | stop the run or skip the file at a limit | download_limits.on_exceed | no |
| `--output DIR` | output root | downloaded_works | yes |
| `--dry-run` | plan without network requests or writes | | no |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`fetch` downloads one known item into a work without search or judgment. It needs
`--entry-id` and either `--url` or `--provider` with `--id`. A work not yet in the
store needs `--title`. `--url` takes a IIIF manifest URL or an item or viewer page
of a provider whose host the tool knows (for example
`https://www.digitale-sammlungen.de/view/bsb00027043`); the item's metadata is
read from its manifest. A URL that names no known provider, or a page the
provider cannot resolve to an item, exits 2. A `needs_review` work needs
`--reason`, which is stored as the work's `status_reason` and appears in
`index.csv`; this is how an agent closes a review. A `complete` work is skipped
unless `--force` is given. `fetch` removes the work's `inspect` pages. An item of
a metadata-only provider (HathiTrust, a DPLA record without a manifest) leaves
the work `needs_review` with a `metadata_only` reason that names where the item
is held.

### search

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--input FILE` | works CSV with entry_id and title | | no |
| `--title TEXT` | ad hoc query | | no |
| `--creator NAME` | creator of the ad hoc query | | no |
| `--year YEAR` | year of the ad hoc query | | no |
| `--entry-id ID` | work id of an ad hoc query, used with --output | adhoc | no |
| `--entry-ids LIST` | only these entry_id values | all rows | no |
| `--limit N` | at most N works after the filters | all | no |
| `--only LIST` | only works in these stored statuses; from new, complete, partial, failed, no_match, needs_review, deferred, blocked | new,partial,failed,deferred,blocked | no |
| `--research` | search and judge again; old candidates kept as history | | no |
| `--matching {judge,fuzzy,hybrid}` | judge needs the OpenRouter key; fuzzy needs none | judge | yes |
| `--edition-policy {any_edition,same_edition,any_language}` | editions accepted without review | any_edition | yes |
| `--providers LIST` | providers to search, in preference order | defaults.providers | yes |
| `--max-candidates N` | candidates kept per provider | 5 | yes |
| `--search-timeout SECONDS` | per-provider search timeout; 0 disables | 60 | yes |
| `--output DIR` | write the results into this output root | print the results | no |
| `--dry-run` | plan without network requests or writes | | no |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`search` takes exactly one of `--input` and `--title`. Without `--output` it
prints one table row per candidate (work, `provider:item`, year, text score,
`same_work` probability, relation, decision, title; `*` marks the accepted item)
on stdout, or on stderr with `--json`, and writes nothing. With `--output` it
stores the candidates, judgments and the resulting status in that root and
downloads nothing; a work with an accepted candidate keeps no outcome but
records the selected candidate, so the next `download` takes it as `new` and
reuses the judgments. The selection options act as in `download`.

### inspect

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--entry-ids LIST` | inspect the stored candidates of these works; needs --output | | no |
| `--provider KEY` | provider of --id; inspects one item outside the store | | no |
| `--id ID` | provider item identifier; inspects one item outside the store | | no |
| `--providers LIST` | only candidates of these providers | all | yes |
| `--first N` | pages sampled per candidate | 10 | no |
| `--output DIR` | store root; pages go to <output>/_inspect/<entry_id>/<provider>_<id>/ | a scratch folder in %TEMP%\chronodownloader\inspect | yes |
| `--decision-model ID` | page model id of the page check; vendor/model ids go to OpenRouter | judge.page_model | no |
| `--decision-provider {openai,openrouter}` | dialect and key of the page model | from the model id | no |
| `--decision-endpoint URL` | Decisions endpoint URL of the page model | judge.endpoint; with another model, the provider's | no |
| `--dry-run` | plan without network requests or writes | | no |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`inspect` takes exactly one of `--entry-ids` (with `--output`) and `--provider`
with `--id`. It downloads the first `--first` pages of each current stored
candidate at most 1,500 pixels on the longer side, into
`<output>/_inspect/<entry_id>/<provider>_<id>/`, and asks the page model one
question over those pages together: whether one of them is the sought work's
title page, whether one shows the sought imprint year, and which kind of page
best shows the title. The table reports `title_page` and `imprint_year` as
probabilities and `page_type` (`title_page`, `half_title`, `cover_or_binding`,
`frontispiece_or_plate` and others). An item named by `--provider` and `--id` is
checked against every work of `--output` whose candidates include it; without
such a work its pages go to `%TEMP%\chronodownloader\inspect\<provider>_<id>\` and
no question is asked. The store records nothing of an inspection. A page-model
outage defers that item's check; a metadata-only provider offers no pages and is
skipped. The `--decision-*` flags replace `judge.page_model` for the check: a
`vendor/model` id goes to OpenRouter with `OPENROUTER_API_KEY`, any other to
OpenAI with `OPENAI_API_KEY`, unless `--decision-provider` names the dialect, and
the endpoint is that dialect's unless `--decision-endpoint` names one.

### verify

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--entry-ids LIST` | only these works | all works | no |
| `--pages` | also check title page and imprint year with the page model | | no |
| `--first N` | pages sampled for the check; only with --pages | 10 | no |
| `--decision-model ID` | page model id of the page check; vendor/model ids go to OpenRouter; only with --pages | judge.page_model | no |
| `--decision-provider {openai,openrouter}` | dialect and key of the page model; only with --pages | from the model id | no |
| `--decision-endpoint URL` | Decisions endpoint URL of the page model; only with --pages | judge.endpoint; with another model, the provider's | no |
| `--output DIR` | output root to verify | downloaded_works | yes |
| `--dry-run` | report without changing statuses | | no |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`verify` compares every file the store records with the disk. A missing file
loses its record, and a `complete` work that lost one becomes `partial` with the
reason recorded, so the next `download` fetches it again; a file of another size
is reported and fails the check but is not repaired. With `--pages`, the first
`--first` page images on disk are sent to the page model as in `inspect`; the
answer is reported and never changes a status, so a wrong copy is replaced with
`fetch --force` or removed with `drop`. The `--decision-*` flags choose the page
model as in `inspect`. A work with a PDF only gets no page
check. Works another run holds are reported `blocked` and left alone.
`--dry-run` makes the same comparison through read-only queries.

### classify

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--entry-ids LIST` | only these works | all works | no |
| `--force` | ask every page again, current labels included | | no |
| `--decision-model ID` | decision model id; vendor/model ids go to OpenRouter | decisions.model | no |
| `--decision-provider {openai,openrouter}` | dialect and key of the decision model | from the model id | no |
| `--decision-endpoint URL` | Decisions endpoint URL | the provider's endpoint | no |
| `--output DIR` | output root to classify | downloaded_works | yes |
| `--dry-run` | list the pages to ask without requests or writes | | no |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`classify` asks a decision model for the page type of every page image the store
records (title page, half-title, preface, privilege, contents, body text, plate,
blank leaf, binding and others; the `culinary` profile adds recipes, menus and
carving) and stores one label per page in `page_labels`. The pages of a work go in
source order, downscaled to `decisions.image_max_px` on the longer side, at most
`decisions.pages_per_request` to a request, with the work's input fields as
context. A model id of the form `vendor/model` goes to OpenRouter with
`OPENROUTER_API_KEY`, any other to OpenAI with `OPENAI_API_KEY`;
`--decision-provider` overrides that, and every `--decision-*` flag wins over the
settings' `decisions:` block. A page whose label was written for its current file
and the same request settings (model, dialect, endpoint, profile, image size,
grouping) is not asked again; `--force` asks every page. A work is classified
under its lease, so a work another run holds is counted `blocked`, and a label
whose file was replaced while it was asked is dropped as stale. A work with a PDF
only is skipped. The view `title_page_picks` names each work's title page from
the labels, and the summary carries the requests' token usage. `classify` never
changes a status.

Once a work's leading pages are all labelled and the view picks a title page, a
chat model reads that page (`title_pages.model`, by default
`anthropic/claude-haiku-5.5` through OpenRouter's chat endpoint with
`OPENROUTER_API_KEY`), sent alone at `title_pages.image_max_px` (1,500 pixels) on
the longer side. It answers in a fixed JSON schema: whether the page is the
book's title page, the title as printed, the place, the year as an integer and
the date as printed; author and printer are not asked. The reading is stored in
`title_pages` and shown through the view `title_page_readings`, which holds only
the reading of the current pick and file. Every field there is a model reading,
not catalog data. A rerun reads a page again only when the pick, its file, the
model or the prompt version changed, or under `--force`; a work whose pick is
gone loses its reading. A reading that fails or does not fit the schema fails the
work, and an outage defers it. `title_pages.model: null` turns the step off. On
the 47 title pages of the evaluation, this route read years as well as the same
prompt through the Anthropic API (38 of 39 exact; a year invented on 3 of 8
undated pages) and titles and places nearly as well (median token-set similarity
97 against 98; places 39 of 43 against 42), at USD 0.00025 a page.

### status

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--input FILE` | works CSV; rows without a store entry count as new | | no |
| `--entry-ids LIST` | only these entry_id values | all rows | no |
| `--only LIST` | only works in these statuses; from new, complete, partial, failed, no_match, needs_review, deferred, blocked | all | no |
| `--output DIR` | output root; statuses, review queue, _inspect leftovers | downloaded_works | yes |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`status` lists each work with its status, file count, pages present of expected,
selected item, `retry_after` and reason, then the review queue (the
`needs_review` works with their reasons), the `unstored` folders and the
`inspect` folders left under `<output>/_inspect/`. It reads only, sends nothing
and writes no log; it exits 0 unless its options or the store are refused.

### drop

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--entry-ids LIST` | drop these works | | no |
| `--only LIST` | drop the works in these stored statuses; from complete, partial, failed, no_match, needs_review, deferred, blocked | | no |
| `--all` | drop every work of the store | | no |
| `--output DIR` | output root | | no |
| `--dry-run` | list what would be dropped | | no |
| `--json` | print one JSON summary line on stdout | | no |

`drop` needs `--output` and exactly one of `--entry-ids`, `--only` and `--all`.
It deletes each selected work's rows (work, candidates, attempts, files), its
recorded files, staging folders and `inspect` pages, and the folders left empty,
so the next run processes the work from scratch. Files no record names stay and
are listed. A work another run holds is refused and counted `blocked` (exit 1);
an entry id the store does not know is skipped. The runs stay recorded.
`index.csv` is rendered again afterwards. `--dry-run` lists what a drop would
delete and changes nothing. `drop` takes no `--settings`; it reads
`config/settings.yaml`, else the packaged example.

### providers

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--json` | print one JSON summary line on stdout | | no |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

`providers` lists the 17 providers with their capabilities (IIIF, downloads or
metadata only, image size cap), the environment variable of a required key and
whether it is set, the circuit breaker (closed, or open or half-open until a time,
with its cause) and the quota left in the current window. It reads the
coordination database without writing and exits 0.

### ui

| Flag | Description | Default | In `defaults:` |
| --- | --- | --- | --- |
| `--settings FILE` | alternate settings file | config/settings.yaml | no |

## Exit Codes and JSON Summary

| Code | Meaning |
| --- | --- |
| 0 | no work failed or stayed partial; works left `no_match`, `needs_review`, `deferred` or `blocked` are open, not failed, and are counted in the summary; a dry run that planned; a listing that was written |
| 1 | a work failed or stayed partial, a `fetch` download failed, an `inspect` candidate could not be sampled or checked, a `verify` work failed its check, a `classify` page could not be read or classified, a `drop` refused a work, or an unexpected error |
| 2 | usage or configuration error: invalid flags or settings, a missing or invalid works CSV, a store the tool refuses, an unset judge key under `--matching judge` or `hybrid` (the message names `--matching fuzzy`), an unset page-model key for `inspect` or `verify --pages`, an unset key of the decision provider of `classify` or of a `download` post-phase, a `fetch` of a `needs_review` work without `--reason`, a `fetch --url` no provider resolves |
| 130 | interrupted (Ctrl+C, or Ctrl+Break on Windows) |

`<command> --help` lists the exit codes of each command. Progress, warnings and
errors go to stderr, in color only on a terminal and when `NO_COLOR` is unset.
With `--json`, stdout carries exactly one JSON line on every exit, errors and
interrupts included; the tables of `search`, `inspect`, `verify`, `status`,
`drop` and `providers` then go to stderr, and `ui` writes no line. `--help`
prints the help on stdout and exits 0 without a summary. Every line carries
`dry_run` (true whenever `--dry-run` was given), `command` and the work counts,
then the command's own fields; every field is present on every exit path, zero
where nothing ran.

| Command | Fields |
| --- | --- |
| every command | `dry_run`, `command`, `items_total`, `items_complete`, `items_partial`, `items_failed`, `items_no_match`, `items_skipped`, `items_needs_review`, `items_deferred`, `items_blocked` |
| `download` | `gate_checks`, `gate_reused`, `gate_failed`, `gate_unavailable`, `gate_stale`, `gate_blocked`, `gate_requests`, `gate_input_tokens`, `gate_output_tokens`, `gate_cached_tokens`, `gate_cache_write_tokens`, `gate_cost`, and the fields of `classify` |
| `search` | `items_accepted` |
| `inspect` | `pages_sampled` |
| `verify` | `files_checked`, `files_missing`, `files_mismatched`, `pages_checked` |
| `classify` | `pages_total`, `pages_asked`, `pages_current`, `pages_labelled`, `pages_stale`, `pages_failed`, `title_pages`, `decision_requests`, `decision_input_tokens`, `decision_output_tokens`, `decision_cached_tokens`, `decision_cache_write_tokens`, `decision_cost`, `title_pages_transcribed`, `title_requests`, `title_input_tokens`, `title_output_tokens`, `title_cost` |
| `status` | `items_new`, `unstored_folders`, `inspect_leftovers` |
| `drop` | `files_deleted`, `files_kept` |
| `providers` | `breakers_open`, `keys_missing` |

For `download` and `fetch`, the counts are works by their outcome in this run;
`items_skipped` counts works the selection or a status re-check left alone, and
`items_blocked` works another run held. The gate fields of `download` count its
checks by outcome (`gate_checks` asked and recorded, the others reused, failed,
unavailable, stale or blocked) and its requests' usage; with `--classify` the
`classify` fields describe the post-phase, and both sets are zero when it is off.
`search` counts a work with an accepted
candidate as `items_accepted`, not `items_complete`, since nothing was
downloaded. `inspect` counts items: complete when checked, failed when not sampled
or the check failed, deferred when the page model is unavailable, skipped when an
item has no pages. `verify` counts works: complete when every recorded file is on
disk at its size, failed when files are missing or differ, or a page check
failed, skipped without recorded files. `classify` counts works: complete when
every page image carries a current label, failed when a page could not be read
or classified or a label went stale, deferred when the decision model is
unavailable, blocked when another run holds the work, skipped without page
images; `decision_cost` is null where the provider reports no cost (OpenAI).
`status` counts the listed works by
status, with `items_new` for works without an outcome. `drop` counts dropped works
as complete, works another run holds as blocked and unknown entry ids as skipped.
`providers` counts the providers in `items_total`. A dry run reports the plan's
counts and sends no network request, makes no judge call and writes nothing, no
log file included.

## Guided UI

`chronodownloader ui` asks first for the command: `download`, `fetch`, `search`
or `verify`. It then asks the command's steps, built from the same option tables
as the flags: for `download` the works CSV and the work selection, the matching
mode and edition policy, the providers, the format and page limit, the size
limits (the settings' limits or the run's own), the output root and the run
options; `search` first asks for a works CSV or an ad hoc query and whether to
print or store the results; `fetch` asks for a provider item or a IIIF manifest
URL. Every list offers Back. Settings defaults are preselected and marked `(s)`
on the review screen, which previews the plan without network requests and
prints the equivalent command. It offers Run, Edit a step, Copy command and quit,
and Quit. The run shows a progress display. The UI needs an interactive terminal
on stdin and stdout, else it exits 2; otherwise its exit codes are those of the
chosen command. `inspect`, `classify`, `status`, `drop` and `providers` are run
with flags.

## Settings File

`--settings FILE` reads the named file, which must exist. Otherwise
ChronoDownloader reads `config/settings.yaml` in the repository (gitignored),
else the packaged `chronodownloader/resources/settings.example.yaml`. Every key
is optional; a missing key keeps the value shown in the example, and unknown keys
or values of the wrong type are an error (exit 2) that names the file and the key.
The example file:

```yaml
# ChronoDownloader settings: machine values and optional run defaults.
#
# Copy this file to config/settings.yaml in the repository (gitignored) and
# edit the copy, or pass another file with --settings FILE. Every key is
# optional; a missing key keeps the value shown here. Unknown keys are errors.
# Relative paths resolve against the current directory.

# Environment-variable NAMES that hold each key (never the key itself).
api_keys:
  europeana: EUROPEANA_API_KEY
  dpla: DPLA_API_KEY
  ddb: DDB_API_KEY
  google_books: GOOGLE_BOOKS_API_KEY
  hathitrust: HATHI_API_KEY      # read but unused: HathiTrust is metadata-only
  annas_archive: ANNAS_ARCHIVE_API_KEY  # optional member key for fast downloads
  openrouter: OPENROUTER_API_KEY  # judge and page model; --matching fuzzy needs none
  openai: OPENAI_API_KEY          # classify through the OpenAI Decisions API

# Decision model that judges candidates and pages (OpenRouter Decisions API).
judge:
  endpoint: https://openrouter.ai/api/alpha/decisions
  model: typesafe/jev-1.14                 # text judgments: same_work and match
  page_model: openai/gpt-6-luna-decisions  # page check of verify --pages, inspect
  retry_after_minutes: 60                  # deferral after a persistent outage
  thresholds:
    same_work_accept: 0.9   # probability at or above: accept (judge, hybrid)
    same_work_reject: 0.1   # probability at or below: reject; between: review
    # text_* (text score total, 0 to 123.5; null: off) set on jev_eval: no
    # text score excludes different works, no same-work pair rejected or dropped
    text_accept: null       # at or above: accept (--matching fuzzy); null: review
    text_reject: 30         # at or below: reject (--matching fuzzy)
    text_prefilter: 30      # at or below: dropped unjudged (--matching hybrid)
  network:                  # pacing, retries and timeout of the endpoint
    delay_ms: 0
    jitter_ms: 0
    max_attempts: 3
    base_backoff_s: 1.5
    backoff_multiplier: 1.5
    max_backoff_s: 60.0
    timeout_s: 300
    slots: 4                # concurrent requests on this machine

# Decision model of classify and of the download post-phase (--decisions,
# --classify); its requests take judge.network's policy and slot limit, with
# pacing and a breaker of their own.
decisions:
  model: typesafe/jev-1.14  # vendor/model ids go to OpenRouter, others to OpenAI
  provider: null            # openai | openrouter; null: from the model id
  endpoint: null            # null: the provider's Decisions endpoint
  image_max_px: 768         # longer side of a page sent
  pages_per_request: 20     # pages per request, at most 400
  profile: general          # page types: general | culinary
  timeout_s: 120            # request timeout

# Chat model that transcribes each work's picked title page (classify, through
# OpenRouter with api_keys.openrouter); its readings are model output, not
# catalog data.
title_pages:
  model: anthropic/claude-haiku-5.5  # null turns the transcription off
  endpoint: https://openrouter.ai/api/v1/chat/completions
  image_max_px: 1500        # longer side of the page sent
  effort: low               # reasoning effort: minimal | low | medium | high
  timeout_s: 120            # request timeout

# Machine-wide coordination of providers and the judge across agents; the
# database is <state_dir>/coordination.sqlite.
coordination:
  default_slots: 2          # downloads per provider without its own slots
  pacing_tolerance_ms: 250  # latest a sender may be after its reserved time
  slot_ttl_s: 120           # a slot not renewed for this long expires
  slot_renew_s: 30          # renewal interval of a held slot
  busy_timeout_s: 30        # wait for a locked database before giving up

# Per-provider network policy and extras. A provider or key missing here keeps
# the value of the provider's built-in profile.
providers:
  internet_archive:
    max_pages: 0                  # pages per work; 0 = no limit
    slots: 2                      # concurrent downloads on this machine
    min_search_timeout_s: 0       # raises the run's search timeout; 0 = none
    network:
      delay_ms: 300               # minimum pause between requests
      jitter_ms: 150              # random extra pause up to this value
      max_attempts: 8             # tries per request, the first included
      base_backoff_s: 1.5         # first wait after a failed try
      backoff_multiplier: 1.5     # growth of the wait per try
      max_backoff_s: 60.0         # longest wait
      timeout_s: 30               # request timeout
      verify_ssl: true            # check the server certificate
      ssl_error_policy: fail      # fail | retry_insecure_once (never with keys)
      dns_retry: false            # retry a name that does not resolve
      impersonate: null           # curl_cffi browser profile, e.g. chrome; '' none
      headers: {}                 # extra request headers, name: value
      circuit_breaker_enabled: true
      circuit_breaker_threshold: 3       # consecutive failures that open it
      circuit_breaker_cooldown_s: 300.0  # seconds before the next probe
  mdz:
    slots: 1
    network: {delay_ms: 300, jitter_ms: 150, max_attempts: 25}
  bnf_gallica:
    slots: 1
    min_search_timeout_s: 180     # concurrent searches queue for the 8 s pacing
    network:
      delay_ms: 8000
      jitter_ms: 2000
      max_attempts: 25
      base_backoff_s: 3.0
      backoff_multiplier: 1.8
      max_backoff_s: 120.0
      impersonate: chrome         # Cloudflare refuses the requests client
  loc:
    network: {delay_ms: 300, jitter_ms: 150, headers: {Accept: application/json}}
  bne:
    network:
      delay_ms: 4000
      jitter_ms: 1000
      base_backoff_s: 2.0
      backoff_multiplier: 1.8
      max_backoff_s: 120.0
      timeout_s: 60
      impersonate: chrome         # Cloudflare refuses the requests client
  ddb:
    network: {delay_ms: 400, jitter_ms: 200}
  polona:                         # retry_insecure_once exists; ships off
    network: {delay_ms: 600, jitter_ms: 200, max_attempts: 8}
  europeana:
    network: {delay_ms: 400, jitter_ms: 200, max_attempts: 8}
  dpla:
    network:
      delay_ms: 400
      jitter_ms: 200
      max_attempts: 8
      headers: {Accept: application/json}
  google_books:
    free_only: true               # skip volumes that are not free to download
    prefer: pdf                   # pdf | epub
    allow_drm: false              # allow DRM-protected files
    max_files: 2                  # direct pdf or epub files per volume
    max_pages: 50                 # bound of the page-by-page image fallback
    slots: 1
    network:
      delay_ms: 2000
      jitter_ms: 500
      max_attempts: 3
      base_backoff_s: 2.0
      backoff_multiplier: 2.0
      max_backoff_s: 30.0
      circuit_breaker_threshold: 2
      circuit_breaker_cooldown_s: 600.0
      headers: {Referer: "https://books.google.com/"}
  wellcome:
    max_pages: 0                  # pages per work; 0 = no limit
    slots: 1
    network: {delay_ms: 300, jitter_ms: 150, max_attempts: 8}
  annas_archive:
    # Official mirrors on 20.10.2026: .gl, .pk, .gd. Search and md5 pages sit
    # behind a DDoS-Guard browser check (403); only the member API answers.
    base_url: https://annas-archive.gl  # '' makes the provider unavailable
    quota:
      enabled: true
      daily_limit: 875            # fast downloads per reset period
      reset_hours: 24
    slots: 1
    network:
      delay_ms: 800
      jitter_ms: 300
      max_attempts: 5
      base_backoff_s: 2.0
      backoff_multiplier: 2.0
      max_backoff_s: 45.0
      timeout_s: 45
      ssl_error_policy: fail      # stays fail: the member key is in the query
  slub:
    slots: 1
    network: {delay_ms: 600, jitter_ms: 200, max_attempts: 8}
  e_rara:
    slots: 1
    network: {delay_ms: 600, jitter_ms: 200, max_attempts: 8}
  sbb_digital:
    slots: 1
    network:
      delay_ms: 700
      jitter_ms: 250
      max_attempts: 8
      timeout_s: 40
      headers: {Accept: application/xml}
  british_library: {}             # its search host no longer resolves
  hathitrust: {}                  # metadata only

# Size limits of a run in GB (MB for metadata per work); 0 = no limit.
download_limits:
  total:
    images_gb: 100
    pdfs_gb: 50
    metadata_gb: 1
  per_work:
    images_gb: 5
    pdfs_gb: 3
    metadata_mb: 250
  on_exceed: stop               # stop the run | skip the file

# Work-folder names; a change renames the folders of new runs only.
naming:
  include_creator_in_work_dir: false
  include_year_in_work_dir: false
  title_slug_max_len: 80

# Folder of the tool's state files, an absolute path (~ allowed); '' uses
# ~/.chronodownloader.
state_dir: ''

# Log folder, or a file ending in .log; '' writes logs/application.log in the
# repository.
logs_dir: ''

# Run defaults keyed by option name; flags override them. The providers are
# searched in this order; a provider missing from the list is not used.
defaults:
  providers:
    - mdz
    - bnf_gallica
    - e_rara
    - internet_archive
    - wellcome
    - europeana
    - ddb
    - polona
  # output: downloaded_works
  # matching: judge             # judge | fuzzy | hybrid
  # edition_policy: any_edition # any_edition | same_edition | any_language
  # format: pdf                 # images | pdf | both
  # max_pages: 0                # 0 = no limit
  # max_candidates: 5           # candidates kept per provider
  # search_timeout: 60          # seconds per provider search; 0 = none
  # include_metadata: true
  # concurrency: 3              # works in parallel
  # decisions: shadow           # 'off' (quoted) | shadow: gate checks after downloads
```

The blocks:

- `api_keys` names the environment variable of each service's key. The keys are
  read from the process environment when a run starts; the tool never stores,
  logs or prints them, and every URL, error text and stored record carries them
  masked (`wskey=***`).
- `judge` configures the decision model: the endpoint, the text model, the page
  model, the deferral after an outage, the thresholds (see Matching) and the
  endpoint's pacing, retries, timeout and slots. The probability thresholds lie
  between 0 and 1, the text thresholds between 0 and 123.5; an explicit `null`
  switches a text threshold off.
- `coordination` tunes the machine-wide coordination database (see Coordination
  Across Runs).
- `providers` holds one block per provider key (`bnf_gallica` is Gallica's key;
  `gallica` is refused with that hint). `max_pages` caps the pages per work, and
  the smaller of it and `--max-pages` applies; `slots` caps that provider's
  concurrent downloads on the machine, else `coordination.default_slots`;
  `min_search_timeout_s` raises `--search-timeout` for that provider, unless
  the run sets 0 (Gallica's 180 s covers the queue of several runs' searches
  behind its 8 s pacing); `network` sets the pacing, retries, timeout, TLS
  policy, impersonation, extra headers and circuit breaker. A key missing from a
  block keeps the provider's built-in profile, which the example shows. Google
  Books adds `free_only`,
  `prefer`, `allow_drm` and `max_files`; Anna's Archive adds `base_url` and
  `quota`; a spent quota stops the download (see Coordination Across Runs).
- `download_limits` holds the size limits the `--total-*`, `--work-*` and
  `--on-exceed` flags override for one run.
- `naming` shapes the work-folder names of new works (see Output Folders).
- `state_dir` holds `coordination.sqlite`. It must be absolute, so that every
  process on the machine shares one database; a relative path is an error.
- `logs_dir` names the log folder or a `.log` file (see Logging).
- `defaults` sets run options by option name, as the Command Reference marks
  them. A flag wins over a settings default, and a settings default over the code
  default. `providers` lists the providers searched and their preference order;
  without the block, the eight providers of the example are searched in that
  order. `search` takes the matching defaults but not `output`; `inspect` takes
  `providers` and `output`; `fetch`, `verify` and `status` take `output`.

The environment variables the tool reads are the key variables of `api_keys`
(`OPENROUTER_API_KEY`, `EUROPEANA_API_KEY`, `DPLA_API_KEY`, `DDB_API_KEY`,
`GOOGLE_BOOKS_API_KEY`, `ANNAS_ARCHIVE_API_KEY` by default), `NO_COLOR` and
`CHRONO_HEARTBEAT_FILE` (see Job Launcher). The HTTP sessions ignore the
environment otherwise: proxy variables and `.netrc` credentials are not used.

## Providers

| Key | Library | Search | Download | Key variable |
| --- | --- | --- | --- | --- |
| `internet_archive` | Internet Archive | advanced search | the item's PDF or EPUB; page images from the Archive's page service, the IIIF canvas as fallback | none |
| `bnf_gallica` | BnF Gallica | SRU (Dublin Core) | IIIF | none |
| `loc` | Library of Congress | loc.gov JSON, the Books collection first | IIIF | none |
| `europeana` | Europeana | Search API | IIIF manifest, key added at fetch time | `EUROPEANA_API_KEY` |
| `dpla` | Digital Public Library of America | DPLA API | IIIF where the record names a manifest, else metadata only | `DPLA_API_KEY` |
| `ddb` | Deutsche Digitale Bibliothek | DDB API | the holding library's IIIF manifest | `DDB_API_KEY` |
| `british_library` | British Library | SRU, BNB SPARQL as fallback | IIIF | none |
| `mdz` | Münchener DigitalisierungsZentrum | JSON API, HTML search as fallback | IIIF (v2, else v3) | none |
| `polona` | Polona (National Library of Poland) | JSON gateway | IIIF | none |
| `bne` | Biblioteca Nacional de España | datos.bne.es SPARQL | BNE Digital PDFs in page ranges | none |
| `google_books` | Google Books | volume search | the volume's PDF or EPUB, page images as fallback | `GOOGLE_BOOKS_API_KEY` |
| `hathitrust` | HathiTrust | Bibliographic API by identifier | none: metadata only | none |
| `wellcome` | Wellcome Collection | catalogue API | IIIF, images capped at 1,024 px | none |
| `annas_archive` | Anna's Archive | HTML search on the mirror | PDF or EPUB through the member fast-download API, else public links | `ANNAS_ARCHIVE_API_KEY` (optional) |
| `slub` | SLUB Dresden | LOD API | IIIF by PPN | none |
| `e_rara` | e-rara.ch | SRU (MODS) | IIIF | none |
| `sbb_digital` | Staatsbibliothek zu Berlin | StaBiKat SRU | the METS document's PDF and page image files | none |

The providers searched by default are those of `defaults.providers`, in that
order: MDZ, Gallica, e-rara, Internet Archive, Wellcome, Europeana, DDB and
Polona; `--providers` names others. A provider that needs a key (Europeana,
DPLA, DDB, Google Books) is skipped with a warning while its variable is unset.
HathiTrust is metadata-only: it searches only when the title or creator carries
an identifier hint (`oclc:21726609`, `isbn:`, `lccn:`, `issn:`, `htid:`), and a
fetch of its items ends `needs_review` with a `metadata_only` reason. A DPLA
record without a manifest names the holding library's page (usually Internet
Archive or HathiTrust), which `fetch --url` can then download. DDB records often
point to the holding library's manifest (MDZ, Göttingen, Heidelberg), and
Europeana records to the contributing library's images; those requests are paced
and counted under the holding provider. Anna's Archive needs `base_url`, since
its mirrors move; its member key is optional, and each fast download draws one
unit of the daily quota (875 by default). Without a key, or when the API offers
no file, the public links of the item's page are tried.

Verified reachability from one machine on 23.-24.10.2026:

- Complete downloads: Internet Archive, MDZ, DDB (through MDZ), Polona, Wellcome,
  SLUB (by manifest URL), e-rara, SBB, Gallica, Europeana (Gallica records through
  Gallica's manifest) and BNE.
- Blocked by the library: Library of Congress (`loc.gov` answers every client,
  browser fingerprints included, with a Cloudflare challenge; `tile.loc.gov` with
  "Restricted Resource"), British Library (`sru.bl.uk`, `api.bl.uk` and
  `access.bl.uk` no longer resolve since the 2023 cyber attack, and no public
  IIIF route replaced them), Google Books (about 85% of searches answer 503,
  and PDF links end on Google's captcha page) and Anna's Archive (every mirror's
  search and item pages sit behind a DDoS-Guard check; the fast-download API
  refused a non-member key).
- SLUB's catalogue source record (`fetch --provider slub --id`) answered HTTP 500;
  the manifest URL of the same item worked.
- A search with a creator that finds nothing is repeated without the creator,
  since catalogs file names in forms a works CSV rarely gives (Gallica files
  Guillaume Tirel as "Taillevent").

## Works CSV

`entry_id` and `title` are required; `creator`, `year`, `place`, `publisher`,
`language`, `edition` and `link` are optional. `short_title` stands for `title`
and `main_author` for `creator`; where a row has both, the canonical column wins
and the alias fills it only when it is empty. Header names are compared without
surrounding spaces and case. Other columns are ignored and named in a warning.
The file is read as UTF-8 (a byte order mark is accepted); values are stripped,
an empty cell is unset, every row needs an entry id and a title, and no entry id
may repeat. The judge receives every field a row provides, so `place`,
`publisher`, `language` and `edition` sharpen its answers.

A row with a `link` is not searched. The link may be a IIIF manifest URL or an
item or viewer page of a known provider (MDZ, Wellcome, Polona, SBB, BNE, British
Library and the others); the item's manifest is read, its metadata becomes the
work's one candidate, and the candidate is judged like any other. A link the judge
rejects, or one that names no item the tool can resolve, leaves the work
`needs_review` with the reason.

## Matching

`--matching` sets how candidates are decided. Every candidate gets a text score
in every mode, which is stored and orders candidates; the decision depends on
the mode:

- `judge` (default): the decision model answers two questions about every
  candidate: `same_work`, the probability that the record describes the sought
  work, and `match`, a choice among `same_edition`, `other_edition`,
  `translation_or_adaptation`, `different_work` and `insufficient`. A
  `same_work` probability at or above `same_work_accept` (.9) accepts the
  candidate, one at or below `same_work_reject` (.1) rejects it, and one between
  sends it to review. The text score never accepts.
- `fuzzy`: the text score alone, for use without a model key. Different works
  reach every text score that a same-work pair reaches (a translation scored
  111.5 on the evaluation set), so `text_accept` is `null` and fuzzy accepts
  nothing on its own: every candidate above `text_reject` (30) goes to review,
  where `inspect` and `fetch` settle it.
- `hybrid`: a candidate with a text score at or below `text_prefilter` (30) is
  rejected unjudged; the judge decides the rest as under `judge`. The lowest
  same-work pair of the evaluation set scored 35.5.

The text score is the token-set ratio of the titles (0 to 100), plus up to 20
for the creator, 3 for a IIIF manifest and 0.5 for an item URL, minus 0.5 per
year by which the candidate's year lies more than 15 years from the sought year
(at most 25). The judge receives the work's fields and the candidate's catalog
record (title, creators, date and the provider's bibliographic fields); the page
model receives the first pages as images. Each judgment is stored with its
probabilities, model and model version, and reused on a rerun under the same
model. A judge outage that outlasts the retries defers the work for
`judge.retry_after_minutes` (`deferred`, with `retry_after`); a refused request
(an invalid key, spent credit) fails it. Nothing falls back to the text score.

An accepted candidate is acceptable when the edition policy allows its relation:

| `--edition-policy` | Accepted without review | Sent to review |
| --- | --- | --- |
| `any_edition` (default) | same or other edition | translations and adaptations |
| `same_edition` | same edition | other editions; under `fuzzy` every candidate, since the relation is unknown |
| `any_language` | any edition, translations and adaptations included | none |

The `same_work` question excludes translations, so a candidate the judge holds a
translation with at least `same_work_accept` probability counts as accepted, and
the policy decides on it. An accepted candidate judged `different_work` or
`insufficient` contradicts itself and goes to review. A work is accepted with the
first acceptable candidate in preference order; else it is `needs_review` when a
candidate lies in the review band or the policy holds one (the reason names
`review_band`, `other_edition`, `translation`, `relation_conflict` or
`edition_unknown`); else it is `no_match`.

Preference order among acceptable candidates, under `judge` and `hybrid`: the
relation (same edition before other edition), then the `same_work` probability in
bands of .05, then IIIF availability, then the provider order of `--providers`.
Under `fuzzy`, where the relation is unknown, the year distance comes first, then
the text score, then IIIF availability and provider order. When the preferred
candidate yields no file, the next acceptable one is downloaded.

On an evaluation set of 294 pairs of a sought work and a catalog record, labeled
by a language model and corrected where a review found a label wrong, the default
judge model `typesafe/jev-1.14` reached .975 accuracy and made
193 automatic decisions at .9/.1 without an error; `openai/gpt-6-luna-decisions`
reached .971 but made 4 automatic errors and serves only as the page model. In
live use the default model placed most other editions between .75 and .88, so
they go to review.

## Output Folders

```text
<output>/
  chronodownloader.sqlite     the store
  index.csv                   one row per work, rendered from the store
  <work stem>/
    objects/                  page images and renderings (PDF, EPUB)
    metadata/                 the provider's JSON documents and preview image
  _inspect/<entry_id>/<provider>_<id>/   pages sampled by inspect
```

The work stem is `<entry slug>_<title slug>`, in lowercase ASCII letters, digits
and underscores (`E0001`, _Le cuisinier françois_ gives
`e_0001_le_cuisinier_francois`), with the creator and year slugs appended when
`naming` includes them. The title slug is cut to `title_slug_max_len`
characters; a title without Latin letters or digits gives `t_` and eight hex
digits of its SHA-1. File names start with the stem cut to 50 characters and the
provider's short name (`ia`, `gallica`, `loc`, `europeana`, `dpla`, `ddb`, `bl`,
`mdz`, `polona`, `bne`, `gb`, `hathi`, `wellcome`, `annas`, `slub`, `erara`,
`sbb`):

```text
objects/<stem50>_mdz_image_005.jpg        page 5; a missing page leaves a gap
objects/<stem50>_ia.pdf                   the item's rendering
objects/<stem50>_bne_2.pdf                the second file of a multi-file rendering
metadata/<stem>_<slug>_manifest.json      one metadata document, named by its item
metadata/<stem>_<slug>_preview.jpg        the preview image
```

Downloads stage under `objects/.staging/<generation>/` and move to their final
names in one store transaction (see Store). On Windows, a work folder path longer
than about 170 characters leaves too little room below the 260-character path
limit for the file names; keep the output root short.

`index.csv` holds the columns `entry_id`, `title`, `creator`, `year`, `folder`,
`status`, `status_reason`, `retry_after`, `selected_provider`,
`selected_source_id`, `selected_item_url`, `last_provider`, `last_source_id`,
`last_format`, `last_outcome`, `last_attempt_at`, `pages_expected`,
`pages_present`, `file_count`, `file_bytes` and `updated_at`, one row per work in
entry id order. It is rewritten after every run that writes the store and never
read back.

## Store

Every output root holds one `chronodownloader.sqlite`, written by `download`,
`search --output`, `fetch`, `verify`, `classify` and `drop`. It records the runs
(command, tool version, process, effective spec, start and end, exit code), the
works with their input fields, status and lease, every candidate with its text
score and judgment, every download attempt with its completeness per part, every
file of every work folder, the page type of each page image, a chat model's
reading of each work's title page and the gate checks of `download --decisions`.
Readers outside the tool use the five views, which stay stable; the tables
behind them (`meta`, `runs`, `works`, `candidates`, `attempts`, `files`,
`page_labels`, `title_pages`, `page_checks`) may change in a later schema
version.

| View | One row per | Columns |
| --- | --- | --- |
| `work_status` | work | `entry_id`, `title`, `creator`, `year`, `place`, `publisher`, `language`, `edition`, `link`, `folder`, `status`, `stored_status`, `status_reason`, `retry_after`, `lease_holder`, `lease_expires_at`, `selected_provider`, `selected_source_id`, `selected_item_url`, `last_attempt_id`, `last_provider`, `last_source_id`, `last_format`, `last_outcome`, `last_stopped`, `last_attempt_at`, `pages_expected`, `pages_present`, `file_count`, `file_bytes`, `first_run_id`, `last_run_id`, `created_at`, `updated_at` |
| `work_candidates` | candidate, history included | `candidate_id`, `entry_id`, `folder`, `origin`, `rank`, `provider`, `source_id`, `title`, `creators`, `date`, `year`, `place`, `publisher`, `language`, `item_url`, `iiif_manifest`, `text_score`, `judge_probabilities`, `judge_decision`, `judge_model`, `judge_model_version`, `judged_at`, `selected`, `current`, `superseded_at`, `run_id`, `created_at`, `updated_at` |
| `title_page_picks` | work with page images | `entry_id`, `pages_total`, `pages_leading`, `pages_labelled`, `path`, `page_index`, `probability` |
| `title_page_readings` | work with a current reading | `entry_id`, `path`, `page_index`, `is_title_page`, `title`, `place`, `year`, `year_as_printed`, `model`, `created_at` |
| `gate_checks` | work and checked item, latest check | `entry_id`, `provider`, `source_id`, `band`, `page_source`, `status`, `same_work`, `title_page_of_sought`, `relation`, `model`, `created_at` |

In `work_status`, `status` is the read status: `blocked` while a run holds an
unexpired lease on the work, else the stored status, or `new` for a work without
an outcome; `stored_status` is the status as stored. `folder` is relative to the
output root, `pages_expected` and `pages_present` count the page images of the
last attempt, and `last_stopped` names why a download stopped early (`budget`,
`breaker_open`, `quota_spent`, `slot_lost`). In `work_candidates`, `origin` is
`search`, `link` or `fetch`, `creators` a JSON list, `judge_probabilities` the
JSON probabilities of both questions, `selected` 1 for the work's selected
candidate and `current` 0 for candidates a `--research` superseded. In
`title_page_picks`, `page_index` is a page's position in the source (item
`p00004` is 3), so a partial download keeps its gaps; a work with an image item
of another form is numbered 0, 1, ... in item order. The title page is the page
with the highest `title_page` probability among those of `page_index` below 10,
if it reaches .5 (`path` and `page_index`, else null); only labels written for
the page's current file count. `pages_leading` is the number of leading pages a
complete work has (the smaller of 10 and the highest `page_index` plus one), and
`pages_labelled` below it marks a leading page missing on disk or not yet
classified rather than a work without a title page. `title_page_readings` shows
a work's stored reading only while its page is the current pick and its file
row is the one read; `is_title_page` is the chat model's own check (0 or 1), and
every field from it to `year_as_printed` is a model reading of the page image,
not catalog data. `gate_checks` shows the latest check of each item a work was
checked on: `band` is `accepted` (the selected candidate of a downloaded work) or
`review` (a review-band candidate), `page_source` `downloaded` or `sampled`,
`status` `ok`, `failed` or `unavailable`; `same_work` and `title_page_of_sought`
are probabilities and `relation` the chosen relation, null unless `ok`. `drop`
deletes a work's checks with its other rows. Times are ISO 8601 UTC text. URLs
and raw fields carry credentials masked, and error texts are cut to 2,000
characters.

| Status | Meaning | Taken by default |
| --- | --- | --- |
| `new` | no store entry, or no outcome yet (read status) | yes |
| `complete` | every expected page or file of the requested format, within the page limit, is present | no; `--force` |
| `partial` | some expected items are present; the run exits 1 | yes, the missing items |
| `failed` | nothing was downloaded, every search failed, or the judge refused; the run exits 1 | yes |
| `no_match` | no acceptable candidate and none in review | no; `--research` |
| `needs_review` | a candidate in the review band, one the edition policy holds, a rejected link, a metadata-only item or a copy replacement | no; `fetch --reason`, `--research` |
| `deferred` | the judge was unavailable; `retry_after` says when to try again | yes, after `retry_after` |
| `blocked` | another run holds an unexpired lease (read status, never stored) | yes |

A folder of the output root that holds no store entry is `unstored`: `status`
lists it, and a run skips the work whose folder it would be unless `--force`
takes the folder over. Open the database read-only and close it after the query,
so a running download never waits on the reader:

```python
import sqlite3
from pathlib import Path

database = Path("out") / "chronodownloader.sqlite"
connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
try:
    rows = connection.execute(
        "SELECT entry_id, status, status_reason, selected_provider, folder "
        "FROM work_status WHERE status = ? ORDER BY entry_id",
        ("needs_review",),
    ).fetchall()
finally:
    connection.close()
```

Every write is one short transaction (rollback journal, never WAL) that never
spans a network call, and only the run's main thread writes work data; a timer
thread only extends the run's leases. A download is
committed in one transaction that checks the lease, renames the staged files to
their final names and records the attempt and the files; a crash between a
rename and the commit is repaired when the store next opens, from a journal in
the staging folder. On opening, the store also removes stale staging folders,
deletes the records of files missing on disk and turns a `complete` work that
lost a file into `partial`. A database that holds tables but no schema version,
a newer schema or an unreadable file is refused with exit 2 and left unchanged; a
store of schema version 1 to 3 is migrated to version 4 in place when a command
that writes opens it, unless a run of an older version still holds a work's
lease, which is refused with exit 2 until that run ends.

## Coordination Across Runs

Several runs, agents or processes may work one output root and the same
libraries at once.

Leases. A run holds a lease on each work it processes (holder, generation,
expiry, 600 s), renewed every 60 s by a timer thread. Every write on a work checks
inside its transaction that the lease is still this run's and unexpired, so a run
that lost its lease writes nothing more. A work another run holds is skipped as
`blocked`: the run counts it in `items_blocked`, exits 0, and a later run picks it
up. A run that takes a lease checks the work's status again, so a work another run
finished meanwhile is skipped. Ctrl+C releases the run's leases. A killed run's
lease is freed at once when the run was on this machine and its process is gone
(by process id and creation time); a lease of another machine's run lasts until it
expires.

The machine-wide database `<state_dir>/coordination.sqlite` (default
`~/.chronodownloader/`) keeps each provider's limits across every process on the
machine:

- Pacing: each request reserves a send time at least `delay_ms` plus up to
  `jitter_ms` after the provider's previous one; a sender that wakes more than
  `pacing_tolerance_ms` late reserves again, and every retry reserves anew.
- Slots: a download holds one of the provider's `slots` for the whole transfer,
  renewed every `slot_renew_s`; a slot not renewed for `slot_ttl_s` expires, and
  a holder that lost its slot starts no further request.
- Breakers: `circuit_breaker_threshold` consecutive failures open a provider's
  breaker for `circuit_breaker_cooldown_s`; then one caller probes, and a success
  closes it. An open breaker stops every process's requests to that provider;
  `providers` shows it with its cause.
- Quota: Anna's Archive fast downloads reserve a unit atomically within a window
  of `reset_hours`; an expected refusal returns its unit. A spent quota stops the
  download (`last_stopped` `quota_spent`), and the work stays open as `failed` or
  `partial` for a later run.

The decision model's endpoint is paced, slotted and breaker-checked as provider
`judge`, and the requests of `classify` and the gate as provider `classify`, with
the same `judge.network` policy and slot limit but a breaker of their own. A
request to the host of another provider (a Europeana record's images on
`gallica.bnf.fr`) is paced and counted under that provider. A database that stays
locked longer than `busy_timeout_s` ends the operation with an error.

Each request is retried on transient failures with backoff, and a server's
`Retry-After` wins. A connect failure is retried at most twice, a name that does
not resolve only under `dns_retry`, and a certificate failure once without
verification under `ssl_error_policy: retry_insecure_once`, never for a request
that carries a key. Redirects are followed by the tool, each hop paced and
breaker-checked.

## Agent Workflow

An agent drives the tool through the commands with `--json` and reads the one
summary line from stdout. Works left `needs_review` form a queue:

```bash
chronodownloader status --output out --only needs_review --json
chronodownloader inspect --entry-ids E0001 --output out --json
chronodownloader fetch --entry-id E0001 --provider mdz --id bsb10311854 \
  --output out --reason "title page p3: same work, 1752 edition" --json
chronodownloader verify --output out --entry-ids E0001 --pages --json
```

`status` lists the queue with each work's reason, `inspect` samples the
candidates' first pages and reports the page model's answers, and `fetch
--reason` downloads the chosen item and records why. A `deferred` work is taken
again after its `retry_after`; a `blocked` one by a later run. An agent stops on
an open breaker or a spent quota (`providers` shows both) and resumes later.

## Logging

Records at WARNING and above go to stderr as `[LEVEL] message`. A run that is not
a dry run also appends INFO and above to the log file:
`logs/application.log` in the repository, or `logs_dir` (a folder, or a file
ending in `.log`). `status` and `providers` write no log file. Every message and
traceback passes through the credential filter before any handler writes it. A
log file that cannot be opened is left out with a warning, and the run goes on.

## Job Launcher

When the environment variable `CHRONO_HEARTBEAT_FILE` names a file, `download`,
`search` and `fetch` write their progress there as one JSON object for a job
launcher: `units_done` of `units_total` `unit_label` (`work`), `eta_seconds` and
`updated_at` (ISO 8601, UTC). The file is replaced at most every 15 seconds, and
a write that fails is skipped without affecting the run.

A job resumes by running its command again, since every command resumes from
the store. A run that exits 0 may still leave works open: check
`items_blocked`, `items_needs_review` and `items_deferred` in the summary.

## Architecture

```text
chronodownloader/
  __main__.py   entry point: dispatch the subcommand
  spec.py       option tables and the run specification of each command
  settings.py   typed settings; resources/settings.example.yaml
  run.py        core: plan and run per command
  _loop.py      run loop: leases, store writes, decisions, fallback
  _worker.py    worker threads: search, judge, download
  _review.py    inspect and verify; _listing.py: status, drop, providers
  _classify.py  classify: page labels under the work lease
  _gate.py      download post-phase: gate checks in shadow mode, --classify
  events.py     run events reported to the front ends
  inputs.py     works CSV
  coord.py      machine-wide coordination database
  net/          paced, retried, breaker-checked requests and downloads
  providers/    one class per library, the registry, IIIF download, staging
  iiif/         manifests v2 and v3, canvases, image sizes, renderings
  pages.py      page samples for inspect and verify
  matching/     fuzzy.py (text score), judge.py (decision model), policy.py,
                classifier.py (page types), title_pages.py (title-page reading),
                gate.py (gate request and answers)
  output/       store.py (schema, views, leases), paths.py, index.py
  options/      option tables, wizard framework, prompts, settings loader
  process/      exit codes, summary line, heartbeat, interrupt, log setup
  common/       decision-model dialects and page types, shared with ChronoTranscriber
  cli/          argparse from the option tables, reporter, summary
  ui/           guided wizard, review screen, progress
```

The core never prints, prompts or exits; it reports through run events, and only
`cli/` and `ui/` write to the terminal. `cli/` never imports `ui/`, providers
never import `output/` or `matching/`, and `coord.py` and `net/` never import
`providers/`; `tests/core/test_architecture.py` enforces these rules. The core is
synchronous: a run owns one thread pool for its works and daemon threads for
searches, so a stalled search never delays the process's exit.

## Development

```bash
uv sync --all-extras
uv run pytest                        # hermetic suite
uv run ruff check .
uv run ruff format --check .
uv run mypy chronodownloader         # strict
```

No test touches the network: HTTP goes to a local test server or to recorded,
scrubbed provider responses under `tests/fixtures/providers/`, and matching is
tested against the frozen evaluation set under `tests/fixtures/matching/`. The
`live` marker is reserved for opt-in tests against the real libraries. After an
intended change of the help texts, rewrite the golden files with
`CD_UPDATE_GOLDEN=1` and review the diff; `tests/README.md` describes the layout.

The tables of the Command Reference mirror the option tables in
`chronodownloader/spec.py`, and the settings block above is the packaged example;
`tests/cli/test_readme_options.py` fails when either differs. A new option needs
an option-table entry and a wizard step that sets it, or an exemption;
`tests/ui/test_parity.py` enforces the step.

## Versioning

Every release is described in [CHANGELOG.md](CHANGELOG.md).

## License

MIT License. Copyright (c) 2025 Paul Goetz.
