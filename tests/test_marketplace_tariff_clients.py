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


def test_yandex_calculator_offer_rejects_zero_category_id():
    """Allowing zero sends an API-invalid calculator offer and causes a provider 400."""
    from collector.yandex_market_api import _calculator_offer

    offer = {
        "offerId": "zero-category",
        "marketCategoryId": 0,
        "basicPrice": {"value": "100"},
        "weightDimensions": {"length": "10", "width": "10", "height": "10", "weight": "1"},
    }

    assert _calculator_offer(offer, None) is None


def test_yandex_calculator_offer_rejects_zero_basic_price():
    """A zero price is invalid for the Yandex tariff calculator."""
    from collector.yandex_market_api import _calculator_offer

    offer = {
        "offerId": "zero-price",
        "marketCategoryId": 1,
        "basicPrice": {"value": "0"},
        "weightDimensions": {"length": "10", "width": "10", "height": "10", "weight": "1"},
    }

    assert _calculator_offer(offer, None) is None


def test_yandex_400_reports_sanitized_offer_mapping_endpoint_without_secret_or_body():
    """Diagnostics need the failing route, but must not retain an ID, key, or provider text."""
    from collector.yandex_market_api import YandexMarketApiError, YandexMarketTariffClient

    raw_provider_body = "provider says key=yandex-test-key for business 987654"
    client = YandexMarketTariffClient("yandex-test-key", transport=FixtureTransport([
        FakeResponse(400, {"errors": [{"message": raw_provider_body}]}),
    ]))

    with pytest.raises(YandexMarketApiError) as error:
        client.list_offer_mappings(987654)

    message = str(error.value)
    assert message == "http_400:/v2/businesses/{business_id}/offer-mappings"
    assert "987654" not in message
    assert "yandex-test-key" not in message
    assert raw_provider_body not in message


def test_yandex_calculator_400_exposes_only_sanitized_provider_code_and_field_path():
    """Raw calculator failures must not expose offer/business IDs, keys, or provider text."""
    from collector.yandex_market_api import YandexMarketApiError, YandexMarketTariffClient

    api_key = "yandex-test-key"
    offer_id = 123456
    business_id = 987654
    raw_provider_message = (
        f"Illegal input at 'offers[0].categoryId' for offer {offer_id}, "
        f"business {business_id}, key {api_key}"
    )
    client = YandexMarketTariffClient(api_key, transport=FixtureTransport([
        FakeResponse(400, {"errors": [{"code": "ILLEGAL_INPUT", "message": raw_provider_message}]}),
    ]))

    with pytest.raises(YandexMarketApiError) as error:
        client.calculate_tariffs(
            {"campaignId": business_id, "currency": "RUR"},
            [{"categoryId": 1, "price": 100, "length": 1, "width": 1, "height": 1, "weight": 1, "quantity": 1}],
        )

    diagnostic = vars(error.value)
    assert error.value.safe_code == "http_400"
    assert error.value.safe_endpoint == "/v2/tariffs/calculate"
    assert error.value.safe_provider_code == "ILLEGAL_INPUT"
    assert error.value.safe_field_path == "offers[].categoryId"
    diagnostic_text = "\n".join((str(error.value), *(str(value) for value in diagnostic.values())))
    assert raw_provider_message not in diagnostic_text
    assert str(offer_id) not in diagnostic_text
    assert str(business_id) not in diagnostic_text
    assert api_key not in diagnostic_text


def test_yandex_calculator_400_maps_generic_allowlisted_field_without_retaining_provider_details():
    """A generic calculator field name must become a safe path without retaining provider details."""
    from collector.yandex_market_api import YandexMarketApiError, YandexMarketTariffClient

    api_key = "yandex-test-key"
    offer_id = 456
    raw_provider_message = f"Invalid categoryId value for offer {offer_id} and key {api_key}"
    client = YandexMarketTariffClient(api_key, transport=FixtureTransport([
        FakeResponse(400, {"errors": [{"code": "BAD_REQUEST", "message": raw_provider_message}]}),
    ]))

    with pytest.raises(YandexMarketApiError) as error:
        client.calculate_tariffs(
            {"campaignId": 7, "currency": "RUR"},
            [{"categoryId": 1, "price": 100, "length": 1, "width": 1, "height": 1, "weight": 1, "quantity": 1}],
        )

    diagnostic = vars(error.value)
    assert error.value.safe_code == "http_400"
    assert error.value.safe_endpoint == "/v2/tariffs/calculate"
    assert error.value.safe_provider_code == "BAD_REQUEST"
    assert error.value.safe_field_path == "offers[].categoryId"
    diagnostic_text = "\n".join((str(error.value), *(str(value) for value in diagnostic.values())))
    assert raw_provider_message not in diagnostic_text
    assert str(offer_id) not in diagnostic_text
    assert api_key not in diagnostic_text


@pytest.mark.parametrize(
    ("provider_code", "expected_safe_provider_code"),
    [
        ("SECRET_API_KEY_123", None),
        (987654, None),
        ("BAD_REQUEST", "BAD_REQUEST"),
    ],
)
def test_yandex_calculator_400_keeps_only_allowlisted_provider_codes(provider_code, expected_safe_provider_code):
    """Unknown provider codes can contain secrets or IDs and must not become diagnostics."""
    from collector.yandex_market_api import YandexMarketApiError, YandexMarketTariffClient

    client = YandexMarketTariffClient("yandex-test-key", transport=FixtureTransport([
        FakeResponse(400, {"errors": [{"code": provider_code, "message": "invalid calculator request"}]}),
    ]))

    with pytest.raises(YandexMarketApiError) as error:
        client.calculate_tariffs(
            {"campaignId": 7, "currency": "RUR"},
            [{"categoryId": 1, "price": 100, "length": 1, "width": 1, "height": 1, "weight": 1, "quantity": 1}],
        )

    diagnostic_text = "\n".join((str(error.value), *(str(value) for value in vars(error.value).values())))
    assert error.value.safe_provider_code == expected_safe_provider_code
    if expected_safe_provider_code is None:
        assert str(provider_code) not in diagnostic_text
    else:
        assert expected_safe_provider_code in diagnostic_text


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
