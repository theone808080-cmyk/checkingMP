"""Safe material-to-finding processing history."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


STAGES = {"collection", "translation", "relevance", "analysis"}
STATUSES = {"success", "failed", "skipped"}
ERRORS = {
    "source_fetch_failed": "Не удалось получить или прочитать источник.",
    "translation_failed": "Перевод не завершён.",
    "relevance_failed": "Локальная оценка релевантности не завершена.",
    "analysis_failed": "ИИ-анализ не завершён.",
    "persistence_failed": "Результат этапа не сохранён.",
}
LABEL = re.compile(r"[A-Za-z0-9._:/+-]{1,120}\Z")


def _timestamp(value: datetime | None, name: str) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result


def _label(value: str | None) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and LABEL.fullmatch(value) else None


def _safe_source_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        query = [(key, "[redacted]" if re.search(r"token|key|auth|password|secret|signature|(^|_)sig($|_)", key, re.I) else item)
                 for key, item in parse_qsl(parsed.query, keep_blank_values=True)]
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
    except ValueError:
        return "invalid source URL"


def record_attempt(conn, *, cycle_id: int | None, run_id: int | None,
                   source_id: int | None, material_id: int | None,
                   stage: str, status: str, started_at: datetime | None = None,
                   finished_at: datetime | None = None, error_code: str | None = None,
                   provider: str | None = None, model: str | None = None,
                   rule_version: str | None = None) -> int:
    if stage not in STAGES or status not in STATUSES:
        raise ValueError("invalid trace stage or status")
    started = _timestamp(started_at, "started_at")
    finished = _timestamp(finished_at, "finished_at")
    if finished < started:
        raise ValueError("trace finish precedes start")
    summary = None
    if status == "failed":
        if error_code not in ERRORS:
            raise ValueError("invalid trace error code")
        summary = ERRORS[error_code]
    elif error_code is not None:
        raise ValueError("successful trace cannot include an error")
    row = conn.execute(
        """INSERT INTO processing_trace
           (cycle_id, run_id, source_id, material_id, stage, status, started_at, finished_at,
            error_code, error_summary, provider, model, rule_version)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (cycle_id, run_id, source_id, material_id, stage, status, started, finished,
         error_code, summary, _label(provider), _label(model), _label(rule_version)),
    ).fetchone()
    return row[0]


def finding_trace(conn, finding_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, platform, topic, importance, verification, effective_date, updated_at
           FROM events WHERE id=%s""",
        (finding_id,),
    ).fetchone()
    if not row:
        return None
    finding = dict(zip(("id", "platform", "topic", "importance", "verification", "effective_date", "updated_at"), row))
    materials = conn.execute(
        """SELECT m.id, m.title, m.url, m.published_at, s.id, s.code, s.name, s.trust
           FROM event_materials em JOIN materials m ON m.id=em.material_id
           JOIN sources s ON s.id=m.source_id
           WHERE em.event_id=%s ORDER BY m.id""",
        (finding_id,),
    ).fetchall()
    material_rows = []
    material_ids = []
    for values in materials:
        item = dict(zip(("material_id", "title", "url", "published_at", "source_id", "source_code", "source", "trust"), values))
        item["title"] = item["title"][:500]
        item["url"] = _safe_source_url(item["url"])
        item["attempts"] = []
        material_ids.append(item["material_id"])
        material_rows.append(item)
    if material_ids:
        attempts = conn.execute(
            """SELECT id, cycle_id, run_id, source_id, material_id, stage, status, started_at,
                      finished_at, error_code, error_summary, provider, model, rule_version
               FROM processing_trace WHERE material_id = ANY(%s) ORDER BY id""",
            (material_ids,),
        ).fetchall()
        by_id = {item["material_id"]: item for item in material_rows}
        columns = ("id", "cycle_id", "run_id", "source_id", "material_id", "stage", "status",
                   "started_at", "finished_at", "error_code", "error_summary", "provider", "model", "rule_version")
        for attempt in attempts:
            item = dict(zip(columns, attempt))
            by_id[item["material_id"]]["attempts"].append(item)
    return {"finding": finding, "materials": material_rows}
