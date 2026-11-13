"""Blind owner-label import and relevance quality metrics."""
from __future__ import annotations

from typing import Any, Iterable

from judge.constants import CLASSIFICATIONS, CLASS_LABELS, RELEVANCE_CLASSES, RELEVANCE_LABELS, SELLER_IMPACTS


def make_blind_export(materials: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    exported = []
    for material in materials:
        material_id = material.get("material_id")
        if type(material_id) is not int or material_id < 1:
            raise ValueError("Blind sample contains an invalid material ID")
        item = {
            "material_id": material_id,
            "title": material.get("title") if isinstance(material.get("title"), str) else "",
            "text": material.get("text") if isinstance(material.get("text"), str) else "",
            "source": material.get("source") if isinstance(material.get("source"), str) else "",
            "label": "",
        }
        if material.get("_include_seller_impact_labels") or any(
            key in material for key in ("relevance", "seller_impact", "topic")
        ):
            item["label_type"] = "relevance"
            item["platforms"] = []
            item["seller_impact"] = ""
        exported.append(item)
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
        label_type = item.get(
            "label_type",
            "relevance" if label == "relevant" or (
                label in ("irrelevant", "uncertain") and ("platforms" in item or "seller_impact" in item)
            ) else "classification",
        )
        if type(material_id) is not int or material_id < 1:
            raise ValueError("Each label entry needs a positive integer material_id")
        if material_id in seen:
            raise ValueError("Duplicate material_id in label file")
        seen.add(material_id)
        if label_type not in ("classification", "relevance"):
            raise ValueError("Each label needs a supported label_type")
        allowed_labels = CLASSIFICATIONS if label_type == "classification" else RELEVANCE_CLASSES
        if not isinstance(label, str) or label not in allowed_labels:
            raise ValueError("Each label must be a supported relevance class")
        normalized = {"material_id": material_id, "label": label}
        if "label_type" in item or label_type == "relevance":
            normalized["label_type"] = label_type
        if "platforms" in item:
            platforms = item["platforms"]
            if not isinstance(platforms, list) or any(not isinstance(value, str) or not value.strip() for value in platforms):
                raise ValueError("platforms must be an array of non-empty labels")
            normalized["platforms"] = list(dict.fromkeys(value.strip()[:80] for value in platforms))
        if "seller_impact" in item:
            if item["seller_impact"] not in SELLER_IMPACTS:
                raise ValueError("Invalid seller impact label")
            normalized["seller_impact"] = item["seller_impact"]
        labels.append(normalized)
    return labels


def compute_metrics(rows: Iterable[dict[str, Any]], *, include_groups: bool = True) -> dict[str, Any]:
    rows = list(rows)
    has_relevance_rows = any(
        row.get("metric_type") == "relevance" or "relevant" in (row.get("label"), row.get("predicted"))
        for row in rows
    )
    def metric_type(row: dict[str, Any]) -> str:
        explicit = row.get("metric_type")
        if explicit in ("classification", "relevance"):
            return explicit
        values = {row.get("label"), row.get("predicted")}
        if values & {"practical_change", "seller_action_or_risk", "industry_context"}:
            return "classification"
        return "relevance" if has_relevance_rows else "classification"

    legacy_rows = [row for row in rows if metric_type(row) == "classification"
                   and row.get("label") in CLASSIFICATIONS and row.get("predicted") in CLASSIFICATIONS]
    evaluated = [row for row in legacy_rows if row.get("label") != "uncertain"]
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
    binary_rows = [row for row in rows if metric_type(row) == "relevance"
                   and row.get("label") in RELEVANCE_CLASSES]
    binary_evaluated = [row for row in binary_rows if row.get("label") != "uncertain"]
    binary_confusion = {
        actual: {predicted: 0 for predicted in ("relevant", "irrelevant", "uncertain")}
        for actual in ("relevant", "irrelevant")
    }
    for row in binary_evaluated:
        actual, predicted = row.get("label"), row.get("predicted")
        if actual not in binary_confusion or predicted not in RELEVANCE_CLASSES:
            raise ValueError("Binary metrics require known relevance predictions and owner labels")
        binary_confusion[actual][predicted] += 1
    tp = binary_confusion["relevant"]["relevant"]
    fp = binary_confusion["irrelevant"]["relevant"]
    fn = binary_confusion["relevant"]["irrelevant"] + binary_confusion["relevant"]["uncertain"]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else None
    binary = {
        "evaluated": len(binary_evaluated),
        "excluded_uncertain": len(binary_rows) - len(binary_evaluated),
        "uncertain_predictions": sum(row.get("predicted") == "uncertain" for row in binary_evaluated),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "confusion_matrix": binary_confusion,
        "false_positives": [row for row in binary_evaluated if row["label"] == "irrelevant" and row["predicted"] == "relevant"],
        "false_negatives": [row for row in binary_evaluated if row["label"] == "relevant" and row["predicted"] != "relevant"],
    }
    by_platform: dict[str, list[dict[str, Any]]] = {}
    by_source_type: dict[str, list[dict[str, Any]]] = {}
    for row in binary_rows:
        platforms = row.get("platforms") or ["Не установлено"]
        for platform in dict.fromkeys(platforms):
            by_platform.setdefault(str(platform), []).append(row)
        by_source_type.setdefault(str(row.get("source_type") or "unknown"), []).append(row)
    def grouped_metrics(groups: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
        if not include_groups:
            return {}
        result = {}
        for key, group in sorted(groups.items()):
            binary_result = compute_metrics(
                [{**item, "metric_type": "relevance"} for item in group], include_groups=False
            )["binary"]
            result[key] = {
                "labeled": len(group),
                "evaluated": binary_result["evaluated"],
                "excluded_uncertain": binary_result["excluded_uncertain"],
                "precision": binary_result["precision"],
                "recall": binary_result["recall"],
                "f1": binary_result["f1"],
                "confusion_matrix": binary_result["confusion_matrix"],
            }
        return result
    correct_binary = tp + binary_confusion["irrelevant"]["irrelevant"] + binary_confusion["irrelevant"]["uncertain"]
    legacy_excluded = len(legacy_rows) - len(evaluated)
    binary_excluded = len(binary_rows) - len(binary_evaluated)
    return {
        "labeled": len(legacy_rows) + len(binary_rows),
        "evaluated": len(evaluated) + len(binary_evaluated),
        "excluded_uncertain": legacy_excluded + binary_excluded,
        "uncertain_predictions": sum(row.get("predicted") == "uncertain" for row in evaluated) + binary["uncertain_predictions"],
        "accuracy": (correct_binary / len(binary_evaluated) if binary_evaluated else
                     correct / len(evaluated) if evaluated else None),
        "class_labels": CLASS_LABELS,
        "relevance_labels": RELEVANCE_LABELS,
        "seller_impact_labels": SELLER_IMPACTS,
        "confusion_matrix": confusion,
        "per_class": per_class,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "binary": binary,
        "by_platform": grouped_metrics(by_platform),
        "by_source_type": grouped_metrics(by_source_type),
    }
