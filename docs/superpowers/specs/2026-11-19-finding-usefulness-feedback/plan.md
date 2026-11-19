# Finding usefulness feedback implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the authenticated owner save and revise a useful/not-suitable vote on each marketplace finding.

**Architecture:** Add one PostgreSQL row per finding for the latest owner vote. Include that value in the existing findings feed, save changes through an authenticated same-origin POST route, and render the two actions in every Mini App finding card. Keep this vote separate from verification, relevance classification, and ranking.

**Tech Stack:** Python 3.12, PostgreSQL, stdlib HTTP server, vanilla JavaScript Mini App, existing unittest/Node test harness.

**Spec:** `docs/superpowers/specs/2026-11-19-finding-usefulness-feedback/spec.md`

## Global Constraints

- Values are exactly `useful` and `not_suitable`.
- One latest vote is stored per finding; selecting the other action replaces it.
- Both reading and writing use existing Mini App/Basic Auth authorization.
- Feedback never changes finding verification, classification, importance, filters, or ranking.
- No deployment or production database writes.

## Review Focus

- Unknown finding ID returns not found without inserting an orphan vote.
- Invalid JSON, extra fields, or unsupported vote values are rejected.
- An unauthorized or cross-origin request cannot read or write feedback.
- A failed save leaves the prior selection intact and communicates that it was not saved.
- A repeated vote updates the existing row instead of creating duplicates.

---

### Task 25: Persist and show usefulness feedback

**Files:**
- Modify: `db/schema.sql`
- Modify: `db/__init__.py`
- Modify: `web/main.py`
- Modify: `web/miniapp.html`
- Modify: `tests/test_finding_feedback.py`
- Modify: `tests/miniapp_findings_test.js`
- Modify: `README.md`, `kanban.md`, `progress.md`
- Create: `docs/superpowers/specs/2026-11-19-finding-usefulness-feedback/tasks.md`

**Interfaces:**
- Produce: `db.set_finding_feedback(conn, finding_id: int, feedback: str) -> dict`.
- Accept values `useful` and `not_suitable`, and reject all other inputs.
- `db.feed_findings` returns `feedback` as `useful`, `not_suitable`, or `None` for each finding.
- `POST /api/findings/{id}/feedback` accepts exactly `{"feedback":"useful"}` or `{"feedback":"not_suitable"}` and requires existing owner authorization plus same-origin validation.

- [ ] Add database tests for first vote, changing a vote, unknown finding, and invalid vote.
- [ ] Add API tests for authorized save, unauthorized save, cross-origin rejection, malformed payload, and unknown finding.
- [ ] Add Mini App tests for both actions, saved selection, changing selection, and failed-save behavior.
- [ ] Implement the idempotent `finding_feedback` table with one row per event and cascade cleanup when an event is deleted.
- [ ] Return saved feedback in the existing findings feed and upsert it from the POST route.
- [ ] Render two accessible buttons on every finding card; show the saved state and error/retry status without changing verification state.
- [ ] Update the project README, Kanban status, progress record, and this task checklist.
- [ ] Run targeted feedback tests, then the full suite as required by the project; run `git diff --check` and the Docker image build if Docker is available.
- [ ] Commit the feature as `feat(25): collect finding usefulness feedback`.
- [ ] Push `feature/task-25-finding-feedback` to `origin`; do not deploy.
