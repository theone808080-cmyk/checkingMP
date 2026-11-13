"""Owner-triggered, non-blocking audits and blind manual evaluation."""
from __future__ import annotations

import os
import time
from typing import Any

import db
from db import finding_quality as store
from quality_audit.checks import check_explicit_values
from quality_audit.constants import RULES_VERSION
from quality_audit.reviewer import review_finding


def run_audit(conn, *, limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Audit limit must be between 1 and 50")
    model = os.getenv("FINDING_AUDIT_MODEL", "").strip()
    candidates = store.audit_sample(conn, limit=limit)
    run_id = store.create_run(conn, limit=limit, model=model, rules_version=RULES_VERSION)
    store.register_run_sample(conn, run_id, [finding["finding_id"] for finding in candidates])
    started = time.monotonic()
    if not model or not os.getenv("OPENROUTER_API_KEY", "").strip():
        code = "configuration_missing"
        failed = []
        for finding in candidates:
            for target in ("summary", "recommendation"):
                failed.append({"finding_id": finding["finding_id"], "target": target,
                               "status": "review", "issues": [], "error_code": code, "elapsed_ms": 0})
        store.save_results(conn, run_id, failed)
        store.finish_run(conn, run_id, status="failed", finding_count=len(candidates),
                         error_code=code, elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
        return run_id

    all_results = []
    total_prompt = total_completion = 0
    prompt_complete = completion_complete = True
    total_cost = 0.0
    cost_complete = True
    had_failure = False
    for finding in candidates:
        materials = finding.get("materials") or []
        deterministic = check_explicit_values(finding.get("summary", ""),
                                              finding.get("recommended_action", ""), materials)
        try:
            reviewed = review_finding(finding, materials, model=model)
        except ValueError:
            reviewed = {
                "summary": {"status": "review", "issues": []},
                "recommendation": {"status": "review", "issues": []},
                "error_code": "configuration_missing",
                "usage": {"prompt_tokens": None, "completion_tokens": None, "cost": None},
                "elapsed_ms": 0,
            }
        usage = reviewed.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if type(prompt_tokens) is int:
            total_prompt += prompt_tokens
        else:
            prompt_complete = False
        if type(completion_tokens) is int:
            total_completion += completion_tokens
        else:
            completion_complete = False
        cost = usage.get("cost")
        if type(cost) in (int, float) and cost >= 0:
            total_cost += float(cost)
        else:
            cost_complete = False
        error_code = reviewed.get("error_code")
        had_failure |= error_code is not None
        elapsed_ms = reviewed.get("elapsed_ms") if type(reviewed.get("elapsed_ms")) is int else 0
        for target, key in (("summary", "summary"), ("recommendation", "recommendation")):
            result = reviewed.get(key, {"status": "review", "issues": []})
            issues = list(result.get("issues") or [])
            literal_issues = [item for item in deterministic if item.get("target") == target]
            issues.extend({name: value for name, value in item.items() if name != "target"}
                          for item in literal_issues)
            status = result.get("status", "review")
            # A literal mismatch is a cue for a human/model review; absence in a
            # saved excerpt alone is not enough to assert that the claim is false.
            if literal_issues and status == "ok":
                status = "review"
            all_results.append({"finding_id": finding["finding_id"], "target": target,
                                "status": status, "issues": issues[:10],
                                "error_code": error_code, "elapsed_ms": elapsed_ms})
    store.save_results(conn, run_id, all_results)
    elapsed = max(0, int((time.monotonic() - started) * 1000))
    store.finish_run(conn, run_id, status="partial" if had_failure else "success",
                     finding_count=len(candidates), elapsed_ms=elapsed,
                     prompt_tokens=total_prompt if prompt_complete and candidates else None,
                     completion_tokens=total_completion if completion_complete and candidates else None,
                     cost=total_cost if cost_complete and candidates else None)
    return run_id


def prepare_labels(conn, run_id: int) -> dict[str, list[dict[str, Any]]]:
    rows = db.finding_quality_label_rows(conn, run_id)
    findings = []
    for row in rows:
        findings.append({
            "finding_id": row["finding_id"],
            "platform": row.get("platform", ""),
            "topic": row.get("topic", ""),
            "importance": row.get("importance"),
            "summary": row.get("summary", ""),
            "recommended_action": row.get("recommended_action", ""),
            "materials": row.get("materials", []),
            "summary_facts": "",
            "omitted_condition": False,
            "omitted_deadline": False,
            "completeness_uncertain": False,
            "recommendation": "",
        })
    return {"findings": findings}


def import_labels(conn, run_id: int, payload: object) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list) or not payload["findings"]:
        raise ValueError("Label file must contain a non-empty findings array")
    labels = []
    seen = set()
    for item in payload["findings"]:
        if not isinstance(item, dict):
            raise ValueError("Each finding label must be an object")
        finding_id = item.get("finding_id")
        if type(finding_id) is not int or finding_id < 1 or finding_id in seen:
            raise ValueError("Each label needs a unique positive finding_id")
        seen.add(finding_id)
        facts = item.get("summary_facts")
        condition, deadline = item.get("omitted_condition"), item.get("omitted_deadline")
        uncertain = item.get("completeness_uncertain")
        recommendation = item.get("recommendation")
        if (facts not in {"ok", "issue", "uncertain"} or type(condition) is not bool or
                type(deadline) is not bool or type(uncertain) is not bool or
                recommendation not in {"supported", "unsupported", "uncertain"}):
            raise ValueError("Invalid summary or recommendation label")
        labels.append({"finding_id": finding_id, "summary_facts": facts,
                       "omitted_condition": condition, "omitted_deadline": deadline,
                       "completeness_uncertain": uncertain, "recommendation": recommendation})
    return store.save_labels(conn, run_id, labels)


def _target_report(rows: list[dict[str, Any]], target: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("target") == target]
    confusion = {auto: {manual: 0 for manual in ("ok", "issue", "uncertain")} for auto in ("ok", "review", "issue")}
    disagreements = []
    labeled = 0
    matches = 0
    for row in selected:
        manual = row.get("manual")
        automatic = row.get("automatic")
        if manual is None:
            continue
        labeled += 1
        if automatic not in confusion or manual not in {"ok", "issue", "uncertain"}:
            continue
        confusion[automatic][manual] += 1
        if automatic == manual:
            matches += 1
        else:
            disagreements.append({"finding_id": row.get("finding_id"), "automatic": automatic,
                                  "manual": manual, "platform": row.get("platform"),
                                  "importance": row.get("importance"),
                                  "summary": row.get("summary"),
                                  "recommended_action": row.get("recommended_action"),
                                  "issues": row.get("issues") or [],
                                  "source_url": row.get("source_url")})
    evaluated = sum(1 for row in selected if row.get("manual") in {"ok", "issue"})
    automatic_counts = {status: sum(1 for row in selected if row.get("automatic") == status)
                        for status in ("ok", "review", "issue")}
    return {"labeled": labeled, "evaluated": evaluated,
            "unlabeled": len(selected) - labeled, "matches": matches,
            "agreement": matches / evaluated if evaluated else None,
            "automatic_counts": automatic_counts,
            "manual_uncertain": sum(1 for row in selected if row.get("manual") == "uncertain"),
            "confusion_matrix": confusion, "disagreements": disagreements}


def report(conn, run_id: int) -> dict[str, Any]:
    run = db.finding_quality_run(conn, run_id)
    if run is None:
        raise ValueError("Finding quality audit run not found")
    rows = db.finding_quality_report_rows(conn, run_id)
    composition: dict[str, int] = {}
    seen_findings = set()
    for row in rows:
        finding_id = row.get("finding_id")
        if finding_id in seen_findings:
            continue
        seen_findings.add(finding_id)
        key = (f"{row.get('platform') or 'unknown'} / {row.get('source_kind') or 'unknown'} / "
               f"importance {row.get('importance') or 'unknown'}")
        composition[key] = composition.get(key, 0) + 1
    return {"run": run, "composition": composition,
            "summary": _target_report(rows, "summary"),
            "recommendation": _target_report(rows, "recommendation")}
