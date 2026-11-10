"""Pure monitoring decisions for the Radar watchdog."""
from __future__ import annotations

from datetime import datetime, timedelta


CYCLE_STALE_AFTER = timedelta(hours=14)
APP_RESTART_AFTER_FAILURES = 3


def restart_due(*, app_available: bool, database_available: bool,
                failed_checks: int, restart_attempted: bool) -> bool:
    """Restart only an unavailable app, after three checks, once per outage."""
    return (not app_available and database_available
            and failed_checks >= APP_RESTART_AFTER_FAILURES
            and not restart_attempted)


def evaluate(cycles: list[dict], *, database_available: bool,
             app_available: bool, now: datetime,
             monitor_started_at: datetime,
             latest_success_at: datetime | None = None,
             stuck_cycles: list[dict] | None = None) -> dict[str, str]:
    """Return current non-transient incidents using newest-first cycle records."""
    incidents: dict[str, str] = {}
    if not database_available:
        incidents["database_unavailable"] = "Не удаётся подключиться к базе данных. Перезапуск приложения не выполнялся."
    if not app_available and database_available:
        incidents["application_unavailable"] = "Веб-приложение не отвечает; проверка базы данных проходит."

    stuck = next((cycle for cycle in [*(stuck_cycles or []), *cycles]
                  if cycle.get("status") == "started" and cycle.get("started_at")
                  and now - cycle["started_at"] > CYCLE_STALE_AFTER), None)
    if stuck:
        incidents["cycle_stuck"] = "Полный цикл не завершился более 14 часов назад."

    latest_success = latest_success_at or next((cycle.get("finished_at") for cycle in cycles
                           if cycle.get("status") == "success" and cycle.get("finished_at")), None)
    reference_time = latest_success or monitor_started_at
    if now - reference_time > CYCLE_STALE_AFTER:
        incidents["cycle_stale"] = "Более 14 часов нет успешного полного цикла сбора и обработки."

    completed = [cycle for cycle in cycles if cycle.get("finished_at")]
    last_two = completed[:2]
    if (len(last_two) == 2 and all(cycle.get("processing_status") == "failed" for cycle in last_two)
            and last_two[0].get("error_stage") == last_two[1].get("error_stage")
            and last_two[0].get("error_message") == last_two[1].get("error_message")):
        incidents["ai_processing"] = "Обработка ИИ завершилась ошибкой в двух последовательных циклах."

    if len(last_two) == 2:
        latest_sources = set(last_two[0].get("source_failures") or [])
        previous_sources = set(last_two[1].get("source_failures") or [])
        latest_signatures = last_two[0].get("source_failure_signatures") or {}
        previous_signatures = last_two[1].get("source_failure_signatures") or {}
        repeated_sources = latest_sources & previous_sources
        if latest_signatures and previous_signatures:
            repeated_sources = {code for code in repeated_sources
                                if latest_signatures.get(code) == previous_signatures.get(code)}
        for source_code in sorted(repeated_sources):
            incidents[f"source_failure:{source_code}"] = f"Источник {source_code} не читается в двух последовательных циклах."

    return incidents
