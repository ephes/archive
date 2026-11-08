# Round 85 archive B23 resume

Parked on `fix/r85-bounded-worker-recovery` from `origin/main`
`93ca0efec7d75dce909bca3aed3e31f11766e541`. Nothing was merged.

All three slices were reverified against that fresh default-branch snapshot;
none was already fixed. Implemented in report bundle order:

1. `archive-health-event-prune-write-budget`: each outcome deletes at most 100
   expired events ordered by timestamp and primary key. Independent exact streak
   membership and recent monitoring counts remain intact.
2. `archive-stale-recovery-bounded-writer-pass`: each stale recovery pass selects
   at most 100 oldest attempts and rechecks their primary key and captured timestamp
   before updating. Existing worker iterations drain the backlog; `--once` recovers
   one batch. Explicit force recovery is unchanged.
3. `archive-worker-kill-restart-durable-claim-journey`: synthetic subprocess tests
   hard-kill at committed claim and staged-upload barriers, reopen temporary SQLite,
   preserve fresh attempts, recover once after cutoff, reject a delayed superseded
   result and preserve shared referenced media. No production change for this slice.

README and Unreleased notes match the changes. No versions, workflows or deployment
configuration changed. Tests use synthetic data and temporary filesystem storage.

## Validation completed

- Python 3.12, frozen lock, all dependency groups installed.
- `just check`: Ruff passed; mypy passed (92 files); **980 tests passed**.
- `DJANGO_SETTINGS_MODULE=config.settings.test uv run python manage.py makemigrations
  --check --dry-run` from `src/django`: no changes detected.
- `just audit`: no known vulnerabilities in locked runtime/development dependencies.
  Set `UV_TOOL_DIR` and `UV_CACHE_DIR` under the workspace's `.cache`; audit initially
  encountered blocked tool-cache writes before that override. Its own optional cache
  also emitted a write warning; the audit completed successfully.
- Three new maintenance assertions failed against the original code: entire expired
  backlog deleted, entire stale population reset, and replaced attempt reset.
- The unmutated subprocess journey passed both kill points; removing durable claim
  timestamp persistence made both fail; bypassing publication fencing made both fail.
  Both temporary mutations were restored. The existing 3,000-retry regression was
  updated for bounded retention while preserving its exact streak assertions.

## Review blocker and next steps

The requested `claude-review-loop` invocation with `--model claude-opus-5-5
--effort high` exited 2 (`PROVIDER_ERROR`) before reviewing any code:
**Failed to authenticate: OAuth session expired and could not be refreshed.**
There is no valid review verdict and no approval to merge this branch yet.
Local artifacts: workspace `reviews/b23-r1/result.json`; no skipped/redacted or
truncated review input. The disposable review copy was removed successfully.

After the owner refreshes authentication, rerun a fresh whole-bundle review using
`~/projects/agent-stuff/codex/skills/claude-review-loop/bin/claude-review-loop`,
with Python bytecode writes disabled, Opus 5.5/high, a new empty run directory,
`--slice-id archive-r85-b23 --record-baseline`, and writable workspace cache,
ledger and lock directories. Context is locally saved as workspace
`review-context.md`; this note also summarizes the intended scope and evidence.
Apply the original four-round/repeated-finding stop rules. Fix/re-review required
findings, keeping delta rounds scoped to the recorded baseline.

Once review is clean or suggestion-only with dispositions recorded: fetch origin,
rebase on current `origin/main`, reverify the slices and rerun local checks if it
moved. Merge locally with `--no-ff`, run the full local checks after merge, push
only `main` and verify it with `git ls-remote`. Do not create PRs, invoke/wait for
Actions, deploy, release/tag, delete remote branches or write to the work app.
