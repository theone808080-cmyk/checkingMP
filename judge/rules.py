"""Deterministic, versioned marketplace relevance and seller-impact rules."""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "relevance-rules.json"


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(value.split())


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
    for key in (
        "change_terms", "seller_terms", "industry_terms", "irrelevant_terms",
        "positive_impact_terms", "negative_impact_terms",
    ):
        terms = data.get(key)
        if not isinstance(terms, list) or not all(isinstance(term, str) and term.strip() for term in terms):
            raise ValueError(f"Invalid relevance rules list: {key}")
    return data


def rules_version() -> str:
    return _load_rules()["version"]


def _seller_impact(text: str, relevance: str, rules: dict[str, Any]) -> str:
    if relevance != "relevant":
        return "unclear"
    positive = any(_matches(text, term) for term in rules["positive_impact_terms"])
    negative = any(_matches(text, term) for term in rules["negative_impact_terms"])
    topics = _rule_hits(text, {"tariffs": rules["topics"]["tariffs"]}, "topic")
    if topics:
        if re.search(r"(?:комисс\w*|тариф\w*|сбор\w*).{0,45}(?:увелич\w*|повыш\w*|выраст\w*|подня\w*)", text):
            negative = True
        if re.search(r"(?:комисс\w*|тариф\w*|сбор\w*).{0,45}(?:сниз\w*|уменьш\w*|сократ\w*)", text):
            positive = True
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def classify(material: dict[str, Any], *, rules_path: Path = RULES_PATH) -> dict[str, Any]:
    """Classify a material without network calls or probabilistic claims."""
    if not isinstance(material, dict):
        raise ValueError("Material must be an object")
    title = material.get("title") if isinstance(material.get("title"), str) else ""
    body = material.get("text") if isinstance(material.get("text"), str) else ""
    text = _normalize(f"{title}\n{body}")
    rules = _load_rules(rules_path)

    platform_rules = {
        code: definition["terms"]
        for code, definition in rules["platforms"].items()
    }
    platform_hits = _rule_hits(text, platform_rules, "platform")
    topic_hits = _rule_hits(text, rules["topics"], "topic")
    change = any(_matches(text, term) for term in rules["change_terms"])
    seller = any(_matches(text, term) for term in rules["seller_terms"])
    industry = any(_matches(text, term) for term in rules["industry_terms"])
    irrelevant = any(_matches(text, term) for term in rules["irrelevant_terms"])

    has_platform = bool(platform_hits)
    has_topic = bool(topic_hits)
    has_marking = "topic:marking" in topic_hits
    has_seller_context = seller or has_platform
    explicit_rule_context = has_platform and has_topic and (change or seller)
    regulatory_context = has_marking and seller
    mixed_signals = irrelevant and (has_platform or has_topic or change or seller)
    body_is_informative = len(body.strip()) >= 12

    if mixed_signals:
        relevance = "uncertain"
        classification = "uncertain"
    elif (explicit_rule_context or regulatory_context) and body_is_informative:
        relevance = "relevant"
        if change and has_platform:
            classification = "practical_change"
        elif seller:
            classification = "seller_action_or_risk"
        else:
            classification = "industry_context"
    elif not text.strip() or (has_platform and has_topic and (change or seller) and not body_is_informative):
        relevance = "uncertain"
        classification = "uncertain"
    elif irrelevant and not has_platform and not has_topic:
        relevance = "irrelevant"
        classification = "irrelevant"
    elif industry or has_topic:
        # Keep the older five-way assessment for historical reports while
        # requiring a concrete seller/platform connection for the new gate.
        relevance = "irrelevant"
        classification = "industry_context"
    else:
        relevance = "uncertain"
        classification = "uncertain"

    if not has_platform and not (has_marking and seller):
        platform_labels = []
    else:
        platform_labels = [
            rules["platforms"][code.split(":", 1)[1]]["label"]
            for code in platform_hits
        ]
        if has_marking and seller and not platform_labels:
            platform_labels = ["Обязательные требования для продавцов"]

    rule_codes = platform_hits + topic_hits
    if change:
        rule_codes.append("signal:change")
    if seller:
        rule_codes.append("signal:seller")
    if industry:
        rule_codes.append("signal:industry_context")
    if irrelevant:
        rule_codes.append("topic:irrelevant")

    score = min(
        100,
        35 * bool(has_platform) + 25 * bool(has_topic) +
        20 * bool(change) + 15 * bool(seller) + 5 * bool(industry),
    )
    if relevance == "irrelevant":
        score = 0
    topic = next((item.split(":", 1)[1] for item in topic_hits), "other_relevant" if relevance == "relevant" else "")
    reason_codes = [code.split(":", 1)[1].replace("_", " ") for code in rule_codes]
    explanation = (
        "Совпали признаки: " + ", ".join(reason_codes) + "."
        if reason_codes else "Явных признаков связи с продавцами целевых площадок не найдено."
    )
    return {
        "classification": classification,
        "relevance": relevance,
        "score": int(score),
        "rule_codes": rule_codes,
        "platforms": platform_labels,
        "topic": topic,
        "seller_impact": _seller_impact(text, relevance, rules),
        "rules_version": rules["version"],
        "explanation": explanation[:300],
    }
