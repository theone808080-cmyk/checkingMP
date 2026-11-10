"""PostgreSQL storage for the local manual-review pilot."""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import psycopg

from judge.constants import CLASSIFICATIONS

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


def _decode_feed_cursor(cursor: str) -> tuple[datetime, int, uuid.UUID]:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 256:
            raise ValueError
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        value = json.loads(raw)
        if (not isinstance(value, list) or len(value) != 3 or
                type(value[1]) is not int or not 1 <= value[1] <= 2147483647 or
                not isinstance(value[2], str)):
            raise ValueError
        timestamp = datetime.fromisoformat(value[0])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        return timestamp, value[1], uuid.UUID(value[2])
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, OverflowError, AttributeError):
        raise ValueError('invalid cursor') from None


def _encode_feed_cursor(timestamp: datetime, row_id: int, snapshot_id: uuid.UUID) -> str:
    raw = json.dumps([timestamp.isoformat(), row_id, str(snapshot_id)], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


_FINDING_PLATFORMS = {
    'ozon': ('ozon',),
    'wildberries': ('wildberries', 'wb'),
    'yandex': ('yandex', 'yandex market', 'яндекс', 'яндекс маркет'),
    'chestny_znak': ('chestny_znak', 'честный знак', 'crpt'),
    'industry': ('industry', 'отрасль'),
}
_FINDING_PLATFORM_PATTERNS = {
    'ozon': r'(^|[^[:alnum:]])ozon([^[:alnum:]]|$)',
    'wildberries': r'(^|[^[:alnum:]])(wildberries|wb|вб)([^[:alnum:]]|$)',
    'yandex': r'(^|[^[:alnum:]])(yandex|яндекс)([^[:alnum:]]|$)',
    'chestny_znak': r'(^|[^[:alnum:]])(chestny[[:space:]_-]*znak|честный[[:space:]_-]*знак|crpt)([^[:alnum:]]|$)',
    'industry': r'(^|[^[:alnum:]])(industry|отрасль)([^[:alnum:]]|$)',
}
_PLATFORM_TOKEN_ALIASES = {
    'Ozon': (('ozon',),),
    'Wildberries': (('wildberries',), ('wb',), ('вб',)),
    'Яндекс Маркет': (('yandex',), ('яндекс',)),
    'Честный знак': (('chestny_znak',), ('chestny', 'znak'), ('честный', 'знак'), ('crpt',)),
}
_PLATFORM_SPECIFIC_PATTERN = r'^(ozon|wildberries|wb|вб|yandex([[:space:]._-]*market)?|яндекс([[:space:]._-]*маркет)?|chestny[[:space:]_-]*znak|честный[[:space:]_-]*знак|crpt)$'
_PLATFORM_CROSS_PATTERN = r'(межплощадочная)|(ozon.*(wildberries|wb|вб|yandex|яндекс|chestny|честный|crpt))|(wildberries.*(ozon|yandex|яндекс|chestny|честный|crpt))|(wb.*(ozon|yandex|яндекс|crpt))|(яндекс.*(ozon|wildberries|wb|crpt))'


def normalize_event_platform(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('invalid event platform')
    tokens = tuple(re.findall(r'\w+', value.casefold(), flags=re.UNICODE))
    phrases = (('all',), ('cross_platform',), ('cross', 'platform'), ('cross', 'marketplace'),
               ('все', 'маркетплейсы'), ('все', 'площадки'))
    if any(any(tokens[i:i + len(phrase)] == phrase for i in range(len(tokens) - len(phrase) + 1)) for phrase in phrases):
        return 'Межплощадочная'
    if 'industry' in tokens or 'отрасль' in tokens:
        return 'Отрасль'
    matches = []
    for canonical, aliases in _PLATFORM_TOKEN_ALIASES.items():
        if any(any(tokens[i:i + len(alias)] == alias for i in range(len(tokens) - len(alias) + 1)) for alias in aliases):
            matches.append(canonical)
    cross_marker = any(value in tokens for value in ('межплощадочная', 'межплощадочный', 'межплощадочные'))
    if len(matches) > 1 or (cross_marker and matches):
        return 'Межплощадочная: ' + ', '.join(matches)
    if len(matches) == 1:
        return matches[0]
    if cross_marker:
        return 'Межплощадочная'
    raise ValueError('invalid event platform')


def _finding_filter_hash(period: str, platform: str, importance: int | None,
                         topic: str | None, verification: str | None, trust: str | None) -> str:
    encoded = json.dumps([period, platform, importance, topic, verification, trust], ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _decode_findings_cursor(cursor: str, filter_hash: str) -> tuple[uuid.UUID, int]:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
            raise ValueError
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        value = json.loads(raw)
        if (not isinstance(value, list) or len(value) != 3 or type(value[1]) is not int or value[1] < 1 or
                value[2] != filter_hash or not isinstance(value[2], str)):
            raise ValueError
        return uuid.UUID(value[0]), value[1]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, OverflowError, AttributeError, IndexError):
        raise ValueError('invalid cursor') from None


def _encode_findings_cursor(snapshot_id: uuid.UUID, offset: int, filter_hash: str) -> str:
    raw = json.dumps([str(snapshot_id), offset, filter_hash], separators=(',', ':')).encode()
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
    where = ' AND '.join(conditions) if conditions else 'true'
    snapshot_id = uuid.uuid4() if position is None else position[2]
    if position is None:
        snapshot = ("expired AS (DELETE FROM feed_snapshots WHERE created_at < now() - INTERVAL '30 minutes'), "
                    "snapshot AS (INSERT INTO feed_snapshots (id, period, platform, material_ids) "
                    "SELECT %s, %s, %s, COALESCE(array_agg(m.id), ARRAY[]::integer[]) "
                    "FROM materials m JOIN sources s ON s.id = m.source_id WHERE " + where +
                    " RETURNING id, material_ids)")
        query_params = [snapshot_id, period, platform] + params
    else:
        snapshot = ("snapshot AS (SELECT id, material_ids FROM feed_snapshots "
                    "WHERE id = %s AND period = %s AND platform = %s "
                    "AND created_at >= now() - INTERVAL '30 minutes')")
        query_params = [snapshot_id, period, platform]
    page_where = '' if position is None else ' WHERE (sort_date, id) < (%s, %s)'
    if position is not None:
        query_params.extend(position[:2])
    results = conn.execute(
        'WITH ' + snapshot + ', filtered AS MATERIALIZED ('
        'SELECT m.id, m.title, m.text, m.url, m.published_at, m.collected_at, '
        'm.translation_status, m.title_ru, m.text_ru, '
        's.name AS source, s.category, COALESCE(m.published_at, m.collected_at) AS sort_date '
        'FROM materials m JOIN sources s ON s.id = m.source_id CROSS JOIN snapshot WHERE m.id = ANY(snapshot.material_ids)) '
        'SELECT snapshot.id, cardinality(snapshot.material_ids), page.* FROM snapshot '
        'LEFT JOIN LATERAL (SELECT * FROM filtered' + page_where +
        ' ORDER BY sort_date DESC, id DESC LIMIT %s) page ON true',
        tuple(query_params + [limit + 1]),
    ).fetchall()
    if not results:
        raise ValueError('expired or invalid cursor')
    snapshot_id, total = results[0][:2]
    rows = [row[2:] for row in results if row[2] is not None]
    has_more = len(rows) > limit
    rows = rows[:limit]
    names = ('id', 'title', 'text', 'url', 'published_at', 'collected_at', 'translation_status',
             'title_ru', 'text_ru', 'source', 'category', 'sort_date')
    cards = [dict(zip(names, row)) for row in rows]
    for card in cards:
        card.pop('sort_date')
        for key in ('published_at', 'collected_at'):
            if card[key] is not None:
                card[key] = card[key].isoformat()
    next_cursor = _encode_feed_cursor(rows[-1][-1], rows[-1][0], snapshot_id) if has_more else None
    return {'materials': cards, 'total': total, 'next_cursor': next_cursor}


def feed_findings(conn, period: str = 'all', platform: str = 'all', importance: int | None = None,
                  topic: str | None = None, verification: str | None = None, trust: str | None = None,
                  cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
    if period not in ('all', 'today', 'week') or platform not in ('all', *_FINDING_PLATFORMS):
        raise ValueError('invalid finding filter')
    if importance is not None and (type(importance) is not int or not 1 <= importance <= 5):
        raise ValueError('invalid importance')
    if topic is not None and (not isinstance(topic, str) or not topic.strip() or len(topic) > 120):
        raise ValueError('invalid topic')
    if verification is not None and verification not in ('confirmed', 'needs_check'):
        raise ValueError('invalid verification')
    if trust is not None and trust not in ('official', 'regulator', 'industry'):
        raise ValueError('invalid source trust')
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid page size')
    limit = min(limit, 20)
    filter_hash = _finding_filter_hash(period, platform, importance, topic, verification, trust)
    position = _decode_findings_cursor(cursor, filter_hash) if cursor is not None else None

    conditions: list[str] = []
    params: list[Any] = []
    if period != 'all':
        start_offset = " - INTERVAL '6 days'" if period == 'week' else ''
        moscow_midnight = "date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
        conditions.append(
            "EXISTS (SELECT 1 FROM event_materials pem JOIN materials pm ON pm.id = pem.material_id "
            "WHERE pem.event_id = e.id AND COALESCE(pm.published_at, pm.collected_at) >= (" +
            moscow_midnight + start_offset + ") AT TIME ZONE 'Europe/Moscow' AND " +
            "COALESCE(pm.published_at, pm.collected_at) < (" + moscow_midnight +
            " + INTERVAL '1 day') AT TIME ZONE 'Europe/Moscow')"
        )
    if platform != 'all':
        aliases = _FINDING_PLATFORMS[platform]
        conditions.append('(lower(e.platform) = ANY(%s) OR lower(e.platform) ~ %s)')
        params.extend((list(aliases), _FINDING_PLATFORM_PATTERNS[platform]))
    if importance is not None:
        conditions.append('e.importance = %s')
        params.append(importance)
    if topic is not None:
        conditions.append('strpos(lower(e.topic), lower(%s)) > 0')
        params.append(topic.strip())
    if verification is not None:
        conditions.append('e.verification = %s')
        params.append(verification)
    if trust is not None:
        conditions.append(
            'EXISTS (SELECT 1 FROM event_materials tem JOIN materials tm ON tm.id = tem.material_id '
            'JOIN sources ts ON ts.id = tm.source_id WHERE tem.event_id = e.id AND ts.trust = %s)'
        )
        params.append(trust)
    where = ' AND '.join(conditions) if conditions else 'true'
    filtered = (
        "filtered AS MATERIALIZED (SELECT e.id, e.platform, e.topic, e.importance, e.summary, "
        "e.recommended_action, e.verification, e.effective_date, e.updated_at, "
        "CASE WHEN lower(e.platform) ~ %s THEN 1 "
        "WHEN lower(e.platform) = ANY(%s) OR lower(e.platform) ~ %s OR bool_or(s.trust = 'regulator') THEN 2 ELSE 3 END AS specificity, "
        "json_agg(json_build_object('name', s.name, 'trust', s.trust, 'category', s.category, 'url', m.url, "
        "'published_at', m.published_at, 'collected_at', m.collected_at) "
        "ORDER BY COALESCE(m.published_at, m.collected_at) DESC, m.id DESC) AS sources "
        "FROM events e JOIN event_materials em ON em.event_id = e.id "
        "JOIN materials m ON m.id = em.material_id JOIN sources s ON s.id = m.source_id "
        "WHERE " + where + " GROUP BY e.id) "
    )
    general_platforms = ['all', 'cross_platform', 'межплощадочная', 'все маркетплейсы']
    base_params = [_PLATFORM_SPECIFIC_PATTERN, general_platforms, _PLATFORM_CROSS_PATTERN] + params
    if position is None:
        snapshot_id = uuid.uuid4()
        event_ids = conn.execute(
            "WITH expired AS (DELETE FROM finding_snapshots WHERE created_at < now() - INTERVAL '30 minutes'), " +
            filtered +
            ", snapshot AS (INSERT INTO finding_snapshots (id, filters_hash, event_ids) "
            "SELECT %s, %s, COALESCE(array_agg(id ORDER BY specificity ASC, importance DESC, updated_at DESC, id DESC), "
            "ARRAY[]::integer[]) FROM filtered RETURNING event_ids) SELECT event_ids FROM snapshot",
            tuple(base_params + [snapshot_id, filter_hash]),
        ).fetchone()[0]
        offset = 0
    else:
        snapshot_id, offset = position
        snapshot = conn.execute(
            "SELECT event_ids FROM finding_snapshots WHERE id = %s AND filters_hash = %s "
            "AND created_at >= now() - INTERVAL '30 minutes'",
            (snapshot_id, filter_hash),
        ).fetchone()
        if snapshot is None:
            raise ValueError('expired or invalid cursor')
        event_ids = snapshot[0]
    total = len(event_ids)
    if position is not None and offset >= total:
        raise ValueError('invalid cursor offset')
    page_ids = event_ids[offset:offset + limit + 1]
    has_more = len(page_ids) > limit
    page_ids = page_ids[:limit]
    rows = conn.execute(
        """SELECT e.id, e.platform, e.topic, e.importance, e.summary, e.recommended_action,
                  e.verification, e.effective_date, e.updated_at,
                  CASE WHEN lower(e.platform) ~ %s THEN 1
                       WHEN lower(e.platform) = ANY(%s) OR lower(e.platform) ~ %s OR bool_or(s.trust = 'regulator') THEN 2 ELSE 3 END AS specificity,
                  json_agg(json_build_object('name', s.name, 'trust', s.trust, 'category', s.category, 'url', m.url,
                    'published_at', m.published_at, 'collected_at', m.collected_at)
                    ORDER BY COALESCE(m.published_at, m.collected_at) DESC, m.id DESC) AS sources
           FROM unnest(%s::integer[]) WITH ORDINALITY AS page(event_id, ordinal)
           JOIN events e ON e.id = page.event_id
           JOIN event_materials em ON em.event_id = e.id
           JOIN materials m ON m.id = em.material_id
           JOIN sources s ON s.id = m.source_id
           GROUP BY e.id, page.ordinal
           ORDER BY page.ordinal""",
        (_PLATFORM_SPECIFIC_PATTERN, general_platforms, _PLATFORM_CROSS_PATTERN, page_ids),
    ).fetchall()
    names = ('id', 'platform', 'topic', 'importance', 'summary', 'recommended_action',
             'verification', 'effective_date', 'updated_at', 'specificity', 'sources')
    findings = [dict(zip(names, row)) for row in rows]
    next_cursor = _encode_findings_cursor(snapshot_id, offset + limit, filter_hash) if has_more else None
    return {'findings': findings, 'total': total, 'next_cursor': next_cursor}


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


CYCLE_STAGES = {
    "collection": ("collection_status", "collected_items"),
    "notifications": ("notification_status", None),
    "processing": ("processing_status", "processed_items"),
}


def start_cycle(conn) -> int:
    return conn.execute("INSERT INTO cycles DEFAULT VALUES RETURNING id").fetchone()[0]


def update_cycle_stage(conn, cycle_id: int, stage: str, status: str, *, items: int = 0,
                       source_failures: list[str] | None = None,
                       source_failure_signatures: dict[str, str] | None = None,
                       notification_delivered_material_ids: list[int] | None = None,
                       error: str | None = None) -> None:
    if stage not in CYCLE_STAGES:
        raise ValueError("Unknown cycle stage")
    allowed = {"collection": {"pending", "success", "failed"},
               "notifications": {"pending", "sent", "skipped", "failed"},
               "processing": {"pending", "success", "failed"}}
    if status not in allowed[stage]:
        raise ValueError("Invalid cycle stage status")
    status_column, items_column = CYCLE_STAGES[stage]
    assignments = [f"{status_column} = %s"]
    values: list[Any] = [status]
    if items_column:
        assignments.append(f"{items_column} = %s")
        values.append(max(0, int(items)))
    if stage == "collection" and source_failures is not None:
        assignments.append("source_failures = %s")
        values.append(sorted({str(code)[:120] for code in source_failures}))
    if stage == "collection" and source_failure_signatures is not None:
        assignments.append("source_failure_signatures = %s::jsonb")
        values.append(json.dumps({str(code)[:120]: str(signature)[:64]
                                  for code, signature in source_failure_signatures.items()}))
    if error is not None:
        assignments.extend(("error_stage = %s", "error_message = %s"))
        values.extend((stage, error[:300]))
    if stage == "notifications":
        assignments.append("notification_checked_at = now()")
        if notification_delivered_material_ids is not None:
            assignments.append("notification_delivered_material_ids = %s::jsonb")
            values.append(json.dumps(sorted({max(0, int(item)) for item in notification_delivered_material_ids})))
    values.append(cycle_id)
    conn.execute(f"UPDATE cycles SET {', '.join(assignments)} WHERE id = %s", values)


def finish_cycle(conn, cycle_id: int, status: str, *, error_stage: str | None = None,
                 error: str | None = None) -> None:
    if status not in {"success", "failed"}:
        raise ValueError("Invalid terminal cycle status")
    conn.execute(
        """UPDATE cycles SET status = %s, finished_at = now(),
           error_stage = COALESCE(%s, error_stage),
           error_message = COALESCE(%s, error_message) WHERE id = %s""",
        (status, error_stage, error[:300] if error else None, cycle_id),
    )


def recent_cycles(conn, limit: int = 3) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, status, collection_status, notification_status, processing_status,
                  collected_items, processed_items, source_failures, source_failure_signatures,
                  notification_checked_at,
                  notification_attempted, notification_target_material_ids, notification_delivered_material_ids,
                  error_stage, error_message,
                  started_at, finished_at
           FROM cycles WHERE finished_at IS NOT NULL OR status = 'started'
           ORDER BY COALESCE(finished_at, started_at) DESC, id DESC LIMIT %s""",
        (max(1, min(int(limit), 20)),),
    ).fetchall()
    keys = ("id", "status", "collection_status", "notification_status", "processing_status",
            "collected_items", "processed_items", "source_failures", "source_failure_signatures",
            "notification_checked_at", "notification_attempted", "notification_target_material_ids",
            "notification_delivered_material_ids", "error_stage",
            "error_message", "started_at", "finished_at")
    return [dict(zip(keys, row)) for row in rows]


def latest_successful_cycle(conn) -> datetime | None:
    row = conn.execute("SELECT finished_at FROM cycles WHERE status = 'success' ORDER BY finished_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def set_notification_target(conn, cycle_id: int) -> list[int]:
    row = conn.execute(
        """WITH target AS MATERIALIZED (
             SELECT COALESCE(jsonb_agg(m.id ORDER BY m.id), '[]'::jsonb) AS material_ids
             FROM materials m
             LEFT JOIN notification_materials nm ON nm.material_id = m.id
             LEFT JOIN notification_batches nb ON nb.id = nm.batch_id
             WHERE nm.material_id IS NULL OR (nm.batch_id IS NOT NULL AND nb.sent_at IS NULL)
           )
           UPDATE cycles c SET notification_target_material_ids = target.material_ids,
             notification_attempted = true
           FROM target WHERE c.id = %s RETURNING target.material_ids""",
        (cycle_id,),
    ).fetchone()
    return row[0] if row else []


def stale_started_cycles(conn, cutoff: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, status, collection_status, notification_status, processing_status,
                  collected_items, processed_items, source_failures, source_failure_signatures,
                  notification_checked_at,
                  notification_attempted, notification_target_material_ids, notification_delivered_material_ids,
                  error_stage, error_message, started_at, finished_at
           FROM cycles WHERE status = 'started' AND started_at < %s ORDER BY started_at""",
        (cutoff,),
    ).fetchall()
    keys = ("id", "status", "collection_status", "notification_status", "processing_status",
            "collected_items", "processed_items", "source_failures", "source_failure_signatures",
            "notification_checked_at", "notification_attempted", "notification_target_material_ids",
            "notification_delivered_material_ids",
            "error_stage", "error_message", "started_at", "finished_at")
    return [dict(zip(keys, row)) for row in rows]


def has_unresolved_notification_failure(conn) -> bool:
    return bool(conn.execute(
        """SELECT EXISTS (
             SELECT 1 FROM cycles failed
             WHERE failed.notification_status = 'failed' AND failed.notification_checked_at IS NOT NULL
               AND failed.notification_attempted AND failed.notification_target_material_ids IS NOT NULL
               AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements_text(failed.notification_target_material_ids) target(material_id)
                 WHERE NOT EXISTS (
                   SELECT 1 FROM cycles delivered,
                     jsonb_array_elements_text(delivered.notification_delivered_material_ids) sent(material_id)
                   WHERE delivered.notification_status = 'sent'
                     AND sent.material_id = target.material_id
                 )
               )
           )"""
    ).fetchone()[0])

def watchdog_runtime_state(conn, now=None) -> dict[str, Any]:
    conn.execute(
        """INSERT INTO watchdog_incidents (incident_key, active, details, opened_at)
           VALUES ('__runtime__', false, 'runtime', COALESCE(%s, now()))
           ON CONFLICT (incident_key) DO NOTHING""", (now,)
    )
    row = conn.execute(
        "SELECT opened_at, consecutive_failures, restart_attempted FROM watchdog_incidents WHERE incident_key = '__runtime__'"
    ).fetchone()
    return {"started_at": row[0], "failed_checks": row[1], "restart_attempted": row[2]}


def watchdog_started_at(conn, now=None):
    return watchdog_runtime_state(conn, now)["started_at"]


def update_watchdog_runtime(conn, *, failed_checks: int, restart_attempted: bool) -> None:
    update_watchdog_incident(conn, "__runtime__", consecutive_failures=failed_checks,
                             restart_attempted=restart_attempted)


def watchdog_incidents(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT incident_key, active, consecutive_failures, restart_attempted, details,
                  opened_at, resolved_at, alert_sent_at, recovery_sent_at
           FROM watchdog_incidents"""
    ).fetchall()
    keys = ("incident_key", "active", "consecutive_failures", "restart_attempted", "details",
            "opened_at", "resolved_at", "alert_sent_at", "recovery_sent_at")
    return {row[0]: dict(zip(keys, row)) for row in rows if row[0] != "__runtime__"}


def open_watchdog_incident(conn, incident_key: str, details: str, *, now=None) -> None:
    conn.execute(
        """INSERT INTO watchdog_incidents
           (incident_key, active, details, opened_at) VALUES (%s, true, %s, COALESCE(%s, now()))
           ON CONFLICT (incident_key) DO UPDATE SET
             active = true, details = EXCLUDED.details,
             opened_at = CASE WHEN watchdog_incidents.active THEN watchdog_incidents.opened_at
                              ELSE EXCLUDED.opened_at END,
             resolved_at = NULL,
             alert_sent_at = CASE WHEN watchdog_incidents.active THEN watchdog_incidents.alert_sent_at ELSE NULL END,
             recovery_sent_at = CASE WHEN watchdog_incidents.active THEN watchdog_incidents.recovery_sent_at ELSE NULL END""",
        (incident_key, details[:500], now),
    )


def update_watchdog_incident(conn, incident_key: str, *, details: str | None = None,
                             consecutive_failures: int | None = None,
                             restart_attempted: bool | None = None) -> None:
    assignments = []
    values: list[Any] = []
    if details is not None:
        assignments.append("details = %s")
        values.append(details[:500])
    if consecutive_failures is not None:
        assignments.append("consecutive_failures = %s")
        values.append(max(0, int(consecutive_failures)))
    if restart_attempted is not None:
        assignments.append("restart_attempted = %s")
        values.append(bool(restart_attempted))
    if assignments:
        values.append(incident_key)
        conn.execute(f"UPDATE watchdog_incidents SET {', '.join(assignments)} WHERE incident_key = %s", values)


def mark_watchdog_notice_sent(conn, incident_key: str, *, recovered: bool = False, now=None) -> None:
    column = "recovery_sent_at" if recovered else "alert_sent_at"
    conn.execute(f"UPDATE watchdog_incidents SET {column} = COALESCE(%s, now()) WHERE incident_key = %s",
                 (now, incident_key))


def resolve_watchdog_incident(conn, incident_key: str, *, now=None) -> None:
    conn.execute("UPDATE watchdog_incidents SET active = false, resolved_at = COALESCE(%s, now()) WHERE incident_key = %s",
                 (now, incident_key))

def add_material(conn, source_id: int, run_id: int, item: dict[str, Any]) -> bool:
    body = (item.get("text") or "").strip()
    fingerprint = hashlib.sha256(
        (item["url"].strip() + "\n" + item["title"].strip() + "\n" + body).encode("utf-8")
    ).hexdigest()
    row = conn.execute(
        """INSERT INTO materials
           (source_id, run_id, url, title, text, published_at, fingerprint, translation_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
           ON CONFLICT (fingerprint) DO NOTHING RETURNING id""",
        (source_id, run_id, item["url"], item["title"], body, item.get("published_at"), fingerprint),
    ).fetchone()
    return row is not None


def pending_materials(conn, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.id, m.url, m.title, m.text, m.published_at,
                  m.translation_status,
                  s.code, s.name, s.trust, s.scope
           FROM materials m JOIN sources s ON s.id = m.source_id
           WHERE m.status = 'new'
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC LIMIT %s""",
        (limit,),
    ).fetchall()
    columns = ("material_id", "url", "title", "text", "published_at", "translation_status",
               "source_code", "source", "trust", "scope")
    return [dict(zip(columns, row)) for row in rows]


def import_signals(conn, signals: Iterable[dict[str, Any]], skipped_material_ids: Iterable[int] = (), *,
                   translations: Iterable[dict[str, Any]] = (), reject_confirmed: bool = False) -> tuple[int, int, int]:
    signals = list(signals)
    skipped_material_ids = list(skipped_material_ids)
    translations = list(translations)
    for signal in signals:
        if not isinstance(signal, dict) or type(signal.get("importance")) is not int or not 1 <= signal["importance"] <= 5:
            raise ValueError("importance должен быть целым числом от 1 до 5")
    translation_ids = []
    for translation in translations:
        if not isinstance(translation, dict) or not {'material_id', 'title_ru', 'text_ru'}.issubset(translation):
            raise ValueError("Material translation is missing required fields")
        material_id = translation['material_id']
        title_ru, text_ru = translation['title_ru'], translation['text_ru']
        if (type(material_id) is not int or not isinstance(title_ru, str) or not title_ru.strip() or
                not isinstance(text_ru, str) or len(text_ru) > 400 or material_id in translation_ids):
            raise ValueError("Invalid material translation")
        translation_ids.append(material_id)
    if translation_ids:
        pending_rows = conn.execute(
            "SELECT id, text FROM materials WHERE id = ANY(%s) AND status = 'new' "
            "AND translation_status = 'pending' FOR UPDATE",
            (translation_ids,),
        ).fetchall()
        pending_text = {row[0]: row[1] for row in pending_rows}
        if set(pending_text) != set(translation_ids):
            raise ValueError("Translation is not for a new pending material")
        for translation in translations:
            if (pending_text[translation['material_id']] or "")[:400].strip() and not translation['text_ru'].strip():
                raise ValueError("text_ru обязателен для непустого текста материала")
    referenced_ids = set()
    for signal in signals:
        ids = signal.get("material_ids", []) if isinstance(signal, dict) else []
        if isinstance(ids, list):
            referenced_ids.update(value for value in ids if type(value) is int)
    referenced_ids.update(value for value in skipped_material_ids if type(value) is int)
    untranslated = set()
    if referenced_ids:
        untranslated = {row[0] for row in conn.execute(
            "SELECT id FROM materials WHERE id = ANY(%s) AND status = 'new' AND translation_status = 'pending' FOR UPDATE",
            (list(referenced_ids),),
        ).fetchall()} - set(translation_ids)
    if untranslated:
        raise ValueError("translation is required for new materials")
    if translation_ids:
        for translation in translations:
            row = conn.execute(
                """UPDATE materials SET title_ru = %s, text_ru = %s, translation_status = 'ready'
                   WHERE id = %s AND status = 'new' AND translation_status = 'pending' RETURNING id""",
                (translation['title_ru'], translation['text_ru'], translation['material_id']),
            ).fetchone()
            if row is None:
                raise ValueError("Материал перестал ожидать перевод")
    created = linked = skipped = 0
    for signal in signals:
        required = {"material_ids", "platform", "topic", "importance", "summary", "recommended_action", "verification"}
        missing = required.difference(signal)
        if missing:
            raise ValueError(f"В сигнале отсутствуют поля: {', '.join(sorted(missing))}")
        if type(signal["importance"]) is not int or not 1 <= signal["importance"] <= 5:
            raise ValueError("importance должен быть целым числом от 1 до 5")
        platform = normalize_event_platform(signal["platform"])
        ids = signal["material_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(value, int) for value in ids):
            raise ValueError("material_ids должен быть непустым списком идентификаторов")
        key = signal.get("event_key") or "|".join(
            [signal["platform"].strip().lower(), signal["topic"].strip().lower(), signal["summary"].strip().lower()]
        )
        conflict_guard = " WHERE events.verification <> 'confirmed'" if reject_confirmed else ""
        event = conn.execute(
            """INSERT INTO events
               (event_key, platform, topic, importance, summary, recommended_action, verification, effective_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (event_key) DO UPDATE SET updated_at = now()""" + conflict_guard + """
               RETURNING id, (xmax = 0)""",
            (key, platform, signal["topic"], signal["importance"], signal["summary"], signal["recommended_action"], signal["verification"], signal.get("effective_date")),
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


def pending_relevance_materials(conn, rules_version: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.id, m.url, m.title, m.text, m.published_at,
                  s.code, s.name, s.trust, s.scope
           FROM materials m JOIN sources s ON s.id = m.source_id
           WHERE m.status = 'new'
             AND NOT EXISTS (
                 SELECT 1 FROM relevance_assessments a
                 WHERE a.material_id = m.id AND a.rules_version = %s
             )
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC""",
        (rules_version,),
    ).fetchall()
    columns = ("material_id", "url", "title", "text", "published_at", "source_code", "source", "trust", "scope")
    return [dict(zip(columns, row)) for row in rows]


def save_relevance_assessments(conn, assessments: Iterable[dict[str, Any]]) -> int:
    assessments = list(assessments)
    seen = set()
    for assessment in assessments:
        material_id = assessment.get("material_id")
        version = assessment.get("rules_version")
        if type(material_id) is not int or material_id < 1 or not isinstance(version, str) or not version.strip():
            raise ValueError("Invalid relevance assessment identity")
        if (material_id, version) in seen:
            raise ValueError("Duplicate relevance assessment in batch")
        seen.add((material_id, version))
        if assessment.get("classification") not in CLASSIFICATIONS:
            raise ValueError("Invalid relevance classification")
        if type(assessment.get("score")) is not int or not 0 <= assessment["score"] <= 100:
            raise ValueError("Relevance score must be an integer from 0 to 100")
        for field in ("rule_codes", "platforms"):
            values = assessment.get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"Invalid relevance assessment field: {field}")
        if not isinstance(assessment.get("explanation"), str) or not assessment["explanation"].strip():
            raise ValueError("Relevance assessment requires an explanation")

    if not assessments:
        return 0
    inserted = 0
    with conn.transaction():
        for item in assessments:
            row = conn.execute(
                """INSERT INTO relevance_assessments
                   (material_id, rules_version, classification, score, rule_codes, platforms, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (material_id, rules_version) DO NOTHING
                   RETURNING id""",
                (item["material_id"], item["rules_version"], item["classification"], item["score"],
                 item["rule_codes"], item["platforms"], item["explanation"][:300]),
            ).fetchone()
            inserted += row is not None
    return inserted


def relevance_review_sample(conn, limit: int = 100) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Review sample limit must be between 1 and 1000")
    rows = conn.execute(
        """WITH candidates AS (
               SELECT m.id, m.title, m.text, s.code, s.name,
                      row_number() OVER (PARTITION BY m.source_id ORDER BY m.collected_at DESC, m.id DESC) AS source_rank
               FROM materials m JOIN sources s ON s.id = m.source_id
               LEFT JOIN relevance_labels l ON l.material_id = m.id
               WHERE l.material_id IS NULL
           )
           SELECT id, title, text, code, name
           FROM candidates
           ORDER BY source_rank, code, id
           LIMIT %s""",
        (limit,),
    ).fetchall()
    columns = ("material_id", "title", "text", "source_code", "source")
    return [dict(zip(columns, row)) for row in rows]


def save_relevance_labels(conn, labels: Iterable[dict[str, Any]], rules_version: str) -> int:
    labels = list(labels)
    seen = set()
    for item in labels:
        material_id = item.get("material_id")
        label = item.get("label")
        if type(material_id) is not int or material_id < 1 or label not in CLASSIFICATIONS:
            raise ValueError("Invalid relevance label")
        if material_id in seen:
            raise ValueError("Duplicate relevance label in batch")
        seen.add(material_id)
    if not labels:
        return 0

    ids = [item["material_id"] for item in labels]
    with conn.transaction():
        eligible = {
            row[0]
            for row in conn.execute(
                """SELECT requested.id
                   FROM unnest(%s::integer[]) AS requested(id)
                   JOIN materials m ON m.id = requested.id
                   JOIN relevance_assessments a ON a.material_id = m.id AND a.rules_version = %s""",
                (ids, rules_version),
            ).fetchall()
        }
        if eligible != set(ids):
            raise ValueError("Labels include materials without a current relevance assessment")
        for item in labels:
            conn.execute(
                """INSERT INTO relevance_labels (material_id, label)
                   VALUES (%s, %s)
                   ON CONFLICT (material_id) DO UPDATE SET label = EXCLUDED.label, labeled_at = now()""",
                (item["material_id"], item["label"]),
            )
    return len(labels)


def relevance_evaluation_rows(conn, rules_version: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.id, m.title, a.classification, l.label
           FROM relevance_labels l
           JOIN relevance_assessments a ON a.material_id = l.material_id AND a.rules_version = %s
           JOIN materials m ON m.id = l.material_id
           ORDER BY m.id""",
        (rules_version,),
    ).fetchall()
    columns = ("material_id", "title", "predicted", "label")
    return [dict(zip(columns, row)) for row in rows]
