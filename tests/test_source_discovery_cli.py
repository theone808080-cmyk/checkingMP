import os
import json
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import psycopg

import db
import main
from source_discovery import SearchBatch, SearchHit
from source_discovery.main import discover_sources


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class SourceDiscoveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "source_discovery_" + uuid.uuid4().hex
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.execute("SET search_path TO public")
        self.conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(self.schema)))
        self.conn.close()

    def test_search_saves_grouped_candidates_and_safe_provider_status(self):
        batch = SearchBatch(hits=[
            SearchHit("One", "https://seller.example/news/1", None, "Seller", "https://seller.example/", "google_news"),
            SearchHit("Two", "https://seller.example/news/2", None, "Seller", "https://seller.example/", "exa"),
        ], errors={"google_news": "transport"})
        with patch("source_discovery.main.search_all", return_value=batch) as search:
            result = discover_sources(conn=self.conn, api_key="test-key",
                                      now=datetime(2026, 11, 10, tzinfo=timezone.utc))

        self.assertEqual(result, (1, 1))
        self.assertEqual(search.call_args.kwargs["api_key"], "test-key")
        rows = db.list_source_candidates(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["example_urls"], ["https://seller.example/news/1", "https://seller.example/news/2"])
        run = self.conn.execute("SELECT service, status, items, note FROM runs").fetchone()
        self.assertEqual(run[:3], ("source_discovery", "finished", 1))
        self.assertNotIn("api_key", run[3])
        self.assertEqual(json.loads(run[3])["providers"], {"google_news": "error:transport", "exa": "ok"})

    def test_exa_limit_pauses_until_next_utc_month_and_rss_results_are_kept(self):
        batch = SearchBatch(hits=[
            SearchHit("Story", "https://seller.example/news", None, "Seller", "https://seller.example/", "google_news"),
        ], errors={"exa": "limit_reached"}, exa_limited=True)
        with patch("source_discovery.main.search_all", return_value=batch):
            result = discover_sources(conn=self.conn, api_key="private-test-key",
                                      now=datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc))

        self.assertEqual(result, (1, 1))
        self.assertEqual(db.exa_pause_until(self.conn), datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(len(db.list_source_candidates(self.conn)), 1)

    def test_explicit_command_is_separate_from_regular_cycle(self):
        self.assertIn("discover-sources", main.COMMANDS)
        self.assertIsNot(main.COMMANDS["discover-sources"], main.cycle)


if __name__ == "__main__":
    unittest.main()
