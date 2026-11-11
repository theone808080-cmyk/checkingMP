# Russian News Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Display newly collected non-Russian news in Russian and ensure new AI findings use Russian text, while retaining source originals and links.

**Architecture:** The existing AI worker returns Russian translations for newly collected materials together with findings. Store translated display fields separately; the original `title` and `text` remain unchanged. Existing rows are marked as legacy and are not translated. Until processing completes, the Mini App shows a Russian pending label instead of new source text.

**Tech Stack:** Python 3, PostgreSQL, existing OpenRouter-compatible worker, Python `unittest`, Node-based Mini App tests.

**Spec:** `SPEC.md`, Task 2 — перевод новых материалов на русский язык.

## Global Constraints

- Translate only materials collected after release; do not translate or rewrite existing rows.
- Preserve original title, text, URL, and source name.
- Translate the title and only the first 400 characters shown in the feed.
- Keep new pending source text hidden until processing succeeds.
- Write new AI finding summary, topic, and recommendation in Russian.
- Use the existing AI processing configuration; add no translation service or secret.
- One task, one commit; the commit message must contain task number `2`.
- Run all repository tests before committing.

## Review Focus

- Old unprocessed queue rows must remain legacy and must not get translated during later worker runs; verify a PostgreSQL test.
- Every pending new material needs a translation even when the model skips it as a finding; verify worker validation and importer test.
- A missing or malformed translation must leave the queue unchanged; verify worker failure tests.
- Russian source text must be preserved unchanged; verify worker output and database persistence.
- The UI must never expose a pending new foreign headline or excerpt; verify API and Mini App rendering tests.

---

### Task 2: Russian translations for new materials

**Files:**
- Modify: `db/schema.sql` — add legacy/pending/ready translation state and nullable translated title/excerpt, with an idempotent migration.
- Modify: `db/__init__.py` — initialize new ingested materials as pending; include translation state in feed and worker queries; persist translations atomically with worker results.
- Modify: `worker/main.py` — request Russian translation for pending materials during inference and manual review, validate complete and unique material coverage, and require finding text in Russian.
- Modify: `main.py` — translate pending source materials referenced by manually reviewed findings before import.
- Modify: `web/miniapp.html` — show translated title/excerpt, and a Russian pending label while translation is unavailable.
- Modify: `tests/test_feed.py` — cover migration, new/legacy state, translated feed values, and atomic translation persistence in PostgreSQL.
- Modify: `tests/test_worker.py` — cover translations for all pending IDs, including skipped findings and manual review; reject missing, duplicate, empty excerpt, or unexpected translation values; verify finding fields and process forwarding.
- Modify: `tests/test_miniapp.py`; create `tests/miniapp_feed_translation_test.js` — cover pending placeholder, translated display, and original source URL. Existing API authentication tests already cover feed access, and the feed API serializes the database result directly.
- Modify: `SPEC.md`, `README.md`, `progress.md` — mark Task 2 approved and document delivered behavior after implementation.

**Interfaces:**
- The worker completion adds `translations`, an array of objects `{material_id, title_ru, text_ru}` for every batch item whose `translation_status` is `pending`; `title_ru` must be non-empty with no arbitrary length cap, and `text_ru` must be a string of at most 400 characters.
- `db.import_signals(conn, signals, skipped_material_ids, *, translations=(), reject_confirmed=False)` persists translation fields and existing signal/skip state in the caller's transaction.
- Feed material API items add `translation_status`, `title_ru`, and `text_ru`; originals remain available as `title`, `text`, and `url`.
- Manual import uses the existing AI settings to translate referenced pending materials. The database importer rejects any pending material being marked done/skipped without a translation.

- [x] **Step 1: Write failing database and feed tests.** Verify existing rows migrate to `legacy`, `add_material` creates `pending`, the queue exposes state, importer translation changes only Russian display fields and marks the material ready, and a later legacy queue row is not translated.
- [x] **Step 2: Run `python -m unittest tests.test_feed -v`.** The new tests failed on missing feed fields and migration columns.
- [x] **Step 3: Implement schema migration and database interfaces** in `db/schema.sql` and `db/__init__.py`; all 9 database tests passed.
- [x] **Step 4: Write failing worker tests.** Verify complete translation coverage for pending IDs (including skipped findings), unchanged Russian text, Russian finding prompt/output, importer forwarding, and no import on incomplete/invalid output.
- [x] **Step 5: Run `python -m unittest tests.test_worker -v`.** The new validation and Russian prompt assertions failed before implementation.
- [x] **Step 6: Implement translation response validation and persistence flow** in `worker/main.py` and `db/__init__.py`; all 18 worker tests passed.
- [x] **Step 7: Write failing UI tests.** Verify pending foreign text is hidden behind «Перевод обрабатывается», completed cards display Russian fields, legacy cards remain unchanged, and source links still target the original URL.
- [x] **Step 8: Run `python -m unittest tests.test_miniapp.MiniAppTests.test_source_feed_displays_new_translations_and_hides_pending_originals -v`.** The new test failed on the untranslated headline before implementation.
- [x] **Step 9: Implement Mini App display behavior** in `web/miniapp.html`; the targeted interface test passed. `web/main.py` already serializes the complete `db.feed_materials` result behind existing authorization, so the new fields pass through without a route change.
- [x] **Step 10: Update `README.md`, `SPEC.md`, and `progress.md`; inspect the complete diff and run `git diff --check`.
- [x] **Step 11: Run the full repository test suite:** `python -m unittest discover -s tests -v`, with isolated PostgreSQL integration tests enabled. 62 tests passed.
- [x] **Step 12: Close final review findings.** Add regression coverage and guards for manual review imports without translations and empty translated excerpts. Targeted tests passed.
- [x] **Step 13: Run the full repository test suite:** `python -m unittest discover -s tests -q`, with isolated PostgreSQL integration tests enabled. 63 tests passed.
- [ ] **Step 14: Commit the complete Task 2 change once:** `git commit -m "feat(2): translate new news into Russian"`.
