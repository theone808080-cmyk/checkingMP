RULES_VERSION = "finding-quality-v1"
STATUS_VALUES = {"ok", "review", "issue"}
ISSUE_TYPES = {
    "unsupported_fact", "omitted_condition", "omitted_deadline",
    "unsupported_action", "contradiction", "insufficient_source",
}
SEVERITIES = {"low", "medium", "high"}
SAFE_ERROR_CODES = {
    "configuration_missing", "request_rejected", "transport_failed",
    "invalid_response", "source_incomplete",
}
