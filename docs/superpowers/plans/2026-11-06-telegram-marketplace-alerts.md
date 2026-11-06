# Telegram Marketplace Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver each new marketplace-radar signal to Ivan's personal Telegram chat through `@Findersmpbot`, without exposing secrets or routinely duplicating messages.

**Architecture:** Keep collection and OpenRouter processing unchanged. After `worker.process()` returns, a dedicated notification module claims undelivered event rows from PostgreSQL, sends them to the one configured Telegram chat, and marks an event delivered only after Telegram returns `ok: true`. A PostgreSQL claim prevents concurrent cycles from selecting the same event; ambiguous network failures remain explicitly at-least-once delivery rather than an untruthful exactly-once claim.

**Tech Stack:** Python 3.12 standard-library `urllib`, PostgreSQL via `psycopg`, Coolify environment variables, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-06-telegram-marketplace-alerts-design.md`

## Global Constraints

- Send all new `info`, `important`, and `critical` events; filtering by severity is not part of this version.
- The recipient is exactly `TELEGRAM_CHAT_ID`; no default recipient, inbound bot commands, channels, or groups.
- `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` exist only in runtime configuration, never in Git, tests, README values, exceptions, or logs.
- Events retain `needs_check`; every rendered Telegram message says that it is a signal for verification, not a legal conclusion.
- The existing 09:00 Finder AI job is outside this codebase; identify its server-side scheduler before any separate user-confirmed disablement.
- Do not retry a Telegram request within the same `cycle` run. A later cycle may retry an event whose prior delivery was not confirmed.

## Review Focus

- A success-looking HTTP response whose JSON omits `ok: true` must leave the event undelivered; test in Task 2.
- An API error body or transport exception must not leak `TELEGRAM_TOKEN`; test in Task 2.
- Two concurrent claim attempts must not receive the same event ID; test the `FOR UPDATE SKIP LOCKED` query shape and state update in Task 1.
- A second completed cycle must not re-send an event with `telegram_sent_at`; test in Task 1 and Task 3.
- An unconfirmed timeout can be retried next cycle and may duplicate a message if Telegram accepted it before disconnect; document this boundary and test that the DB record stays pending in Task 2.

---

### Task 1: PostgreSQL delivery state and claim interface

**Files:**
- Modify: `db/schema.sql`
- Modify: `db/__init__.py`
- Create: `tests/test_telegram_delivery.py`

**Interfaces:**
- Produces: `db.claim_telegram_events(conn, limit: int = 50) -> list[dict[str, object]]`
- Produces: `db.mark_telegram_delivered(conn, event_id: int, message_id: int) -> None`
- Produces: `db.release_telegram_claim(conn, event_id: int, error: str) -> None`
- Consumes: rows from `events` with `id`, `platform`, `topic`, `severity`, `summary`, `recommended_action`, `verification`, `telegram_sent_at`, `telegram_claimed_at`, and `telegram_last_error`.

- [ ] **Step 1: Write failing database-interface tests in `tests/test_telegram_delivery.py`**

Use a small recording connection stub. Assert that claiming selects only rows where `telegram_sent_at IS NULL` and `telegram_claimed_at IS NULL`, orders by event ID, and includes `FOR UPDATE SKIP LOCKED`. Assert that delivery stores the timestamp and Telegram message ID, while release clears the claim and truncates a diagnostic error without including a synthetic token.

- [ ] **Step 2: Run the new test module to verify it fails**

Run: `..\.venv\Scripts\python.exe -m unittest tests.test_telegram_delivery -v`

Expected: FAIL because the three delivery functions do not exist.

- [ ] **Step 3: Add idempotent delivery columns and indexes in `db/schema.sql`**

Add `ALTER TABLE events ADD COLUMN IF NOT EXISTS` statements for `telegram_sent_at TIMESTAMPTZ`, `telegram_claimed_at TIMESTAMPTZ`, `telegram_message_id BIGINT`, and `telegram_last_error TEXT`. Add one partial index for rows with `telegram_sent_at IS NULL`. Preserve all existing tables and data.

- [ ] **Step 4: Implement the three `db` functions in `db/__init__.py`**

`claim_telegram_events` must run its candidate selection and claim update in one transaction, use `FOR UPDATE SKIP LOCKED`, return only the fields required to format a message, set `telegram_claimed_at = now()`, and limit the batch to a bounded positive value. `mark_telegram_delivered` must set `telegram_sent_at = now()`, persist the API `message_id`, clear the claim and clear the last error. `release_telegram_claim` must clear the claim and store only a bounded, token-safe diagnostic string.

- [ ] **Step 5: Run the database-interface tests to verify they pass**

Run: `..\.venv\Scripts\python.exe -m unittest tests.test_telegram_delivery -v`

Expected: PASS.

- [ ] **Step 6: Commit the database delivery state**

```powershell
git add db/schema.sql db/__init__.py tests/test_telegram_delivery.py
git commit -m "feat: track Telegram event delivery"
```

### Task 2: Safe Telegram sender and message rendering

**Files:**
- Create: `notifier/__init__.py`
- Create: `notifier/telegram.py`
- Modify: `tests/test_telegram_delivery.py`

**Interfaces:**
- Consumes: one event dictionary returned by `db.claim_telegram_events`.
- Produces: `telegram.settings() -> tuple[str, int, float]`
- Produces: `telegram.render(event: dict[str, object]) -> str`
- Produces: `telegram.send(event: dict[str, object]) -> int`
- Produces: `telegram.deliver_pending(conn, limit: int = 50) -> tuple[int, int]`, returning `(delivered, failed)`.

- [ ] **Step 1: Extend `tests/test_telegram_delivery.py` with failing sender tests**

Mock the HTTP opener and database functions. Cover one rendered message for each severity value, `needs_check` text, source URL, valid `ok: true` API result with numeric `message_id`, empty queue without HTTP, missing configuration without HTTP, malformed JSON, `ok: false`, and a timeout. Assert that successful send calls `mark_telegram_delivered`; all failures call `release_telegram_claim`; no assertion or raised text contains a synthetic token.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `..\.venv\Scripts\python.exe -m unittest tests.test_telegram_delivery -v`

Expected: FAIL because `notifier.telegram` does not exist.

- [ ] **Step 3: Implement `notifier/telegram.py` with fixed Telegram API transport**

Read `TELEGRAM_TOKEN`, integer `TELEGRAM_CHAT_ID`, and optional `TELEGRAM_TIMEOUT` (default `15`, allowed range `1..120`) from the environment. Reject absent, whitespace-containing, or non-printable configuration before any request. POST JSON only to `https://api.telegram.org/bot<TOKEN>/sendMessage`, with the configured `chat_id`, rendered plain text, and link-preview suppression. Use a no-redirect opener; parse JSON with duplicate-key rejection; accept only a top-level object whose `ok` is exactly `true` and whose `result.message_id` is an integer. Never include URL, headers, response body, or token in raised errors.

