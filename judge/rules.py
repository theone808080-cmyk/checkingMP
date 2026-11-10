"""Deterministic, versioned relevance rules for marketplace materials."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "relevance-rules.json"


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _matches(text: str, term: str) -> bool:
    term = _normalize(term)
    if len(term) <= 3:
        return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, flags=re.UNICODE) is not None
    return term in text


def _rule_hits(text: str, rules: dict[str, list[str]], prefix: str) -> list[str]:
    return [
        f"{prefix}:{code}"
        for code, terms in rules.items()
        if any(_matches(text, term) for term in terms)
    ]


@lru_cache(maxsize=8)
def _load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"].strip():
        raise ValueError("Invalid relevance rules version")
    for key in ("platforms", "topics"):
        group = data.get(key)
        if not isinstance(group, dict) or any(
            not isinstance(code, str)
            or not isinstance(rule, (dict, list))
            or not (rule.get("terms") if isinstance(rule, dict) else rule)
            or not all(isinstance(term, str) and term.strip() for term in (rule.get("terms") if isinstance(rule, dict) else rule))
            for code, rule in group.items()
        ):
            raise ValueError(f"Invalid relevance rules group: {key}")
    for key in ("change_terms", "seller_terms", "industry_terms", "irrelevant_terms"):
        terms = data.get(key)
        if not isinstance(terms, list) or not all(isinstance(term, str) and term.strip() for term in terms):
            raise ValueError(f"Invalid relevance rules list: {key}")
    return data


def rules_version() -> str:
    return _load_rules()["version"]


def classify(material: dict[str, Any], *, rules_path: Path = RULES_PATH) -> dict[str, Any]:
    """Classify a material without network calls or probabilistic claims."""
    if not isinstance(material, dict):
        raise ValueError("Material must be an object")
    title = material.get("title") if isinstance(material.get("title"), str) else ""
    body = material.get("text") if isinstance(material.get("text"), str) else ""
    text = _normalize(f"{title}\n{body}")
    rules = _load_rules(rules_path)

    platform_hits = _rule_hits(text, {
        code: definition["terms"]
        for code, definition in rules["platforms"].items()
    }, "platform")
    topic_hits = _rule_hits(text, rules["topics"], "topic")
    change = any(_matches(text, term) for term in rules["change_terms"])
    seller = any(_matches(text, term) for term in rules["seller_terms"])
    industry = any(_matches(text, term) for term in rules["industry_terms"])
    irrelevant = any(_matches(text, term) for term in rules["irrelevant_terms"])

    rule_codes = platform_hits + topic_hits
    if change:
        rule_codes.append("signal:change")
    if seller:
        rule_codes.append("signal:seller")
    if industry:
        rule_codes.append("signal:industry_context")
    if irrelevant:
        rule_codes.append("topic:irrelevant")

    has_platform = bool(platform_hits)
    has_topic = bool(topic_hits)
    score = min(100, 30 * bool(has_platform) + 25 * bool(has_topic) + 25 * bool(change) + 15 * bool(seller) + 5 * bool(industry))
    mixed_signals = irrelevant and (has_platform or has_topic or change or seller or industry)
    if mixed_signals:
        classification = "uncertain"
    elif has_platform and has_topic and change:
        classification = "practical_change"
    elif has_platform and has_topic and seller:
        classification = "seller_action_or_risk"
    elif has_platform or has_topic or industry:
        classification = "industry_context"
        score = max(score, 20)
    elif irrelevant:
        classification = "irrelevant"
        score = 0
    else:
        classification = "uncertain"

    platform_labels = [rules["platforms"][code.split(":", 1)[1]]["label"] for code in platform_hits]
    reason_names = {
        "change": "сигнал изменения",
        "seller": "действия продавца",
        "industry_context": "отраслевой контекст",
        "irrelevant": "посторонняя тема",
    }
    reasons = [
        rules["platforms"][code.split(":", 1)[1]]["label"] if code.startswith("platform:")
        else reason_names.get(code.split(":", 1)[1], code.split(":", 1)[1].replace("_", " "))
        for code in rule_codes
    ]
    explanation = "Совпали признаки: " + ", ".join(reasons) + "." if reasons else "Явных совпадений по правилам не найдено."
    return {
        "classification": classification,
        "score": int(score),
        "rule_codes": rule_codes,
        "platforms": platform_labels,
        "rules_version": rules["version"],
        "explanation": explanation[:300],
    }
