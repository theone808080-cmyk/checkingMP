# Railway trial и локальный LM Studio worker: план реализации

## Global Constraints

- Не подключать API Ozon, Wildberries, Яндекс Маркета или «Честного Знака».
- Не использовать платный API модели и не развёртывать Railway до отдельного подтверждения на внешний аккаунт и публикацию.
- Секреты не попадают в Git: только `.env` или Railway Variables.
- Worker использует локальный LM Studio на `http://127.0.0.1:1234/v1`, запускается вручную и не меняет очередь при ошибке модели.
- Railway-архитектура: web и collector используют внутренний `DATABASE_URL`; local worker общается с web по HTTPS и `WORKER_TOKEN`, без публичного PostgreSQL.
- Плановый cron для Railway collector: `0 6,18 * * *` (09:00 и 21:00 МСК).

## Task 1: Local LM Studio worker and validation

Add a `worker` module and a `process` CLI command. The command obtains a bounded pending-material batch, submits it to LM Studio's OpenAI-compatible chat completion API, parses only strict JSON, validates the signal structure before persistence, and preserves all material statuses when LM Studio is unavailable, times out, or returns invalid output. Add configuration defaults to `.env.example`, an LM Studio probe to `health`, unit tests using mocked HTTP responses, and documentation for local execution.

## Task 2: Protected web worker API and private web access

Extend the existing web server for Railway compatibility: bind to `PORT`, expose unauthenticated `/health`, protect all other browser pages with Basic Auth, and expose token-protected worker endpoints to retrieve a bounded pending batch and submit validated processing output. Use a distinct `WORKER_TOKEN`; reject missing or incorrect credentials without revealing queue data. The endpoints must use the same database operations as the CLI and preserve material states on rejected payloads. Add focused authentication and API tests.

## Task 3: Railway deployment preparation and project documentation

Add Railway configuration for a web service and a one-shot collector cron service, using the same public repository and private database reference variables. Document exact Railway Variables without values, healthcheck, Basic Auth setup, worker-token setup, cron `0 6,18 * * *`, $3 alert/$5 hard usage limit, and the post-trial cron constraint. Update README, progress, and the Holst board text to describe the actual hybrid boundary: Railway handles collection/database/web; LM Studio handles manual local inference. Run the full test suite and a local HTTP smoke check.
