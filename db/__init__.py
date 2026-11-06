"""PostgreSQL storage for the local manual-review pilot."""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
from datetime import datetime
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
            """INSERT INTO sources (code, name, kind, url, trust, scope, category, active)
               VALUES (%(code)s, %(name)s, %(kind)s, %(url)s, %(trust)s, %(scope)s, %(category)s, true)
               ON CONFLICT (code) DO UPDATE SET
                 name = EXCLUDED.name, kind = EXCLUDED.kind, url = EXCLUDED.url,
                 trust = EXCLUDED.trust, scope = EXCLUDED.scope, category = EXCLUDED.category""",
            source,
        )
    return len(sources)


def _decode_feed_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 256:
            raise ValueError
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 2 or type(value[1]) is not int or value[1] < 1:
            raise ValueError
        timestamp = datetime.fromisoformat(value[0])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        return timestamp, value[1]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, OverflowError):
        raise ValueError('invalid cursor') from None


def _encode_feed_cursor(timestamp: datetime, row_id: int) -> str:
    raw = json.dumps([timestamp.isoformat(), row_id], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def feed_materials(conn, period: str = 'all', platform: str = 'all', cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
    if period not in ('all', 'today', 'week') or platform not in ('all', 'ozon', 'wildberries', 'yandex', 'chestny_znak', 'industry'):
        raise ValueError('invalid feed filter')
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid page size')
    limit = min(limit, 20)
    position = _decode_feed_cursor(cursor) if cursor is not None else None
    conditions = []
    params: list[Any] = []
    if period != 'all':
        start_offset = " - INTERVAL '6 days'" if period == 'week' else ''
        moscow_midnight = "date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
        conditions.append("COALESCE(m.published_at, m.collected_at) >= (" + moscow_midnight + start_offset + ") AT TIME ZONE 'Europe/Moscow'")
        conditions.append("COALESCE(m.published_at, m.collected_at) < (" + moscow_midnight + " + INTERVAL '1 day') AT TIME ZONE 'Europe/Moscow'")
    if platform != 'all':
        conditions.append('s.category = %s')
        params.append(platform)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    base = ' FROM materials m JOIN sources s ON s.id = m.source_id'
    total = conn.execute('SELECT count(*)' + base + where, tuple(params)).fetchone()[0]
    if position is not None:
        conditions.append('(COALESCE(m.published_at, m.collected_at), m.id) < (%s, %s)')
        params.extend(position)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = conn.execute(
        'SELECT m.id, m.title, m.text, m.url, m.published_at, m.collected_at, s.name, s.category, '
        'COALESCE(m.published_at, m.collected_at)' + base + where +
        ' ORDER BY COALESCE(m.published_at, m.collected_at) DESC, m.id DESC LIMIT %s',
        tuple(params + [limit + 1]),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    names = ('id', 'title', 'text', 'url', 'published_at', 'collected_at', 'source', 'category', 'sort_date')
    cards = [dict(zip(names, row)) for row in rows]
    for card in cards:
        card.pop('sort_date')
        for key in ('published_at', 'collected_at'):
            if card[key] is not None:
                card[key] = card[key].isoformat()
    next_cursor = _encode_feed_cursor(rows[-1][-1], rows[-1][0]) if has_more else None
    return {'materials': cards, 'total': total, 'next_cursor': next_cursor}


def feed_sources(conn) -> list[dict[str, Any]]:
    rows = conn.execute('SELECT code, name, category, active, last_checked_at, last_error FROM sources ORDER BY name, id').fetchall()
    return [{'code': code, 'name': name, 'category': category,
             'status': 'inactive' if not active else 'error' if error else 'ok' if checked else 'not_checked',
             'last_checked_at': checked.isoformat() if checked else None}
            for code, name, category, active, checked, error in rows]


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


def import_signals(conn, signals: Iterable[dict[str, Any]], skipped_material_ids: Iterable[int] = (), *, reject_confirmed: bool = False) -> tuple[int, int, int]:
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
        conflict_guard = " WHERE events.verification <> 'confirmed'" if reject_confirmed else ""
        event = conn.execute(
            """INSERT INTO events
               (event_key, platform, topic, severity, summary, recommended_action, verification, effective_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (event_key) DO UPDATE SET updated_at = now()""" + conflict_guard + """
               RETURNING id, (xmax = 0)""",
            (key, signal["platform"], signal["topic"], signal["severity"], signal["summary"], signal["recommended_action"], signal["verification"], signal.get("effective_date")),
        ).fetchone()
        if event is None:
            raise ValueError("model event collides with confirmed event")
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

