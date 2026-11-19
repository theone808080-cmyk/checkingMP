"""Normalize and compare read-only marketplace tariff snapshots."""

from __future__ import annotations


def collect_tariff_sources(sources: dict[str, object]) -> dict[str, object]:
    """Collect each provider independently so one outage cannot hide the other."""
    rows = []
    failures = {}
    for source_code in sorted(sources):
        reader = sources[source_code]
        try:
            fetched = reader()
            if not isinstance(fetched, list) or any(not isinstance(row, dict) for row in fetched):
                raise ValueError("invalid response")
            rows.extend(fetched)
        except Exception:
            failures[source_code] = "source_fetch_failed"
    rows.sort(key=lambda row: str(row.get("stable_key", "")))
    return {"rows": rows, "failures": failures}


def compare_tariff_snapshots(previous: list[dict] | None, current: list[dict]) -> list[dict]:
    """Return stable old/new tariff deltas; `None` means first successful baseline."""
    if previous is None:
        return []
    before = _by_key(previous)
    after = _by_key(current)
    deltas = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            row = after[key]
            if "|offer:" in key:
                continue
            deltas.append({"stable_key": key, "before": None, "after": row.get("amount"),
                           "approximate": bool(row.get("approximate", False))})
            continue
        if key not in after:
            row = before[key]
            if "|offer:" in key:
                continue
            deltas.append({"stable_key": key, "before": row.get("amount"), "after": None,
                           "approximate": bool(row.get("approximate", False))})
            continue
        old_row, new_row = before[key], after[key]
        if old_row.get("comparison_basis") != new_row.get("comparison_basis"):
            continue
        if old_row.get("amount") == new_row.get("amount") and old_row.get("unit") == new_row.get("unit"):
            continue
        deltas.append({
            "stable_key": key,
            "before": old_row.get("amount"),
            "after": new_row.get("amount"),
            "approximate": bool(new_row.get("approximate", old_row.get("approximate", False))),
        })
    return deltas


def _by_key(rows: list[dict]) -> dict[str, dict]:
    if not isinstance(rows, list):
        raise ValueError("invalid snapshot")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid snapshot")
        key = row.get("stable_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("invalid snapshot")
        if key in result:
            raise ValueError("duplicate snapshot key")
        result[key] = row
    return result
