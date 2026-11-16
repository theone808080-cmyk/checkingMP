"""Owner-reviewed warehouse impact cards attached to marketplace findings."""

from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import urlsplit


FIELDS = {"platform", "object_type", "object_name", "change_type", "effective_date",
          "processes", "consequence", "source_url"}
PLATFORMS = {"Ozon", "Wildberries", "Яндекс Маркет", "Честный знак", "Межплощадочная", "Отрасль"}
OBJECT_TYPES = {"marketplace", "sorting_center", "seller_warehouse", "other"}
CHANGE_TYPES = {"delivery_tariff", "commission", "acceptance", "storage", "dispatch", "returns", "logistics_rule", "other"}
PROCESSES = {"inbound", "acceptance", "storage", "inventory", "shipment", "delivery", "returns", "labeling", "costs", "other"}
DECISIONS = {"confirmed", "rejected"}
MAX_TEXT = 2000


def _validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("invalid impact card")
    if (not isinstance(payload["platform"], str) or payload["platform"] not in PLATFORMS
            or not isinstance(payload["object_type"], str) or payload["object_type"] not in OBJECT_TYPES):
        raise ValueError("invalid impact card")
    if (payload["change_type"] is not None
            and (not isinstance(payload["change_type"], str) or payload["change_type"] not in CHANGE_TYPES)):
        raise ValueError("invalid impact card")
    object_name = payload["object_name"]
    if object_name is not None and (not isinstance(object_name, str) or len(object_name) > 200):
        raise ValueError("invalid impact card")
    if isinstance(object_name, str):
        object_name = object_name.strip() or None
    processes = payload["processes"]
    if (not isinstance(processes, list) or len(processes) > len(PROCESSES)
            or any(not isinstance(value, str) or value not in PROCESSES for value in processes)):
        raise ValueError("invalid impact card")
    if len(set(processes)) != len(processes):
        raise ValueError("invalid impact card")
    consequence = payload["consequence"]
    if not isinstance(consequence, str) or len(consequence.strip()) > MAX_TEXT:
        raise ValueError("invalid impact card")
    effective_date = payload["effective_date"]
    if isinstance(effective_date, str):
        try:
            effective_date = date.fromisoformat(effective_date)
        except ValueError:
            raise ValueError("invalid impact card") from None
    if effective_date is not None and not isinstance(effective_date, date):
        raise ValueError("invalid impact card")
    source_url = payload["source_url"]
    try:
        parsed_url = urlsplit(source_url)
        if (not isinstance(source_url, str) or parsed_url.scheme != "https" or not parsed_url.hostname
                or parsed_url.username or parsed_url.password or parsed_url.fragment):
            raise ValueError("invalid impact card")
    except (TypeError, ValueError):
        raise ValueError("invalid impact card") from None
    return {**payload, "object_name": object_name, "effective_date": effective_date,
            "processes": processes, "consequence": consequence.strip()}


def _source_material(conn, finding_id: int, source_url: str) -> int:
    rows = conn.execute(
        """SELECT m.id, m.url FROM event_materials em JOIN materials m ON m.id=em.material_id
           WHERE em.event_id=%s ORDER BY m.id""",
        (finding_id,),
    ).fetchall()
    for material_id, url in rows:
        if url == source_url:
            return material_id
    raise ValueError("source is not linked to finding")


def _record(row) -> dict:
    names = ("id", "finding_id", "material_id", "platform", "object_type", "object_name", "change_type",
             "effective_date", "processes", "consequence", "source_url", "status", "decided_by",
             "decided_at", "created_at", "updated_at")
    result = dict(zip(names, row))
    for key in ("effective_date", "decided_at", "created_at", "updated_at"):
        if isinstance(result[key], (date, datetime)):
            result[key] = result[key].isoformat()
    result["processes"] = list(result["processes"])
    return result


SELECT_CARD = """SELECT i.id, i.event_id, i.material_id, i.platform, i.object_type, i.object_name,
       i.change_type, i.effective_date, i.processes, i.consequence, m.url, i.status, i.decided_by,
       i.decided_at, i.created_at, i.updated_at
       FROM finding_impacts i JOIN materials m ON m.id=i.material_id"""


def list_finding_impacts(conn, finding_id: int) -> list[dict]:
    if type(finding_id) is not int or finding_id < 1:
        raise ValueError("invalid finding id")
    rows = conn.execute(SELECT_CARD + " WHERE i.event_id=%s ORDER BY i.id", (finding_id,)).fetchall()
    return [_record(row) for row in rows]


def create_finding_impact(conn, finding_id: int, payload: object) -> dict:
    if type(finding_id) is not int or finding_id < 1:
        raise ValueError("invalid finding id")
    values = _validate_payload(payload)
    material_id = _source_material(conn, finding_id, values["source_url"])
    row = conn.execute(
        """INSERT INTO finding_impacts
           (event_id, material_id, platform, object_type, object_name, change_type, effective_date, processes, consequence)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (finding_id, material_id, values["platform"], values["object_type"], values["object_name"],
         values["change_type"], values["effective_date"], values["processes"], values["consequence"]),
    ).fetchone()
    return next(card for card in list_finding_impacts(conn, finding_id) if card["id"] == row[0])


def update_finding_impact(conn, impact_id: int, payload: object) -> dict:
    if type(impact_id) is not int or impact_id < 1:
        raise ValueError("invalid impact id")
    values = _validate_payload(payload)
    row = conn.execute("SELECT event_id FROM finding_impacts WHERE id=%s", (impact_id,)).fetchone()
    if row is None:
        raise ValueError("impact not found")
    material_id = _source_material(conn, row[0], values["source_url"])
    conn.execute(
        """UPDATE finding_impacts SET material_id=%s, platform=%s, object_type=%s, object_name=%s,
                  change_type=%s, effective_date=%s, processes=%s, consequence=%s,
                  status='needs_review', decided_by=NULL, decided_at=NULL, updated_at=now()
           WHERE id=%s""",
        (material_id, values["platform"], values["object_type"], values["object_name"], values["change_type"],
         values["effective_date"], values["processes"], values["consequence"], impact_id),
    )
    return next(card for card in list_finding_impacts(conn, row[0]) if card["id"] == impact_id)


def decide_finding_impact(conn, impact_id: int, decision: str, owner_id: str) -> dict:
    if (type(impact_id) is not int or impact_id < 1 or not isinstance(decision, str) or decision not in DECISIONS
            or not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 120):
        raise ValueError("invalid impact decision")
    row = conn.execute(
        """UPDATE finding_impacts SET status=%s, decided_by=%s, decided_at=%s, updated_at=%s
           WHERE id=%s RETURNING event_id""",
        (decision, owner_id.strip(), datetime.now(timezone.utc), datetime.now(timezone.utc), impact_id),
    ).fetchone()
    if row is None:
        raise ValueError("impact not found")
    return next(card for card in list_finding_impacts(conn, row[0]) if card["id"] == impact_id)
