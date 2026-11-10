"""Standalone weekly source-discovery orchestration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import db
from source_discovery.grouping import group_candidates
from source_discovery.providers import SearchBatch, search_all
from source_discovery.queries import QUERIES


def next_month_start(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("discovery time must include a timezone")
    current = now.astimezone(timezone.utc)
    year, month = current.year, current.month + 1
    if month == 13:
        year, month = year + 1, 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _run(conn, *, api_key: str | None, now: datetime) -> tuple[int, int]:
    db.init_schema(conn)
    db.sync_sources(conn)
    run_id = db.start_run(conn, "source_discovery")
    try:
        paused = db.exa_pause_until(conn) is not None
        if paused:
            api_key = None
        expected_providers = 1 + int(bool(api_key))
        batch: SearchBatch = search_all(api_key=api_key, queries=list(QUERIES))
        if batch.exa_limited:
            db.pause_exa_until(conn, next_month_start(now), "limit_reached")
        candidates = group_candidates(batch.hits)
        created = db.upsert_source_candidates(conn, [candidate.__dict__ for candidate in candidates])
        all_failed = not batch.hits and len(batch.errors) >= expected_providers
        provider_status = {"google_news": "error:" + batch.errors["google_news"]
                           if "google_news" in batch.errors else "ok"}
        provider_status["exa"] = ("paused" if paused or batch.exa_limited else "not_configured") if not api_key or batch.exa_limited else (
            "error:" + batch.errors["exa"] if "exa" in batch.errors else "ok"
        )
        note = json.dumps({"new_candidates": created, "providers": provider_status},
                          ensure_ascii=False, separators=(",", ":"))[:1000]
        db.finish_run(conn, run_id, "failed" if all_failed else "finished", created, note)
        return created, len(batch.errors)
    except Exception:
        db.finish_run(conn, run_id, "failed", 0, "Source discovery failed")
        raise


def discover_sources(conn=None, *, api_key: str | None = None, now: datetime | None = None) -> tuple[int, int]:
    current = now or datetime.now(timezone.utc)
    key = api_key if api_key is not None else os.environ.get("EXA_API_KEY", "").strip() or None
    if conn is not None:
        return _run(conn, api_key=key, now=current)
    with db.connect() as owned:
        return _run(owned, api_key=key, now=current)
