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
    names = ('id', 'title', 'text', 'url', 'published_at', 'collected_at', 'source', 'category', 'sort_date')
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


def import_signals(conn, signals: Iterable[dict[str, Any]], skipped_material_ids: Iterable[int] = (), *, reject_confirmed: bool = False) -> tuple[int, int, int]:
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

