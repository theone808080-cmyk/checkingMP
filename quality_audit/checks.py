"""Literal checks for explicit dates, numbers, and likely proper names."""
from __future__ import annotations

import re
from typing import Any


MONTHS = ("января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря|"
          "january|february|march|april|may|june|july|august|september|october|november|december")
DATE_PATTERNS = (
    re.compile(rf"\b\d{{1,2}}\s+(?:{MONTHS})(?:\s+\d{{4}}\s*(?:года|г\.)?)?", re.I),
    re.compile(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b"),
)
NUMBER_PATTERN = re.compile(r"(?<![\w])\d+(?:[.,]\d+)?(?:\s?(?:%|руб(?:лей|ля|ль)?|₽|шт\.?|дней|дня|день))?", re.I)
NAME_PATTERN = re.compile(r"\b[A-ZА-ЯЁ][a-zа-яё-]+(?:\s+[A-ZА-ЯЁ][a-zа-яё-]+)*\b")


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).replace("ё", "е")


def _values(text: str) -> list[str]:
    dates = [match.group(0).strip() for pattern in DATE_PATTERNS for match in pattern.finditer(text)]
    numbers = [match.group(0).strip() for match in NUMBER_PATTERN.finditer(text)]
    names = []
    for match in NAME_PATTERN.finditer(text):
        prefix = text[:match.start()].rstrip()
        if not prefix or prefix[-1:] in ".!?\n:;":
            continue
        if len(match.group(0)) > 2:
            names.append(match.group(0))
    output, seen = [], set()
    for value in dates + numbers + names:
        key = _normal(value)
        if key and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _evidence(material: dict[str, Any], target: str) -> str:
    text = material.get("text") or material.get("translation") or material.get("title") or material.get("translated_title") or ""
    match = re.search(r"\d", text)
    if match:
        return text[max(0, match.start() - 100):max(0, match.start() - 100) + 300]
    return text[:300]


def check_explicit_values(summary: str, action: str, materials: list[dict]) -> list[dict]:
    if not isinstance(summary, str) or not isinstance(action, str) or not isinstance(materials, list) or not materials:
        return []
    usable = [item for item in materials if isinstance(item, dict) and type(item.get("material_id")) is int
              and isinstance(item.get("text"), str) and item.get("text", "").strip()]
    if not usable:
        return []
    corpus = "\n".join(" ".join(filter(None, (item.get("title"), item.get("translated_title"),
                                                   item.get("text"), item.get("translation")))) for item in usable)
    normalized_corpus = _normal(corpus)
    results = []
    for target, text in (("summary", summary), ("recommendation", action)):
        for value in _values(text):
            if _normal(value) in normalized_corpus:
                continue
            # A linked source with a different explicit value is the most useful
            # evidence for a literal mismatch. The machine finding remains a
            # review aid; semantic truth is left to the independent reviewer.
            material = usable[0]
            results.append({
                "target": target,
                "type": "unsupported_fact",
                "severity": "medium",
                "material_id": material["material_id"],
                "evidence": _evidence(material, target),
                "explanation": f"Явное значение «{value}» не найдено в связанных материалах.",
            })
    return results
