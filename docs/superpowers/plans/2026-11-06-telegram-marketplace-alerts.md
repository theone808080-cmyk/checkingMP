# Telegram Mini App: implementation plan

> For agentic workers: execute sequentially in the isolated worktree with an independent review after each task. The specification is `docs/superpowers/specs/2026-11-06-telegram-marketplace-alerts-design.md`.

**Goal:** Display marketplace source materials as a private Telegram Mini App and send the owner a compact notification when new materials arrive.

**Architecture:** Keep the existing collector → PostgreSQL → OpenRouter → dashboard pipeline. Expose read-only Mini App APIs on the current server; verify Telegram initData server-side before granting a short-lived session. The collector's newly saved materials feed a PostgreSQL-backed notification checkpoint; a separate sender posts a single message with a Mini App button.

**Stack:** Python 3.12 standard library, psycopg/PostgreSQL, responsive HTML/CSS/JavaScript, Telegram Bot API. No new external service.

## Global constraints

- The ordinary browser remains protected by existing Basic Auth. Mini App access requires a valid signed Telegram initData user ID equal to `TELEGRAM_CHAT_ID`; never trust client-side identity or expose token.
- API and notification responses, error logs, Git, tests, and documentation contain no real credentials; configure token and chat ID in Coolify only.
- Show raw materials rather than model-generated summaries. Do not access marketplace accounts, receive bot commands, or disable the old Finder AI job automatically.
- Notify only newly collected materials. Initial activation must baseline pre-existing material IDs and must not send a historical backlog.
- Confirmed Telegram sends are not repeated; if Telegram accepted a request but its response was lost, a subsequent retry may duplicate a notice. Never claim exactly-once delivery.
- Any external test send, bot menu change, deployment, or old-job change is a separate observable activation step and must follow the user's authorization and environment access.

## Review focus

- HMAC verification, freshness and identity: forged, duplicate-key, missing, expired or future initData must fail closed.
- API must not leak material data before authentication; Basic Auth, `/health`, protected error handling and existing dashboard continue to work.
- News pagination and counts must remain consistent under ties and live insertions; calendar filters use Europe/Moscow.
- Notification checkpoints survive process crashes and overlapping scheduled cycles without dropping new materials or resending confirmed batches.
- Token-bearing Telegram URLs, malformed API responses and exceptions must never reveal credentials.

### Task 1: Material feed and source status

**Files:** `config/sources.json`, `db/schema.sql`, `db/__init__.py`, `tests/test_feed.py`.

**Interfaces:** Implement `db.feed_materials(conn, period, platform, cursor, limit)` returning material cards, total, and next cursor; `db.feed_sources(conn)` returning source statuses.

- [ ] Write failing tests for all categories, all/today/week date boundaries in Moscow, fallback date, deterministic descending (date, id) cursor pagination and source statuses. Assert fresh unprocessed materials appear and injected filter values cannot become SQL.
- [ ] Run tests and verify expected failures.
- [ ] Add explicit source category in configuration and an idempotent `sources.category` migration that works on an existing database; update `sync_sources` to backfill categories. Map the nine current sources as specified, including Rospotrebnadzor as industry.
- [ ] Implement parameterized read-only queries with capped page size 20 and opaque cursor containing sort date and ID. Use the effective publication timestamp or collection timestamp, stable descending ordering, and category-aware totals. Return source title, status and sanitized last check; never expose raw database errors in API.
- [ ] Run focused and full tests and commit the task.

### Task 2: Telegram authentication and read-only routes

**Files:** `web/main.py`, `web/telegram_auth.py`, `tests/test_web_api.py`, `tests/test_telegram_auth.py`.

**Interfaces:** `validate_init_data(raw, token, allowed_user_id, now) -> bool`; POST `/miniapp/session` creates a short-lived HttpOnly Secure SameSite=Strict session; GET `/api/feed` and GET `/api/sources` require this session or existing Basic Auth.

