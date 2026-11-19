"""Persist complete tariff snapshots and create deterministic owner-review findings."""

from __future__ import annotations

import hashlib
import json
import re

from db.impacts import create_finding_impact
from collector.tariff_snapshots import compare_tariff_snapshots

SOURCE_URLS = {
    "wildberries_tariffs": "https://seller.wildberries.ru/instructions/ru/tj/material/fees-site-section",
    "yandex_market_tariffs": "https://yandex.ru/dev/market/partner-api/doc/ru/reference/tariffs/calculateTariffs",
}


def _source_url(source_code: str) -> str | None:
    if source_code in SOURCE_URLS:
        return SOURCE_URLS[source_code]
    if source_code.startswith("wb_"):
        return SOURCE_URLS["wildberries_tariffs"]
    if source_code.startswith("yandex_"):
        return SOURCE_URLS["yandex_market_tariffs"]
    return None


def apply_tariff_snapshot(conn, *, source_id: int, source_code: str, rows: list[dict]) -> dict:
    """Save one complete read and create findings/cards only for measured differences."""
    if (type(source_id) is not int or source_id < 1 or _source_url(source_code) is None
            or not isinstance(rows, list)):
        raise ValueError("invalid tariff snapshot")
    _validate_rows(rows)
    with conn.transaction():
        return _apply_locked_snapshot(conn, source_id=source_id, source_code=source_code, rows=rows)


