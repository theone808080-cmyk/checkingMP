# Радар маркетплейсов и «Честного Знака»

Локальный ручной пилот для мониторинга публичных изменений Ozon, Wildberries, Яндекс Маркета и «Честного Знака» по бытовой химии.

```text
источники → collector → PostgreSQL → очередь ручного разбора → PostgreSQL → web
```

На этом этапе модельный API не используется. Collector собирает публикации, команда `prepare-review` экспортирует неразобранную партию, а агент по команде пользователя готовит проверенные сигналы и импортирует их в базу. Автономный worker и Railway — следующий, отдельно подтверждаемый этап.

## Структура

| Путь | Назначение |
|---|---|
| `config/sources.json` | Реестр публичных источников и их уровень доверия |
| `collector/` | Получение RSS, публичных HTML-лент и Telegram preview |
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