- [ ] **Step 4: Implement `render(event)` and `deliver_pending(conn, limit=50)`**

Render severity, platform and topic, saved summary, recommended action, first linked material URL supplied by Task 1, and the fixed verification disclaimer. `deliver_pending` claims once, sends each event once, marks confirmed sends, releases failed sends, and returns counts without re-raising routine Telegram delivery failures into collection or OpenRouter processing.

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `..\.venv\Scripts\python.exe -m unittest tests.test_telegram_delivery -v`

Expected: PASS, including no-network cases and failure-safe queue handling.

- [ ] **Step 6: Commit the Telegram sender**

```powershell
git add notifier tests/test_telegram_delivery.py
git commit -m "feat: deliver radar signals to Telegram"
```

### Task 3: Attach delivery to the cycle and document Coolify setup

**Files:**
- Modify: `main.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `progress.md`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_telegram_delivery.py`

**Interfaces:**
- Consumes: `telegram.deliver_pending(conn, limit=50)` from Task 2.
- Produces: `main.cycle()` executes collection, OpenRouter processing, then Telegram delivery in that order.

- [ ] **Step 1: Add failing orchestration tests**

Extend `tests/test_worker.py` with `test_cycle_delivers_after_processing`, mocking collection, processing, and `notifier.telegram.deliver_pending`. Assert their order is exactly `collect`, `process`, `deliver`. Add a duplicate test in `tests/test_telegram_delivery.py` that mocks an already delivered event and proves no sender call occurs.

