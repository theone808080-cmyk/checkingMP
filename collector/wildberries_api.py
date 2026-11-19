"""Read seller-specific Wildberries tariff data without mutating the account."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation

API_ROOT = "https://common-api.wildberries.ru"
TOKEN_ENV = "WB_SELLER_API_KEY"
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 2_000_000


class WildberriesApiError(RuntimeError):
    """Safe API error code; provider response text and credentials stay private."""

    def __init__(self, code: str):
        super().__init__(code)
        self.safe_code = code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _UrllibTransport:
    def get(self, url: str, *, headers: dict, params: dict, timeout: int):
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{url}?{query}" if query else url,
            headers=headers,
            method="GET",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error
        except (urllib.error.URLError, TimeoutError, OSError):
            raise WildberriesApiError("network_error") from None


class WildberriesTariffClient:
    """Small read-only client for current seller tariffs."""

    def __init__(self, token: str, *, transport=None):
        self._token = token.strip() if isinstance(token, str) else ""
        self._transport = transport or _UrllibTransport()

    @classmethod
    def from_env(cls):
        token = os.getenv(TOKEN_ENV, "")
        if not token.strip():
            raise WildberriesApiError("credentials_missing")
        return cls(token)

    def _get_json(self, path: str, *, params: dict | None = None) -> dict:
        if not self._token:
            raise WildberriesApiError("credentials_missing")
        response = self._transport.get(
            f"{API_ROOT}{path}",
            headers={"Authorization": self._token},
            params=params or {},
            timeout=REQUEST_TIMEOUT,
        )
        status = getattr(response, "status", getattr(response, "code", 200))
        if status in (401, 403):
            _close(response)
            raise WildberriesApiError("authorization_failed")
        if status == 429:
            _close(response)
            raise WildberriesApiError("rate_limited")
        if status < 200 or status >= 300:
            _close(response)
            raise WildberriesApiError(f"http_{status}")
        try:
            if callable(getattr(response, "read", None)):
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            else:
                raw = getattr(response, "body", b"")
        finally:
            _close(response)
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise WildberriesApiError("invalid_response")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WildberriesApiError("invalid_response") from None
        if not isinstance(payload, dict):
            raise WildberriesApiError("invalid_response")
        return payload

    def read_tariffs(self) -> list[dict[str, object]]:
        today = date.today().isoformat()
        rows = []
        requests = (
            ("box", "/api/v1/tariffs/box", {"date": today}),
            ("pallet", "/api/v1/tariffs/pallet", {"date": today}),
            ("commission", "/api/v1/tariffs/commission", {}),
        )
        for kind, path, params in requests:
            payload = self._get_json(path, params=params)
            rows.extend(normalize_wb_tariffs(payload, kind=kind))
        if not rows:
            raise WildberriesApiError("invalid_response")
        return rows


def _close(response):
    if callable(getattr(response, "close", None)):
        response.close()


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


def _tariff_rows(scope: str, values: dict, field_map: tuple[tuple[str, str], ...], *, unit="RUB"):
    result = []
    for field, service in field_map:
        amount = _decimal_text(values.get(field))
        if amount is None:
            continue
        result.append({
            "stable_key": f"wildberries|{scope}|{service}",
            "platform": "Wildberries",
            "scope": scope,
            "service": service,
            "amount": amount,
            "unit": "%" if "coefficient" in service else unit,
            "approximate": False,
        })
    return result


def normalize_wb_tariffs(payload: dict, *, kind: str = "box") -> list[dict[str, object]]:
    """Keep only comparable numeric tariff values; ignore response ordering/metadata."""
    if not isinstance(payload, dict):
        raise WildberriesApiError("invalid_response")
    response = payload.get("response", {})
    if not isinstance(response, dict):
        raise WildberriesApiError("invalid_response")
    data = response.get("data", payload.get("data", payload))
    if not isinstance(data, dict):
        raise WildberriesApiError("invalid_response")
    rows = []
    if kind in ("box", "pallet", "return"):
        warehouses = data.get("warehouseList") if isinstance(data, dict) else None
        if not isinstance(warehouses, list):
            raise WildberriesApiError("invalid_response")
        prefix = {"box": "box", "pallet": "pallet", "return": ""}[kind]
        fields = (
            (f"{prefix}DeliveryBase", "delivery_base"),
            (f"{prefix}DeliveryLiter", "delivery_liter"),
            (f"{prefix}DeliveryCoefExpr", "delivery_coefficient"),
            (f"{prefix}StorageBase", "storage_base"),
            (f"{prefix}StorageLiter", "storage_liter"),
            (f"{prefix}StorageCoefExpr", "storage_coefficient"),
        )
        if kind == "pallet":
            fields = (
                ("palletDeliveryValueBase", "delivery_base"),
                ("palletDeliveryValueLiter", "delivery_liter"),
                ("palletDeliveryExpr", "delivery_coefficient"),
                ("palletStorageValueExpr", "storage_base"),
                ("palletStorageExpr", "storage_coefficient"),
            )
        elif kind == "return":
            fields = (("returnBase", "return_base"), ("returnLiter", "return_liter"))
        for item in warehouses:
            if not isinstance(item, dict):
                raise WildberriesApiError("invalid_response")
            name = item.get("warehouseName")
            if not isinstance(name, str) or not name.strip():
                continue
            rows.extend(_tariff_rows(f"warehouse:{name.strip()}", item, fields))
    elif kind == "commission":
        items = data.get("report") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise WildberriesApiError("invalid_response")
        fields = (("kgvpMarketplace", "commission_marketplace"),
                  ("kgvpSupplier", "commission_supplier"),
                  ("kgvpSupplierExpress", "commission_supplier_express"),
                  ("kgvpBooking", "commission_booking"),
                  ("kgvpPickup", "commission_pickup"))
        for item in items:
            if not isinstance(item, dict):
                raise WildberriesApiError("invalid_response")
            subject = item.get("subjectName")
            if not isinstance(subject, str) or not subject.strip():
                continue
            rows.extend(_tariff_rows(f"category:{subject.strip()}", item, fields, unit="%"))
    else:
        raise WildberriesApiError("invalid_response")
    return sorted(rows, key=lambda row: row["stable_key"])
