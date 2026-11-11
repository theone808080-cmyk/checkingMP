# Поток данных проекта

Диаграмма показывает обработку публикации до находки и смежные процессы наблюдения, резервирования и проверки источников. Локальная оценка выполняется в теневом режиме и не фильтрует поток: все новые материалы идут в ИИ-анализ. Перевод сохраняется из ответа ИИ вместе с результатами анализа. Статусы относятся к коду в рабочей ветке; наличие кода не означает, что расписания или серверное развёртывание включены.

```mermaid
flowchart LR
    public[Источник: публичные публикации]
    owner[Владелец]
    cycle[Цикл обработки<br/>main.py cycle]
    cycles[(cycles)]
    collector[Сбор<br/>collector]
    source[(sources)]
    material[(materials)]
    translation[Перевод новых материалов<br/>worker]
    relevance[Локальная оценка релевантности<br/>теневой режим: не фильтрует поток]
    assessments[(relevance_assessments)]
    analysis[ИИ-анализ<br/>OpenRouter]
    event[(events + event_materials)]
    review[Ручная проверка]
    web[Лента публикаций и находок<br/>web / Mini App]
    notify[Уведомление<br/>notifier]
    tg[Telegram владельца]
    error[Ошибка этапа<br/>безопасная запись статуса]
    watchdog[Сторож<br/>watchdog]
    health[/Health endpoint/]
    backup[Создание и проверка копии<br/>backup]
    backupdb[(backup_runs)]
    archive[(Зашифрованная копия)]
    backupSchedule[Запланировано: расписание резервных копий]
    discovery[Поиск новых источников<br/>source_discovery]
    candidates[(source_candidates)]
    probe[Безопасная проба адреса]
    discoverySchedule[Запланировано: еженедельный поиск]

    public -->|публикует| collector
    source -->|задаёт адрес сбора| collector
    cycle -->|начинает и завершает| cycles
    cycle -->|запускает| collector
    collector -->|сохраняет материал и источник| material
    collector -->|связь материала с источником| source
    collector -->|сбой сбора| error
    cycle -->|после сбора запускает| notify
    notify -->|отправляет новые публикации| tg
    notify -->|сбой отправки| error
    cycle -->|запускает оценку и обработку| relevance
    material -->|оценивает без блокировки| relevance
    relevance -.->|теневая оценка: не блокирует| analysis
    material -->|пакет новых материалов| analysis
    analysis -->|возвращает русский перевод| translation
    translation -->|сохраняется рядом с оригиналом| material
    relevance -->|сохраняет оценку и версию правил| assessments
    analysis -->|создаёт черновик находки| event
    analysis -->|сбой перевода или анализа| error
    material -->|связь первоисточника с находкой| event
    event --> review
    review -->|проверяет и принимает решение| owner
    event --> web
    material --> web
    owner -->|читает ленту| web
    error --> cycles
    watchdog -->|проверяет свежесть циклов и инциденты| cycles
    watchdog -->|проверяет доступность| health
    watchdog -->|проверяет статус резервирования| backupdb
    health --> web
    backupSchedule -.->|пока не настроено| backup
    backup -->|результат и проверка| backupdb
    backup -->|создаёт| archive
    discoverySchedule -.->|пока не настроено| discovery
    discovery -->|сохраняет кандидатов| candidates
    candidates -->|показывает для решения| owner
    owner -->|одобряет или отклоняет| probe
    probe -->|успех бесплатного публичного доступа| source
    probe -->|ошибка или неизвестные условия| candidates

    classDef implemented fill:#dcefe4,stroke:#34764a,color:#173b25;
    classDef branch fill:#e4e9fa,stroke:#5669a8,color:#202b55;
    classDef planned fill:#fff1d6,stroke:#a87516,color:#503800,stroke-dasharray:5 4;
    class public,collector,material,event,web,notify,tg implemented;
    class owner,cycle,cycles,source,translation,relevance,assessments,analysis,review,error,watchdog,health,backup,backupdb,archive,discovery,candidates,probe branch;
    class backupSchedule,discoverySchedule planned;
```

## Статусы и границы

- **Реализовано** — компонент и связи присутствуют в коде проекта.
- **В этой ветке** — функции задач 3 и 6–9 находятся в текущей рабочей ветке; задачи 1–5 входят в её базу. Эта ветка не означает, что изменения уже объединены или развёрнуты.
- **Запланировано** — код команды резервирования и поиска источников существует, но ежедневное/ежемесячное и еженедельное расписания в среде размещения не настроены. Пунктир показывает отдельный запуск, а не часть `cycle`.
- Находки остаются черновиками до ручной проверки владельцем. Сторож фиксирует инциденты и сообщает о сбоях; он не исправляет данные и не принимает продуктовые решения.
- Диаграмма не содержит адресов инфраструктуры, реквизитов доступа или содержимого публикаций.
