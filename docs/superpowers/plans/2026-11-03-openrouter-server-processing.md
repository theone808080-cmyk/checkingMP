# OpenRouter Server Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run one server-side collection and OpenRouter processing cycle against PostgreSQL, without LM Studio or a remote-worker API.

**Architecture:** `python main.py cycle` first collects public materials and then processes one bounded PostgreSQL batch through OpenRouter's OpenAI-compatible Chat Completions API. The model can only produce draft signals marked `needs_check`; invalid or failed responses leave the pending queue unchanged. The web server remains Basic-authenticated and no longer exposes worker endpoints.

**Tech Stack:** Python 3.12, psycopg, standard-library urllib, PostgreSQL, OpenRouter, Coolify.

**Spec:** Conversation-approved server model: `OPENROUTER_MODEL=openai/gpt-4.1-nano`, batch size 10, twice-daily Coolify schedule, user-managed OpenRouter $5 spend limit, and no secrets in Git.

## Global Constraints

- Read only public configured sources; do not add cabinet access, outgoing Telegram messages, or marketplace actions.
- Read `OPENROUTER_API_KEY` only from the runtime environment; never log, commit, or render it.
- Default model is exactly `openai/gpt-4.1-nano`; default batch size is exactly `10` and allowed range remains `1`–`50`.
- OpenRouter requests use HTTPS, an `Authorization: Bearer` header, JSON-object responses, temperature `0`, and a bounded output token count.
- All model-originated signals must be stored with `verification: needs_check`; a model response can never confirm a signal.
- Any model/network/validation failure leaves the material queue unchanged.
- Remove dependency on `LM_STUDIO_*`, `WORKER_API_URL`, and `WORKER_TOKEN`; keep Basic authentication for the web page.
- Documentation and `.env.example` must describe Coolify plus OpenRouter and must contain placeholders only.

## Review Focus

- Missing API key: processing must fail before any queue mutation.
- OpenRouter HTTP request: it must go only to the HTTPS API URL and include the bearer token without exposing it in errors.
- Malformed or duplicate-key model JSON: no event import and no status mutation.
- Empty pending batch: no model request is made.
- `cycle`: a collection pass happens before the processing pass, while a collection failure prevents processing.

---

### Task 1: Server-side OpenRouter cycle

**Files:**
- Modify: `worker/main.py`
- Modify: `main.py`
- Modify: `web/main.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_web_api.py`

**Interfaces:**
- Produces: `worker.process(conn) -> tuple[int, int, int]` using only direct PostgreSQL access and OpenRouter configuration.
- Produces: `main.cycle() -> None`, callable with `python main.py cycle`.
- Produces: a web handler with `/health` public and all non-health routes protected by Basic auth, with no `/api/worker/*` endpoint.

- [ ] **Step 1: Write failing tests**

Add focused tests proving the OpenRouter request URL, model and bearer header; a missing key fails before import; an empty batch does not call the model; `cycle` calls collection before processing; and former worker routes return 404 behind valid Basic auth.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m unittest tests.test_worker tests.test_web_api -v`

Expected: FAIL because current code still uses LM Studio and worker endpoints.

- [ ] **Step 3: Implement the bounded direct server flow**

Replace LM Studio settings with OpenRouter settings. Require a non-empty key, reject non-HTTPS base URLs, issue a POST to `/chat/completions` with `Authorization: Bearer <key>`, retain strict parsing and validation, cap `max_tokens` at the documented default, and retain direct PostgreSQL transactions. Remove remote worker transport and web worker API routes. Add `cycle()` which calls `collect()` and then `process()` in that order.

- [ ] **Step 4: Run focused and full tests**

Run: `./.venv/Scripts/python.exe -m unittest discover -s tests -v`

Expected: PASS with all tests green.

- [ ] **Step 5: Commit**

```bash
git add worker/main.py main.py web/main.py tests/test_worker.py tests/test_web_api.py
git commit -m "feat: run processing through OpenRouter on server"
```

### Task 2: Runtime configuration and Coolify documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `progress.md`

**Interfaces:**
- Consumes: Task 1 command and variables.
- Produces: a copy-safe configuration reference for Coolify Scheduled Tasks and local manual verification.

- [ ] **Step 1: Add documentation assertions through existing text checks**

Use `rg` to verify the updated documentation contains `OPENROUTER_API_KEY`, `OPENROUTER_MODEL=openai/gpt-4.1-nano`, `python main.py cycle`, `DATABASE_URL`, Basic-auth variables, and the Coolify cron `0 6,18 * * *`; verify it contains no real credential values.

- [ ] **Step 2: Update configuration and documentation**

Replace LM Studio/remote-worker variable examples with `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_BASE_URL`, `OPENROUTER_TIMEOUT`, `OPENROUTER_BATCH_SIZE`, and `OPENROUTER_MAX_TOKENS`. Explain Coolify deployment, that only the user enters the OpenRouter key, that `cycle` is the Scheduled Task command, and that the schedule is twice daily. Record the delivery milestone in `progress.md`.

- [ ] **Step 3: Run documentation checks and full tests**

Run: `rg -n "OPENROUTER_API_KEY|OPENROUTER_MODEL|main.py cycle|0 6,18 \* \* \*" .env.example README.md`

Run: `./.venv/Scripts/python.exe -m unittest discover -s tests -v`

Expected: required setup strings found and all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md progress.md
git commit -m "docs: document Coolify OpenRouter processing"
```

## Execution Handoff

The user explicitly requested implementation. Execute with subagent-driven development, review every task and perform a final whole-branch review before merging and pushing.