- [ ] **Step 2: Run the orchestration tests to verify they fail**

Run: `..\.venv\Scripts\python.exe -m unittest tests.test_worker.WorkerTests.test_cycle_delivers_after_processing -v`

Expected: FAIL until `cycle()` includes the delivery step.

- [ ] **Step 3: Modify `main.cycle()` to deliver after successful processing**

Keep the existing order and allow no notification work when no claimed events exist. Use a fresh PostgreSQL connection for delivery after `process()` closes its connection, initialize the schema, invoke `deliver_pending`, and print only delivered/failed counts. Do not expose a manual public endpoint and do not add inbound Telegram polling.

- [ ] **Step 4: Update configuration and operator documentation**

In `.env.example`, add blank `TELEGRAM_TOKEN=`, blank `TELEGRAM_CHAT_ID=`, and `TELEGRAM_TIMEOUT=15` with an explicit warning that token and chat ID belong only in Coolify. In `README.md`, describe all-severity personal alerts, delivery retry semantics, the rare ambiguous-network duplicate boundary, and the exact Coolify verification sequence: deploy, add secrets, run one deliberate test cycle, check one message, then rerun without a duplicate. In `progress.md`, record the implemented milestone only after tests pass. State that disabling the legacy Finder AI task requires locating the old scheduler first and is not performed by deployment of this repository.

- [ ] **Step 5: Run full regression verification**

Run: `..\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: PASS for all existing collector, web, worker, and new Telegram delivery tests.

- [ ] **Step 6: Commit orchestration and documentation**

```powershell
git add main.py .env.example README.md progress.md tests/test_worker.py tests/test_telegram_delivery.py
git commit -m "feat: run Telegram delivery after radar cycle"
```

### Task 4: Controlled server activation and legacy Finder separation

**Files:**
- Modify: `README.md`
- Modify: `progress.md`

**Interfaces:**
- Consumes: deployed Task 3 application and Coolify Scheduled Task.
- Produces: an evidence-backed activation record; no automated change to the separate Finder service.

- [ ] **Step 1: Add an operator checklist for the required user-controlled secret entry**

Document that the user enters the existing bot token and personal chat ID directly in Coolify, confirms both variables are hidden, and never sends either value in chat, GitHub, or screenshots. Include the exact variable names but no values.

- [ ] **Step 2: Perform one user-observed controlled delivery only after separate confirmation**

Use one known pending event or a bounded test record. Verify the message arrives from `@Findersmpbot`, contains the expected radar fields and disclaimer, and record its event ID/message ID without secrets. Do not perform this external send automatically from implementation.

- [ ] **Step 3: Verify no routine duplicate and document delivery state**

Re-run only the delivery portion against the same database state and verify no second message appears for the confirmed event. If a network-ambiguous failure occurs, report it as unknown delivery rather than claiming either success or duplicate prevention.

- [ ] **Step 4: Locate, then separately seek confirmation to disable the old Finder AI scheduler**

Inspect the Finder deployment/server for the exact scheduled command that generated the 09:00 AI digest. Report its resource and command first. Do not stop, delete, or alter that task until the user explicitly confirms that precise target.

- [ ] **Step 5: Commit only documentation changes actually made during activation**

```powershell
git add README.md progress.md
git commit -m "docs: record Telegram alert activation"
```
