"""Command entry point for the local manual-review pilot."""

from __future__ import annotations

import json
import hashlib
import os
import sys
from datetime import date, datetime
from pathlib import Path

import db
from collector.main import main as collect
from notifier import telegram
from web.main import main as web
from worker.main import main as process, probe, translate
from watchdog.service import Settings as WatchdogSettings, container_healthcheck, run as run_watchdog
from judge.main import import_labels, prepare_review as prepare_judge_sample, report as relevance_report

WORK = Path(__file__).with_name("work")


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Не поддерживается: {type(value).__name__}")


def watchdog() -> None:
    run_watchdog(WatchdogSettings.from_env())


def check_container_health() -> None:
    if not container_healthcheck():
        sys.exit("Проверка контейнера не пройдена")

def prepare_review() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        batch = db.pending_materials(conn)
    WORK.mkdir(exist_ok=True)
    target = WORK / "pending-review.json"
    target.write_text(json.dumps({"materials": batch}, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(f"Подготовлено {len(batch)} материалов: {target}")


def prepare_judge_review(limit: int = 100) -> Path:
    with db.connect() as conn:
        db.init_schema(conn)
        payload = prepare_judge_sample(conn, limit)
    WORK.mkdir(exist_ok=True)
    target = WORK / "judge-review.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(f"Подготовлено для слепой разметки {len(payload['materials'])} материалов: {target}")
    return target


def import_judge_labels(path: str) -> None:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    with db.connect() as conn:
        db.init_schema(conn)
        count = import_labels(conn, payload)
    print(f"Сохранено ручных меток: {count}")


def show_judge_report() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        result = relevance_report(conn)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def import_review(path: str) -> None:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    signals = payload.get("signals")
    if not isinstance(signals, list):
        raise ValueError("Ожидается объект JSON с массивом signals")
    skipped = payload.get("skipped_material_ids", [])
    if not isinstance(skipped, list):
        raise ValueError("skipped_material_ids должен быть массивом")
    referenced_ids = set(skipped)
    for signal in signals:
        if isinstance(signal, dict) and isinstance(signal.get("material_ids"), list):
            referenced_ids.update(signal["material_ids"])
    with db.connect() as conn:
        db.init_schema(conn)
        pending = db.pending_materials(conn, limit=max(50, len(referenced_ids)))
        translation_batch = [item for item in pending
                             if item["material_id"] in referenced_ids and item.get("translation_status") == "pending"]
        translations = translate(translation_batch)
        created, linked, skipped_count = db.import_signals(conn, signals, skipped, translations=translations)
    print(f"Импортировано событий: {created}; связано материалов: {linked}; пропущено: {skipped_count}")


def show_health() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        result = db.health(conn)
    result["openrouter"] = probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def prepare_notifications():
    try:
        config = telegram.settings()
        if config is not None:
            with db.connect() as conn:
                db.init_schema(conn)
                telegram.initialize(conn)
        return config
    except Exception:
        raise RuntimeError('Telegram initialization failed; collection postponed') from None


def send_notifications(config=None, *, cycle_id=None):
    if config is None:
        return ("skipped", None)
    try:
        with db.connect() as conn:
            status, delivered_through = telegram.notify_new_materials_status(conn, config, cycle_id=cycle_id)
        return ({"sent": "sent", "empty": "skipped", "busy": "failed"}.get(status, "failed"),
                delivered_through)
    except Exception:
        print("Telegram notification failed; it will be retried")
        return ("failed", None)


def _record_cycle_stage(cycle_id: int, stage: str, status: str, **details) -> None:
    with db.connect() as conn:
        db.update_cycle_stage(conn, cycle_id, stage, status, **details)


def _finish_cycle(cycle_id: int, status: str, *, error_stage: str | None = None,
                  error: str | None = None) -> None:
    with db.connect() as conn:
        db.finish_cycle(conn, cycle_id, status, error_stage=error_stage, error=error)


def _safe_stage_error(stage: str, error: Exception) -> str:
    signature = getattr(error, "failure_signature", None)
    if not signature:
        signature = hashlib.sha256(str(error).encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"Сбой этапа {stage}: {type(error).__name__} [{signature}]"[:300]


def cycle() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        cycle_id = db.start_cycle(conn)
    failed_stage = None
    safe_error = None
    try:
        try:
            config = prepare_notifications()
        except Exception as error:
            failed_stage = "notifications"
            safe_error = _safe_stage_error(failed_stage, error)
            _record_cycle_stage(cycle_id, failed_stage, "failed", error=safe_error)
            raise

        try:
            collection = collect() or {}
            _record_cycle_stage(cycle_id, "collection", "success",
                                items=collection.get("items", 0),
                                source_failures=collection.get("source_failures", []),
                                source_failure_signatures=collection.get("source_failure_signatures", {}))
        except Exception as error:
            failed_stage = "collection"
            safe_error = _safe_stage_error(failed_stage, error)
            _record_cycle_stage(cycle_id, failed_stage, "failed", error=safe_error,
                                source_failures=getattr(error, "source_failures", []),
                                source_failure_signatures=getattr(error, "source_failure_signatures", {}))
            raise

        try:
            if config is not None:
                with db.connect() as conn:
                    db.set_notification_target(conn, cycle_id)
            notification_result = send_notifications(config, cycle_id=cycle_id)
            notification_status, delivered_ids = (notification_result if isinstance(notification_result, tuple)
                                                   else (notification_result, None))
        except Exception as error:
            notification_status, delivered_ids = "failed", None
            failed_stage = "notifications"
            safe_error = _safe_stage_error(failed_stage, error)
        else:
            safe_error = None
        if notification_status not in {"sent", "skipped"}:
            failed_stage = "notifications"
            safe_error = safe_error or "Сбой этапа notifications"
            _record_cycle_stage(cycle_id, "notifications", "failed", error=safe_error)
        else:
            _record_cycle_stage(cycle_id, "notifications", notification_status,
                                notification_delivered_material_ids=delivered_ids)

        try:
            processing = process() or {}
            _record_cycle_stage(cycle_id, "processing", "success",
                                items=processing.get("items", 0))
        except Exception as error:
            failed_stage = "processing"
            safe_error = _safe_stage_error(failed_stage, error)
            _record_cycle_stage(cycle_id, "processing", "failed", error=safe_error)
            raise

        _finish_cycle(cycle_id, "failed" if failed_stage else "success",
                      error_stage=failed_stage, error=safe_error)
    except Exception:
        _finish_cycle(cycle_id, "failed", error_stage=failed_stage, error=safe_error)
        raise


COMMANDS = {
    "cycle": cycle,
    "collect": collect,
    "process": process,
    "prepare-review": prepare_review,
    "prepare-judge-review": prepare_judge_review,
    "import-judge-labels": import_judge_labels,
    "judge-report": show_judge_report,
    "web": web,
    "health": show_health,
    "telegram-set-menu": telegram.set_menu,
    "watchdog": watchdog,
    "container-healthcheck": check_container_health,
}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Использование: python main.py cycle | collect | process | prepare-review | import-review <файл> | prepare-judge-review [limit] | import-judge-labels <файл> | judge-report | web | health | watchdog | container-healthcheck")
    command = sys.argv[1]
    if command == "import-review":
        if len(sys.argv) != 3:
            sys.exit("Использование: python main.py import-review work\\reviewed-signals.json")
        import_review(sys.argv[2])
    elif command == "import-judge-labels":
        if len(sys.argv) != 3:
            sys.exit("Использование: python main.py import-judge-labels work\\judge-review.json")
        import_judge_labels(sys.argv[2])
    elif command == "prepare-judge-review":
        if len(sys.argv) > 3:
            sys.exit("Использование: python main.py prepare-judge-review [limit]")
        try:
            limit = int(sys.argv[2]) if len(sys.argv) == 3 else 100
        except ValueError:
            sys.exit("limit должен быть целым числом от 1 до 1000")
        prepare_judge_review(limit)
    elif command in COMMANDS:
        COMMANDS[command]()
    else:
        sys.exit(f"Неизвестная команда: {command}")

