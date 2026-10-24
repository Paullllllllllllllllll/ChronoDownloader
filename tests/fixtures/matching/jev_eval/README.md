# jev_eval matching fixture

Frozen on 19.10.2026 from the local evaluation folder `docs/jev_eval/` (not
tracked) by `docs/jev_eval/freeze_fixture.py`. Sources: `works.jsonl` (30
works sought), `candidates.jsonl` (search candidates with the v1.30.3 text
scores), `decision_answers.jsonl` and `page_answers.jsonl` (OpenRouter
Decisions answers), and `labels_out/{pairs,pages}/*_output.json`. The labels
were produced by gpt-6-luna through ChronoMiner from the metadata (pairs) and
from the page transcriptions (pages); they are not hand labels. Images, raw API
envelopes, usage, prompts and the research notes the labeler saw are left out.

## Files

`pairs.jsonl` holds one record per labeled search pair (294):

- `pair_id`, `entry_id`.
- `work`: the sought work (title, full title, author, year, place, publisher,
  language, edition statement, total editions, query title and creator, known
  link and provider, known copy year and edition, `group`).
- `candidate`: provider, rank, title, creators, date, year, source id, item URL,
  IIIF manifest, raw fields.
- `judge_catalog_record`: the catalog record as sent to the judge.
- `text_score`: v1.30.3 `cd_scores`, `cd_min_title_score`,
  `passes_min_title_score`, `cd_selected`, `cd_selected_with_year`,
  `year_penalty_if_year_given`, `known_copy_match`.
- `label`: `relation` (five-way), `same_work`, `certainty`; corrected where
  `corrected` is true, with `original_label` and `correction` (source, reason).
- `answers[condition][model]`: `model` (dated version), `requested_model` and
  `answers` (`same_work` noul; `match` choice, probabilities, confidence; under
  `images` also `title_page_visible` and `n_images`).

The judge state under `minimal` is the work's short title, author and year plus
`judge_catalog_record`; `rich` adds full title, place, publisher, language and
edition statement. Under `images` (28 pairs) the state is the `rich` sought work
followed by up to 15 page images of the candidate, without the catalog record.
Models: `jev` is `typesafe/jev-1.14`, `luna` is `openai/gpt-6-luna-decisions`.

`pages.jsonl` holds one record per labeled corpus page (150): `page_id`
(`{stem}__{page_index:03d}`), `entry_id`, `stem`, `file`, `page_index`,
`sought_work`, `label` (`page_type` and four booleans), `corrected` (with
`original_label` and `correction` as for pairs), and `answers[model]` with
`model`, `requested_model`, `sent_size` and `answers`.

## Corrections

One pair label is corrected. The review of label/model disagreements on
19.10.2026 found it wrong; the corrected relation and certainty were set at the
freeze.

| Pair | Original | Corrected | Reason |
| --- | --- | --- | --- |
| `10840-europeana-1` | translation_or_adaptation, false | same_edition, true | Europeana gives an English translation of the French title of Colas's 1770 *Manuel du cultivateur* |

The same review also took Audot's book to be the work sought for entry 12085
(`12085-polona-1`, `-2`); its labels stand. The sought work is *La Cuisinière de
la ville et de la campagne, ou Recueil des procédés...* (Paris; Metz 1831, one
edition), whereas Audot's *La cuisinière de la campagne et de la ville, ou
nouvelle cuisine économique* had many editions: subtitle and edition history
differ.

Five page labels are corrected, each checked against the page image at the
freeze. The other pages the review inspected (show-through on blank versos,
the half-title of `16377`) keep their labels.

| Page | Original | Corrected | Image |
| --- | --- | --- | --- |
| `3675_the_queen_like_closet__004` | blank, no recipe | cover_or_binding, recipe | endleaf, handwritten recipe for plain cakes |
| `3675_the_queen_like_closet__005` | blank, no recipe | cover_or_binding, recipe | pastedown, handwritten gooseberry wine recipe |
| `14284_neues_kochbuch_oder_unterricht_allerlei_arten_von_speisen__000` | blank | cover_or_binding | marbled paper board, leather spine |
| `16377_cours_complet_de_cuisine_moderne__000` | blank | cover_or_binding | leather board, gilt border |
| `3152_the_thorough_good_cook__000` | title_page, title page of sought | cover_or_binding, not title page | cloth cover with gilt title and author |

## Reference figures

Same-work accuracy at .5, false accepts (FA), misses, and automatic decisions
at .1/.9 with their errors, over the 280 decided pairs (26 under `images`).
Without corrections the figures equal those of `docs/jev_eval/score.py`.

| Condition | Model | Accuracy | FA | Miss | Auto, wrong | Corrected: accuracy | FA | Miss | Auto, wrong |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |
| minimal | jev | .97 | 6 | 3 | 219, 0 | .97 | 5 | 3 | 219, 0 |
| minimal | luna | .97 | 8 | 0 | 265, 4 | .97 | 7 | 0 | 265, 3 |
| rich | jev | .98 | 4 | 2 | 197, 1 | .98 | 3 | 2 | 197, 0 |
| rich | luna | .98 | 3 | 2 | 269, 4 | .99 | 2 | 2 | 269, 3 |
| images | jev | 1.00 | 0 | 0 | 8, 0 | 1.00 | 0 | 0 | 8, 0 |
| images | luna | .92 | 0 | 2 | 21, 1 | .92 | 0 | 2 | 21, 1 |

The v1.30.3 `passes_min_title_score` has accuracy .55 with 126 false accepts
and 1 miss (corrected: .54, 126 and 2); its selected candidate is the same work
for 8 of 30 works.

Pages (150), accuracy at .5 with FA and misses, before and after correction:

| Question | Jev | Jev corrected | Luna | Luna corrected |
| --- | --- | --- | --- | --- |
| title_page_of_sought | .97 (0, 4) | .98 (0, 3) | .99 (1, 1) | .98 (2, 1) |
| title_text_present | .95 (2, 6) | .95 (2, 6) | .94 (0, 9) | .94 (0, 9) |
| imprint_year_matches | .99 (2, 0) | .99 (2, 0) | .98 (2, 1) | .98 (2, 1) |
| contains_recipe | .97 (5, 0) | .98 (3, 0) | .92 (10, 2) | .92 (9, 3) |
| page_type (accuracy) | .83 | .85 | .84 | .87 |
