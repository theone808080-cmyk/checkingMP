"""Append-only storage and sampling for finding quality audits."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from psycopg.types.json import Jsonb


def audit_sample(conn, *, limit: int) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Audit sample limit must be between 1 and 50")
    rows = conn.execute(
        """SELECT e.id, e.platform, e.topic, e.importance, e.summary,
                  e.recommended_action, e.effective_date, e.created_at,
                  MIN(s.kind) AS source_kind,
                  jsonb_agg(jsonb_build_object(
                      'material_id', m.id, 'source', s.name, 'source_kind', s.kind,
                      'url', m.url, 'title', m.title, 'translated_title', m.title_ru,
                      'text', COALESCE(m.text, ''), 'translation', COALESCE(m.text_ru, '')
                  ) ORDER BY m.id) AS materials
           FROM events e
           JOIN event_materials em ON em.event_id = e.id
           JOIN materials m ON m.id = em.material_id
           JOIN sources s ON s.id = m.source_id
           GROUP BY e.id
           ORDER BY e.created_at DESC, e.id DESC"""
    ).fetchall()
    names = ("finding_id", "platform", "topic", "importance", "summary",
             "recommended_action", "effective_date", "created_at", "source_kind", "materials")
    candidates = [dict(zip(names, row)) for row in rows]
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        key = (candidate["platform"], candidate["source_kind"], candidate["importance"])
        groups[key].append(candidate)

    sample = []
    ordered_groups = sorted(groups)
    while len(sample) < limit:
        added = False
        for key in ordered_groups:
            if groups[key] and len(sample) < limit:
                sample.append(groups[key].pop(0))
                added = True
        if not added:
            break
    return sample


def create_run(conn, *, limit: int, model: str, rules_version: str) -> int:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Audit limit must be between 1 and 50")
    if not isinstance(model, str) or len(model) > 200:
        raise ValueError("Invalid audit model")
    if not isinstance(rules_version, str) or not rules_version.strip() or len(rules_version) > 80:
        raise ValueError("Invalid audit rules version")
    return conn.execute(
        """INSERT INTO finding_quality_runs (rules_version, model, requested_limit)
           VALUES (%s, %s, %s) RETURNING id""",
        (rules_version, model, limit),
    ).fetchone()[0]


def register_run_sample(conn, run_id: int, finding_ids: Iterable[int]) -> int:
    finding_ids = list(finding_ids)
    if any(type(finding_id) is not int or finding_id < 1 for finding_id in finding_ids):
        raise ValueError("Invalid audit sample finding id")
    if len(set(finding_ids)) != len(finding_ids):
        raise ValueError("Audit sample contains duplicate findings")
    with conn.transaction():
        if not conn.execute(
            "SELECT 1 FROM finding_quality_runs WHERE id=%s AND status='started'", (run_id,)
        ).fetchone():
            raise ValueError("Audit run is missing or already finished")
        eligible = {row[0] for row in conn.execute(
            """SELECT DISTINCT em.event_id FROM event_materials em
               WHERE em.event_id=ANY(%s)""", (finding_ids,)
        ).fetchall()} if finding_ids else set()
        if eligible != set(finding_ids):
            raise ValueError("Audit sample includes findings without linked materials")
        for finding_id in finding_ids:
            conn.execute(
                "INSERT INTO finding_quality_run_findings (run_id, finding_id) VALUES (%s, %s)",
                (run_id, finding_id),
            )
    return len(finding_ids)


def finish_run(conn, run_id: int, *, status: str, finding_count: int, elapsed_ms: int,
               error_code: str | None = None, prompt_tokens: int | None = None,
               completion_tokens: int | None = None, cost: float | None = None) -> None:
    if status not in {"success", "partial", "failed"}:
        raise ValueError("Invalid audit run status")
    if type(finding_count) is not int or finding_count < 0 or type(elapsed_ms) is not int or elapsed_ms < 0:
        raise ValueError("Invalid audit run metrics")
    for value in (prompt_tokens, completion_tokens):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Invalid token usage")
    if cost is not None and (type(cost) not in (int, float) or cost < 0):
        raise ValueError("Invalid cost")
    conn.execute(
        """UPDATE finding_quality_runs
           SET status=%s, finding_count=%s, error_code=%s, finished_at=now(), elapsed_ms=%s,
               prompt_tokens=%s, completion_tokens=%s, cost=%s
           WHERE id=%s AND status='started'""",
        (status, finding_count, error_code, elapsed_ms, prompt_tokens, completion_tokens, cost, run_id),
    )


def _normal_source(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _validate_issue(issue: Any, materials: dict[int, tuple[str, ...]]) -> dict[str, Any]:
    issue_types = {"unsupported_fact", "omitted_condition", "omitted_deadline",
                   "unsupported_action", "contradiction", "insufficient_source"}
    severities = {"low", "medium", "high"}
    if not isinstance(issue, dict) or issue.get("type") not in issue_types or issue.get("severity") not in severities:
        raise ValueError("Invalid quality issue")
    material_id = issue.get("material_id")
    if type(material_id) is not int or material_id not in materials:
        raise ValueError("Invalid quality issue material")
    evidence = issue.get("evidence")
    explanation = issue.get("explanation")
    if not isinstance(evidence, str) or len(evidence) > 300 or not isinstance(explanation, str) or len(explanation) > 500:
        raise ValueError("Invalid quality issue text")
    if not evidence.strip() or not any(
            _normal_source(evidence) in _normal_source(source_text)
            for source_text in materials[material_id]):
        raise ValueError("Quality issue evidence is not a source quotation")
    return {"type": issue["type"], "severity": issue["severity"], "material_id": material_id,
            "evidence": evidence, "explanation": explanation}


def save_results(conn, run_id: int, results: Iterable[dict[str, Any]]) -> int:
    results = list(results)
    seen: set[tuple[int, str]] = set()
    normalized = []
    # Validate evidence references against linked materials before any insert.
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("Invalid quality result")
        finding_id = item.get("finding_id")
        target = item.get("target")
        status = item.get("status")
        key = (finding_id, target)
        if (type(finding_id) is not int or finding_id < 1 or target not in {"summary", "recommendation"} or
                status not in {"ok", "review", "issue"} or key in seen):
            raise ValueError("Invalid quality result identity or status")
        seen.add(key)
        sample_member = conn.execute(
            "SELECT 1 FROM finding_quality_run_findings WHERE run_id=%s AND finding_id=%s",
            (run_id, finding_id),
        ).fetchone()
        if not sample_member:
            raise ValueError("Quality result finding is outside the audit sample")
        material_rows = conn.execute(
            """SELECT m.id, m.title, m.title_ru, m.text, m.text_ru
               FROM event_materials em JOIN materials m ON m.id=em.material_id
               WHERE em.event_id=%s""", (finding_id,)
        ).fetchall()
        materials = {row[0]: tuple(value or "" for value in row[1:]) for row in material_rows}
        issues = item.get("issues", [])
        if not isinstance(issues, list) or len(issues) > 10:
            raise ValueError("Invalid quality issue list")
        clean_issues = [_validate_issue(issue, materials) for issue in issues]
        if status == "issue" and all(issue["type"] == "insufficient_source" for issue in clean_issues):
            raise ValueError("Insufficient source must be recorded as review")
        error_code = item.get("error_code")
        if error_code is not None and error_code not in {
            "configuration_missing", "request_rejected", "transport_failed", "invalid_response", "source_incomplete"
        }:
            raise ValueError("Invalid safe quality error code")
        if ((status == "ok" and (clean_issues or error_code is not None)) or
                (status == "issue" and (not clean_issues or error_code is not None)) or
                (status == "review" and not clean_issues and error_code is None) or
                (error_code is not None and (status != "review" or clean_issues))):
            raise ValueError("Quality result status, issues, and error code are inconsistent")
        elapsed_ms = item.get("elapsed_ms")
        if elapsed_ms is not None and (type(elapsed_ms) is not int or elapsed_ms < 0):
            raise ValueError("Invalid quality duration")
        normalized.append((run_id, finding_id, target, status, Jsonb(clean_issues), error_code, elapsed_ms))
    if not normalized:
        return 0
    with conn.transaction():
        for item in normalized:
            conn.execute(
                """INSERT INTO finding_quality_results
                   (run_id, finding_id, target, status, issues, error_code, elapsed_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""", item,
            )
    return len(normalized)


def save_labels(conn, run_id: int, labels: Iterable[dict[str, Any]]) -> int:
    labels = list(labels)
    seen = set()
    normalized = []
    for item in labels:
        if not isinstance(item, dict):
            raise ValueError("Invalid quality label")
        finding_id = item.get("finding_id")
        facts = item.get("summary_facts")
        omitted_condition = item.get("omitted_condition")
        omitted_deadline = item.get("omitted_deadline")
        completeness_uncertain = item.get("completeness_uncertain")
        recommendation = item.get("recommendation")
        if (type(finding_id) is not int or finding_id < 1 or finding_id in seen or
                facts not in {"ok", "issue", "uncertain"} or type(omitted_condition) is not bool or
                type(omitted_deadline) is not bool or type(completeness_uncertain) is not bool or
                recommendation not in {"supported", "unsupported", "uncertain"}):
            raise ValueError("Invalid quality label values")
        seen.add(finding_id)
        normalized.append((run_id, finding_id, facts, omitted_condition, omitted_deadline,
                           completeness_uncertain, recommendation))
    if not normalized:
        return 0
    with conn.transaction():
        eligible = {row[0] for row in conn.execute(
            """SELECT finding_id FROM finding_quality_run_findings
               WHERE run_id=%s AND finding_id=ANY(%s)""", (run_id, list(seen))
        ).fetchall()}
        if eligible != seen:
            raise ValueError("Labels include findings outside the audit run")
        for item in normalized:
            conn.execute(
                """INSERT INTO finding_quality_labels
                   (run_id, finding_id, summary_facts, omitted_condition, omitted_deadline,
                    completeness_uncertain, recommendation)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""", item,
            )
    return len(normalized)


def finding_quality_label_rows(conn, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT e.id, e.platform, e.topic, e.importance, e.summary, e.recommended_action,
                  COALESCE((SELECT jsonb_agg(jsonb_build_object(
                      'material_id', m.id, 'source', s.name, 'source_kind', s.kind,
                      'url', m.url, 'title', m.title, 'translated_title', m.title_ru,
                      'text', COALESCE(m.text, ''), 'translation', COALESCE(m.text_ru, '')
                  ) ORDER BY m.id)
                    FROM event_materials em JOIN materials m ON m.id=em.material_id
                    JOIN sources s ON s.id=m.source_id WHERE em.event_id=e.id), '[]'::jsonb) AS materials
           FROM events e JOIN finding_quality_results r ON r.finding_id=e.id
           WHERE r.run_id=%s GROUP BY e.id ORDER BY e.id""", (run_id,)
    ).fetchall()
    names = ("finding_id", "platform", "topic", "importance", "summary", "recommended_action", "materials")
    return [dict(zip(names, row)) for row in rows]


