# Changelog

## Unreleased

- Close opened media on response construction or header-setup failure without
  replacing the primary exception; retain streaming response ownership on success.
- Traverse deep storage trees without recursion and reject invalid child names or
  incomplete subtrees before the orphan sweep can delete any objects.

- Bound orphan-sweep memory with a temporary disk inventory and batched database
  reference streaming; retain path normalization, sorted unique reports and finish
  listing before deletion.
- Reject negative orphan age grace before any work and retain objects with unusable
  modification times while continuing the sweep; zero explicitly disables age grace.

- Provision admin accounts atomically from a locked current row, preserve inactive
  accounts and unchanged password hashes, and save only changed provisioning fields.
- Isolate post-deletion media cleanup failures from committed item deletion; log a
  safe recovery diagnostic and leave retained objects for the orphan sweep.

- Protect search-index repair with real SQLite drift, repeat-rebuild, visibility,
  refusal/failure and success-count regressions; command behavior is unchanged.

- Refuse stale admin change forms while preserving submitted edits for conflict recovery.
- Reload current rows before admin reprocessing; retain active worker attempts and retire
  only current media references, with storage cleanup deferred until commit.

- Require JSON artifacts and nonempty readable segments when batch coverage checking
  is enabled; keep the explicit nonpositive-threshold compatibility opt-out.
- Retain delayed health outcomes as attempts without reversing newer step state or
  streak membership; let success win equal-timestamp ties.
- Read health states, attempt totals and streak membership in one consistent SQLite
  snapshot without acquiring the configured IMMEDIATE writer lock.

- Reject missing/unreadable batch segment lists, invalid/nonfinite segment times and
  malformed transcript text; compute
  coverage from the interval union so out-of-order/overlapping segments remain valid.

- Recheck public visibility when hydrating ranked search results.
- Group overview navigation by local ISO week in the database and fetch only card
  columns, leaving large content/processing fields deferred.
- Batch all active health failure-streak item counts into one grouped query.

- Reject nonpositive reclassification limits before any selection or mutation.
- Keep exact ongoing failure-streak membership independently of retry events;
  backfill existing streaks and prune all attempt events beyond 30 days.

- Stream historical reclassification selections without transcript payloads and
  decide/apply each item from its current row in one transaction.

- Fence failure bookkeeping against current source, result and processing state;
  preserve newer successes and count unchanged-source failures under a row lock.
- Return invalid-JSON 400 responses for excessive API JSON nesting.
- Replace XML 1.0-forbidden characters in RSS item text/attributes at serialization,
  preserving valid Unicode and stored content.

- Exclude active processing stages from item claims and acquire newly pending
  stages atomically, preserving explicit stale recovery.
- Fence source-media publication against source, classification, attempt and media
  reference changes; reclaim losing uploads without deleting referenced objects.
- Ignore synthesis submission/poll results superseded by job, source, classification
  or script edits, retaining newer artifacts and polling state.

- Reject generated summaries with nontext summaries, nonarray tags, or malformed
  tag entries; retain existing content and stage retry handling.
- Verify article-copy timeout cleanup and retained references under a trickling
  synthetic MP3 response, using the shared authenticated transfer deadline.

- Bound authenticated upstream exchanges, including DNS, redirects, error bodies
  and transport teardown, by one elapsed deadline in a disposable process.
- Stop transcription polling before its deadline expires and reject late completion.

- Reserve login budgets before password verification, preserving other pending attempts
  on successful login and ignoring settlement from expired windows.
- Bound metadata DNS, connections, redirects and body reads by a single elapsed fetch
  deadline using the guarded downloader subprocess; retain charset parsing and byte caps.

- Confine authenticated job/artifact URLs to the configured API origin and refuse
  cross-origin, downgrade and userinfo redirects before forwarding bearer credentials.

- Keep metadata and operator classification edits made during metadata fetching;
  serialize classifier PATCH lookup, override conflict checks and publication.

- Preserve concurrent summary, tag and transcript edits when workers publish generated
  text; reconcile completion and summary refresh from current retained values.

- Update the locked Brotli decompressor to 1.2.1 for PYSEC-2026-4322.
- Gate CI on the existing frozen runtime/development dependency audit, pin pip-audit
  to 2.10.1, and propagate export failures in the local and CI commands.
- Validate capture API string types and model length limits before classification/capture,
  returning field-specific 400 responses while preserving optional empty values.
- Return the existing invalid-JSON 400 response for malformed byte encodings on both
  item creation and updates, retaining authentication before decoding.
- Normalize capture publication timestamps to UTC before persistence and reject invalid
  dates or out-of-range UTC/display-timezone conversions with the existing field-specific
  400 response.

- Bound podcast feed materialization to streamed policy batches and one page of full items,
  preserving eligibility, exact archive page counts, enclosure priority, and navigation.

- Paginate public full-text search in SQLite, counting matches exactly and loading only
  the requested 20-result page while preserving relevance ordering and page navigation.

- Apply the stale-processing age cutoff at worker startup as well as during polling,
  preserving fresh processing leases when another worker starts.
- Return a nonzero exit status from `run_metadata_worker --once` after any failed or
  stalled item, while finishing independent work and printing the processed count.

- Reject incomplete Voxhelm batch transcripts. whisper.cpp splits long audio across
  processors and ignores a worker thread that fails, so a batch job could succeed with whole
  parts of the audio missing: item 408 (a 3:09 h podcast) kept the first hour plus two
  12-minute stretches, 44 % of the audio. Archive now reads the job's segment JSON and
  fails the transcript step when a stretch longer than `ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS`
  (default 600, `0` disables) has no segment, instead of storing the partial text.
- `reprocess_items --only transcript` re-queues failed transcripts; `--regenerate` also clears
  and re-queues complete transcripts the archive generated itself (never hand-entered ones),
  so a partial transcript like item 408's can be transcribed again.
- Add the `reprocess_items` management command to re-queue one failed step (`summary`,
  `media_archive`, or `article_audio`) for specific item ids or ranges without re-fetching
  metadata or touching other steps, which the admin "Reprocess selected items" action always
  does. Only failed steps are re-queued (others are reported as skipped); `--stagger` spaces
  the retry times to stay below upstream rate limits, `--hold-article-audio` keeps a
  successful summary from starting paid text-to-speech, and `--dry-run` shows the changes.
  Items the worker is processing are skipped, and the worker no longer rewrites unchanged
  waiting article-audio state, so a hold placed during its pass sticks.
- Add `GET /api/health/`, an unauthenticated JSON processing-health endpoint for monitoring.
  Per worker step (metadata, YouTube media archival, other media archival, transcription,
  summaries, article audio) it reports success and failure counts over the last 24 hours, the
  latest success and failure, and how long and across how many distinct items the step has
  been failing without a success. It exposes counts and timestamps only. The worker records
  each attempted step run in two new tables (migration `0016`; events are pruned after 30 days except those of an ongoing failure
  streak, so `failing_items` never drops without a success);
  migration `0017` seeds the last YouTube archival success from existing archived items. This
  makes failures like the August–October 2026 YouTube outage (HTTP 403 on audio downloads)
  visible to Nyxmon instead of only in item error fields.
- `just deploy` now runs `just deploy-one archive` in `~/projects/ops-control` (override with
  `OPS_CONTROL`) instead of a non-existent workspace path.
