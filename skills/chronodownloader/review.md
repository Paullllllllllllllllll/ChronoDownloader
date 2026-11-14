# ChronoDownloader review queue

Brief for the agent that settles the open works of one output root: every
`needs_review` work, and the `no_match` works the user wants rechecked. Fill in
`<root>` and `<clone>` before handing it over. Every command runs as in
[SKILL.md](SKILL.md): `uv --directory <clone> run chronodownloader <command>`, with
absolute paths.

## Steps

1. **List the queue.** Run `status --output <root> --only needs_review --json`. The
   stderr table gives each work's reason: `review_band`, `other_edition`,
   `translation`, `relation_conflict`, `edition_unknown`, `metadata_only`, a rejected
   link, or a copy replacement. Done when you hold every entry id with its reason.
2. **Inspect each work.** If the run used `--decisions shadow`, read the work's
   `gate_checks` row first: the gate's `same_work`, `title_page_of_sought` and
   `relation` on its best review-band candidate, evidence rather than a verdict.
   Run `inspect --entry-ids <id> --output <root> --first 6 --json` (`--providers`
   narrows it); each candidate costs one page-model call. The table reports
   `title_page`, `imprint_year` and `page_type` per candidate, and the pages lie
   under `<root>\_inspect\<id>\<provider>_<item>\`. The page model can miss a correct
   edition, so read the title page yourself; the catalog records are in the
   `work_candidates` view. Done when you have read the title page or first pages of
   every candidate.
3. **Decide.** For the same work, prefer the same edition, then the edition nearest
   the sought year in the same language, then the more complete copy. Download it
   with `fetch --entry-id <id> --provider <key> --id <item> --output <root> --reason
   "<evidence: page, title, imprint, edition>" --json`. A `metadata_only` reason
   names where the item is held. When that is an Internet Archive page or a IIIF
   manifest, fetch it with `fetch --url <url> --entry-id <id> --output <root>
   --reason ... --json`; HathiTrust is metadata-only, so a work held only there stays
   in review. A copy replacement holds another copy's files, so its fetch needs
   `--force`, which deletes them: ask the user first, else leave the work in review.
   When no candidate is the work, leave it in review and say why.
4. **Check.** Done when every fetch exited 0, its work is `complete`, and a final
   `status --output <root> --only needs_review` lists only the works left open on
   purpose.

## Rechecking a no_match work

A reject is not proof of absence: the judges and the page model can miss a title
that lacks the author or carries an addition. Search again with `search --title
"<title>" --creator <name> --year <year>`, which prints candidates and writes
nothing; try the title without its additions and the catalog's spelling. SBB
searches its catalog word by word, so "Brandenbursche" finds nothing where the
catalog has "Brandenburgische". Download a found item with `fetch`, as in step 3.

## Stop and report

Stop without retrying when `providers` shows an open breaker for the next fetch's
provider, a work stops with `last_stopped` `breaker_open` or `quota_spent`, or a
command exits 2. Use `--force` and `drop` only with the user's consent; `fetch
--force` deletes the work's other copy. Report per work: the decision, the item, the
reason recorded and the exit code.