def _apply_locked_snapshot(conn, *, source_id: int, source_code: str, rows: list[dict]) -> dict:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"tariffs:{source_code}",))
    previous = _latest_snapshot(conn, source_code)
    deltas = compare_tariff_snapshots(previous, rows)
    run_id = conn.execute(
        "INSERT INTO marketplace_tariff_snapshot_runs(source_code,row_count) VALUES (%s,%s) RETURNING id",
        (source_code, len(rows)),
    ).fetchone()[0]
    for row in rows:
        conn.execute(
            """INSERT INTO marketplace_tariff_snapshots
               (snapshot_id,stable_key,platform,scope,service,amount,unit,approximate,comparison_basis)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, row["stable_key"], row["platform"], row["scope"], row["service"],
             row["amount"], row["unit"], bool(row.get("approximate", False)), row.get("comparison_basis")),
        )
    if previous is None:
        return {"baseline": True, "findings_created": 0, "snapshot_id": run_id}

    before = {row["stable_key"]: row for row in previous}
    after = {row["stable_key"]: row for row in rows}
    created = 0
    grouped = {}
    for delta in deltas:
        row = after.get(delta["stable_key"]) or before[delta["stable_key"]]
        group_key = (row["platform"], row["scope"], row["service"])
        grouped.setdefault(group_key, []).append((delta, row))
    for (platform, scope, service), changes in sorted(grouped.items()):
        created += _create_finding(conn, source_id, source_code, run_id,
                                   platform, scope, service, changes)
    return {"baseline": False, "findings_created": created, "snapshot_id": run_id}


def _validate_rows(rows: list[dict]) -> None:
    seen = set()
    required = {"stable_key", "platform", "scope", "service", "amount", "unit"}
    for row in rows:
        if (not isinstance(row, dict) or not required.issubset(row)
                or any(not isinstance(row[key], str) or not row[key].strip() for key in required)
                or row["stable_key"] in seen):
            raise ValueError("invalid tariff snapshot")
        seen.add(row["stable_key"])


def _latest_snapshot(conn, source_code: str) -> list[dict] | None:
    run = conn.execute(
        "SELECT id FROM marketplace_tariff_snapshot_runs WHERE source_code=%s ORDER BY id DESC LIMIT 1",
        (source_code,),
    ).fetchone()
    if run is None:
        return None
    rows = conn.execute(
        """SELECT stable_key,platform,scope,service,amount,unit,approximate,comparison_basis
           FROM marketplace_tariff_snapshots WHERE snapshot_id=%s ORDER BY stable_key""",
        (run[0],),
    ).fetchall()
    return [dict(zip(("stable_key", "platform", "scope", "service", "amount", "unit", "approximate", "comparison_basis"), row))
            for row in rows]


def _create_finding(conn, source_id, source_code, run_id, platform, scope, service, changes) -> int:
    observations = []
    for delta, row in changes[:4]:
        observations.append(f"{delta['before'] or 'нет тарифа'} → {delta['after'] or 'нет тарифа'} {row['unit']}")
    detail = "; ".join(observations)
    if len(changes) > len(observations):
        detail += f"; ещё {len(changes) - len(observations)} изменений"
    approximate = any(delta["approximate"] for delta, _ in changes)
    approx_label = " (оценка калькулятора; не фактическое списание)" if approximate else ""
    safe_scope = scope.split(":", 1)[-1].strip()[:180]
    if scope.startswith("campaign:"):
        safe_scope = f"Кампания {safe_scope}"
    title = f"Изменение тарифа {platform}: {service} — {safe_scope}"
    summary = f"Обнаружено изменение тарифа: {detail}{approx_label}."
    action = "Проверьте первоисточник и подтвердите влияние на складские процессы."
    text = f"{summary}\nИсточник данных: {source_code}."
    url = _source_url(source_code)
    fingerprint = hashlib.sha256(f"{url}\n{title}\n{text}".encode("utf-8")).hexdigest()
    material_id = conn.execute(
        """INSERT INTO materials
           (source_id,url,title,text,fingerprint,content_fingerprint,status,translation_status,title_ru,text_ru)
           VALUES (%s,%s,%s,%s,%s,%s,'done','ready',%s,%s)
           ON CONFLICT(fingerprint) DO NOTHING RETURNING id""",
        (source_id, url, title, text, fingerprint,
         hashlib.sha256(f"{title}\n{text}".encode("utf-8")).hexdigest(), title, text),
    ).fetchone()
    if material_id is None:
        material_id = conn.execute("SELECT id FROM materials WHERE fingerprint=%s", (fingerprint,)).fetchone()[0]
    material_id = material_id[0] if isinstance(material_id, tuple) else material_id
    key_data = json.dumps([source_code, platform, scope, service,
                           [(delta["before"], delta["after"]) for delta, _ in changes]],
                          ensure_ascii=False, separators=(",", ":"))
    event_key = f"tariff:{hashlib.sha256(key_data.encode('utf-8')).hexdigest()}"
    event_row = conn.execute(
        """INSERT INTO events(event_key,platform,topic,importance,seller_impact,summary,recommended_action,verification)
           VALUES (%s,%s,'tariffs',5,'negative',%s,%s,'needs_check')
           ON CONFLICT(event_key) DO NOTHING RETURNING id""",
        (event_key, platform, summary, action),
    ).fetchone()
    created = event_row is not None
    event_id = event_row[0] if event_row else conn.execute(
        "SELECT id FROM events WHERE event_key=%s", (event_key,),
    ).fetchone()[0]
    conn.execute("INSERT INTO event_materials(event_id,material_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                 (event_id, material_id))
    if not conn.execute("SELECT 1 FROM finding_impacts WHERE event_id=%s", (event_id,)).fetchone():
        create_finding_impact(conn, event_id, {
            "platform": platform,
            "object_type": ("marketplace" if scope.startswith("campaign:") else
                            "sorting_center" if re.search(r"(^|[: ])(СЦ|SC)(?:[ -]|$)", safe_scope, re.I)
                            else "seller_warehouse"),
            "object_name": safe_scope,
            "change_type": _change_type(service),
            "effective_date": None,
            "processes": _processes(service),
            "consequence": f"Изменено тарифов: {len(changes)}. Примеры: {detail}{approx_label}. Возможные последствия для склада требуют подтверждения владельцем.",
            "source_url": url,
        })
    return int(created)


def _change_type(service: str) -> str:
    service = service.lower()
    if "commission" in service:
        return "commission"
    if "storage" in service:
        return "storage"
    if "acceptance" in service:
        return "acceptance"
    if "return" in service:
        return "returns"
    if "delivery" in service or "logistic" in service:
        return "delivery_tariff"
    return "other"


def _processes(service: str) -> list[str]:
    kind = _change_type(service)
    return {"commission": ["costs"], "storage": ["storage", "costs"],
            "acceptance": ["acceptance", "inbound"], "returns": ["returns", "costs"],
            "delivery_tariff": ["shipment", "delivery", "costs"]}.get(kind, ["other"])
