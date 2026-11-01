# Changelog

## Unreleased

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
