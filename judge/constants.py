"""Shared labels for relevance rules, blind reviews, and evaluation."""

CLASSIFICATIONS = (
    "practical_change",
    "seller_action_or_risk",
    "industry_context",
    "irrelevant",
    "uncertain",
)

CLASS_LABELS = {
    "practical_change": "Практическое изменение маркетплейса",
    "seller_action_or_risk": "Действие или риск для продавца",
    "industry_context": "Общий отраслевой контекст",
    "irrelevant": "Не относится к проекту",
    "uncertain": "Не уверен",
}
