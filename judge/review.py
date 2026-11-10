"""Blind owner-label import and relevance quality metrics."""
from __future__ import annotations

from typing import Any, Iterable

from judge.constants import CLASSIFICATIONS, CLASS_LABELS


def make_blind_export(materials: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    exported = []
    for material in materials:
        material_id = material.get("material_id")
        if type(material_id) is not int or material_id < 1:
            raise ValueError("Blind sample contains an invalid material ID")
        exported.append({
            "material_id": material_id,
            "title": material.get("title") if isinstance(material.get("title"), str) else "",
            "text": material.get("text") if isinstance(material.get("text"), str) else "",
            "source": material.get("source") if isinstance(material.get("source"), str) else "",
            "label": "",
        })
    return {"materials": exported}


def validate_labels(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("materials"), list) or not payload["materials"]:
        raise ValueError("Label file must contain a non-empty materials array")
    labels = []
    seen = set()
    for item in payload["materials"]:
        if not isinstance(item, dict):
            raise ValueError("Each label entry must be an object")
        material_id = item.get("material_id")
        label = item.get("label")
        if type(material_id) is not int or material_id < 1:
            raise ValueError("Each label entry needs a positive integer material_id")
        if material_id in seen:
            raise ValueError("Duplicate material_id in label file")
        seen.add(material_id)
        if not isinstance(label, str) or label not in CLASSIFICATIONS:
            raise ValueError("Each label must be one of the five relevance classes")
        labels.append({"material_id": material_id, "label": label})
    return labels


def compute_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    evaluated = [row for row in rows if row.get("label") != "uncertain"]
    confusion = {label: {prediction: 0 for prediction in CLASSIFICATIONS} for label in CLASSIFICATIONS}
    for row in evaluated:
        label, prediction = row.get("label"), row.get("predicted")
        if label not in CLASSIFICATIONS or prediction not in CLASSIFICATIONS:
            raise ValueError("Metrics require known predicted and owner labels")
        confusion[label][prediction] += 1

    per_class = {}
    for label in CLASSIFICATIONS:
        true_positive = confusion[label][label]
        false_positive = sum(confusion[actual][label] for actual in CLASSIFICATIONS if actual != label)
        false_negative = sum(confusion[label][predicted] for predicted in CLASSIFICATIONS if predicted != label)
        predicted_count = true_positive + false_positive
        actual_count = true_positive + false_negative
        per_class[label] = {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": true_positive / predicted_count if predicted_count else None,
            "recall": true_positive / actual_count if actual_count else None,
        }

    false_positives = []
    false_negatives = []
    for row in evaluated:
        if row["label"] == row["predicted"]:
            continue
        example = {
            "material_id": row["material_id"],
            "title": row.get("title") or "",
            "expected": row["label"],
            "predicted": row["predicted"],
        }
        false_positives.append({**example, "for_class": row["predicted"]})
        false_negatives.append({**example, "for_class": row["label"]})

    correct = sum(confusion[label][label] for label in CLASSIFICATIONS)
    return {
        "labeled": len(rows),
        "evaluated": len(evaluated),
        "excluded_uncertain": len(rows) - len(evaluated),
        "uncertain_predictions": sum(row.get("predicted") == "uncertain" for row in evaluated),
        "accuracy": correct / len(evaluated) if evaluated else None,
        "class_labels": CLASS_LABELS,
        "confusion_matrix": confusion,
        "per_class": per_class,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }
