"""Read Ozon Seller API notification messages as ordinary source materials."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

API_ROOT = "https://api-seller.ozon.ru"
SOURCE_URL = "https://api-seller.ozon.ru"
SELLER_HOME = "https://seller.ozon.ru/"
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 1_000_000
MAX_CHATS = 100
MAX_MESSAGES = 100
CHAT_TYPE = "SELLER_API_NOTIFICATIONS"


class OzonApiError(RuntimeError):
    """A safe diagnostic code that never includes API credentials or response data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.safe_code = code


class _UrllibTransport:
    def post(self, url: str, *, headers: dict, payload: dict, timeout: int):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OzonApiError("network_error") from None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OzonSellerClient:
    """Small read-only client for the two Seller API notification endpoints."""

    def __init__(self, client_id: str, api_key: str, *, transport=None):
        self._client_id = client_id.strip() if isinstance(client_id, str) else ""
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._transport = transport or _UrllibTransport()

    @classmethod
    def from_env(cls):
        client_id = os.getenv("OZON_SELLER_CLIENT_ID", "")
        api_key = os.getenv("OZON_SELLER_API_KEY", "")
        if not client_id.strip() or not api_key.strip():
            raise OzonApiError("credentials_missing")
        return cls(client_id, api_key)

    def _post(self, path: str, payload: dict) -> dict:
        if not self._client_id or not self._api_key:
            raise OzonApiError("credentials_missing")
        result = self._transport.post(
            f"{API_ROOT}{path}",
            headers={"Client-Id": self._client_id, "Api-Key": self._api_key,
                     "Content-Type": "application/json"},
            payload=payload,
            timeout=REQUEST_TIMEOUT,
        )
        status = getattr(result, "status", getattr(result, "code", 200))
        if status in (401, 403):
            if callable(getattr(result, "close", None)):
                result.close()
            raise OzonApiError("authorization_failed")
        if status == 429:
            if callable(getattr(result, "close", None)):
                result.close()
            raise OzonApiError("rate_limited")
        if status < 200 or status >= 300:
            if callable(getattr(result, "close", None)):
                result.close()
            raise OzonApiError(f"http_{status}")
        try:
            if callable(getattr(result, "read", None)):
                raw = result.read(MAX_RESPONSE_BYTES + 1)
            elif isinstance(getattr(result, "body", None), bytes):
                raw = result.body
            else:
                raw = json.dumps(result).encode("utf-8")
        finally:
            if callable(getattr(result, "close", None)):
                result.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OzonApiError("response_too_large")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OzonApiError("invalid_response_json") from None
        if not isinstance(parsed, dict):
            raise OzonApiError("invalid_response_shape")
        return parsed

    def list_chats(self) -> list[dict]:
        rows = _required_rows(self._post("/v3/chat/list", {"limit": MAX_CHATS, "offset": 0}), "chats")
        normalized = []
        for row in rows:
            nested = row.get("chat")
            normalized.append({**row, **nested} if isinstance(nested, dict) else row)
        return normalized

    def history(self, chat_id: str) -> list[dict]:
        return _required_rows(
            self._post("/v3/chat/history", {
                "chat_id": chat_id, "from_message_id": 0, "limit": MAX_MESSAGES,
            }),
            "messages",
        )


def _required_rows(payload: dict, key: str) -> list[dict]:
    rows = payload.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise OzonApiError("invalid_response")
    return rows


def _message_id(message: dict) -> str:
    value = message.get("message_id", message.get("id"))
    return str(value).strip() if isinstance(value, (str, int)) and str(value).strip() else ""


def _message_text(message: dict) -> str:
    data = message.get("data")
    candidates = [message.get("text"), data if isinstance(data, str) else None]
    if isinstance(data, dict):
        candidates.extend((data.get("text"), data.get("content")))
    if isinstance(data, list):
        candidates.append(" ".join(part for part in data if isinstance(part, str)))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()[:12_000]
    return ""


def _published_at(message: dict) -> datetime | None:
    value = message.get("created_at") or message.get("date")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _source_url(text: str, chat_id: str, message_id: str) -> str:
    for candidate in re.findall(r"https://[^\s<>\]\[()\"']+", text):
        candidate = candidate.rstrip(".,;:!?»)")
        host = (urlparse(candidate).hostname or "").lower().rstrip(".")
        if host == "ozon.ru" or host.endswith(".ozon.ru"):
            return candidate
    chat = urllib.parse.quote(chat_id, safe="")
    message = urllib.parse.quote(message_id, safe="")
    return f"{SELLER_HOME}#ozon-message-{chat}-{message}"


def read_ozon_notifications(client: OzonSellerClient | None = None) -> list[dict[str, object]]:
    """Read only SELLER_API_NOTIFICATIONS; the method never marks messages read."""
    client = client or OzonSellerClient.from_env()
    messages_out: list[dict[str, object]] = []
    for chat in client.list_chats()[:MAX_CHATS]:
        if chat.get("chat_type") != CHAT_TYPE:
            continue
        chat_id_value = chat.get("chat_id")
        if not isinstance(chat_id_value, (str, int)) or not str(chat_id_value).strip():
            raise OzonApiError("invalid_response")
        chat_id = str(chat_id_value).strip()
        for message in client.history(chat_id)[-MAX_MESSAGES:]:
            message_id = _message_id(message)
            text = _message_text(message)
            if not message_id or not text:
                continue
            title = re.sub(r"https://\S+", "", text).strip(" \t\r\n-–—:;|")[:160] or text[:160]
            messages_out.append({
                "url": _source_url(text, chat_id, message_id),
                "message_id": message_id,
                "title": title,
                "text": text,
                "published_at": _published_at(message),
            })
    return messages_out
