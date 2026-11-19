"""Read Yandex Market catalog data and estimated service tariffs."""

from __future__ import annotations

import json
import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

API_ROOT = "https://api.partner.market.yandex.ru"
TOKEN_ENV = "YANDEX_MARKET_API_KEY"
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 2_000_000
MAX_OFFERS_PER_TARIFF_REQUEST = 200


class YandexMarketApiError(RuntimeError):
    """Safe API error code; raw responses and credentials are never retained."""

    def __init__(self, code: str):
        super().__init__(code)
        self.safe_code = code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _UrllibTransport:
    def _send(self, url: str, *, headers: dict, payload: dict | None, timeout: int):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="GET" if payload is None else "POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error
        except (urllib.error.URLError, TimeoutError, OSError):
            raise YandexMarketApiError("network_error") from None

    def get(self, url: str, *, headers: dict, params: dict, timeout: int):
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        return self._send(f"{url}?{query}" if query else url, headers=headers, payload=None, timeout=timeout)

    def post(self, url: str, *, headers: dict, payload: dict, timeout: int):
        return self._send(url, headers=headers, payload=payload, timeout=timeout)


class YandexMarketTariffClient:
    """Read-only catalog and tariff calculator client for the partner API."""

    def __init__(self, api_key: str, *, transport=None):
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._transport = transport or _UrllibTransport()

    @classmethod
    def from_env(cls):
        key = os.getenv(TOKEN_ENV, "")
        if not key.strip():
            raise YandexMarketApiError("credentials_missing")
        return cls(key)

    def _request(self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None):
        if not self._api_key:
            raise YandexMarketApiError("credentials_missing")
        headers = {"Api-Key": self._api_key, "Content-Type": "application/json"}
        url = f"{API_ROOT}{path}"
        if method == "GET":
            response = self._transport.get(url, headers=headers, params=params or {}, timeout=REQUEST_TIMEOUT)
        else:
            query = urllib.parse.urlencode({key: value for key, value in (params or {}).items()
                                            if value is not None})
            request_url = f"{url}?{query}" if query else url
            response = self._transport.post(request_url, headers=headers, payload=payload or {},
                                            timeout=REQUEST_TIMEOUT)
        status = getattr(response, "status", getattr(response, "code", 200))
        if status in (401, 403):
            _close(response)
            raise YandexMarketApiError("authorization_failed")
        if status == 429:
            _close(response)
            raise YandexMarketApiError("rate_limited")
        if status < 200 or status >= 300:
            _close(response)
            raise YandexMarketApiError(f"http_{status}")
        try:
            if callable(getattr(response, "read", None)):
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            else:
                raw = getattr(response, "body", b"")
        finally:
            _close(response)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise YandexMarketApiError("invalid_response")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise YandexMarketApiError("invalid_response") from None
        if not isinstance(result, dict):
            raise YandexMarketApiError("invalid_response")
        return result

    def list_campaigns(self) -> list[dict]:
        payload = self._request("GET", "/v2/campaigns")
        result = payload.get("result", payload)
        campaigns = result.get("campaigns") if isinstance(result, dict) else None
        if not isinstance(campaigns, list) or any(not isinstance(row, dict) for row in campaigns):
            raise YandexMarketApiError("invalid_response")
        return campaigns

    def list_offer_mappings(self, business_id: int) -> list[dict]:
        if type(business_id) is not int or business_id < 1:
            raise YandexMarketApiError("invalid_response")
        rows = []
        page_token = None
        seen_tokens = set()
        while True:
            params = {"limit": 100, "pageToken": page_token}
            payload = self._request(
                "POST", f"/v2/businesses/{business_id}/offer-mappings", params=params, payload={}
            )
            result = payload.get("result")
            page_rows = result.get("offerMappings") if isinstance(result, dict) else None
            paging = result.get("paging") if isinstance(result, dict) else None
            if not isinstance(page_rows, list) or any(not isinstance(row, dict) for row in page_rows):
                raise YandexMarketApiError("invalid_response")
            rows.extend(page_rows)
            next_token = paging.get("nextPageToken") if isinstance(paging, dict) else None
            if not next_token:
                return rows
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise YandexMarketApiError("invalid_response")
            seen_tokens.add(next_token)
            page_token = next_token

    def calculate_tariffs(self, parameters: dict, offers: list[dict]) -> list[dict]:
        if not isinstance(parameters, dict) or not isinstance(offers, list):
            raise YandexMarketApiError("invalid_response")
        if not offers:
            return []
        rows = []
        for start in range(0, len(offers), MAX_OFFERS_PER_TARIFF_REQUEST):
            batch = offers[start:start + MAX_OFFERS_PER_TARIFF_REQUEST]
            payload = self._request(
                "POST", "/v2/tariffs/calculate",
                payload={"parameters": parameters, "offers": batch},
            )
            result = payload.get("result")
            calculated = result.get("offers") if isinstance(result, dict) else None
            if payload.get("status") not in (None, "OK") or not isinstance(calculated, list):
                raise YandexMarketApiError("invalid_response")
            rows.extend(calculated)
        if any(not isinstance(row, dict) for row in rows):
            raise YandexMarketApiError("invalid_response")
        return rows

    def read_tariffs(self) -> list[dict[str, object]]:
        """Calculate approximate tariffs for active, fully specified catalog offers."""
        campaigns = self.list_campaigns()
        business_ids = sorted({
            business_id for campaign in campaigns
            if (business_id := _positive_int((campaign.get("business") or {}).get("id")
                                             if isinstance(campaign.get("business"), dict)
                                             else campaign.get("businessId"))) is not None
        })
        catalog_by_business = {business_id: self.list_offer_mappings(business_id) for business_id in business_ids}
        output = []
        for campaign in campaigns:
            campaign_id = _positive_int(campaign.get("id") or campaign.get("campaignId"))
            business = campaign.get("business")
            business_id = _positive_int(business.get("id") if isinstance(business, dict) else campaign.get("businessId"))
            if campaign_id is None or business_id is None:
                continue
            selected = []
            metadata = []
            for mapping in catalog_by_business.get(business_id, []):
                offer = mapping.get("offer")
                if not isinstance(offer, dict) or offer.get("archived") is True:
                    continue
                if not _offer_in_campaign(offer, campaign_id):
                    continue
                params = _calculator_offer(offer, mapping.get("mapping"))
                if params is None:
                    continue
                selected.append(params[0])
                basis = json.dumps(params[0], sort_keys=True, separators=(",", ":"))
                metadata.append((params[1], str(offer.get("offerId", "")),
                                 hashlib.sha256(basis.encode("utf-8")).hexdigest()))
            if not selected:
                continue
            calculated = self.calculate_tariffs({"campaignId": campaign_id, "currency": "RUR"}, selected)
            if len(calculated) != len(metadata):
                raise YandexMarketApiError("invalid_response")
            for result, (scope, offer_id, comparison_basis) in zip(calculated, metadata):
                tariffs = result.get("tariffs")
                if not isinstance(tariffs, list):
                    raise YandexMarketApiError("invalid_response")
                for tariff in tariffs:
                    if not isinstance(tariff, dict):
                        raise YandexMarketApiError("invalid_response")
                    amount = _decimal_text(tariff.get("amount"))
                    service = tariff.get("type")
                    if amount is None or not isinstance(service, str) or not service:
                        continue
                    key = f"yandex|campaign:{campaign_id}|offer:{offer_id}|service:{service}"
                    output.append({
                        "stable_key": key,
                        "platform": "Яндекс Маркет",
                        "scope": f"campaign:{campaign_id}",
                        "service": service,
                        "amount": amount,
                        "unit": str(tariff.get("currency") or "RUR"),
                        "approximate": True,
                        "comparison_basis": comparison_basis,
                    })
        if not output and campaigns:
            # An empty/partial catalog must not silently replace the previous successful snapshot.
            raise YandexMarketApiError("no_calculable_offers")
        return sorted(output, key=lambda row: row["stable_key"])


