import os
import unittest
import uuid

import psycopg

import db


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class SourceCandidateStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "source_candidates_" + uuid.uuid4().hex
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.execute("SET search_path TO public")
        self.conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(self.schema)))
        self.conn.close()

    @staticmethod
    def candidate(**changes):
        value = {
            "publisher_domain": "seller.example.org",
            "name": "Seller News",
            "collection_url": "https://seller.example.org/",
            "kind": "html",
            "category": "industry",
            "trust": "industry",
            "scope": "marketplaces",
            "example_urls": ["https://seller.example.org/news/1"],
        }
        value.update(changes)
        return value

    def test_schema_migrates_sources_and_creates_persistent_review_queue(self):
        source_columns = {row[0] for row in self.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='sources'",
            (self.schema,),
        ).fetchall()}
        tables = {row[0] for row in self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
            (self.schema,),
        ).fetchall()}

        self.assertIn("managed_by", source_columns)
        self.assertIn("source_candidates", tables)
        self.assertIn("source_provider_state", tables)

    def test_candidate_upsert_deduplicates_domain_and_merges_examples(self):
        created = db.upsert_source_candidates(self.conn, [self.candidate()])
        again = db.upsert_source_candidates(self.conn, [self.candidate(
            publisher_domain="SELLER.EXAMPLE.ORG", name="Seller news",
            example_urls=["https://seller.example.org/news/1", "https://seller.example.org/news/2"],
        )])

        self.assertEqual(created, 1)
        self.assertEqual(again, 0)
        rows = db.list_source_candidates(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["publisher_domain"], "seller.example.org")
        self.assertEqual(rows[0]["example_urls"], [
            "https://seller.example.org/news/1", "https://seller.example.org/news/2",
        ])

    def test_source_config_sync_does_not_overwrite_manual_source(self):
        configured = db.sync_sources(self.conn)
        self.assertGreater(configured, 0)
        self.conn.execute(
            """INSERT INTO sources (code, name, kind, url, trust, scope, category, active, managed_by)
               VALUES ('discovered_manual', 'Owner name', 'rss', 'https://owner.example/feed',
                       'official', 'owner scope', 'ozon', true, 'owner')"""
        )

        db.sync_sources(self.conn)

        row = self.conn.execute(
            "SELECT name, url, scope, category, managed_by FROM sources WHERE code='discovered_manual'"
        ).fetchone()
        self.assertEqual(row, ("Owner name", "https://owner.example/feed", "owner scope", "ozon", "owner"))

    def test_exa_limit_pause_is_persisted(self):
        from datetime import datetime, timezone

        until = datetime(2026, 12, 1, tzinfo=timezone.utc)
        db.pause_exa_until(self.conn, until, "monthly free limit reached")

        self.assertEqual(db.exa_pause_until(self.conn), until)

    def test_rejected_candidate_remains_rejected_after_repeat_search(self):
        db.upsert_source_candidates(self.conn, [self.candidate()])
        candidate_id = db.list_source_candidates(self.conn)[0]["id"]

        result = db.review_source_candidate(self.conn, candidate_id, "reject", {
            "collection_url": "https://seller.example.org/updates",
        }, None)
        db.upsert_source_candidates(self.conn, [self.candidate(example_urls=[
            "https://seller.example.org/news/2",
        ])])

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(db.list_source_candidates(self.conn, status="all")[0]["status"], "rejected")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sources WHERE code LIKE 'discovered_%'").fetchone()[0], 0)

    def test_candidate_is_not_activated_without_successful_probe(self):
        db.upsert_source_candidates(self.conn, [self.candidate()])
        candidate_id = db.list_source_candidates(self.conn)[0]["id"]

        result = db.review_source_candidate(self.conn, candidate_id, "approve", {}, {
            "ok": False, "kind": None, "collection_url": "https://seller.example.org/", "error": "probe_failed",
        })

        self.assertEqual(result["status"], "needs_setup")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sources WHERE code LIKE 'discovered_%'").fetchone()[0], 0)
        self.assertEqual(db.list_source_candidates(self.conn, status="all")[0]["probe_status"], "failed")

    def test_approval_uses_stable_code_and_manual_source_survives_sync(self):
        db.upsert_source_candidates(self.conn, [self.candidate()])
        row = db.list_source_candidates(self.conn)[0]
        probe = {"ok": True, "kind": "rss", "collection_url": "https://feeds.example.net/seller.xml", "error": None}

        result = db.review_source_candidate(self.conn, row["id"], "approve", {
            "name": "Seller News Updated", "collection_url": "https://feeds.example.net/seller.xml",
            "kind": "rss", "category": "ozon", "trust": "official", "scope": "ozon sellers",
        }, probe)
        db.sync_sources(self.conn)
        source = self.conn.execute(
            "SELECT code, name, url, kind, trust, scope, category, managed_by FROM sources WHERE code=%s",
            (result["source_code"],),
        ).fetchone()

        self.assertTrue(source[0].startswith("discovered_"))
        self.assertEqual(source[1:], ("Seller News Updated", "https://feeds.example.net/seller.xml", "rss",
                                      "official", "ozon sellers", "ozon", "owner"))
        self.assertEqual(db.list_source_candidates(self.conn, status="all")[0]["status"], "approved")
        self.assertEqual(db.upsert_source_candidates(self.conn, [self.candidate()]), 0)
        self.assertEqual(len(db.list_source_candidates(self.conn, status="all")), 1)

    def test_search_does_not_enqueue_an_existing_static_source(self):
        db.sync_sources(self.conn)
        self.assertEqual(db.upsert_source_candidates(self.conn, [{
            "publisher_domain": "markirovka.ru", "name": "Markirovka",
            "collection_url": "https://markirovka.ru/", "kind": "html", "category": "chestny_znak",
            "trust": "official", "scope": "marking", "example_urls": ["https://markirovka.ru/story"],
        }]), 0)
        self.assertEqual(db.list_source_candidates(self.conn, status="all"), [])


if __name__ == "__main__":
    unittest.main()
