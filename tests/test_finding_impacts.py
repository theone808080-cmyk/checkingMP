import os
import unittest
import uuid

import psycopg

import db


IMPACT_API = (
    "list_finding_impacts",
    "create_finding_impact",
    "update_finding_impact",
    "decide_finding_impact",
)


def impact_payload(**changes):
    payload = {
        "platform": "Ozon",
        "object_type": "sorting_center",
        "object_name": "СЦ Казань",
        "change_type": "delivery_tariff",
        "effective_date": None,
        "processes": ["shipment", "returns"],
        "consequence": "Потребуется проверить экономику отгрузок.",
        "source_url": "https://example.test/ozon/news",
    }
    payload.update(changes)
    return payload


class FindingImpactContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            all(hasattr(db, name) for name in IMPACT_API),
            "Task 17 must expose the finding-impact storage API through db",
        )

    def test_creator_rejects_unknown_fields_and_invalid_card_values_before_writing(self):
        invalid_payloads = (
            impact_payload(object_type="unknown"),
            impact_payload(change_type="unknown"),
            impact_payload(consequence="x" * 2001),
            impact_payload(extra="must not be accepted"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                db.create_finding_impact(None, 1, payload)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class FindingImpactPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "finding_impact_test_" + uuid.uuid4().hex[:12]
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        self.addCleanup(self.conn.close)
        self.addCleanup(self._drop_schema)
        db.init_schema(self.conn)
        db.sync_sources(self.conn)
        self.source_url = "https://example.test/ozon/news"
        material_id = self.conn.execute(
            """INSERT INTO materials(source_id, url, title, text, fingerprint)
               SELECT id, %s, 'Изменение тарифа', 'Условия доставки изменены.', 'impact-source'
               FROM sources WHERE code='ozon_telegram' RETURNING id""",
            (self.source_url,),
        ).fetchone()[0]
        self.finding_id = self.conn.execute(
            """INSERT INTO events(event_key, platform, topic, importance, summary, recommended_action, verification)
               VALUES ('impact-test', 'Ozon', 'logistics', 4, 'Изменение условий', 'Проверить', 'needs_check')
               RETURNING id"""
        ).fetchone()[0]
        self.conn.execute("INSERT INTO event_materials(event_id, material_id) VALUES (%s, %s)", (self.finding_id, material_id))

    def _drop_schema(self):
        if not self.conn.closed:
            self.conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(self.schema)))

    def test_finding_keeps_distinct_cards_and_owner_decision_changes_only_the_selected_card(self):
        first = db.create_finding_impact(self.conn, self.finding_id, impact_payload())
        second = db.create_finding_impact(
            self.conn,
            self.finding_id,
            impact_payload(object_name="СЦ Тверь", change_type="acceptance", processes=["acceptance"]),
        )

        decided = db.decide_finding_impact(self.conn, first["id"], "confirmed", "owner-42")
        cards = db.list_finding_impacts(self.conn, self.finding_id)

        self.assertEqual([card["id"] for card in cards], [first["id"], second["id"]])
        for field, expected in impact_payload().items():
            self.assertEqual(next(card for card in cards if card["id"] == first["id"])[field], expected)
        self.assertEqual(decided["status"], "confirmed")
        self.assertEqual(decided["decided_by"], "owner-42")
        self.assertIsNotNone(decided["decided_at"])
        untouched = next(card for card in cards if card["id"] == second["id"])
        self.assertEqual(untouched["status"], "needs_review")
        self.assertEqual(
            self.conn.execute("SELECT verification FROM events WHERE id=%s", (self.finding_id,)).fetchone()[0],
            "needs_check",
        )

    def test_creator_accepts_only_a_source_url_linked_to_its_finding(self):
        stored = db.create_finding_impact(self.conn, self.finding_id, impact_payload(source_url=self.source_url))
        self.assertEqual(stored["source_url"], self.source_url)
        with self.assertRaises(ValueError):
            db.create_finding_impact(
                self.conn,
                self.finding_id,
                impact_payload(source_url="https://example.test/other-news"),
            )

    def test_creator_saves_platform_only_card_with_explicitly_unknown_processes(self):
        payload = impact_payload(
            object_type="marketplace",
            object_name=None,
            change_type=None,
            effective_date=None,
            processes=[],
            consequence="",
            source_url=self.source_url,
        )
        stored = db.create_finding_impact(self.conn, self.finding_id, payload)
        self.assertEqual({field: stored[field] for field in payload}, payload)
        self.assertEqual(stored["status"], "needs_review")
        decided = db.decide_finding_impact(self.conn, stored["id"], "confirmed", "owner-42")
        self.assertEqual(decided["status"], "confirmed")

    def test_update_preserves_unknown_effective_date_and_does_not_accept_partial_or_foreign_card(self):
        card = db.create_finding_impact(self.conn, self.finding_id, impact_payload())
        updated = db.update_finding_impact(
            self.conn,
            card["id"],
            impact_payload(object_type="marketplace", object_name="", effective_date=None),
        )
        self.assertEqual(updated["object_type"], "marketplace")
        self.assertIsNone(updated["object_name"])
        self.assertIsNone(updated["effective_date"])
        with self.assertRaises(ValueError):
            db.update_finding_impact(self.conn, card["id"], {"object_name": "СЦ Омск"})
        with self.assertRaises(ValueError):
            db.decide_finding_impact(self.conn, card["id"], "unknown", "owner-42")


if __name__ == "__main__":
    unittest.main()
