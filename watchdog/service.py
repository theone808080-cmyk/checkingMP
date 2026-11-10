"""Watchdog orchestration, persistent incident state, and five-minute loop."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
import urllib.request

import psycopg

import db
from watchdog.integrations import open_url, restart_coolify, send_telegram_alert
from watchdog.logic import CYCLE_STALE_AFTER, evaluate, restart_due


CHECK_INTERVAL_SECONDS = 300
HEALTH_TIMEOUT_SECONDS = 10
HEARTBEAT_MAX_AGE = timedelta(minutes=10)
BACKUP_STALE_AFTER = timedelta(hours=36)
BACKUP_RESTORE_STALE_AFTER = timedelta(days=35)


@dataclass(frozen=True)
class Settings:
    database_url: str
    radar_health_url: str
    coolify_api_url: str
    coolify_application_uuid: str
    coolify_api_token: str
    telegram_token: str
    telegram_chat_id: str
    state_path: Path = Path("/data/watchdog-fallback.json")
    heartbeat_path: Path = Path("/data/watchdog-heartbeat")

    @classmethod
    def from_env(cls) -> "Settings":
        names = ("WATCHDOG_DATABASE_URL", "RADAR_HEALTH_URL", "COOLIFY_API_URL",
                 "COOLIFY_APPLICATION_UUID", "COOLIFY_API_TOKEN", "TELEGRAM_TOKEN",
                 "TELEGRAM_CHAT_ID")
        values = {name: os.environ.get(name, "").strip() for name in names}
        if any(not value for value in values.values()):
            raise ValueError("Missing required watchdog environment settings")
        if not values["WATCHDOG_DATABASE_URL"].startswith(("postgresql://", "postgres://")):
            raise ValueError("Invalid watchdog database URL")
        for name in ("RADAR_HEALTH_URL", "COOLIFY_API_URL"):
            from urllib.parse import urlsplit
            parsed = urlsplit(values[name])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Watchdog endpoints must use HTTPS without embedded credentials")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", values["COOLIFY_APPLICATION_UUID"]):
            raise ValueError("Invalid Coolify application identifier")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", values["TELEGRAM_TOKEN"]):
            raise ValueError("Invalid Telegram bot token format")
        if not re.fullmatch(r"[1-9][0-9]*", values["TELEGRAM_CHAT_ID"]):
            raise ValueError("Invalid Telegram chat identifier")
        return cls(
            database_url=values["WATCHDOG_DATABASE_URL"],
            radar_health_url=values["RADAR_HEALTH_URL"],
            coolify_api_url=values["COOLIFY_API_URL"],
            coolify_application_uuid=values["COOLIFY_APPLICATION_UUID"],
            coolify_api_token=values["COOLIFY_API_TOKEN"],
            telegram_token=values["TELEGRAM_TOKEN"],
            telegram_chat_id=values["TELEGRAM_CHAT_ID"],
            state_path=Path(os.environ.get("WATCHDOG_STATE_PATH", "/data/watchdog-fallback.json")),
            heartbeat_path=Path(os.environ.get("WATCHDOG_HEARTBEAT_PATH", "/data/watchdog-heartbeat")),
        )


def probe_app_health(url: str) -> bool:
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    try:
        with open_url(request, timeout=HEALTH_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _read_fallback(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_last_success(path: Path, last_success: datetime | None) -> None:
    if last_success is None:
        return
    state = _read_fallback(path)
    state["last_success"] = last_success.isoformat()
    _write_fallback(path, state)


def _remember_app_probe(path: Path, app_available: bool) -> None:
    state = _read_fallback(path)
    if app_available:
        state["app_recovered_during_database_outage"] = True
    _write_fallback(path, state)


def _remembered_last_success(state: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(state["last_success"])
    except (KeyError, TypeError, ValueError):
        return None

def _write_fallback(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _write_heartbeat(path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now.astimezone(timezone.utc).isoformat(), encoding="ascii")


def healthcheck(heartbeat_path: Path | None = None, *, now: datetime | None = None) -> bool:
    path = heartbeat_path or Path(os.environ.get("WATCHDOG_HEARTBEAT_PATH", "/data/watchdog-heartbeat"))
    try:
        heartbeat = datetime.fromisoformat(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    return current - heartbeat <= HEARTBEAT_MAX_AGE and heartbeat <= current + timedelta(minutes=1)


def container_healthcheck() -> bool:
    if os.environ.get("WATCHDOG_MODE") == "1":
        return healthcheck()
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        return False
    return probe_app_health(f"http://127.0.0.1:{port}/health")

def _format_notice(key: str, details: str, last_success: datetime | None,
                   *, recovered: bool = False) -> str:
    state = "Восстановлено" if recovered else "Сбой"
    last = last_success.astimezone(timezone.utc).isoformat(timespec="minutes") if last_success else "успешных циклов пока нет"
    return f"Радар — {state}: {key}\n{details}\nПоследний успешный цикл: {last}."[:4000]


def sync_incidents(conn, desired: dict[str, str], send_notice, *,
                   last_success: datetime | None = None, now: datetime | None = None) -> None:
    """Open, notify, deduplicate, resolve, and notify recovery for incidents."""
    existing = db.watchdog_incidents(conn)
    for key, details in desired.items():
        state = existing.get(key)
        if not state or not state["active"]:
            db.open_watchdog_incident(conn, key, details, now=now)
        else:
            db.update_watchdog_incident(conn, key, details=details)
    existing = db.watchdog_incidents(conn)
    delivery = existing.get("telegram_delivery")
    if (delivery and delivery["active"] and "telegram_delivery" not in desired
            and db.has_unresolved_notification_failure(conn)):
        desired["telegram_delivery"] = delivery["details"]
    for key, state in existing.items():
        if state["active"] and key not in desired:
            db.resolve_watchdog_incident(conn, key, now=now)
            state["active"] = False
            state["resolved_at"] = now
        if key in desired and state["active"] and state["alert_sent_at"] is None:
            try:
                sent = send_notice(key, _format_notice(key, desired[key], last_success), False)
            except Exception:
                sent = False
            if sent:
                db.mark_watchdog_notice_sent(conn, key, now=now)
        elif not state["active"] and state["alert_sent_at"] and state["recovery_sent_at"] is None:
            try:
                sent = send_notice(key, _format_notice(key, state["details"], last_success,
                                                       recovered=True), True)
            except Exception:
                sent = False
            if sent:
                db.mark_watchdog_notice_sent(conn, key, recovered=True, now=now)


def backup_incidents(status: dict, *, now: datetime) -> dict[str, str]:
    incidents = {}
    last_success = status.get("last_success_at")
    if status.get("last_status") == "failed":
        incidents["backup_failed"] = "Последнее резервное копирование завершилось ошибкой. Проверьте защищённый статус и настройки задачи."
    if last_success is None or now - last_success > BACKUP_STALE_AFTER:
        incidents["backup_stale"] = "Нет успешной резервной копии за последние 36 часов."
    if status.get("last_restore_status") == "failed":
        incidents["backup_restore_failed"] = "Последняя проверка восстановления резервной копии завершилась ошибкой."
    checked_at = status.get("last_restore_checked_at")
    reference = checked_at or status.get("first_success_at")
    if reference and now - reference > BACKUP_RESTORE_STALE_AFTER:
        incidents["backup_restore_stale"] = "Проверка восстановления не выполнялась более 35 дней."
    return incidents


def _default_sender(settings: Settings):
    def send(key: str, text: str, recovered: bool = False) -> bool:
        return send_telegram_alert(settings.telegram_token, settings.telegram_chat_id, text)
    return send


def _handle_database_outage(settings: Settings, send_notice, now: datetime) -> None:
    state = _read_fallback(settings.state_path)
    if not state.get("database_unavailable"):
        state["database_unavailable"] = True
        state["database_alert_sent"] = False
    if not state.get("database_alert_sent"):
        try:
            sent = send_notice("database_unavailable",
                               _format_notice("database_unavailable",
                                              "Не удаётся подключиться к базе мониторинга. Перезапуск приложения не выполнялся.",
                                              _remembered_last_success(state)), False)
        except Exception:
            sent = False
        if sent:
            state["database_alert_sent"] = True
    _write_fallback(settings.state_path, state)
    _write_heartbeat(settings.heartbeat_path, now)


def _recover_database_notice(settings: Settings, send_notice, now: datetime) -> None:
    state = _read_fallback(settings.state_path)
    if not state.get("database_unavailable"):
        return
    if state.get("database_alert_sent"):
        try:
            sent = send_notice("database_unavailable",
                               _format_notice("database_unavailable",
                                              "Подключение к базе мониторинга восстановлено.",
                                              _remembered_last_success(state), recovered=True), True)
        except Exception:
            sent = False
        if not sent:
            return
    state["database_unavailable"] = False
    state["database_alert_sent"] = False
    _write_fallback(settings.state_path, state)


def check_once(settings: Settings, *, now: datetime | None = None,
               app_probe=probe_app_health, restart=restart_coolify,
               send_notice=None, connect=None) -> dict[str, bool]:
    current = now or datetime.now(timezone.utc)
    sender = send_notice or _default_sender(settings)
    app_available = bool(app_probe(settings.radar_health_url))
    connector = connect or (lambda: psycopg.connect(settings.database_url, autocommit=True))
    conn = None
    try:
        conn = connector()
        with conn:
            conn.execute("SELECT 1")
            started_at = db.watchdog_started_at(conn, now=current)
            cycles = db.recent_cycles(conn, limit=5)
            stuck_cycles = db.stale_started_cycles(conn, current - CYCLE_STALE_AFTER)
            runtime = db.watchdog_runtime_state(conn, now=current)
            fallback = _read_fallback(settings.state_path)
            if fallback.pop("app_recovered_during_database_outage", False):
                runtime = {**runtime, "failed_checks": 0, "restart_attempted": False}
                db.update_watchdog_runtime(conn, failed_checks=0, restart_attempted=False)
            last_success = db.latest_successful_cycle(conn)
            desired = evaluate(cycles, database_available=True, app_available=True,
                               now=current, monitor_started_at=started_at,
                               latest_success_at=last_success,
                               stuck_cycles=stuck_cycles)
            if db.has_unresolved_notification_failure(conn):
                desired["telegram_delivery"] = "Обнаружен цикл с неподтверждённой доставкой уведомления в Telegram."
            desired.update(backup_incidents(db.latest_backup_status(conn), now=current))
            failures = 0 if app_available else runtime["failed_checks"] + 1
            attempted = False if app_available else runtime["restart_attempted"]
            if not app_available and restart_due(app_available=False, database_available=True,
                                                 failed_checks=failures,
                                                 restart_attempted=attempted):
                attempted = True
                db.update_watchdog_runtime(conn, failed_checks=failures, restart_attempted=True)
                try:
                    restart(settings.coolify_api_url, settings.coolify_api_token,
                            settings.coolify_application_uuid)
                    desired["application_unavailable"] = (
                        "Веб-приложение не отвечает после трёх проверок; запрос на один перезапуск отправлен.")
                except Exception:
                    desired["application_unavailable"] = (
                        "Веб-приложение не отвечает после трёх проверок; запрос на перезапуск не выполнен.")
                    desired["coolify_restart_failed"] = "Coolify не подтвердил запрос на перезапуск приложения."
            elif not app_available and failures >= 3:
                desired["application_unavailable"] = (
                    "Веб-приложение не восстановилось после единственной попытки перезапуска.")
                desired["coolify_restart_failed"] = "Приложение остаётся недоступным после запроса на перезапуск."
            db.update_watchdog_runtime(conn, failed_checks=failures,
                                       restart_attempted=attempted)
            sync_incidents(conn, desired, sender, last_success=last_success, now=current)
        _remember_last_success(settings.state_path, last_success)
        _write_fallback(settings.state_path, {**_read_fallback(settings.state_path),
                                             "app_recovered_during_database_outage": False})
        _recover_database_notice(settings, sender, current)
        _write_heartbeat(settings.heartbeat_path, current)
        return {"database_available": True, "app_available": app_available}
    except Exception:
        try:
            _remember_app_probe(settings.state_path, app_available)
            _handle_database_outage(settings, sender, current)
        except Exception:
            pass
        return {"database_available": False, "app_available": app_available}
    finally:
        if conn is not None:
            conn.close()


def run(settings: Settings) -> None:
    while True:
        try:
            check_once(settings)
        except Exception:
            print("Watchdog check failed; it will retry on the next interval", flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)
