# Task 5 Local Relevance Filter Implementation Plan

> **For agentic workers:** Use native inline execution, one implementation commit for SPEC task 5. Keep the work in the existing isolated worktree.

**Goal:** Add a local rule-based material relevance classifier that records shadow assessments and supports blind owner labels and an evaluation report without changing existing feed or AI-processing decisions.

**Architecture:** Keep deterministic classification in a pure `judge` module backed by a versioned JSON ruleset. Persist assessments and owner labels in separate PostgreSQL tables. Run the filter before the existing model call, but catch filter failures so the current worker continues unchanged. Provide local CLI commands to export blind samples, import labels, and report metrics.

**Tech Stack:** Python 3.12+, PostgreSQL, psycopg 3, `unittest`, JSON.

**Spec:** `SPEC.md`, Task 5 — судья релевантности материалов (approved 2026-11-10).

## Global Constraints

- The filter runs only on new materials and never changes material statuses, findings, importance, feed ordering, notifications, or the review queue.
- The rule score is an integer heuristic from 0 to 100, never represented as a probability.
- The five classes are `practical_change`, `seller_action_or_risk`, `industry_context`, `irrelevant`, and `uncertain`.
- Store assessments separately from source material, translations, and findings; retain previous ruleset versions.
- Owner labels must not be returned with predicted results before the owner submits them.
- Do not add external APIs, paid calls, credentials, or secrets.
- Run the complete `python -m unittest discover -s tests -v` suite before the single Task 5 commit.

## Review Focus

- A filter error or database write failure must not stop the existing OpenRouter analysis or mutate its queue.
- One material assessed repeatedly under the same ruleset must not create duplicate rows; a new ruleset version must preserve the prior assessment.
- Explicit irrelevant matches combined with marketplace/action matches must become `uncertain`, not a confident rejection.
- Blind sample exports must exclude predicted class, score, and matched rule codes.
- Imported labels must reject unknown materials, invalid classes, missing labels, and duplicate IDs before any database writes.
- Metrics must include class-wise false positives/negatives and report uncertain ground-truth exclusions clearly.

---

### Task 5: Local relevance filter, blind labels, and report

**Files:**
- Create: `judge/__init__.py`, `judge/constants.py`, `judge/rules.py`, `judge/review.py`, `judge/main.py`, `config/relevance-rules.json`
- Modify: `db/schema.sql`, `db/__init__.py`, `worker/main.py`, `main.py`, `Dockerfile`, `README.md`, `progress.md`, `SPEC.md`
- Create: `tests/test_judge.py`
- Create: this implementation plan

**Interfaces:**
- `judge.rules.classify(material: dict) -> dict` returns `classification`, `score`, `rule_codes`, `platforms`, `rules_version`, and a short safe `explanation`.
- `judge.main.assess_pending(conn) -> int` assesses new materials not yet recorded under the current ruleset; persistence is idempotent for `(material_id, rules_version)`.
- Local commands: `python main.py prepare-judge-review [limit]`, `python main.py import-judge-labels <file>`, and `python main.py judge-report`.
- The model worker invokes `assess_pending` before its normal batch. Any judge error is logged safely and does not prevent the normal model call.

- [x] **Step 1: Write failing tests for the pure rule classifier**

Cover practical marketplace changes, seller risks/actions, general context, explicit irrelevant topics, uncertain/no-match material, conflicting signals, Russian and English terms, score bounds, stable rule codes, and absent/empty source text.

- [x] **Step 2: Run classifier tests to verify they fail for the missing module**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p test_judge.py -v`
Expected: classifier assertions fail because the module is missing.

- [x] **Step 3: Implement versioned rules and the pure classifier**

Read the JSON ruleset from the repository configuration path. Normalize case and `ё`, collect matched rule codes and platform names, use explicit class precedence, cap the heuristic score at 100, and mark mixed positive/irrelevant evidence as uncertain.

- [x] **Step 4: Run classifier tests to verify they pass**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p test_judge.py -v`
Expected: all classifier cases pass without database or network access.

- [x] **Step 5: Write failing persistence and shadow-mode tests**

Test assessment schema constraints and idempotent writes, keeping rule versions, selecting only new unassessed materials, worker ordering, and continuing the existing analysis when the classifier raises. Add optional PostgreSQL integration coverage using `TEST_DATABASE_URL`.

- [x] **Step 6: Run persistence and worker tests to verify they fail for missing behavior**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p test_worker.py -v`
Expected: failures identify missing tables, operations, or worker integration.

- [x] **Step 7: Implement schema, database operations, and shadow integration**

Add `relevance_assessments` with unique `(material_id, rules_version)` and `relevance_labels` keyed by material. Assess all eligible new materials locally before the existing AI request. Keep the filter exception boundary separate from the model-processing transaction.

- [x] **Step 8: Write failing tests for blind samples, label import, and metrics**

Verify exports contain no prediction fields, import validation is atomic, all five classes are accepted, duplicates and unknown material IDs fail, confusion counts and per-class precision/recall are correct, and uncertain truth labels are counted but excluded from class metrics.

- [x] **Step 9: Run labeling and reporting tests to verify they fail for missing behavior**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p test_judge.py -v`
Expected: failures identify missing export, import, or report behavior.

- [x] **Step 10: Implement local owner commands and update project navigation**

Export an editable sample under ignored `work/` with stable material IDs and no predictions. Import labels into their own table only after validating the full file. Report dataset counts, confusion matrix, per-class precision/recall, and concrete false-positive/false-negative examples. Update README and progress with implemented behavior and its shadow-only limit.

- [x] **Step 11: Run focused tests, then the complete suite**

Run focused tests: `.venv\Scripts\python.exe -m unittest discover -s tests -p test_judge.py -v`
Run full suite: `.venv\Scripts\python.exe -m unittest discover -s tests -v`
Expected: all runnable tests pass; PostgreSQL tests are only accepted as skipped if no test database is configured, and the report must state that limitation.

- [x] **Step 12: Request independent read-only code review**

Give a reviewer the approved Task 5 requirements and the complete uncommitted diff against the branch base. Fix Critical and Important findings, then rerun affected tests.

- [x] **Step 13: Review the complete Task 5 diff against SPEC.md**

Confirm no project code or tests changed any source status, findings, sorting, notification, or manual review behavior. Confirm no keys or tokens were added to tracked files.

- [x] **Step 14: Commit Task 5 once**

Stage the SPEC approval, implementation plan, code, tests, and documentation; create one commit whose message contains `5`.

```powershell
git add SPEC.md README.md progress.md docs/superpowers/plans/2026-11-10-local-relevance-filter.md config/relevance-rules.json judge db worker/main.py main.py tests/test_judge.py
git commit -m "feat(5): add shadow relevance filter"
```
