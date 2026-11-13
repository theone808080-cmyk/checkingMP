"""PostgreSQL storage for the local manual-review pilot."""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import psycopg

from judge.constants import CLASSIFICATIONS, RELEVANCE_CLASSES, SELLER_IMPACTS

SCHEMA = Path(__file__).with_name("schema.sql")
SOURCE_CONFIG = Path(__file__).parents[1] / "config" / "sources.json"
SCHEMA_LOCK = 21429


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def init_schema(conn) -> None:
    conn.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK,))
    try:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
        _backfill_content_fingerprints(conn)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK,))


def _normalized_publication_part(value: str | None) -> str:
    text = unicodedata.normalize("NFC", value or "")
    return re.sub(r"\s+", " ", text).strip()


def normalized_publication_fingerprint(title: str, text: str | None) -> str | None:
    normalized_title = _normalized_publication_part(title)
    normalized_text = _normalized_publication_part(text)
    if not normalized_title or not normalized_text:
        return None
    return hashlib.sha256((normalized_title + "\n" + normalized_text).encode("utf-8")).hexdigest()


def _backfill_content_fingerprints(conn) -> None:
    rows = conn.execute(
        "SELECT id, title, text FROM materials WHERE content_fingerprint IS NULL"
    ).fetchall()
    for material_id, title, text in rows:
        fingerprint = normalized_publication_fingerprint(title, text)
        if fingerprint:
            conn.execute(
                "UPDATE materials SET content_fingerprint=%s WHERE id=%s AND content_fingerprint IS NULL",
                (fingerprint, material_id),
            )


def sync_sources(conn) -> int:
    sources = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    supports_access = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='sources'::regclass "
        "AND attname='access_public' AND NOT attisdropped)"
    ).fetchone()[0]
    for source in sources:
        existing = conn.execute(
            "SELECT url FROM sources WHERE code=%s AND managed_by='config'", (source["code"],),
        ).fetchone()
        changed_url = bool(existing and existing[0] != source["url"])
        update_fields = (
            "active = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN false ELSE sources.active END, "
            "access_public = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN NULL ELSE sources.access_public END, "
            "access_requires_credentials = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN NULL ELSE sources.access_requires_credentials END, "
            "access_rate_limit = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN NULL ELSE sources.access_rate_limit END, "
            "access_cost = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN 'unknown' ELSE sources.access_cost END, "
            "access_checked_at = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN NULL ELSE sources.access_checked_at END, "
            "access_check_result = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN 'unknown' ELSE sources.access_check_result END"
            if supports_access else
            "active = CASE WHEN sources.url IS DISTINCT FROM EXCLUDED.url THEN false ELSE sources.active END"
        )
        conn.execute(
            """INSERT INTO sources (code, name, kind, url, trust, scope, category, active, managed_by)
               VALUES (%(code)s, %(name)s, %(kind)s, %(url)s, %(trust)s, %(scope)s, %(category)s, true, 'config')
               ON CONFLICT (code) DO UPDATE SET
                 name = EXCLUDED.name, kind = EXCLUDED.kind, url = EXCLUDED.url,
                 trust = EXCLUDED.trust, scope = EXCLUDED.scope, category = EXCLUDED.category, """ +
            update_fields + " WHERE sources.managed_by = 'config'",
            source,
        )
        if changed_url:
            domain = _candidate_domain(urlparse(source["url"]).hostname or "")
            conn.execute(
                """INSERT INTO source_candidates
                     (publisher_domain, collection_url, name, kind, category, trust, scope)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (publisher_domain, collection_url) DO NOTHING""",
                (domain, source["url"], source["name"], source["kind"], source["category"],
                 source["trust"], source["scope"]),
            )
    return len(sources)


def _candidate_domain(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 253:
        raise ValueError("invalid publisher domain")
    value = value.strip().rstrip(".").lower()
    try:
        domain = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid publisher domain") from None
    if any(not part or len(part) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
           for part in domain.split(".")):
        raise ValueError("invalid publisher domain")
    return domain


def _candidate_collection_url(value: str, domain: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid collection URL")
    parsed = urlparse(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid collection URL") from None
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
            or port not in (None, 443)):
        raise ValueError("invalid collection URL")
    try:
        parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid collection URL") from None
    return parsed._replace(scheme="https", fragment="").geturl()


def _candidate_examples(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("invalid candidate examples")
    result = []
    for url in value:
        if not isinstance(url, str) or len(url) > 2048:
            continue
        parsed = urlparse(url)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password):
            continue
        if url not in result:
            result.append(url)
        if len(result) == 10:
            break
    return result


def upsert_source_candidates(conn, candidates: list[dict[str, Any]]) -> int:
    created = 0
    known_domains = set()
    for (source_url,) in conn.execute("SELECT url FROM sources WHERE active").fetchall():
        try:
            known_domains.add(_candidate_domain(urlparse(source_url).hostname or ""))
        except ValueError:
            continue
    for candidate in candidates:
        domain = _candidate_domain(candidate.get("publisher_domain"))
        if domain in known_domains:
            continue
        already_reviewed = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM source_candidates WHERE publisher_domain=%s AND status IN ('approved','rejected'))",
            (domain,),
        ).fetchone()[0]
        if already_reviewed:
            continue
        collection_url = _candidate_collection_url(candidate.get("collection_url"), domain)
        name = candidate.get("name")
        kind = candidate.get("kind", "html")
        category = candidate.get("category", "industry")
        trust = candidate.get("trust", "industry")
        scope = candidate.get("scope", "marketplaces")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("invalid candidate name")
        if kind not in ("rss", "html", "telegram") or trust not in ("official", "regulator", "industry"):
            raise ValueError("invalid candidate type")
        if not isinstance(category, str) or not category or len(category) > 80:
            raise ValueError("invalid candidate category")
        if not isinstance(scope, str) or not scope or len(scope) > 300:
            raise ValueError("invalid candidate scope")
        examples = _candidate_examples(candidate.get("example_urls", []))
        row = conn.execute(
            """INSERT INTO source_candidates
                 (publisher_domain, collection_url, name, kind, category, trust, scope, examples,
                  access_public, access_requires_credentials, access_rate_limit, access_cost)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
               ON CONFLICT (publisher_domain, collection_url) DO UPDATE SET
                 name = CASE WHEN source_candidates.status IN ('pending', 'needs_setup') THEN EXCLUDED.name ELSE source_candidates.name END,
                 examples = (SELECT COALESCE(jsonb_agg(value ORDER BY first_ord), '[]'::jsonb)
                             FROM (SELECT value, min(ord) AS first_ord FROM (
                               SELECT value, ord FROM jsonb_array_elements_text(source_candidates.examples)
                                 WITH ORDINALITY AS old(value, ord)
                               UNION ALL
                               SELECT value, 100 + ord FROM jsonb_array_elements_text(EXCLUDED.examples)
                                 WITH ORDINALITY AS new(value, ord)
                             ) combined GROUP BY value ORDER BY first_ord LIMIT 10) merged),
                 updated_at = now()
               RETURNING (xmax = 0) AS inserted""",
            (domain, collection_url, name.strip(), kind, category, trust, scope,
             json.dumps(examples, ensure_ascii=False), candidate.get("access_public"),
             candidate.get("access_requires_credentials"), candidate.get("access_rate_limit"),
             candidate.get("access_cost", "unknown")),
        ).fetchone()
        created += int(row[0])
    return created


