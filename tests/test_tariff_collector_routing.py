"""Contract tests for explicit marketplace API source routing."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_api_source_code_selects_its_provider_without_changing_ozon_reader():
    from collector import main as collector

    with patch.object(collector, "read_ozon_notifications", return_value=[{"title": "Ozon"}]), \
         patch.object(collector, "read_wildberries_tariffs", return_value=[{"title": "WB"}]), \
         patch.object(collector, "read_yandex_market_tariffs", return_value=[{"title": "Yandex"}]):
        assert list(collector.read_api_source("ozon_seller_api_notifications")) == [{"title": "Ozon"}]
        assert list(collector.read_api_source("wildberries_tariffs")) == [{"title": "WB"}]
        assert list(collector.read_api_source("yandex_market_tariffs")) == [{"title": "Yandex"}]


def test_unknown_api_source_is_rejected_instead_of_using_ozon_by_accident():
    from collector import main as collector

    with pytest.raises(ValueError, match="unknown api source"):
        list(collector.read_api_source("unexpected_api_source"))
