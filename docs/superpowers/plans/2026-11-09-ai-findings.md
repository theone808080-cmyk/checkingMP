# Лента ИИ-находок Implementation Plan

> **For agentic workers:** Execute this plan task-by-task; all work belongs to SPEC task 1 and one numbered commit.

**Goal:** Добавить в Mini App отдельную ленту ИИ-находок с пятиуровневой важностью, сортировкой, фильтрами и ссылками на источники.

**Architecture:** `events` becomes the source of finding importance, with an idempotent migration from the existing three string severities. The worker validates/emits levels 1–5; a protected API returns findings with source metadata, deterministic ranking, combined filters, and keyset pagination; the Mini App adds a findings view while preserving the raw materials feed. Update the protected dashboard to read and display the new importance field so the schema change does not break it.

**Tech Stack:** Python, PostgreSQL/psycopg, `unittest`, HTTP server, vanilla JavaScript in `web/miniapp.html`.

**Spec:** `SPEC.md` (approved by the owner on 2026-11-09).

## Global Constraints

- One task from `SPEC.md` means one commit; commit message includes task number `1`.
- Add tests for the behavior and run the full test suite before the commit.
- Keep secrets and tokens out of repository files and test output.
- Preserve source-to-finding links and current Mini App authorization.
- Do not implement behavior outside task 1; report any unavoidable deviation before commit.

## Review Focus

- Existing databases may have only the old text `severity` column and constraint; migration must be safe to rerun and preserve event/material rows.
- The model may omit importance or return values outside integer range 1–5; reject before database import.
- One finding can link to multiple source materials; the finding API must return every source URL and combine source-type filtering predictably.
- Period, platform, importance, topic, verification, and source-type filters must compose without invalid or stale cursors.
- Mini App shell is public, but findings data must remain protected by Basic Auth or the validated Telegram session.

---

### Task 1: Implement five-level findings feed

**Files:**
- Modify: `db/schema.sql`, `db/__init__.py`
- Modify: `worker/main.py`
- Modify: `web/main.py`, `web/miniapp.html`
- Modify: `tests/test_worker.py`, `tests/test_web_api.py`, `tests/test_miniapp.py`, `tests/test_feed.py`, `config/reviewed-signals.example.json`, `README.md`, `progress.md`
- Create if useful: `tests/test_findings.py`
- Include: `SPEC.md` and this implementation plan in the same task commit.

**Interfaces:**
- `db.feed_findings(conn, period='all', platform='all', importance=None, topic=None, verification=None, trust=None, cursor=None, limit=20)` returns `{'findings': [...], 'total': int, 'next_cursor': str | None}`.
- Each finding item includes `id`, `platform`, `topic`, `importance` (integer 1–5), `summary`, `recommended_action`, `verification`, `effective_date`, `updated_at`, and `sources` (all linked source name, trust/category, URL, and publication date).
- Add authenticated `GET /api/findings`; validate single-valued query parameters and reject unknown/invalid values with HTTP 400. Reuse the existing API auth and no-store response behavior.
- В доступном тексте показывать номер уровня (`Важность 1` ... `Важность 5`); не добавлять неутверждённые словесные оценки. Цвета следуют SPEC: серый, синий, жёлтый, оранжевый, красный.

- [ ] **Step 1: Add failing tests for worker importance validation**
  - In `tests/test_worker.py`, exercise accepted integer values 1–5 and rejection of missing, boolean, string, zero, six, and fractional values before import.
  - Assert the model prompt requests the five-level importance scale.
- [ ] **Step 2: Run the worker tests and confirm expected failures**
  - Run `python -m unittest tests.test_worker -v`.
  - Expected: failures show current three-string validation and missing importance behavior.
- [ ] **Step 3: Implement worker contract**
- In `worker/main.py` and `db.import_signals`, request/accept `importance` 1–5, validate strict integer values (booleans excluded), and keep model verification forced to `needs_check`; update `config/reviewed-signals.example.json`.
- [ ] **Step 4: Add failing migration and findings-query tests**
  - Test old `critical`/`important`/`info` mapping to 5/3/1, repeated schema initialization, and preserved links.
  - Test specificity groups (single known marketplace, multi-marketplace/regulator, industry), importance descending within group, stable date/ID tie-break, all filters combined, every linked source returned, and cursor continuation.
- [ ] **Step 5: Run targeted DB tests and confirm expected failures**
  - Run `python -m unittest tests.test_feed tests.test_findings -v` with the configured test PostgreSQL database.
- [ ] **Step 6: Implement schema migration and findings query**
  - In `db/schema.sql`, use integer importance 1–5 and an idempotent migration from legacy text severity while retaining event rows and links; test the migration twice inside a rollback-only schema as in `tests/test_feed.py`.
  - In `db/__init__.py`, implement parameterized filters, deterministic specificity/importance/date/ID order, bounded page size, and a cursor tied to sort keys and filters.
- [ ] **Step 7: Add failing API tests**
  - In `tests/test_web_api.py`, verify unauthenticated requests are rejected, Basic Auth and Telegram session are accepted, duplicate/unknown/invalid filters return 400, and valid query parameters reach the DB layer.
- [ ] **Step 8: Implement `/api/findings` and dashboard compatibility**
  - In `web/main.py`, expose the protected endpoint and update the dashboard rendering/filter options to show importance levels 1–5.
- [ ] **Step 9: Add failing Mini App interaction assertions**
  - In `tests/test_miniapp.py`, assert findings navigation and controls exist; update the Node interaction test to verify level square plus text label, source links, combined filters, and preservation of the publications view.
- [ ] **Step 10: Implement Mini App findings view**
  - In `web/miniapp.html`, add a separate «Находки» view, controls for period/platform/importance/topic/verification/source trust, colored and labeled level square, status/date/recommendation, linked sources, empty/error states, cursor reset on filter changes, and support for both theme modes.
- [ ] **Step 11: Run the full test suite**
  - Run `python -m unittest discover -s tests -v`.
  - Expected: complete pass; report any integration-only checks that skip due missing PostgreSQL or Node rather than counting them as passed.
- [ ] **Step 12: Review diff and commit task 1**
  - Update README navigation and add a significant milestone to `progress.md`. Confirm no secrets/tokens, no unplanned files, and no deviation from `SPEC.md`.
  - Commit all task 1 files once, with a message containing `1`.