def list_source_candidates(conn, status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
    if status not in ("pending", "needs_setup", "approved", "rejected", "all"):
        raise ValueError("invalid candidate status")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid candidate limit")
    condition = "" if status == "all" else "WHERE status = %s"
    params = (limit,) if status == "all" else (status, limit)
    rows = conn.execute(
        f"""SELECT id, publisher_domain, collection_url, name, kind, category, trust, scope,
                   status, probe_status, probe_error, examples, discovered_at, updated_at,
                   access_public, access_requires_credentials, access_rate_limit, access_cost,
                   access_checked_at, access_check_result
            FROM source_candidates {condition} ORDER BY updated_at DESC, id DESC LIMIT %s""",
        params,
    ).fetchall()
    keys = ("id", "publisher_domain", "collection_url", "name", "kind", "category", "trust", "scope",
            "status", "probe_status", "probe_error", "example_urls", "discovered_at", "updated_at",
            "access_public", "access_requires_credentials", "access_rate_limit", "access_cost",
            "access_checked_at", "access_check_result")
    return [dict(zip(keys, row)) for row in rows]


def pause_exa_until(conn, until: datetime, reason: str) -> None:
    safe_reason = reason if reason in {"limit_reached", "payment_required"} else "temporarily_unavailable"
    conn.execute(
        """INSERT INTO source_provider_state (provider, pause_until, pause_reason)
           VALUES ('exa', %s, %s)
           ON CONFLICT (provider) DO UPDATE SET pause_until = EXCLUDED.pause_until,
             pause_reason = EXCLUDED.pause_reason, updated_at = now()""",
        (until, safe_reason),
    )


def exa_pause_until(conn) -> datetime | None:
    row = conn.execute(
        "SELECT pause_until FROM source_provider_state WHERE provider = 'exa' AND pause_until > now()"
    ).fetchone()
    return row[0] if row else None


def review_source_candidate(conn, candidate_id: int, action: str, fields: dict[str, Any],
                            probe: dict[str, Any] | None) -> dict[str, Any]:
    if type(candidate_id) is not int or candidate_id < 1 or action not in ("approve", "reject"):
        raise ValueError("invalid candidate action")
    access_fields = {"access_public", "access_requires_credentials", "access_rate_limit", "access_cost"}
    if not isinstance(fields, dict) or set(fields) - {"name", "collection_url", "kind", "category", "trust", "scope"} - access_fields:
        raise ValueError("invalid candidate fields")
    with conn.transaction():
        row = conn.execute(
            """SELECT publisher_domain, collection_url, name, kind, category, trust, scope, status,
                      access_public, access_requires_credentials, access_rate_limit, access_cost
               FROM source_candidates WHERE id = %s FOR UPDATE""", (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("candidate not found")
        domain, old_url, old_name, old_kind, old_category, old_trust, old_scope, status, old_public, old_credentials, old_rate_limit, old_cost = row
        if status in ("approved", "rejected"):
            raise ValueError("candidate already reviewed")
        name = fields.get("name", old_name)
        kind = fields.get("kind", old_kind)
        category = fields.get("category", old_category)
        trust = fields.get("trust", old_trust)
        scope = fields.get("scope", old_scope)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise ValueError("invalid candidate name")
        if kind not in ("rss", "html", "telegram"):
            raise ValueError("invalid candidate kind")
        if category not in ("ozon", "wildberries", "yandex", "chestny_znak", "industry"):
            raise ValueError("invalid candidate category")
        if trust not in ("official", "regulator", "industry"):
            raise ValueError("invalid candidate trust")
        if not isinstance(scope, str) or not scope.strip() or len(scope.strip()) > 300:
            raise ValueError("invalid candidate scope")
        collection_url = _candidate_collection_url(fields.get("collection_url", old_url), domain)
        access_public = fields.get("access_public", old_public)
        access_credentials = fields.get("access_requires_credentials", old_credentials)
        access_rate_limit = fields.get("access_rate_limit", old_rate_limit)
        access_cost = fields.get("access_cost", old_cost or "unknown")
        if access_public not in (True, False, None) or access_credentials not in (True, False, None):
            raise ValueError("invalid source access visibility")
        if access_rate_limit is not None and (not isinstance(access_rate_limit, str) or len(access_rate_limit) > 200):
            raise ValueError("invalid source rate limit")
        if access_cost not in ("free", "paid", "unknown"):
            raise ValueError("invalid source access cost")
        if action == "reject":
            conn.execute(
                """UPDATE source_candidates SET name=%s, collection_url=%s, kind=%s, category=%s,
                     trust=%s, scope=%s, status='rejected', updated_at=now() WHERE id=%s""",
                (name.strip(), collection_url, kind, category, trust, scope.strip(), candidate_id),
            )
            return {"status": "rejected", "source_code": None}
        safe_probe_error = (probe.get("error") if isinstance(probe, dict) else "probe_failed")
        if safe_probe_error not in {"unsafe_url", "probe_failed", "no_supported_feed", "unsupported_type",
                                    "unsupported_telegram_url", "invalid_telegram_preview", "invalid_url",
                                    "source_redirect", "article_url_not_source", None}:
            safe_probe_error = "probe_failed"
        probe_ok = (isinstance(probe, dict) and probe.get("ok") is True
                    and probe.get("kind") == kind and probe.get("collection_url") == collection_url)
        if not probe_ok:
            conn.execute(
                """UPDATE source_candidates SET name=%s, collection_url=%s, kind=%s, category=%s,
                     trust=%s, scope=%s, access_public=%s, access_requires_credentials=%s, access_rate_limit=%s,
                     access_cost=%s, access_checked_at=now(), access_check_result='failed',
                     status='needs_setup', probe_status='failed', probe_error=%s,
                     updated_at=now() WHERE id=%s""",
                (name.strip(), collection_url, kind, category, trust, scope.strip(), access_public,
                 access_credentials, access_rate_limit, access_cost, safe_probe_error, candidate_id),
            )
            return {"status": "needs_setup", "source_code": None}
        access_ok = access_public is True and access_credentials is False and access_cost == "free"
        if not access_ok:
            conn.execute(
                """UPDATE source_candidates SET name=%s, collection_url=%s, kind=%s, category=%s, trust=%s,
                     scope=%s, access_public=%s, access_requires_credentials=%s, access_rate_limit=%s, access_cost=%s,
                     access_checked_at=now(), access_check_result='passed', probe_status='passed', probe_error=NULL,
                     status='needs_setup', updated_at=now() WHERE id=%s""",
                (name.strip(), collection_url, kind, category, trust, scope.strip(), access_public,
                 access_credentials, access_rate_limit, access_cost, candidate_id),
            )
            return {"status": "needs_setup", "source_code": None}
        code_hash = hashlib.sha256(f"{domain}|{collection_url}".encode("utf-8")).hexdigest()[:24]
        source_code = "discovered_" + code_hash
        conn.execute(
            """INSERT INTO sources (code, name, kind, url, trust, scope, category, active, managed_by,
                                    access_public, access_requires_credentials, access_rate_limit, access_cost,
                                    access_checked_at, access_check_result)
               VALUES (%s, %s, %s, %s, %s, %s, %s, true, 'owner', %s, %s, %s, %s, now(), 'passed')
               ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name, kind=EXCLUDED.kind,
                 url=EXCLUDED.url, trust=EXCLUDED.trust, scope=EXCLUDED.scope,
                 category=EXCLUDED.category, active=true, access_public=EXCLUDED.access_public,
                 access_requires_credentials=EXCLUDED.access_requires_credentials,
                 access_rate_limit=EXCLUDED.access_rate_limit, access_cost=EXCLUDED.access_cost,
                 access_checked_at=now(), access_check_result='passed'
               WHERE sources.managed_by='owner'""",
            (source_code, name.strip(), kind, collection_url, trust, scope.strip(), category,
             access_public, access_credentials, access_rate_limit, access_cost),
        )
        conn.execute(
            """UPDATE source_candidates SET name=%s, collection_url=%s, kind=%s, category=%s,
                 trust=%s, scope=%s, access_public=%s, access_requires_credentials=%s, access_rate_limit=%s,
                 access_cost=%s, access_checked_at=now(), access_check_result='passed',
                 status='approved', probe_status='passed', probe_error=NULL,
                 updated_at=now() WHERE id=%s""",
            (name.strip(), collection_url, kind, category, trust, scope.strip(), access_public,
             access_credentials, access_rate_limit, access_cost, candidate_id),
        )
        return {"status": "approved", "source_code": source_code}


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
                         topic: str | None, verification: str | None, trust: str | None,
                         seller_impact: str | None = None, summary_audit: str | None = None,
                         recommendation_audit: str | None = None,
                         relevance_filter: bool = False, relevance_version: str | None = None) -> str:
    encoded = json.dumps([period, platform, importance, topic, verification, trust,
                          seller_impact, summary_audit, recommendation_audit,
                          relevance_filter, relevance_version], ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _finding_evaluation_ctes() -> str:
    """Aggregate persisted classifier and audit outcomes without inventing missing results."""
    return """relevance_by_event AS (
             SELECT e.id AS event_id,
                    CASE count(DISTINCT a.classification)
                      WHEN 0 THEN 'not_assessed' WHEN 1 THEN min(a.classification) ELSE 'uncertain' END AS classification,
                    CASE count(DISTINCT a.relevance)
                      WHEN 0 THEN 'not_assessed' WHEN 1 THEN min(a.relevance) ELSE 'uncertain' END AS relevance,
                    CASE count(DISTINCT a.seller_impact)
                      WHEN 0 THEN NULL WHEN 1 THEN min(a.seller_impact) ELSE 'unclear' END AS seller_impact
             FROM events e
             LEFT JOIN event_materials em ON em.event_id=e.id
             LEFT JOIN relevance_assessments a ON a.material_id=em.material_id AND a.rules_version=%s
             GROUP BY e.id
           ), latest_quality AS (
             SELECT DISTINCT ON (finding_id, target) finding_id, target, status
             FROM finding_quality_results
             ORDER BY finding_id, target, created_at DESC, id DESC
           ), quality_by_finding AS (
             SELECT finding_id,
                    max(status) FILTER (WHERE target='summary') AS summary_audit,
                    max(status) FILTER (WHERE target='recommendation') AS recommendation_audit
             FROM latest_quality GROUP BY finding_id
           )"""


_FINDING_AUDIT_STATUSES = ('ok', 'review', 'issue', 'not_checked')


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
                    "FROM materials m JOIN sources s ON s.id = m.source_id WHERE m.translation_status <> 'pending' AND " + where +
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
        "FROM materials m JOIN sources s ON s.id = m.source_id CROSS JOIN snapshot WHERE m.id = ANY(snapshot.material_ids) AND m.translation_status <> 'pending') "
        'SELECT snapshot.id, (SELECT count(*) FROM filtered), page.* FROM snapshot '
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


def finding_filter_counts(conn, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return full-result counts for each finding filter, ignoring its own group."""
    filters = dict(filters or {})
    filter_active = relevance_filter_active(conn)
    period = filters.get('period', 'all')
    platform = filters.get('platform', 'all')
    importance = filters.get('importance')
    topic = filters.get('topic')
    verification = filters.get('verification')
    trust = filters.get('trust')
    seller_impact = filters.get('seller_impact')
    summary_audit = filters.get('summary_audit')
    recommendation_audit = filters.get('recommendation_audit')
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
    if seller_impact is not None and seller_impact not in SELLER_IMPACTS:
        raise ValueError('invalid seller impact')
    if summary_audit is not None and summary_audit not in _FINDING_AUDIT_STATUSES:
        raise ValueError('invalid summary audit status')
    if recommendation_audit is not None and recommendation_audit not in _FINDING_AUDIT_STATUSES:
        raise ValueError('invalid recommendation audit status')

    def where_for(exclude: str, extra: str | None = None, extra_params: tuple[Any, ...] = ()):
        clauses = []
        values: list[Any] = []
        if filter_active:
            clauses.append(
                "EXISTS (SELECT 1 FROM event_materials rem JOIN relevance_assessments ra "
                "ON ra.material_id=rem.material_id WHERE rem.event_id=e.id "
                "AND ra.rules_version=%s AND ra.relevance='relevant')"
            )
            values.append(active_relevance_version())
        if exclude != 'period' and period != 'all':
            midnight = "date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
            start = " - INTERVAL '6 days'" if period == 'week' else ''
            clauses.append(
                "EXISTS (SELECT 1 FROM event_materials pem JOIN materials pm ON pm.id = pem.material_id "
                "WHERE pem.event_id=e.id AND COALESCE(pm.published_at,pm.collected_at) >= (" + midnight + start +
                ") AT TIME ZONE 'Europe/Moscow' AND COALESCE(pm.published_at,pm.collected_at) < (" + midnight +
                " + INTERVAL '1 day') AT TIME ZONE 'Europe/Moscow')"
            )
        if exclude != 'platform' and platform != 'all':
            clauses.append('(lower(e.platform)=ANY(%s) OR lower(e.platform) ~ %s)')
            values.extend((list(_FINDING_PLATFORMS[platform]), _FINDING_PLATFORM_PATTERNS[platform]))
        if exclude != 'importance' and importance is not None:
            clauses.append('e.importance=%s'); values.append(importance)
        if exclude != 'topic_status' and topic is not None:
            clauses.append('strpos(lower(e.topic),lower(%s))>0'); values.append(topic.strip())
        if exclude != 'topic_status' and verification is not None:
            clauses.append('e.verification=%s'); values.append(verification)
        if exclude != 'source_type' and trust is not None:
            clauses.append("EXISTS (SELECT 1 FROM event_materials tem JOIN materials tm ON tm.id=tem.material_id JOIN sources ts ON ts.id=tm.source_id WHERE tem.event_id=e.id AND ts.trust=%s)")
            values.append(trust)
        if exclude != 'seller_impact' and seller_impact is not None:
            clauses.append('COALESCE(ev.seller_impact,e.seller_impact)=%s'); values.append(seller_impact)
        if exclude != 'summary_audit' and summary_audit is not None:
            clauses.append("COALESCE(qa.summary_audit, 'not_checked')=%s"); values.append(summary_audit)
        if exclude != 'recommendation_audit' and recommendation_audit is not None:
            clauses.append("COALESCE(qa.recommendation_audit, 'not_checked')=%s"); values.append(recommendation_audit)
        if extra:
            clauses.append(extra); values.extend(extra_params)
        return (' AND '.join(clauses) if clauses else 'true'), tuple(values)

    def count(exclude: str, extra: str | None = None, args: tuple[Any, ...] = ()):
        where, values = where_for(exclude, extra, args)
        return conn.execute(
            'WITH ' + _finding_evaluation_ctes() + ' SELECT count(DISTINCT e.id) FROM events e '
            'LEFT JOIN relevance_by_event ev ON ev.event_id=e.id '
            'LEFT JOIN quality_by_finding qa ON qa.finding_id=e.id '
            'JOIN event_materials em ON em.event_id=e.id '
            'JOIN materials m ON m.id=em.material_id JOIN sources s ON s.id=m.source_id WHERE ' + where,
            (active_relevance_version(),) + values,
        ).fetchone()[0]

    result: dict[str, Any] = {}
    result['period'] = {'all': count('period')}
    for value in ('today', 'week'):
        midnight = "date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
        start = " - INTERVAL '6 days'" if value == 'week' else ''
        clause = (
            "EXISTS (SELECT 1 FROM event_materials pem JOIN materials pm ON pm.id=pem.material_id "
            "WHERE pem.event_id=e.id AND COALESCE(pm.published_at,pm.collected_at) >= (" + midnight + start +
            ") AT TIME ZONE 'Europe/Moscow' AND COALESCE(pm.published_at,pm.collected_at) < (" + midnight +
            " + INTERVAL '1 day') AT TIME ZONE 'Europe/Moscow')"
        )
        result['period'][value] = count('period', clause)
    result['platform'] = {'all': count('platform')}
    for value in _FINDING_PLATFORMS:
        result['platform'][value] = count('platform', '(lower(e.platform)=ANY(%s) OR lower(e.platform) ~ %s)',
                                          (list(_FINDING_PLATFORMS[value]), _FINDING_PLATFORM_PATTERNS[value]))
    result['importance'] = {'all': count('importance')}
    for value in range(1, 6):
        result['importance'][str(value)] = count('importance', 'e.importance=%s', (value,))
    result['source_type'] = {'all': count('source_type')}
    for value in ('official', 'regulator', 'industry'):
        result['source_type'][value] = count('source_type', 's.trust=%s', (value,))
    result['seller_impact'] = {'all': count('seller_impact')}
    for value in sorted(SELLER_IMPACTS):
        result['seller_impact'][value] = count('seller_impact', 'COALESCE(ev.seller_impact,e.seller_impact)=%s', (value,))
    for group, column in (('summary_audit', 'qa.summary_audit'),
                          ('recommendation_audit', 'qa.recommendation_audit')):
        result[group] = {'all': count(group)}
        for value in _FINDING_AUDIT_STATUSES:
            result[group][value] = count(group, f"COALESCE({column}, 'not_checked')=%s", (value,))
    result['topic_status'] = {'verification': {'all': count('topic_status')}}
    for value in ('confirmed', 'needs_check'):
        result['topic_status']['verification'][value] = count('topic_status', 'e.verification=%s', (value,))
    where, values = where_for('topic_status')
    topics = conn.execute(
        'WITH ' + _finding_evaluation_ctes() + ' SELECT e.topic, count(DISTINCT e.id) FROM events e '
        'LEFT JOIN relevance_by_event ev ON ev.event_id=e.id '
        'LEFT JOIN quality_by_finding qa ON qa.finding_id=e.id '
        'JOIN event_materials em ON em.event_id=e.id '
        'JOIN materials m ON m.id=em.material_id JOIN sources s ON s.id=m.source_id WHERE ' + where +
        ' GROUP BY e.topic ORDER BY lower(e.topic), e.topic', (active_relevance_version(),) + values,
    ).fetchall()
    result['topic_status']['topics'] = [{'value': row[0], 'count': row[1]} for row in topics]
    return result


def relevance_filter_enabled() -> bool:
    return os.getenv("RELEVANCE_FILTER_ENABLED", "0").strip().casefold() in ("1", "true", "yes", "on")


def active_relevance_version() -> str:
    from judge.rules import rules_version
    return rules_version()


def relevance_filter_active(conn) -> bool:
    """Keep the feed unchanged until every collected material has a current assessment."""
    if not relevance_filter_enabled():
        return False
    try:
        version = active_relevance_version()
        missing = conn.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM materials m
                   WHERE NOT EXISTS (
                       SELECT 1 FROM relevance_assessments a
                       WHERE a.material_id=m.id AND a.rules_version=%s
                   )
               )""",
            (version,),
        ).fetchone()[0]
        return not missing
    except Exception:
        # A broken rules file or schema must not silently hide existing findings.
        return False


def feed_findings(conn, period: str = 'all', platform: str = 'all', importance: int | None = None,
                  topic: str | None = None, verification: str | None = None, trust: str | None = None,
                  cursor: str | None = None, limit: int = 20, *, seller_impact: str | None = None,
                  summary_audit: str | None = None, recommendation_audit: str | None = None) -> dict[str, Any]:
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
    if seller_impact is not None and seller_impact not in SELLER_IMPACTS:
        raise ValueError('invalid seller impact')
    if summary_audit is not None and summary_audit not in _FINDING_AUDIT_STATUSES:
        raise ValueError('invalid summary audit status')
    if recommendation_audit is not None and recommendation_audit not in _FINDING_AUDIT_STATUSES:
        raise ValueError('invalid recommendation audit status')
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid page size')
    limit = min(limit, 20)
    filter_enabled = relevance_filter_active(conn)
    relevance_version = active_relevance_version() if filter_enabled else None
    filter_hash = _finding_filter_hash(period, platform, importance, topic, verification, trust,
                                       seller_impact, summary_audit, recommendation_audit,
                                       filter_enabled, relevance_version)
    position = _decode_findings_cursor(cursor, filter_hash) if cursor is not None else None

    conditions: list[str] = []
    params: list[Any] = []
    if filter_enabled:
        conditions.append(
            "EXISTS (SELECT 1 FROM event_materials rem JOIN relevance_assessments ra "
            "ON ra.material_id=rem.material_id WHERE rem.event_id=e.id "
            "AND ra.rules_version=%s AND ra.relevance='relevant')"
        )
        params.append(relevance_version)
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
    if seller_impact is not None:
        conditions.append('COALESCE(ev.seller_impact,e.seller_impact) = %s')
        params.append(seller_impact)
    if summary_audit is not None:
        conditions.append("COALESCE(qa.summary_audit, 'not_checked') = %s")
        params.append(summary_audit)
    if recommendation_audit is not None:
        conditions.append("COALESCE(qa.recommendation_audit, 'not_checked') = %s")
        params.append(recommendation_audit)
    where = ' AND '.join(conditions) if conditions else 'true'
    filtered = (
        "filtered AS MATERIALIZED (SELECT e.id, e.platform, e.topic, e.importance, "
        "COALESCE(ev.seller_impact,e.seller_impact) AS seller_impact, e.summary, "
        "e.recommended_action, e.verification, e.effective_date, e.updated_at, "
        "CASE WHEN lower(e.platform) ~ %s THEN 1 "
        "WHEN lower(e.platform) = ANY(%s) OR lower(e.platform) ~ %s OR bool_or(s.trust = 'regulator') THEN 2 ELSE 3 END AS specificity, "
        "COALESCE(ev.classification, 'not_assessed') AS classification, "
        "COALESCE(ev.relevance, 'not_assessed') AS relevance, "
        "COALESCE(qa.summary_audit, 'not_checked') AS summary_audit, "
        "COALESCE(qa.recommendation_audit, 'not_checked') AS recommendation_audit, "
        "CASE COALESCE(ev.classification, 'not_assessed') "
        "WHEN 'practical_change' THEN 1 WHEN 'seller_action_or_risk' THEN 2 "
        "WHEN 'industry_context' THEN 3 WHEN 'uncertain' THEN 4 ELSE 5 END AS usefulness_rank, "
        "json_agg(json_build_object('name', s.name, 'trust', s.trust, 'category', s.category, 'url', m.url, "
        "'published_at', m.published_at, 'collected_at', m.collected_at) "
        "ORDER BY COALESCE(m.published_at, m.collected_at) DESC, m.id DESC) AS sources "
        "FROM events e JOIN event_materials em ON em.event_id = e.id "
        "JOIN materials m ON m.id = em.material_id JOIN sources s ON s.id = m.source_id "
        "LEFT JOIN relevance_by_event ev ON ev.event_id=e.id "
        "LEFT JOIN quality_by_finding qa ON qa.finding_id=e.id "
        "WHERE " + where + " GROUP BY e.id, ev.classification, ev.relevance, ev.seller_impact, qa.summary_audit, qa.recommendation_audit) "
    )
    general_platforms = ['all', 'cross_platform', 'межплощадочная', 'все маркетплейсы']
    base_params = [active_relevance_version(), _PLATFORM_SPECIFIC_PATTERN, general_platforms,
                   _PLATFORM_CROSS_PATTERN] + params
    if position is None:
        snapshot_id = uuid.uuid4()
        event_ids = conn.execute(
            "WITH " + _finding_evaluation_ctes() + ", expired AS (DELETE FROM finding_snapshots WHERE created_at < now() - INTERVAL '30 minutes'), " +
            filtered +
            ", snapshot AS (INSERT INTO finding_snapshots (id, filters_hash, event_ids) "
            "SELECT %s, %s, COALESCE(array_agg(id ORDER BY usefulness_rank ASC, importance DESC, updated_at DESC, id DESC), "
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
        "WITH " + _finding_evaluation_ctes() + " "
        """SELECT e.id, e.platform, e.topic, e.importance, COALESCE(ev.seller_impact,e.seller_impact) AS seller_impact,
                  e.summary, e.recommended_action,
                  e.verification, e.effective_date, e.updated_at,
                  CASE WHEN lower(e.platform) ~ %s THEN 1
                       WHEN lower(e.platform) = ANY(%s) OR lower(e.platform) ~ %s OR bool_or(s.trust = 'regulator') THEN 2 ELSE 3 END AS specificity,
                  COALESCE(ev.classification, 'not_assessed') AS classification,
                  COALESCE(ev.relevance, 'not_assessed') AS relevance,
                  COALESCE(qa.summary_audit, 'not_checked') AS summary_audit,
                  COALESCE(qa.recommendation_audit, 'not_checked') AS recommendation_audit,
                  json_agg(json_build_object('name', s.name, 'trust', s.trust, 'category', s.category, 'url', m.url,
                    'published_at', m.published_at, 'collected_at', m.collected_at)
                    ORDER BY COALESCE(m.published_at, m.collected_at) DESC, m.id DESC) AS sources
           FROM unnest(%s::integer[]) WITH ORDINALITY AS page(event_id, ordinal)
           JOIN events e ON e.id = page.event_id
           JOIN event_materials em ON em.event_id = e.id
           JOIN materials m ON m.id = em.material_id
           JOIN sources s ON s.id = m.source_id
            LEFT JOIN relevance_by_event ev ON ev.event_id=e.id
            LEFT JOIN quality_by_finding qa ON qa.finding_id=e.id
            GROUP BY e.id, page.ordinal, ev.classification, ev.relevance, ev.seller_impact,
                     qa.summary_audit, qa.recommendation_audit
            ORDER BY page.ordinal""",
         (active_relevance_version(), _PLATFORM_SPECIFIC_PATTERN, general_platforms, _PLATFORM_CROSS_PATTERN, page_ids),
    ).fetchall()
    names = ('id', 'platform', 'topic', 'importance', 'seller_impact', 'summary', 'recommended_action',
             'verification', 'effective_date', 'updated_at', 'specificity', 'classification', 'relevance',
             'summary_audit', 'recommendation_audit', 'sources')
    findings = [dict(zip(names, row)) for row in rows]
    next_cursor = _encode_findings_cursor(snapshot_id, offset + limit, filter_hash) if has_more else None
    return {'findings': findings, 'total': total, 'next_cursor': next_cursor}


def feed_sources(conn) -> list[dict[str, Any]]:
    rows = conn.execute('SELECT code, name, category, active, last_checked_at, last_error FROM sources ORDER BY name, id').fetchall()
    return [{'code': code, 'name': name, 'category': category,
             'status': 'inactive' if not active else 'error' if error else 'ok' if checked else 'not_checked',
             'last_checked_at': checked.isoformat() if checked else None}
            for code, name, category, active, checked, error in rows]


def start_run(conn, service: str, *, cycle_id: int | None = None) -> int:
    return conn.execute(
        "INSERT INTO runs (service, status, cycle_id) VALUES (%s, 'started', %s) RETURNING id",
        (service, cycle_id),
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

def add_material(conn, source_id: int, run_id: int | None, item: dict[str, Any], *, return_id: bool = False):
    body = (item.get("text") or "").strip()
    fingerprint = hashlib.sha256(
        (item["url"].strip() + "\n" + item["title"].strip() + "\n" + body).encode("utf-8")
    ).hexdigest()
    content_fingerprint = normalized_publication_fingerprint(item["title"], body)
    row = conn.execute(
        """INSERT INTO materials
           (source_id, run_id, url, title, text, published_at, fingerprint, content_fingerprint, translation_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
           ON CONFLICT (fingerprint) DO NOTHING RETURNING id""",
        (source_id, run_id, item["url"], item["title"], body, item.get("published_at"), fingerprint,
         content_fingerprint),
    ).fetchone()
    if not return_id:
        return row is not None
    if row:
        return True, row[0]
    existing = conn.execute("SELECT id FROM materials WHERE fingerprint=%s", (fingerprint,)).fetchone()
    return False, existing[0] if existing else None


def record_trace_attempt(conn, **values) -> int:
    from db.trace import record_attempt
    return record_attempt(conn, **values)


def finding_trace(conn, finding_id: int) -> dict[str, Any] | None:
    from db.trace import finding_trace as fetch_finding_trace
    return fetch_finding_trace(conn, finding_id)


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


def pending_relevant_materials(conn, rules_version: str, limit: int = 50) -> list[dict[str, Any]]:
    if not isinstance(rules_version, str) or not rules_version.strip():
        raise ValueError("Invalid relevance rules version")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Invalid processing limit")
    rows = conn.execute(
        """SELECT m.id, m.url, m.title, m.text, m.published_at, m.translation_status,
                  s.code, s.name, s.trust, s.scope, a.seller_impact
           FROM materials m JOIN sources s ON s.id = m.source_id
           JOIN relevance_assessments a ON a.material_id = m.id AND a.rules_version = %s
           WHERE m.status = 'new' AND a.relevance = 'relevant'
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC LIMIT %s""",
        (rules_version, limit),
    ).fetchall()
    columns = ("material_id", "url", "title", "text", "published_at", "translation_status",
               "source_code", "source", "trust", "scope", "seller_impact")
    return [dict(zip(columns, row)) for row in rows]


def _exact_duplicate_finding_id(conn, material_ids: list[int]) -> int | None:
    rows = conn.execute(
        "SELECT id, content_fingerprint FROM materials WHERE id = ANY(%s) ORDER BY id",
        (material_ids,),
    ).fetchall()
    if len(rows) != len(set(material_ids)):
        return None
    fingerprints = {row[1] for row in rows}
    if len(fingerprints) != 1 or None in fingerprints:
        return None
    fingerprint = next(iter(fingerprints))
    matches = conn.execute(
        """SELECT DISTINCT em.event_id
           FROM event_materials em JOIN materials existing ON existing.id=em.material_id
           WHERE existing.content_fingerprint=%s AND existing.id <> ALL(%s)
           ORDER BY em.event_id""",
        (fingerprint, material_ids),
    ).fetchall()
    return matches[0][0] if len(matches) == 1 else None


def _manual_duplicate_matches_signal(conn, event_id: int, signal: dict[str, Any], platform: str) -> bool:
    row = conn.execute(
        "SELECT platform, topic, summary, recommended_action FROM events WHERE id=%s",
        (event_id,),
    ).fetchone()
    return row == (platform, signal["topic"], signal["summary"], signal["recommended_action"])


def _collision_safe_event_key(conn, key: str, material_ids: list[int], signal: dict[str, Any], platform: str) -> str:
    rows = conn.execute(
        "SELECT fingerprint FROM materials WHERE id = ANY(%s) ORDER BY fingerprint",
        (material_ids,),
    ).fetchall()
    material_fingerprints = [row[0] for row in rows]
    identity = json.dumps(
        [material_fingerprints, platform, signal["topic"], signal["summary"], signal["recommended_action"]],
        ensure_ascii=False, separators=(",", ":"),
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{key}|material:{suffix}"


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
        impact = signal.get("seller_impact", "unclear")
        if impact not in SELLER_IMPACTS:
            raise ValueError("Invalid seller impact")
        platform = normalize_event_platform(signal["platform"])
        ids = signal["material_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(value, int) for value in ids):
            raise ValueError("material_ids должен быть непустым списком идентификаторов")
        key = signal.get("event_key") or "|".join(
            [signal["platform"].strip().lower(), signal["topic"].strip().lower(), signal["summary"].strip().lower()]
        )
        event_id = _exact_duplicate_finding_id(conn, ids)
        if event_id is not None and not reject_confirmed:
            if not _manual_duplicate_matches_signal(conn, event_id, signal, platform):
                event_id = None
            elif signal["verification"] == "confirmed":
                conn.execute(
                    "UPDATE events SET verification='confirmed', updated_at=now() "
                    "WHERE id=%s AND verification='needs_check'",
                    (event_id,),
                )
        was_created = False
        if event_id is None:
            event = conn.execute(
                """INSERT INTO events
                   (event_key, platform, topic, importance, seller_impact, summary, recommended_action, verification, effective_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (event_key) DO NOTHING RETURNING id""",
                (key, platform, signal["topic"], signal["importance"], impact, signal["summary"],
                 signal["recommended_action"], signal["verification"], signal.get("effective_date")),
            ).fetchone()
            if event is None:
                if reject_confirmed:
                    unconfirmed = conn.execute(
                        "SELECT id FROM events WHERE event_key=%s AND events.verification <> 'confirmed'",
                        (key,),
                    ).fetchone()
                    if unconfirmed is None:
                        raise ValueError("model event collides with confirmed event")
                safe_key = _collision_safe_event_key(conn, key, ids, signal, platform)
                event = conn.execute(
                    """INSERT INTO events
                       (event_key, platform, topic, importance, seller_impact, summary, recommended_action, verification, effective_date)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (event_key) DO NOTHING RETURNING id""",
                    (safe_key, platform, signal["topic"], signal["importance"], impact, signal["summary"],
                     signal["recommended_action"], signal["verification"], signal.get("effective_date")),
                ).fetchone()
                if event is None:
                    raise ValueError("Could not isolate conflicting model event")
            event_id = event[0]
            was_created = True
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


def start_backup_run(conn) -> int:
    return conn.execute(
        "INSERT INTO backup_runs (status) VALUES ('started') RETURNING id"
    ).fetchone()[0]


def finish_backup_run(conn, run_id: int, *, status: str, file_name: str | None = None,
                      size_bytes: int | None = None, sha256: str | None = None,
                      error_code: str | None = None) -> None:
    if status not in {"success", "failed"}:
        raise ValueError("invalid backup status")
    conn.execute(
        """UPDATE backup_runs SET status=%s, file_name=%s, finished_at=now(), size_bytes=%s,
                  sha256=%s, error_code=%s WHERE id=%s""",
        (status, file_name, size_bytes, sha256, error_code, run_id),
    )


def latest_backup_status(conn) -> dict[str, Any]:
    row = conn.execute(
        """SELECT
               (SELECT status FROM backup_runs ORDER BY id DESC LIMIT 1),
               (SELECT error_code FROM backup_runs ORDER BY id DESC LIMIT 1),
               (SELECT finished_at FROM backup_runs WHERE status='success' ORDER BY id DESC LIMIT 1),
               (SELECT finished_at FROM backup_runs WHERE status='success' ORDER BY id ASC LIMIT 1),
               (SELECT size_bytes FROM backup_runs WHERE status='success' ORDER BY id DESC LIMIT 1),
               (SELECT sha256 FROM backup_runs WHERE status='success' ORDER BY id DESC LIMIT 1),
               (SELECT restore_status FROM backup_runs WHERE status='success' AND restore_checked_at IS NOT NULL
                ORDER BY restore_checked_at DESC, id DESC LIMIT 1),
               (SELECT restore_checked_at FROM backup_runs WHERE status='success' AND restore_checked_at IS NOT NULL
                ORDER BY restore_checked_at DESC, id DESC LIMIT 1)"""
    ).fetchone()
    keys = ("last_status", "last_error_code", "last_success_at", "first_success_at", "last_success_size_bytes",
            "last_success_sha256", "last_restore_status", "last_restore_checked_at")
    return dict(zip(keys, row))


def mark_backup_restore(conn, run_id: int, *, status: str, error_code: str | None = None) -> None:
    if status not in {"success", "failed"}:
        raise ValueError("invalid restore status")
    conn.execute(
        """UPDATE backup_runs SET restore_status=%s, restore_checked_at=now(),
                  error_code=COALESCE(%s, error_code) WHERE id=%s AND status='success'""",
        (status, error_code, run_id),
    )


def pending_relevance_materials(conn, rules_version: str, limit: int = 200) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Invalid relevance assessment limit")
    rows = conn.execute(
        """SELECT m.id, m.url, m.title, m.text, m.published_at, m.status,
                  s.code, s.name, s.trust, s.scope
           FROM materials m JOIN sources s ON s.id = m.source_id
           WHERE NOT EXISTS (
                 SELECT 1 FROM relevance_assessments a
                 WHERE a.material_id = m.id AND a.rules_version = %s
             )
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC LIMIT %s""",
        (rules_version, limit),
    ).fetchall()
    columns = ("material_id", "url", "title", "text", "published_at", "status", "source_code", "source", "trust", "scope")
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
        relevance = assessment.get("relevance", {
            "practical_change": "relevant", "seller_action_or_risk": "relevant",
            "industry_context": "irrelevant", "irrelevant": "irrelevant", "uncertain": "uncertain",
        }.get(assessment.get("classification")))
        if relevance not in RELEVANCE_CLASSES:
            raise ValueError("Invalid relevance result")
        if assessment.get("seller_impact", "unclear") not in SELLER_IMPACTS:
            raise ValueError("Invalid seller impact result")
        if not isinstance(assessment.get("topic", ""), str):
            raise ValueError("Invalid relevance topic")
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
                   (material_id, rules_version, classification, relevance, topic, seller_impact,
                    score, rule_codes, platforms, explanation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (material_id, rules_version) DO NOTHING
                   RETURNING id""",
                (item["material_id"], item["rules_version"], item["classification"],
                 item.get("relevance", {
                     "practical_change": "relevant", "seller_action_or_risk": "relevant",
                     "industry_context": "irrelevant", "irrelevant": "irrelevant", "uncertain": "uncertain",
                 }.get(item["classification"])), item.get("topic", ""), item.get("seller_impact", "unclear"),
                 item["score"], item["rule_codes"], item["platforms"], item["explanation"][:300]),
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
        label_type = item.get(
            "label_type",
            "relevance" if label == "relevant" or (
                label in ("irrelevant", "uncertain") and ("platforms" in item or "seller_impact" in item)
            ) else "classification",
        )
        allowed_labels = CLASSIFICATIONS if label_type == "classification" else RELEVANCE_CLASSES
        if (type(material_id) is not int or material_id < 1 or label_type not in ("classification", "relevance")
                or label not in allowed_labels):
            raise ValueError("Invalid relevance label")
        if not isinstance(item.get("platforms", []), list) or any(
            not isinstance(value, str) or not value.strip() for value in item.get("platforms", [])
        ):
            raise ValueError("Invalid labeled platforms")
        if item.get("seller_impact", "unclear") not in SELLER_IMPACTS:
            raise ValueError("Invalid labeled seller impact")
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
                """INSERT INTO relevance_labels (material_id, label, label_type, platforms, seller_impact)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (material_id) DO UPDATE SET label = EXCLUDED.label,
                     label_type = EXCLUDED.label_type,
                     platforms = EXCLUDED.platforms, seller_impact = EXCLUDED.seller_impact, labeled_at = now()""",
                (item["material_id"], item["label"],
                 item.get("label_type", "relevance" if item["label"] == "relevant"
                          or (item["label"] in ("irrelevant", "uncertain")
                              and ("platforms" in item or "seller_impact" in item)) else "classification"),
                 item.get("platforms", []), item.get("seller_impact", "unclear")),
            )
    return len(labels)


def relevance_evaluation_rows(conn, rules_version: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.id, m.title,
                  CASE WHEN l.label_type='relevance' THEN a.relevance ELSE a.classification END,
                  l.label, l.label_type, l.platforms, s.kind
           FROM relevance_labels l
           JOIN relevance_assessments a ON a.material_id = l.material_id AND a.rules_version = %s
           JOIN materials m ON m.id = l.material_id
           JOIN sources s ON s.id=m.source_id
           ORDER BY m.id""",
        (rules_version,),
    ).fetchall()
    columns = ("material_id", "title", "predicted", "label", "metric_type", "platforms", "source_type")
    return [dict(zip(columns, row)) for row in rows]


def audit_sample(conn, *, limit: int):
    from db.finding_quality import audit_sample as fetch_sample
    return fetch_sample(conn, limit=limit)


def create_finding_quality_run(conn, *, limit: int, model: str, rules_version: str) -> int:
    from db.finding_quality import create_run
    return create_run(conn, limit=limit, model=model, rules_version=rules_version)


def save_finding_quality_results(conn, run_id: int, results):
    from db.finding_quality import save_results
    return save_results(conn, run_id, results)


def save_finding_quality_labels(conn, run_id: int, labels):
    from db.finding_quality import save_labels
    return save_labels(conn, run_id, labels)


def finish_finding_quality_run(conn, run_id: int, **values):
    from db.finding_quality import finish_run
    return finish_run(conn, run_id, **values)


def finding_quality_label_rows(conn, run_id: int):
    from db.finding_quality import finding_quality_label_rows as fetch_rows
    return fetch_rows(conn, run_id)


def finding_quality_report_rows(conn, run_id: int):
    from db.finding_quality import finding_quality_report_rows as fetch_rows
    return fetch_rows(conn, run_id)


def finding_quality_run(conn, run_id: int):
    from db.finding_quality import finding_quality_run as fetch_run
    return fetch_run(conn, run_id)
