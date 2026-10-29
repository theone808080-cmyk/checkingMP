# Радар маркетплейсов и «Честного Знака»

Локальный ручной пилот для мониторинга публичных изменений Ozon, Wildberries, Яндекс Маркета и «Честного Знака» по бытовой химии.

```text
источники → collector → PostgreSQL → очередь ручного разбора → PostgreSQL → web
```

Collector собирает публикации. Для локальной обработки можно явно запустить `process` с LM Studio либо использовать ручной экспорт `prepare-review` и импорт проверенных сигналов. Railway в этот этап не входит.

## Структура

| Путь | Назначение |
|---|---|
| `config/sources.json` | Реестр публичных источников и их уровень доверия |
| `collector/` | Получение RSS, публичных HTML-лент и Telegram preview |
| `worker/` | Явный запуск ограниченной партии через локальный LM Studio |
| `db/` | Схема PostgreSQL и операции очереди/сигналов |
| `web/` | Локальная страница с фильтрами и состоянием источников |
| `work/` | Временные экспортированные партии ручного разбора (не коммитятся) |

## Подготовка

Нужны Python 3.12+ и локальный PostgreSQL. Создайте базу `marketplace_radar`, скопируйте `.env.example` в `.env` и укажите актуальную строку подключения.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Переменные из `.env` загружаются командной оболочкой перед запуском. В PowerShell для пилота можно задать их в текущем окне:

```powershell
$env:DATABASE_URL = 'postgresql://postgres:password@localhost:5432/marketplace_radar'
```

## Ручной цикл

Для обработки через LM Studio запустите его локальный сервер, загрузите модель и задайте в PowerShell имя модели из ответа `/v1/models`:

```powershell
$env:LM_STUDIO_MODEL = 'имя-загруженной-модели'
.\.venv\Scripts\python.exe main.py health
.\.venv\Scripts\python.exe main.py process
```

По умолчанию используется `http://127.0.0.1:1234/v1`, размер партии — 10 (допустимо 1–50), тайм-аут — 120 секунд. Параметры `LM_STUDIO_BASE_URL`, `LM_STUDIO_MODEL`, `LM_STUDIO_TIMEOUT` и `LM_STUDIO_BATCH_SIZE` можно задать в окружении; `.env` автоматически не загружается. `health` проверяет `/models`, но не запускает обработку. При недоступности сервера или некорректном ответе статусы очереди сохраняются. Сигналы модели требуют проверки человеком. Одновременные запуски `process` не координируются; запускайте только один процесс за раз.

```powershell
.\.venv\Scripts\python.exe main.py collect
.\.venv\Scripts\python.exe main.py prepare-review
.\.venv\Scripts\python.exe main.py web
```

После `prepare-review` файл `work/pending-review.json` передаётся агенту вместе с командой «обработай новости». Агент создаёт `work/reviewed-signals.json`, после чего примените его:

```powershell
.\.venv\Scripts\python.exe main.py import-review work\reviewed-signals.json
```

Структура импортируемого файла приведена в `config/reviewed-signals.example.json`. Значение `severity` допускает только `critical`, `important` или `info`, а `verification` — `confirmed` либо `needs_check`. В `skipped_material_ids` попадают нерелевантные материалы, чтобы они не возвращались в следующую партию.

Откройте `http://localhost:8000`. Команда `python main.py health` проверяет соединение с PostgreSQL.

## Границы пилота

Система читает только публичные источники. Она не входит в кабинеты, не обходит защиту сайтов и не выполняет действий на маркетплейсах. Сигнал — это черновой вывод для проверки человеком, а не юридическое заключение.

