# Changelog

## Unreleased

- Keep owned media-command leaders unreaped until the final process-group signal,
  preventing PID/group reuse during retirement; bound every final reap wait.

- Batch migration `0018` rollback membership lookups and missing representative
  event inserts, preserving active distinct failure counts, existing events and
  `failing_since` on the selected database without per-item database queries.

- Isolate malformed stored URLs during migration `0008`, checking the other URL
  and remaining rows without rewriting evidence or logging private URLs.

- Bound migration `0008` legacy video scanning and requeue updates to 200-row
  primary-key batches on the selected database, preserving its atomic upgrade
  and original suffix rules for future/replayed migrations.

- Stop blocking the metadata worker on Voxhelm batch transcriptions: the worker
  uploads oversized archived audio, submits the job and stores it on the item
  (migration 0022: `transcript_job_id`, `transcript_job_ref`,
  `transcript_job_submitted_at`, `transcript_poll_at`), then polls it with one
  status request every `ARCHIVE_TRANSCRIPTION_POLL_SECONDS` (default now 60).
  Retries and worker restarts poll the stored job and never upload again; failed
  status requests keep the job until `ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS`
  (default 7200) after submission. Failed or incomplete jobs fail the step and are
  forgotten, as are jobs made for an older source. `reprocess_items --only
  transcript` also forgets an earlier job. `--transcription-timeout` is now the
  budget of one pass (upload and submission, or one poll), not the wait for the job.

- Recheck mutable reclassification selectors under the current row lock; report
  changed selections as skipped without normalizing or counting them as applied.
- Treat malformed classification URLs as unknown sources, retaining explicit kind
  overrides and independent usable media instead of leaking parser exceptions.

- Normalize supported adapter authorities by hostname and scheme-default port,
  refusing userinfo and non-equivalent authorities; media archival and processing
  health share the same YouTube page check. Skip damaged restored media
  candidates with bounded, typed fields and validated URL/provenance semantics.

- Select usable video evidence beyond weak earlier discoveries, accept explicit
  HTTP(S) audio endpoints without file suffixes, and bind cached classification
  candidates/signals to all current source URLs. Legacy unbound discoveries are
  discarded until metadata enrichment records their source identity.

- Drop extracted media references whose HTTP(S) authority is unusable (empty host,
  invalid/zero/out-of-range port, malformed bracketed host or IPv6 zone, host
  delimiters/whitespace, URL controls), keeping other metadata and valid relative,
  IPv6 and internationalized references.
- Serve public archived/article audio with `public, no-cache` and a strong
  generation `ETag` (304 on `If-None-Match`, `If-Range` only on an exact match)
  instead of `max-age=3600`, so unpublication takes effect at the next request.
  Audio 404/405 and other error responses are `no-store`.

- Treat impossible optional publication dates (for example `2026-02-31`) as
  absent instead of aborting metadata extraction; try the next HTML/JSON-LD date
  candidate and keep title, media and other extracted metadata.
- Refuse storage-returned media keys outside the reserved upload prefix before
  publication; discard only the provably owned reserved object and reservation,
  never another upload's object or reservation.

- Record the owning item and processing attempt on upload reservations
  (migration 0021). New `reconcile_archive_media_reservations` reports and, with
  `--apply`, releases reservations whose attempt is provably retired, deleting only
  their unreferenced objects. Live and legacy (unowned) reservations stay protected.
- Validate a stored object's opened length against its recorded size before
  committing stored-audio lengths or ranges; mismatches get a `no-store` 502 for
  GET/HEAD/range and close the handle once. HEAD no longer closes the file twice.

- Fence summary/transcript success against source inputs and processing attempts;
  requeue changed inputs without settling newer work, preserving manual edits.
- Bind metadata success and failure to the fetched URL/current attempt, preserving
  newer results and suppressing obsolete processing-health events.
- Use the owning/write database for runtime capture, worker stage transactions,
  claims, recovery and outcome writes; guard summary/transcript no-work completion
  against current source, output ownership and attempt state.
- Retire concurrently deleted worker claims, discard owned unpublished media and
  continue processing siblings without masking unrelated database failures.
  Limit extracted display metadata to model character widths, retaining manual values.
- Validate worker batch/timer/poll options before handlers, recovery or claims;
  cover real graceful SIGTERM, committed output and restart with synthetic SQLite.

- Fence all article-audio fast-path writes and copy publication against current
  source/text, hold, classification, job and processing-attempt state. Superseded
  work leaves newer authority intact without publishing obsolete completion/copies.
- Bound complete summary model requests to 64 KiB of serialized UTF-8 JSON,
  sharing remaining space across optional metadata and excerpts with explicit
  prefix truncation. Refuse required-context/envelope overflow before model I/O.

- Reject recursively duplicated members in upstream API JSON with controlled
  invalid-JSON failures, preserving job identity and previously stored results.
  Ignore ambiguous JSON-LD script blobs independently, retaining valid metadata
  from other scripts and HTML tags. Reuse capture's object-member admission.

- Retire FFmpeg/yt-dlp process groups on success and failure, with bounded TERM/KILL
  escalation before temporary-file cleanup. Run supported video-page downloads
  behind one elapsed deadline, including retries and postprocessing; discard
  partial results and private diagnostics while retaining normal retries.

- Bound extracted MP3 output with FFmpeg's size option and parent-side checks;
  refuse truncated/oversized output before uploading video or audio. Drain both
  diagnostic streams with separate 1 MiB budgets and fixed private-safe failures,
  preserving the previous archived generation on extraction failure. Video-page
  downloads turn off yt-dlp's progress bar, so long downloads do not use up the
  diagnostic budget.