- [ ] Write failing tests with independently constructed Telegram HMAC fixtures for valid payload, changed signature, duplicate fields, wrong user, absent data/configuration, timestamps older than 5 minutes or more than 30 seconds in the future. Test routes for unauthorized requests, valid sessions, expiration, no public sensitive response, browser Basic Auth, and /health regression.
- [ ] Run tests and verify expected failures.
- [ ] Verify `initData` using Telegram's documented HMAC construction and constant-time comparison, strict query parsing, user JSON validation, integer owner ID matching and timestamp bounds. Do not log token, signature or initData.
- [ ] Serve a data-free Mini App shell at `/miniapp`; POST-only session bootstrap with bounded request size; return a cryptographically random signed session cookie with 30-minute expiry and no token or user data in URL. Require authentication on every feed/status API request; no-store sensitive responses. Reject duplicate or invalid filters and cursors.
- [ ] Test adversarial inputs, existing routes and health; run full tests and commit.

### Task 3: Responsive Telegram feed

**Files:** `web/miniapp.html`, `web/main.py`, `Dockerfile`, `tests/test_miniapp.py`.

**Interfaces:** `/miniapp` serves the static data-free shell; shell uses Telegram WebApp initData to establish session and calls Task 2 APIs.

- [ ] Write failing tests for shell delivery and absence of private data before login; escaping and safe HTTP(S) source links, filter controls, pagination and both screen sections. Verify static assets are included in Docker.
- [ ] Run tests and verify expected failures.
- [ ] Build accessible mobile-first cards, category and period controls, load-more pagination, counts, empty/loading/error states and source status section. Use Telegram theme parameters with light/dark fallback and respond to theme changes. Escape all source text, validate link schemes, and show publication/collection date appropriately.
- [ ] Preserve existing browser dashboard and its Basic Auth; support browser access to the feed with the same existing credentials. Confirm that clicking a source link opens the original page.
- [ ] Run automated tests plus a local browser smoke test at mobile and desktop widths in light and dark themes, then commit.

### Task 4: Compact bot notifications

**Files:** `notifier/telegram.py`, `db/schema.sql`, `db/__init__.py`, `main.py`, `.env.example`, `Dockerfile`, `tests/test_notifications.py`.

**Interfaces:** `notify_new_materials(conn)` uses `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` and HTTPS `RADAR_MINIAPP_URL`, producing one message with a web_app button per new-material batch.

- [ ] Write failing tests: baseline old rows on first activation; zero new rows produce no HTTP request; fresh rows produce correct count and button URL; concurrent cycles cannot claim the same batch; confirmed send advances checkpoint; timeout preserves pending batch and later retry; newly collected rows during sending remain pending; transport/API errors do not reveal token.
- [ ] Run tests and verify expected failures.
- [ ] Add idempotent durable outbox/checkpoint schema. At the start of the first configured cycle, before collection, atomically baseline current max material ID; thus existing history is excluded but that cycle's new rows are counted. A batch records immutable upper/lower IDs and count; claim excludes other senders, avoids holding transactions indefinitely, and recovers stale claims. Mark a batch sent only on Telegram `ok: true` with message ID. Never discard later material IDs while a batch is in flight.
- [ ] POST fixed HTTPS Telegram `sendMessage` with plain text «Новых новостей: N» and inline `web_app` button «Открыть ленту». Validate token, numeric private chat ID, and HTTPS Mini App URL; no redirects or secret-bearing diagnostics. Missing configuration skips notifier without preventing collector or worker; call notification after collection even if subsequent model processing fails.
- [ ] Provide explicit user-invoked operator command for `setChatMenuButton`, without inbound bot polling or background configuration. Document that running it changes the existing bot menu.
- [ ] Test retries, empty collections, invalid config and full regression; commit.

### Task 5: Documentation and controlled activation

**Files:** `README.md`, `progress.md`.

- [ ] Update documentation with environment variable names only, canonical Mini App URL from Coolify, baseline behavior, rare ambiguous-network duplicate, Basic Auth and Mini App access, menu configuration and the current scheduler.
- [ ] Record implementation and test evidence in progress; do not state that deployment or message delivery succeeded until observed.
- [ ] Deploy only the reviewed change according to user-authorized workflow. User enters token, ID and URL directly in Coolify; no secrets in chat or screenshots.
- [ ] Perform one user-observed controlled notification and check button, feed, both themes, browser access, source links, pagination and lack of routine duplicate.
- [ ] Locate the existing Finder AI scheduler and report its exact resource and command before changing it. Disable only that identified job when authorized; never delete the bot or database.
- [ ] Commit documentation and run full tests, security review and UI smoke checks before final handoff.
