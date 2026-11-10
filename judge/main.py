"""Database-backed shadow assessments and owner review operations."""
from __future__ import annotations

import db
from judge.review import compute_metrics, make_blind_export, validate_labels
from judge.rules import classify, rules_version


def assess_pending(conn) -> int:
    version = rules_version()
    materials = db.pending_relevance_materials(conn, version)
    assessments = [
        {"material_id": material["material_id"], **classify(material)}
        for material in materials
    ]
    if not assessments:
        return 0
    return db.save_relevance_assessments(conn, assessments)


def prepare_review(conn, limit: int = 100) -> dict:
    materials = db.relevance_review_sample(conn, limit)
    assessments = [
        {"material_id": material["material_id"], **classify(material)}
        for material in materials
    ]
    db.save_relevance_assessments(conn, assessments)
    return make_blind_export(materials)


def import_labels(conn, payload: object) -> int:
    labels = validate_labels(payload)
    return db.save_relevance_labels(conn, labels, rules_version())


def report(conn) -> dict:
    return compute_metrics(db.relevance_evaluation_rows(conn, rules_version()))