- Continue orphan cleanup after independent filesystem/S3 deletion failures, report
  bounded failure counts and exit nonzero for incomplete sweeps. Preserve final
  reference/reservation checks and allow retries to reclaim retained candidates.

- Log stored-media range seek failures as a fixed operation category without
  backend exception text or traceback, retaining exactly-once handle cleanup.

- Serve transient pre-stream media errors with no-store, preserving caller cache
  policy for successful and stable range outcomes and allowing immediate retries.

- Fetch only search card columns and a database-bounded excerpt from the first
  nonempty summary/notes/transcript fallback; display at most 240 characters while
  retaining full-content ranking and detail pages.

- Bound HTML week overviews to 50 cards per page in stable newest-time/id order,
  with server-rendered page links retaining the selected week.

- Reject duplicate JSON object members on capture and classifier updates before
  classification or database work, including duplicates in ignored nested fields.

- Share one transcription deadline across media loading, upload staging, submission,
  polling and coverage evidence; reject late results without starting subsequent
  requests or retrying ambiguous submissions inside the attempt. Remote media
  sources use the killable download process, so slow bodies cannot outlast it.
- Include all synthesis payload settings in v3 task identity while continuing to
  poll existing recorded jobs unchanged. Log article-source extraction failures
  with only item ID and a fixed category, preserving stored-text fallback.

- Validate transcription/synthesis job envelopes and upload IDs (at most 64
  characters, the stored width); reject poll identity mismatches and malformed
  artifact paths before publishing results. Explicit null artifacts still mean
  "no segment evidence" for services used with coverage checking disabled.
- Admit multipart filenames/MIME values before reading storage, sharing exact
  framing with length calculation. Verify staged source length and archive byte
  limits before starting authenticated upload I/O.

- Recheck public article-audio visibility and artifact/copy identity after lazy
  preparation and storage open; close acquired handles on retired responses.
  Transient preparation failures log only the exception class, not its message.
- Keep committed kind changes successful when post-commit source-media cleanup
  fails; log a fixed category and retain objects for reference-safe orphan recovery.

- Paginate week JSON in 50-row keyset pages with signed `next` continuations and a
  narrow public-field projection; document whole-week consumer traversal.
- Refuse public search queries over 512 characters, 32 terms or 64 characters per
  lowercased term before SQLite FTS execution, with bounded validation output.

- Index capture deduplication by persisted normalized-URL digest and capture time,
  verifying full identity on digest matches and preserving newest-match semantics.
- Backfill all existing URL identities in bounded migration batches; keep digests
  synchronized on model, literal queryset and bulk URL writes. Apply migration 0020
  before starting updated app/workers; populated rollback/upgrade and FTS are covered.
  The digest column has a database default, so writers still on pre-0020 code keep
  inserting during a migrate-then-restart window (their rows are not deduplicated).

- Validate capture dedupe windows as integers from zero (disabled) to 365 days at
  startup and before capture transactions, with fixed configuration diagnostics.

- Bound reclassification runs to their starting ID horizon; later captures are left
  for the next invocation, preserving selectors, limits and locked row revalidation.

- Redact private URL components in reclassification diagnostics, including selected media,
  while retaining item IDs and safe HTTP(S) origins.

- Bound health-event retention pruning to 100 expired events per outcome, preserving
  exact recent counts and ongoing failure streaks while later outcomes drain backlogs.
- Recover at most 100 stale items per worker iteration, oldest first, and recheck
  selected attempt timestamps before resetting processing states.
- Cover killed-worker/reopened-SQLite recovery at claim and media-publication barriers,
  including fresh leases, delayed superseded results and shared media references.

- Reject generated summaries and tags beyond documented per-field/count limits
  before publication, retaining operator content and existing retry behavior.
- Preserve nested HTML container text once in order for summary and article-audio
  sources, including short headings and blockquote tails.
- Close omitted `<li>`/`<p>` end tags per HTML5 so later page chrome is not captured,
  separate text at block boundaries, and drop top-level list items whose whole text is
  under 25 characters.

- Normalize invalid UTF-8, JSON recursion and integer-decoding failures to fixed
  summary/transcription/article-audio errors with response cleanup.
- Keep upstream error bodies, failed-job messages and raw transport details out of
  normal stored errors and logs; retain HTTP status and fixed diagnostic categories.
- Report fetch-policy refusals as `blocked destination` and name the exception class
  for other transport/local read failures.
- Reject malformed article-audio `Content-Length` headers with a fixed error that
  omits the header text.

- Skip FTS updates when indexed text and row identity are unchanged, with a
  reversible null-safe trigger migration.
- Persist derived publication dates on partial saves while preserving empty-save
  semantics and unrelated concurrent edits.

- Bind historical queue/health data migrations to the selected database.
- Preserve FTS insert/update/delete triggers when rolling back the indexed processing
  timestamp; cover populated upgrade/rollback/upgrade and visibility journeys.

- Cover both stored-audio routes with real local HTTP byte-range, HEAD and
  interrupted-client cleanup journeys using synthetic storage files.
- Identify worker progress by item ID and stage, omitting raw URLs, title fallbacks
  and stored error text; log metadata-fetch failures by exception category without
  copying potentially credential-bearing exception text or traceback.

- Deduplicate discovered URLs and media-candidate identities with ordered set
  membership while preserving first-seen output and distinct type/source provenance.
- Preserve caller cache policy on unsatisfiable ranges, retaining range headers
  and file ownership; transient seek-error 502s now use no-store as described above.

- Ignore malformed or over-budget JSON-LD blobs without losing independent metadata;
  bound iterative traversal to depth 32 and 10,000 values per blob.
- Decode ARD inline audio URL JSON escapes once before HTTP(S) normalization.

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
