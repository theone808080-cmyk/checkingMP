import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

import db


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class UnreadFindingPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "unread_finding_" + uuid.uuid4().hex[:12]
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        self.addCleanup(self.conn.close)
        self.addCleanup(self._drop_schema)
        db.init_schema(self.conn)
        db.sync_sources(self.conn)
        self.now = datetime.now(timezone.utc)
        self.fresh_id = self.add_finding("fresh", self.now - timedelta(hours=71, minutes=59))
        self.old_id = self.add_finding("old", self.now - timedelta(hours=72, minutes=1))

    def _drop_schema(self):
        if not self.conn.closed:
            self.conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(self.schema)))

    def add_finding(self, key, published_at, *, collected_at=None, updated_at=None):
        finding_id = self.conn.execute(
            """INSERT INTO events(event_key, platform, topic, importance, summary, recommended_action, verification, updated_at)
               VALUES (%s, 'Ozon', 'logistics', 4, %s, 'Проверить', 'needs_check', %s) RETURNING id""",
            (key, key, updated_at or self.now),
        ).fetchone()[0]
        material_id = self.conn.execute(
            """INSERT INTO materials(source_id, url, title, text, published_at, collected_at, fingerprint)
               SELECT id, %s, %s, 'Текст источника', %s, %s, %s FROM sources
               WHERE code='ozon_telegram' RETURNING id""",
            (f"https://example.test/{key}", key, published_at, collected_at or self.now, f"unread-{key}"),
        ).fetchone()[0]
        self.conn.execute("INSERT INTO event_materials(event_id, material_id) VALUES (%s, %s)", (finding_id, material_id))
        return finding_id

    def test_read_state_persists_and_unread_fresh_queue_has_exact_72_hour_cutoff(self):
        self.assertEqual([row["id"] for row in db.feed_findings(self.conn, read="unread", period="fresh")["findings"]], [self.fresh_id])
        changed = db.set_finding_read(self.conn, self.fresh_id, True)
        self.assertTrue(changed["read"])
        self.assertEqual(db.feed_findings(self.conn, read="unread", period="fresh")["findings"], [])
        self.assertCountEqual([row["id"] for row in db.feed_findings(self.conn, read="all", period="all")["findings"]], [self.fresh_id, self.old_id])
        restored = db.set_finding_read(self.conn, self.fresh_id, False)
        self.assertFalse(restored["read"])
        db.set_finding_read(self.conn, self.old_id, True)
        db.set_finding_read(self.conn, self.old_id, False)
        self.assertEqual([row["id"] for row in db.feed_findings(self.conn, read="unread", period="fresh")["findings"]], [self.fresh_id])

    def test_cursor_never_returns_finding_marked_read_between_snapshot_pages(self):
        snapshot_first_id = self.add_finding("snapshot-first", self.now - timedelta(hours=1), updated_at=self.now - timedelta(minutes=1))
        snapshot_other_id = self.add_finding("snapshot-archived", self.now - timedelta(hours=1), updated_at=self.now - timedelta(minutes=2))
        fallback_id = self.add_finding("snapshot-fallback", None, collected_at=self.now - timedelta(hours=1), updated_at=self.now - timedelta(minutes=3))
        first_page = db.feed_findings(self.conn, read="unread", period="fresh", limit=1)
        first_page_id = first_page["findings"][0]["id"]
        self.assertIsNotNone(first_page["next_cursor"])
        archived_id = next(candidate for candidate in (snapshot_first_id, snapshot_other_id, self.fresh_id) if candidate != first_page_id)
        db.set_finding_read(self.conn, archived_id, True)
        next_page = db.feed_findings(self.conn, read="unread", period="fresh", limit=10, cursor=first_page["next_cursor"])
        next_ids = [row["id"] for row in next_page["findings"]]
        self.assertNotIn(archived_id, next_ids)
        self.assertIn(fallback_id, next_ids)


if __name__ == "__main__":
    unittest.main()
