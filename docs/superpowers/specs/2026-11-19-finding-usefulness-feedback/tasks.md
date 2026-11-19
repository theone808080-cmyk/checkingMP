# Task checklist — usefulness feedback

Status: branch pushed to GitHub; production deployment remains with the owner.

## Task 25 — persist and display the owner's latest vote

- [x] Add an idempotent `finding_feedback` table keyed by finding ID; constrain values to `useful` and `not_suitable`.
- [x] Add database upsert and include the current value in findings feed rows.
- [x] Add authenticated, same-origin `POST /api/findings/{id}/feedback` with strict payload validation.
- [x] Add «Полезно» and «Не подходит» controls with persisted selected state and retryable error display.
- [x] Add database, API, and Mini App coverage for the approved behavior.
- [x] Update README, Kanban, and progress documentation.
- [x] Targeted checks: Python 2 passed, 2 PostgreSQL tests skipped without `TEST_DATABASE_URL`, 5 subtests passed; Mini App Node test passed. Full suite: 181 passed, 98 skipped, 6 failed because `TEST_DATABASE_URL` is unset (6 `tests/test_watchdog_state.py` setup errors; 183 subtests passed). No new task-related full-suite failures appeared.
- [ ] Build the Docker image; Docker CLI is unavailable in this environment.
- [x] Commit once with task number 25 and push `feature/task-25-finding-feedback` to `origin`; no deployment.