def finding_quality_report_rows(conn, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.finding_id, r.target, r.status, r.issues,
                  CASE WHEN r.target='summary' THEN
                       CASE WHEN l.summary_facts='issue' OR l.omitted_condition OR l.omitted_deadline THEN 'issue'
                            WHEN l.summary_facts='ok' AND NOT l.completeness_uncertain THEN 'ok'
                            ELSE 'uncertain' END
                       ELSE CASE l.recommendation WHEN 'supported' THEN 'ok'
                                                  WHEN 'unsupported' THEN 'issue'
                                                  ELSE 'uncertain' END END AS manual,
                  e.platform, e.importance, e.summary, e.recommended_action,
                  (SELECT m.url FROM event_materials em JOIN materials m ON m.id=em.material_id
                   WHERE em.event_id=e.id ORDER BY m.id LIMIT 1) AS source_url,
                  (SELECT s.kind FROM event_materials em JOIN materials m ON m.id=em.material_id
                   JOIN sources s ON s.id=m.source_id WHERE em.event_id=e.id ORDER BY m.id LIMIT 1) AS source_kind
           FROM finding_quality_results r JOIN events e ON e.id=r.finding_id
           LEFT JOIN finding_quality_labels l ON l.run_id=r.run_id AND l.finding_id=r.finding_id
           WHERE r.run_id=%s ORDER BY r.finding_id, r.target""", (run_id,)
    ).fetchall()
    names = ("finding_id", "target", "automatic", "issues", "manual", "platform", "importance",
             "summary", "recommended_action", "source_url", "source_kind")
    return [dict(zip(names, row)) for row in rows]


def finding_quality_run(conn, run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, rules_version, model, requested_limit, status, finding_count, error_code,
                  started_at, finished_at, elapsed_ms, prompt_tokens, completion_tokens, cost
           FROM finding_quality_runs WHERE id=%s""", (run_id,)
    ).fetchone()
    if row is None:
        return None
    names = ("run_id", "rules_version", "model", "requested_limit", "status", "finding_count",
             "error_code", "started_at", "finished_at", "elapsed_ms", "prompt_tokens",
             "completion_tokens", "cost")
    return dict(zip(names, row))
