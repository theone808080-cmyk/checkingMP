"""Database contract for tariff snapshots and deterministic owner-review findings."""

from __future__ import annotations

import os
import unittest
import uuid

import psycopg

import db


def tariff_row(amount: str) -> dict:
    return {
        "stable_key": "wildberries|warehouse:SC-A|storage_base",
        "platform": "Wildberries",
        "scope": "warehouse:SC-A",
        "service": "storage_base",
        "amount": amount,
        "unit": "RUB",
        "approximate": False,
    }


class TariffMonitorPublicApiTests(unittest.TestCase):
    def test_monitor_exposes_a_single_transactional_snapshot_entry_point(self):
        """The collector needs one API so a baseline cannot partly create a finding."""
        from collector.tariff_monitor import apply_tariff_snapshot

        self.assertTrue(callable(apply_tariff_snapshot))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class TariffSnapshotPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "tariff_snapshot_test_" + uuid.uuid4().hex[:12]
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        self.addCleanup(self.conn.close)
        self.addCleanup(self._drop_schema)
        db.init_schema(self.conn)
        self.source_id = self.conn.execute(
            """INSERT INTO sources(code, name, kind, url, trust, category, scope, active)
               VALUES ('wb_tariffs_test', 'WB tariff API test', 'api',
                       'https://common-api.wildberries.ru', 'official', 'wildberries',
                       'read-only seller tariffs', true)
               RETURNING id"""
        ).fetchone()[0]

    def _drop_schema(self):
        if not self.conn.closed:
            self.conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(self.schema)))

    def _apply(self, rows):
        from collector.tariff_monitor import apply_tariff_snapshot

        return apply_tariff_snapshot(
            self.conn,
            source_id=self.source_id,
            source_code="wb_tariffs_test",
            rows=rows,
        )

    def test_first_snapshot_is_saved_as_a_baseline_without_a_finding(self):
        result = self._apply([tariff_row("0.10")])

        self.assertEqual(result["baseline"], True)
        self.assertEqual(result["findings_created"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_changed_tariff_creates_one_high_priority_finding_and_owner_review_card(self):
        self._apply([tariff_row("0.10")])
        result = self._apply([tariff_row("0.15")])

        self.assertEqual(result["baseline"], False)
        self.assertEqual(result["findings_created"], 1)
        finding = self.conn.execute(
            "SELECT platform, topic, importance, verification, summary FROM events"
        ).fetchone()
        self.assertEqual(finding[:4], ("Wildberries", "tariffs", 5, "needs_check"))
        self.assertIn("0.10", finding[4])
        self.assertIn("0.15", finding[4])
        card = self.conn.execute(
            """SELECT platform, object_type, object_name, change_type, effective_date, status
               FROM finding_impacts"""
        ).fetchone()
        self.assertEqual(card, ("Wildberries", "sorting_center", "SC-A", "storage", None, "needs_review"))

    def test_repeating_the_same_snapshot_creates_no_duplicate_finding_or_card(self):
        self._apply([tariff_row("0.10")])
        self._apply([tariff_row("0.15")])
        result = self._apply([tariff_row("0.15")])

        self.assertEqual(result["findings_created"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM finding_impacts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
