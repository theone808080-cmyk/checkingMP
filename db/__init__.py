"""PostgreSQL storage for the local manual-review pilot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

import psycopg

SCHEMA = Path(__file__).with_name("schema.sql")
SOURCE_CONFIG = Path(__file__).parents[1] / "config" / "sources.json"
SCHEMA_LOCK = 21429


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def init_schema(conn) -> None:
    conn.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK,))
    try:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK,))


def sync_sources(conn) -> int:
    sources = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    for source in sources:
        conn.execute(
            """INSERT INTO sources (code, name, kind, url, trust, scope, active)
               VALUES (%(code)s, %(name)s, %(kind)s, %(url)s, %(trust)s, %(scope)s, true)
               ON CONFLICT (code) DO UPDATE SET
                 name = EXCLUDED.name, kind = EXCLUDED.kind, url = EXCLUDED.url,
                 trust = EXCLUDED.trust, scope = EXCLUDED.scope""",
            source,
        )
    return len(sources)


def start_run(conn, service: str) -> int:
    return conn.execute(
        "INSERT INTO runs (service, status) VALUES (%s, 'started') RETURNING id", (service,)
    ).fetchone()[0]


def finish_run(conn, run_id: int, status: str, items: int, note: str) -> None:
    conn.execute(
        """UPDATE runs SET status = %s, items = %s, note = %s, finished_at = now()
           WHERE id = %s""",
        (status, items, note[:1000], run_id),
    )


def add_material(conn, source_id: int, run_id: int, item: dict[str, Any]) -> bool:
    body = (item.get("text") or "").strip()
    fingerprint = hashlib.sha256(
        (item["url"].strip() + "\n" + item["title"].strip() + "\n" + body).encode("utf-8")
    ).hexdigest()
    row = conn.execute(
        """INSERT INTO materials
           (source_id, run_id, url, title, text, published_at, fingerprint)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (fingerprint) DO NOTHING RETURNING id""",
        (source_id, run_id, item["url"], item["title"], body, item.get("published_at"), fingerprint),
    ).fetchone()
    return row is not None


def pending_materials(conn, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.id, m.url, m.title, m.text, m.published_at,
                  s.code, s.name, s.trust, s.scope
           FROM materials m JOIN sources s ON s.id = m.source_id
           WHERE m.status = 'new'
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC LIMIT %s""",
        (limit,),
    ).fetchall()
    columns = ("material_id", "url", "title", "text", "published_at", "source_code", "source", "trust", "scope")
    return [dict(zip(columns, row)) for row in rows]


def import_signals(conn, signals: Iterable[dict[str, Any]], skipped_material_ids: Iterable[int] = ()) -> tuple[int, int, int]:
    created = linked = skipped = 0
    for signal in signals:
        required = {"material_ids", "platform", "topic", "severity", "summary", "recommended_action", "verification"}
        missing = required.difference(signal)
        if missing:
            raise ValueError(f"В сигнале отсутствуют поля: {', '.join(sorted(missing))}")
        ids = signal["material_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(value, int) for value in ids):
            raise ValueError("material_ids должен быть непустым списком идентификаторов")
        key = signal.get("event_key") or "|".join(
            [signal["platform"].strip().lower(), signal["topic"].strip().lower(), signal["summary"].strip().lower()]
        )
        event = conn.execute(
            """INSERT INTO events
               (event_key, platform, topic, severity, summary, recommended_action, verification, effective_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (event_key) DO UPDATE SET updated_at = now()
               RETURNING id, (xmax = 0)""",
            (key, signal["platform"], signal["topic"], signal["severity"], signal["summary"], signal["recommended_action"], signal["verification"], signal.get("effective_date")),
        ).fetchone()
        event_id, was_created = event
        created += int(was_created)
        for material_id in ids:
            row = conn.execute(
                """INSERT INTO event_materials (event_id, material_id)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING material_id""",
                (event_id, material_id),
            ).fetchone()
            linked += row is not None
            conn.execute("UPDATE materials SET status = 'done' WHERE id = %s", (material_id,))
    for material_id in skipped_material_ids:
        if not isinstance(material_id, int):
            raise ValueError("skipped_material_ids должен содержать только идентификаторы")
        row = conn.execute(
            "UPDATE materials SET status = 'skipped' WHERE id = %s AND status = 'new' RETURNING id",
            (material_id,),
        ).fetchone()
        skipped += row is not None
    return created, linked, skipped


def health(conn) -> dict[str, int]:
    sources, materials, events, pending = conn.execute(
        """SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM materials),
                  (SELECT count(*) FROM events), (SELECT count(*) FROM materials WHERE status = 'new')"""
    ).fetchone()
    return {"sources": sources, "materials": materials, "events": events, "pending": pending}