def _close(response):
    if callable(getattr(response, "close", None)):
        response.close()


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _offer_in_campaign(offer: dict, campaign_id: int) -> bool:
    campaigns = offer.get("campaigns")
    if not isinstance(campaigns, list):
        return False
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        current_id = _positive_int(campaign.get("campaignId") or campaign.get("id"))
        if current_id == campaign_id and campaign.get("status") == "PUBLISHED":
            return True
    return False


def _calculator_offer(offer: dict, mapping: object) -> tuple[dict, str] | None:
    dims = offer.get("weightDimensions")
    price = offer.get("basicPrice")
    category = offer.get("marketCategoryId")
    if not isinstance(category, int) or isinstance(category, bool):
        category = mapping.get("marketCategoryId") if isinstance(mapping, dict) else None
    if not isinstance(dims, dict) or not isinstance(price, dict) or not isinstance(category, int):
        return None
    values = {name: _decimal_text(dims.get(name)) for name in ("length", "width", "height", "weight")}
    amount = _decimal_text(price.get("value"))
    if amount is None or any(value is None or Decimal(value) <= 0 for value in values.values()):
        return None
    offer_id = offer.get("offerId")
    if not isinstance(offer_id, str) or not offer_id.strip():
        return None
    return ({
        "categoryId": category,
        "price": float(amount),
        "length": float(values["length"]),
        "width": float(values["width"]),
        "height": float(values["height"]),
        "weight": float(values["weight"]),
        "quantity": 1,
    }, f"offer:{offer_id.strip()}")


def _decimal_text(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    try:
        if not text or not Decimal(text).is_finite():
            return None
    except InvalidOperation:
        return None
    return text
