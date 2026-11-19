"""Contract tests for read-only marketplace tariff clients.

The production modules deliberately do not exist yet.  The intended public
interface is kept here so that implementation starts from observable behavior
rather than from a live marketplace request.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest


class FakeResponse:
    def __init__(self, status: int, body: object):
        self.status = status
        self.body = json.dumps(body).encode("utf-8")


class FixtureTransport:
    """Only the transport is replaced; client normalization stays real."""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url: str, *, headers: dict, params: dict, timeout: int):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params, "timeout": timeout})
        return self.responses.pop(0)

    def post(self, url: str, *, headers: dict, payload: dict, timeout: int, params: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "payload": payload,
                           "timeout": timeout, "params": params})
        return self.responses.pop(0)


def test_wb_client_requires_a_token_and_never_exposes_it_in_safe_error():
    """Removing the environment guard would allow a bad request with an empty credential."""
    from collector.wildberries_api import WildberriesApiError, WildberriesTariffClient

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(WildberriesApiError, match="credentials_missing") as error:
            WildberriesTariffClient.from_env()

    assert "WB_SELLER_API_KEY" not in str(error.value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (FakeResponse(401, {"detail": "token rejected"}), "authorization_failed"),
        (FakeResponse(403, {"detail": "token rejected"}), "authorization_failed"),
        (FakeResponse(429, {"detail": "limit"}), "rate_limited"),
        (FakeResponse(200, {"unexpected": []}), "invalid_response"),
    ],
)
def test_wb_client_turns_access_and_schema_failures_into_safe_codes(response, code):
    """A raw marketplace error must not leak a token or be accepted as a zero tariff."""
    from collector.wildberries_api import WildberriesApiError, WildberriesTariffClient

    client = WildberriesTariffClient("wb-test-token", transport=FixtureTransport([response]))

    with pytest.raises(WildberriesApiError, match=code) as error:
        client.read_tariffs()

    assert "wb-test-token" not in str(error.value)


def test_wb_normalizer_keeps_only_complete_comparable_rows_in_stable_order():
    """Reordering a marketplace response must not produce a tariff change."""
    from collector.wildberries_api import normalize_wb_tariffs

    payload = {
        "warehouseList": [
            {"warehouseName": "СЦ Б", "boxStorageBase": "0.20", "boxDeliveryBase": "48"},
            {"warehouseName": "СЦ А", "boxStorageBase": "0.10", "boxDeliveryBase": "42"},
            {"warehouseName": "без тарифа"},
        ]
    }

    assert normalize_wb_tariffs(payload) == [
        {
            "stable_key": "wildberries|warehouse:СЦ А|delivery_base",
            "platform": "Wildberries",
            "scope": "warehouse:СЦ А",
            "service": "delivery_base",
            "amount": "42",
            "unit": "RUB",
            "approximate": False,
        },
        {
            "stable_key": "wildberries|warehouse:СЦ А|storage_base",
            "platform": "Wildberries",
            "scope": "warehouse:СЦ А",
            "service": "storage_base",
            "amount": "0.10",
            "unit": "RUB",
            "approximate": False,
        },
        {
            "stable_key": "wildberries|warehouse:СЦ Б|delivery_base",
            "platform": "Wildberries",
            "scope": "warehouse:СЦ Б",
            "service": "delivery_base",
            "amount": "48",
            "unit": "RUB",
            "approximate": False,
        },
        {
            "stable_key": "wildberries|warehouse:СЦ Б|storage_base",
            "platform": "Wildberries",
            "scope": "warehouse:СЦ Б",
            "service": "storage_base",
            "amount": "0.20",
            "unit": "RUB",
            "approximate": False,
        },
    ]


def test_yandex_client_reads_all_offer_mapping_pages_and_splits_tariff_request_at_200_items():
    """Dropping later pages or making a 201-item request would silently miss the assortment."""
    from collector.yandex_market_api import YandexMarketTariffClient

    first_batch = [{"categoryId": 1, "price": 100, "length": 1, "width": 1, "height": 1, "weight": 1, "quantity": 1}] * 200
    last_batch = [{"categoryId": 2, "price": 200, "length": 2, "width": 2, "height": 2, "weight": 2, "quantity": 1}]
    transport = FixtureTransport([
        FakeResponse(200, {"campaigns": [{"id": 7, "business": {"id": 9}}]}),
        FakeResponse(200, {"result": {"offerMappings": [{"offer": {"offerId": "one"}}], "paging": {"nextPageToken": "next"}}}),
        FakeResponse(200, {"result": {"offerMappings": [{"offer": {"offerId": "two"}}], "paging": {}}}),
        FakeResponse(200, {"status": "OK", "result": {"offers": []}}),
        FakeResponse(200, {"status": "OK", "result": {"offers": []}}),
    ])
    client = YandexMarketTariffClient("yandex-test-key", transport=transport)

    assert client.list_campaigns() == [{"id": 7, "business": {"id": 9}}]
    assert client.list_offer_mappings(9) == [{"offer": {"offerId": "one"}}, {"offer": {"offerId": "two"}}]
    assert client.calculate_tariffs({"campaignId": 7, "currency": "RUR"}, first_batch + last_batch) == []

    tariff_requests = [call for call in transport.calls if call["url"].endswith("/v2/tariffs/calculate")]
    assert [len(call["payload"]["offers"]) for call in tariff_requests] == [200, 1]
    assert all(call["headers"] == {"Api-Key": "yandex-test-key", "Content-Type": "application/json"} for call in transport.calls)


def test_yandex_offer_mapping_pagination_sends_next_page_token_in_query_parameters():
    """The next page must be addressed explicitly or later assortment rows are repeated/lost."""
    from collector.yandex_market_api import YandexMarketTariffClient

    transport = FixtureTransport([
        FakeResponse(200, {"result": {"offerMappings": [], "paging": {"nextPageToken": "page-2"}}}),
        FakeResponse(200, {"result": {"offerMappings": [], "paging": {}}}),
    ])

    assert YandexMarketTariffClient("yandex-test-key", transport=transport).list_offer_mappings(9) == []
    mapping_calls = [call for call in transport.calls
                     if urlsplit(call["url"]).path.endswith("/v2/businesses/9/offer-mappings")]
    assert [parse_qs(urlsplit(call["url"]).query) for call in mapping_calls] == [
        {"limit": ["100"]},
        {"limit": ["100"], "pageToken": ["page-2"]},
    ]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_yandex_client_returns_safe_failure_codes_for_access_or_limit_errors(status):
    """Authorization text from the provider must not reach a project log or finding."""
    from collector.yandex_market_api import YandexMarketApiError, YandexMarketTariffClient

    client = YandexMarketTariffClient("yandex-test-key", transport=FixtureTransport([
        FakeResponse(status, {"errors": [{"message": "credential rejected"}]}),
    ]))

    with pytest.raises(YandexMarketApiError) as error:
        client.list_campaigns()

    assert str(error.value) in {"authorization_failed", "rate_limited"}
    assert "yandex-test-key" not in str(error.value)
