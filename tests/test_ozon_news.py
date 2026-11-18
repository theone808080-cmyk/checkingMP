from datetime import datetime, timezone
import json

import pytest

from collector.ozon_api import OzonApiError, OzonSellerClient, read_ozon_notifications


API_ROOT = "https://api-seller.ozon.ru"
FALLBACK_URL = "https://seller.ozon.ru/"


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = json.dumps(body).encode("utf-8")


class FixtureTransport:
    """A local transport: tests must never contact the Seller API."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return self.responses.pop(0)


def test_client_posts_read_only_endpoints_with_seller_credentials_and_documented_payloads():
    """A wrong credential header or payload would make Ozon reject the reader."""
    transport = FixtureTransport([
        FakeResponse(200, {"chats": []}),
        FakeResponse(200, {"messages": []}),
    ])
    client = OzonSellerClient("test-client-id", "test-api-key", transport=transport)

    assert client.list_chats() == []
    assert client.history("seller-notify-chat") == []
    assert transport.calls == [
        {
            "url": f"{API_ROOT}/v3/chat/list",
            "headers": {
                "Client-Id": "test-client-id",
                "Api-Key": "test-api-key",
                "Content-Type": "application/json",
            },
            "payload": {"limit": 100, "offset": 0},
            "timeout": 30,
        },
        {
            "url": f"{API_ROOT}/v3/chat/history",
            "headers": {
                "Client-Id": "test-client-id",
                "Api-Key": "test-api-key",
                "Content-Type": "application/json",
            },
            "payload": {"chat_id": "seller-notify-chat", "from_message_id": 0, "limit": 100},
            "timeout": 30,
        },
    ]


def test_reader_uses_only_notification_chats_and_creates_one_stably_identified_material_per_message():
    """Reading SELLER_API_UPDATES would mix technical API announcements into seller news."""
    transport = FixtureTransport([
        FakeResponse(200, {
            "chats": [
                {"chat_id": "seller-notify-chat", "chat_type": "SELLER_API_NOTIFICATIONS"},
                {"chat_id": "technical-updates", "chat_type": "SELLER_API_UPDATES"},
            ]
        }),
        FakeResponse(200, {
            "messages": [
                {
                    "message_id": "notice-42",
                    "created_at": "2026-11-18T10:15:00Z",
                    "data": "Tariff update: https://docs.ozon.ru/seller/rules/",
                },
                {
                    "message_id": "notice-43",
                    "created_at": "2026-11-18T11:00:00Z",
                    "data": "Warehouse acceptance schedule changed.",
                },
            ]
        }),
    ])

    rows = read_ozon_notifications(OzonSellerClient("test-client-id", "test-api-key", transport=transport))

    assert rows == [
        {
            "url": "https://docs.ozon.ru/seller/rules/",
            "message_id": "notice-42",
            "title": "Tariff update",
            "text": "Tariff update: https://docs.ozon.ru/seller/rules/",
            "published_at": datetime(2026, 11, 18, 10, 15, tzinfo=timezone.utc),
        },
        {
            "url": f"{FALLBACK_URL}#ozon-message-seller-notify-chat-notice-43",
            "message_id": "notice-43",
            "title": "Warehouse acceptance schedule changed.",
            "text": "Warehouse acceptance schedule changed.",
            "published_at": datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc),
        },
    ]
    assert [call["url"] for call in transport.calls] == [
        f"{API_ROOT}/v3/chat/list",
        f"{API_ROOT}/v3/chat/history",
    ]
    assert transport.calls[1]["payload"]["chat_id"] == "seller-notify-chat"


@pytest.mark.parametrize(
    "response",
    [FakeResponse(200, {}), FakeResponse(200, {"chats": "not-a-list"})],
)
def test_reader_reports_empty_or_unexpected_api_payload_as_safe_api_error(response):
    """A changed response must not be silently treated as an empty successful collection."""
    client = OzonSellerClient("test-client-id", "test-api-key", transport=FixtureTransport([response]))

    with pytest.raises(OzonApiError, match="invalid_response") as error:
        read_ozon_notifications(client)

    assert "test-api-key" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403])
def test_reader_turns_authorization_denial_into_safe_api_error(status):
    """Authorization failures must leave the surrounding collection cycle able to continue."""
    client = OzonSellerClient(
        "test-client-id",
        "test-api-key",
        transport=FixtureTransport([FakeResponse(status, {"message": "credential rejected"})]),
    )

    with pytest.raises(OzonApiError, match="authorization_failed") as error:
        read_ozon_notifications(client)

    assert "test-client-id" not in str(error.value)
    assert "test-api-key" not in str(error.value)
