"""Command entry point for the local manual-review pilot."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import db
from collector.main import main as collect
from notifier import telegram
from web.main import main as web
from worker.main import main as process, probe

WORK = Path(__file__).with_name("work")


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Не поддерживается: {type(value).__name__}")


def prepare_review() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        batch = db.pending_materials(conn)
    WORK.mkdir(exist_ok=True)
    target = WORK / "pending-review.json"
    target.write_text(json.dumps({"materials": batch}, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(f"Подготовлено {len(batch)} материалов: {target}")


def import_review(path: str) -> None:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    signals = payload.get("signals")
    if not isinstance(signals, list):
        raise ValueError("Ожидается объект JSON с массивом signals")
    skipped = payload.get("skipped_material_ids", [])
    if not isinstance(skipped, list):
        raise ValueError("skipped_material_ids должен быть массивом")
    with db.connect() as conn:
        db.init_schema(conn)
        created, linked, skipped_count = db.import_signals(conn, signals, skipped)
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
        print('Telegram notifications are unavailable')
        return None


def send_notifications(config=None):
    if config is None:
        return
    try:
        with db.connect() as conn:
            telegram.notify_new_materials(conn, config)
    except Exception:
        print('Telegram notification failed; it will be retried')


def cycle() -> None:
    config = prepare_notifications()
    collect()
    send_notifications(config)
    process()


COMMANDS = {"cycle": cycle, "collect": collect, "process": process, "prepare-review": prepare_review, "web": web, "health": show_health, "telegram-set-menu": telegram.set_menu}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Использование: python main.py cycle | collect | process | prepare-review | import-review <файл> | web | health")
    command = sys.argv[1]
    if command == "import-review":
        if len(sys.argv) != 3:
            sys.exit("Использование: python main.py import-review work\\reviewed-signals.json")
        import_review(sys.argv[2])
    elif command in COMMANDS:
        COMMANDS[command]()
    else:
        sys.exit(f"Неизвестная команда: {command}")

