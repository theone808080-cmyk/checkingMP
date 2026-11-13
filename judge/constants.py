"""Shared labels for relevance rules, blind reviews, and evaluation."""

CLASSIFICATIONS = (
    "practical_change",
    "seller_action_or_risk",
    "industry_context",
    "irrelevant",
    "uncertain",
)

RELEVANCE_CLASSES = ("relevant", "irrelevant", "uncertain")
SELLER_IMPACTS = ("positive", "negative", "mixed", "neutral", "unclear")

CLASS_LABELS = {
    "practical_change": "Практическое изменение маркетплейса",
    "seller_action_or_risk": "Действие или риск для продавца",
    "industry_context": "Общий отраслевой контекст",
    "irrelevant": "Не относится к проекту",
    "uncertain": "Не уверен",
}

RELEVANCE_LABELS = {
    "relevant": "Релевантно продавцам",
    "irrelevant": "Не относится к целевым площадкам",
    "uncertain": "Не уверен, нужна проверка",
}

SELLER_IMPACT_LABELS = {
    "positive": "Выгода или возможность",
    "negative": "Риск или дополнительные затраты",
    "mixed": "Смешанное влияние",
    "neutral": "Влияние нейтральное",
    "unclear": "Влияние неясно",
}
