import json
import os
import unittest
import uuid
from unittest.mock import patch

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

    def test_schema_migrates_access_requirements_for_sources_and_candidates(self):
        expected = {"access_public", "access_requires_credentials", "access_rate_limit",
                    "access_cost", "access_checked_at", "access_check_result"}
        for table in ("sources", "source_candidates"):
            with self.subTest(table=table):
                columns = {row[0] for row in self.conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                    (self.schema, table),
                ).fetchall()}
                self.assertTrue(expected <= columns)

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
            """INSERT INTO sources (code, name, kind, url, trust, scope, category, active, managed_by,
                                    access_public, access_requires_credentials, access_rate_limit, access_cost,
                                    access_check_result)
               VALUES ('discovered_manual', 'Owner name', 'rss', 'https://owner.example/feed',
                       'official', 'owner scope', 'ozon', true, 'owner', true, false, '60/hour', 'free', 'passed')"""
        )

        db.sync_sources(self.conn)

        row = self.conn.execute(
            """SELECT name, url, scope, category, managed_by, access_public, access_requires_credentials,
                      access_rate_limit, access_cost, access_check_result
               FROM sources WHERE code='discovered_manual'"""
        ).fetchone()
        self.assertEqual(row, ("Owner name", "https://owner.example/feed", "owner scope", "ozon", "owner",
                               True, False, "60/hour", "free", "passed"))

    def test_changed_config_url_requires_new_access_check_without_touching_owner_source(self):
        class SourceConfig:
            def __init__(self, payload):
                self.payload = payload

            def read_text(self, encoding):
                if encoding != "utf-8":
                    raise AssertionError(f"unexpected encoding: {encoding}")
                return json.dumps(self.payload)

        initial = [{
            "code": "config_url_change", "name": "Configured", "kind": "rss",
            "url": "https://configured.example.org/original.xml", "trust": "official",
            "scope": "configured scope", "category": "ozon",
        }]
        config = SourceConfig(initial)
        with patch.object(db, "SOURCE_CONFIG", config):
            db.sync_sources(self.conn)
            self.conn.execute(
                """UPDATE sources SET active=true, access_public=true, access_requires_credentials=false,
                       access_rate_limit='60/hour', access_cost='free', access_checked_at=now(),
                       access_check_result='passed' WHERE code='config_url_change'"""
            )
            self.conn.execute(
                """INSERT INTO sources (code, name, kind, url, trust, scope, category, active, managed_by,
                                        access_public, access_requires_credentials, access_rate_limit, access_cost,
                                        access_check_result)
                   VALUES ('owner_unchanged', 'Owner', 'rss', 'https://owner.example.org/feed', 'official',
                           'owner scope', 'ozon', true, 'owner', true, false, '30/hour', 'free', 'passed')"""
            )
            config.payload = [{**initial[0], "url": "https://configured.example.org/changed.xml"}]
            db.sync_sources(self.conn)

        config_row = self.conn.execute(
            """SELECT url, active, access_public, access_requires_credentials, access_rate_limit, access_cost,
                      access_checked_at, access_check_result FROM sources WHERE code='config_url_change'"""
        ).fetchone()
        self.assertEqual(config_row, ("https://configured.example.org/changed.xml", False, None, None, None,
                                      "unknown", None, "unknown"))
        pending = db.list_source_candidates(self.conn, status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["collection_url"], "https://configured.example.org/changed.xml")
        owner_row = self.conn.execute(
            """SELECT url, active, access_public, access_requires_credentials, access_rate_limit, access_cost,
                      access_check_result FROM sources WHERE code='owner_unchanged'"""
        ).fetchone()
        self.assertEqual(owner_row, ("https://owner.example.org/feed", True, True, False, "30/hour", "free", "passed"))

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

    def test_paid_unknown_or_private_access_needs_manual_setup_and_never_activates_source(self):
        for access in (
            {"access_cost": "paid", "access_public": True, "access_requires_credentials": False},
            {"access_cost": "unknown", "access_public": True, "access_requires_credentials": False},
            {"access_cost": "free", "access_public": False, "access_requires_credentials": True},
        ):
            with self.subTest(access=access):
                db.upsert_source_candidates(self.conn, [self.candidate(
                    publisher_domain=f"{access['access_cost']}-{int(access['access_public'])}.example.org",
                    collection_url=f"https://{access['access_cost']}-{int(access['access_public'])}.example.org/feed",
                )])
                candidate = db.list_source_candidates(self.conn, status="all")[0]
                probe = {"ok": True, "kind": "html", "collection_url": candidate["collection_url"],
                         "error": None, **access}
                result = db.review_source_candidate(self.conn, candidate["id"], "approve", access, probe)
                self.assertEqual(result["status"], "needs_setup")
                self.assertIsNone(result["source_code"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sources WHERE managed_by='owner'").fetchone()[0], 0)

    def test_free_public_source_records_safe_access_check_and_url_change_requires_new_check(self):
        db.upsert_source_candidates(self.conn, [self.candidate(kind="rss")])
        candidate = db.list_source_candidates(self.conn)[0]
        access = {"access_public": True, "access_requires_credentials": False,
                  "access_rate_limit": "60/hour", "access_cost": "free"}
        approved = db.review_source_candidate(self.conn, candidate["id"], "approve", access, {
            "ok": True, "kind": "rss", "collection_url": candidate["collection_url"], "error": None, **access,
        })
        self.assertEqual(approved["status"], "approved")
        stored = db.list_source_candidates(self.conn, status="all")[0]
        self.assertEqual({key: stored[key] for key in access}, access)
        self.assertEqual(stored["access_check_result"], "passed")
        self.assertIsNotNone(stored["access_checked_at"])
        source = self.conn.execute(
            "SELECT access_public, access_requires_credentials, access_rate_limit, access_cost, access_check_result "
            "FROM sources WHERE code=%s", (approved["source_code"],),
        ).fetchone()
        self.assertEqual(source, (True, False, "60/hour", "free", "passed"))

        changed_url = "https://changed.example.org/feed"
        db.upsert_source_candidates(self.conn, [self.candidate(
            publisher_domain="changed.example.org", collection_url="https://changed.example.org/original-feed",
        )])
        changed = db.list_source_candidates(self.conn, status="pending")[0]
        stale_probe = {"ok": True, "kind": "html", "collection_url": "https://changed.example.org/original-feed",
                       "error": None, **access}
        result = db.review_source_candidate(self.conn, changed["id"], "approve", {
            **access, "collection_url": changed_url,
        }, stale_probe)
        self.assertEqual(result, {"status": "needs_setup", "source_code": None})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sources WHERE managed_by='owner'").fetchone()[0], 1)


    def test_approval_uses_stable_code_and_manual_source_survives_sync(self):
        db.upsert_source_candidates(self.conn, [self.candidate()])
        row = db.list_source_candidates(self.conn)[0]
        access = {"access_public": True, "access_requires_credentials": False,
                  "access_rate_limit": "60/hour", "access_cost": "free"}
        probe = {"ok": True, "kind": "rss", "collection_url": "https://feeds.example.net/seller.xml", "error": None,
                 **access}

        result = db.review_source_candidate(self.conn, row["id"], "approve", {
            "name": "Seller News Updated", "collection_url": "https://feeds.example.net/seller.xml",
            "kind": "rss", "category": "ozon", "trust": "official", "scope": "ozon sellers", **access,
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
