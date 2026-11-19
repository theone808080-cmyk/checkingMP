"""Contract tests for deterministic tariff-change detection."""

from __future__ import annotations


def _row(key: str, amount: str, *, platform: str = "Wildberries", approximate: bool = False) -> dict:
    return {
        "stable_key": key,
        "platform": platform,
        "scope": "warehouse:СЦ А",
        "service": "storage_base",
        "amount": amount,
        "unit": "RUB",
        "approximate": approximate,
    }


def test_first_successful_snapshot_is_a_baseline_and_creates_no_delta():
    """Treating a baseline as a change would generate false news on first deployment."""
    from collector.tariff_snapshots import compare_tariff_snapshots

    assert compare_tariff_snapshots(None, [_row("wildberries|warehouse:СЦ А|storage_base", "0.10")]) == []


def test_identical_snapshot_in_different_response_order_creates_no_delta():
    """Changing only API array order must not create a duplicate tariff finding."""
    from collector.tariff_snapshots import compare_tariff_snapshots

    before = [_row("wb|A", "0.10"), _row("wb|B", "48")]
    after = [_row("wb|B", "48"), _row("wb|A", "0.10")]

    assert compare_tariff_snapshots(before, after) == []


def test_changed_value_creates_one_stably_sorted_delta_with_before_and_after_values():
    """Ignoring the old value or returning nondeterministic order would make owner review unreliable."""
    from collector.tariff_snapshots import compare_tariff_snapshots

    before = [_row("wb|B", "48"), _row("wb|A", "0.10")]
    after = [_row("wb|B", "50"), _row("wb|A", "0.15")]

    assert compare_tariff_snapshots(before, after) == [
        {"stable_key": "wb|A", "before": "0.10", "after": "0.15", "approximate": False},
        {"stable_key": "wb|B", "before": "48", "after": "50", "approximate": False},
    ]


def test_yandex_tariff_delta_preserves_its_estimate_label():
    """Losing this label would present a calculator estimate as an actual charge."""
    from collector.tariff_snapshots import compare_tariff_snapshots

    before = [_row("yandex|offer:one|FEE", "12.00", platform="Яндекс Маркет", approximate=True)]
    after = [_row("yandex|offer:one|FEE", "13.00", platform="Яндекс Маркет", approximate=True)]

    assert compare_tariff_snapshots(before, after) == [
        {"stable_key": "yandex|offer:one|FEE", "before": "12.00", "after": "13.00", "approximate": True},
    ]


def test_failed_marketplace_does_not_hide_successful_rows_from_the_other_marketplace():
    """A WB outage must not prevent a Yandex snapshot from being recorded."""
    from collector.tariff_snapshots import collect_tariff_sources

    def broken():
        raise RuntimeError("provider detail that must stay inside the source")

    result = collect_tariff_sources({
        "wildberries_tariffs": broken,
        "yandex_market_tariffs": lambda: [_row("yandex|offer:one|FEE", "12.00", platform="Яндекс Маркет", approximate=True)],
    })

    assert result == {
        "rows": [_row("yandex|offer:one|FEE", "12.00", platform="Яндекс Маркет", approximate=True)],
        "failures": {"wildberries_tariffs": "source_fetch_failed"},
    }
