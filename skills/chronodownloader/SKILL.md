---
name: chronodownloader
description: >-
  ChronoDownloader. Use when the user wants digitized historical sources (books,
  prints, manuscripts) found and downloaded from digital libraries, works a
  needs_review queue, resumes, verifies or inspects a download run, classifies
  downloaded pages or reads title pages, or reads its outputs
  (chronodownloader.sqlite, index.csv).
---

# ChronoDownloader

The tool runs from a clone of the repository set up as in the README, Installation.
The README documents every behavior (Command Reference, Exit Codes and JSON Summary,
Providers, Matching, Store, Coordination Across Runs); `<command> --help` gives the
flags, defaults and exit codes of each command. Run every command through uv with
absolute paths, since `uv --directory` resolves relative paths against the clone:

```bash
uv --directory <clone> run chronodownloader <command> [options]
```

## Running a job

1. **Check the providers.** Run `providers --json`. Done when no provider you need
   has an open breaker (`breakers_open`; the stderr table names each cause and the
   time it closes) or a missing key (`keys_missing`). Several runs may share one
   output root.
2. **Dry run.** Add `--dry-run --json` to the intended command. Done when it exits 0
   and `items_total` counts the intended works.
3. **Search first** for a CSV of more than a few works: `search --input <csv>
   --output <root> --json` judges every candidate without downloading, and the next
   `download` into the same root reuses the judgments. Done when you know
   `items_accepted`, `items_needs_review` and `items_no_match`.
4. **Run.** A run that finishes within the turn runs directly with `--json`; a longer
   one runs as a background process whose `--json` line you read when it ends. On
   the user's request, `download --decisions shadow` gates every downloaded and
   review-band work and `--classify` labels the downloaded pages and reads their
   title pages, both after the last download; `classify --output <root>` does the
   same for stored works. Both need page images, so pass `--format images` (or
   `both`) when the settings prefer PDFs: a PDF-only work is left unchecked. Both
   call a decision model per page or work; project a large job from a sample.
5. **Verify.** Done when the exit code is 0 and you have read `items_needs_review`,
   `items_deferred`, `items_blocked` and `items_no_match`, which exit 0 leaves open.
   Exit 1 means a work failed or stayed partial; rerun the same command, which
   downloads only the missing pages and files. The gate and `--classify` leave the
   exit code alone, and a run the size limits stopped skips them, so compare
   `gate_checks` plus `gate_reused` with the works downloaded, read `gate_failed`,
   `gate_unavailable`, `gate_stale`, `gate_blocked`, `pages_failed` and
   `title_pages_transcribed`, and report every work left without a check. A rerun
   skips complete works and their post-phase: `classify --output <root>` fills
   label and title gaps, while a missing gate check stays missing. A `complete` work
   whose `gate_checks` relation is `other_edition` or `different_work` deserves a
   look at its title page. Then settle the open works with [review.md](review.md),
   or hand that file to a retrieval agent as its brief.

## Conventions

- The settings file decides the providers, their order and the concurrency
  (`defaults:`); pass `--providers` and `--concurrency` only on the user's request.
  A relative `defaults.output` resolves in the clone, so always pass `--output` with
  an absolute path. A task that edits the settings file restores it afterwards.
- Keep the output root short: a work folder path past about 170 characters leaves no
  room for the file names under the Windows path limit, and the `naming` settings can
  add the creator and the year to every folder name.
- Keys come from the environment; the settings name only the variables.
  `--matching fuzzy` serves only when `OPENROUTER_API_KEY` is unset. With the
  default `text_accept: null` it accepts nothing on its own: a score above
  `text_reject` goes to review, one below is rejected (README, Matching).
- Under the default judge, other editions mostly land in the review band, so a run
  of more than a few works leaves a review queue.
- `chronodownloader.sqlite` in each output root is the only status record;
  `index.csv` is rendered from it and never read back. Query the store read-only
  through its views (README, Store): `work_status` and `work_candidates`, and after
  `classify` or a shadow run `title_page_picks`, `title_page_readings` and
  `gate_checks`. Title readings and gate answers are model readings, not catalog
  data.
- `--force` and `drop` delete files: use them only with the user's consent.
  `fetch --force` deletes the work's other copy.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| exit 0 with `items_blocked` | another run holds those works; a dead run on this machine frees them at once, another machine's after 600 s; rerun later |
| `items_deferred` | judge outage; rerun after `retry_after` |
| `last_stopped` `breaker_open` or `quota_spent` | `providers` shows when the breaker closes or the quota resets; rerun then, not before |
| warning `<provider> left out of this run's searches until <time>` | three searches of that provider in a row timed out; a work found nowhere else ends `failed`; rerun after that time, which searches it again |
| exit 2: an older chronodownloader holds works in this store | a run of an earlier version still leases works, so the store cannot move to the new schema; rerun once it ends |
| exit 2: the store carries a newer schema version than the tool knows | an older checkout opened a root a newer version has migrated; update the clone |
| a folder listed as `unstored` | a folder without a store entry: runs skip its work; `--force` takes it over, with the user's consent |
