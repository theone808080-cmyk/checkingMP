import os
import unittest
from datetime import datetime, timedelta, timezone

import psycopg

import db


class FeedValidationTests(unittest.TestCase):
    def test_rejects_untrusted_filter_cursor_and_limit_before_query(self):
        for kwargs in ({"period": "today' OR true--"}, {"platform": "ozon' OR true--"},
                       {"cursor": "not-a-cursor"}, {"limit": 0}, {"limit": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.feed_materials(None, **kwargs)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class FeedPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute("""CREATE TEMP TABLE sources (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, url TEXT NOT NULL, trust TEXT NOT NULL, scope TEXT NOT NULL,
            active BOOLEAN DEFAULT true, last_checked_at TIMESTAMPTZ, last_error TEXT,
            category TEXT)""")
        self.conn.execute("""CREATE TEMP TABLE materials (
            id SERIAL PRIMARY KEY, source_id INTEGER NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, text TEXT, published_at TIMESTAMPTZ,
            collected_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL)""")
        db.sync_sources(self.conn)

    def add(self, source, title, published, collected, status="new"):
        self.conn.execute("""INSERT INTO materials(source_id, url, title, text, published_at, collected_at, status)
            SELECT id, %s, %s, %s, %s, %s, %s FROM sources WHERE code = %s""",
            ("https://example.org/" + title, title, "original " + title, published, collected, status, source))

    def test_categories_map_all_nine_sources_and_safe_statuses(self):
        rows = self.conn.execute("SELECT code, category FROM sources ORDER BY id").fetchall()
        self.assertEqual([category for _, category in rows],
                         ["ozon", "wildberries", "yandex", "chestny_znak", "chestny_znak",
                          "industry", "industry", "industry", "industry"])
        self.conn.execute("UPDATE sources SET last_error = 'secret error', last_checked_at = now() WHERE code = 'ozon_telegram'")
        statuses = db.feed_sources(self.conn)
        ozon = next(row for row in statuses if row["code"] == "ozon_telegram")
        self.assertEqual(ozon["status"], "error")
        self.assertNotIn("secret", str(statuses))
        self.assertIsNotNone(ozon["last_checked_at"])

    def test_filters_moscow_calendar_fallback_and_unprocessed(self):
        # Derive boundaries in Moscow independently of the server's local timezone.
        moscow = timezone(timedelta(hours=3))
        now = datetime.now(moscow)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.add("ozon_telegram", "today", midnight, midnight, "new")
        self.add("wb_telegram", "yesterday", midnight - timedelta(microseconds=1), now, "done")
        self.add("rospotrebnadzor_federal", "fallback", None, midnight, "skipped")
        self.add("crpt_breaking", "week-first", midnight - timedelta(days=6), now)
        self.add("retail_ru_telegram", "too-old", midnight - timedelta(days=6, microseconds=1), now)
        self.add("yandex_market_telegram", "future", midnight + timedelta(days=1), now)
        today = db.feed_materials(self.conn, period="today")
        self.assertEqual({x["title"] for x in today["materials"]}, {"today", "fallback"})
        week = db.feed_materials(self.conn, period="week")
        self.assertEqual({x["title"] for x in week["materials"]}, {"today", "fallback", "yesterday", "week-first"})
        industry = db.feed_materials(self.conn, platform="industry")
        self.assertEqual({x["title"] for x in industry["materials"]}, {"fallback", "too-old"})
        self.assertEqual(db.feed_materials(self.conn, platform="ozon")["materials"][0]["title"], "today")
        for platform, title in (("wildberries", "yesterday"), ("chestny_znak", "week-first")):
            self.assertEqual(db.feed_materials(self.conn, platform=platform)["materials"][0]["title"], title)
        self.assertEqual(db.feed_materials(self.conn, platform="yandex")["total"], 1)

    def test_stable_cursor_does_not_repeat_ties_or_newer_inserts(self):
        timestamp = datetime.now(timezone.utc) - timedelta(hours=1)
        for title in ("one", "two", "three"):
            self.add("ozon_telegram", title, timestamp, timestamp)
        first = db.feed_materials(self.conn, limit=2)
        self.assertEqual([x["title"] for x in first["materials"]], ["three", "two"])
        self.add("ozon_telegram", "later", timestamp + timedelta(minutes=1), timestamp)
        self.add("ozon_telegram", "late-old-publication", timestamp - timedelta(days=3), timestamp)
        second = db.feed_materials(self.conn, cursor=first["next_cursor"], limit=2)
        self.assertEqual([x["title"] for x in second["materials"]], ["one"])
        self.assertEqual(first["total"], 3)
        self.assertEqual(second["total"], 3)
        self.assertIsNone(second["next_cursor"])

    def test_first_page_and_total_are_read_in_one_database_statement(self):
        class CountingConnection:
            def __init__(self, conn):
                self.conn = conn
                self.calls = 0

            def execute(self, query, params):
                self.calls += 1
                return self.conn.execute(query, params)

        wrapped = CountingConnection(self.conn)
        result = db.feed_materials(wrapped)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["materials"], [])
        self.assertEqual(wrapped.calls, 1)

    def test_old_schema_migrates_twice_and_backfills_categories(self):
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
            conn.execute('BEGIN')
            try:
                conn.execute('CREATE SCHEMA feed_migration_test')
                conn.execute('SET LOCAL search_path = feed_migration_test')
                conn.execute("""CREATE TABLE sources (
                    id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, url TEXT NOT NULL, trust TEXT NOT NULL, scope TEXT NOT NULL,
                    active BOOLEAN DEFAULT true, last_checked_at TIMESTAMPTZ, last_error TEXT)""")
                conn.execute("INSERT INTO sources (code, name, kind, url, trust, scope) VALUES ('ozon_telegram', 'old', 'telegram', 'https://example.org', 'official', 'old')")
                db.init_schema(conn)
                db.init_schema(conn)
                db.sync_sources(conn)
                self.assertEqual(conn.execute("SELECT category FROM sources WHERE code = 'ozon_telegram'").fetchone()[0], 'ozon')
            finally:
                conn.execute('ROLLBACK')


if __name__ == "__main__":
    unittest.main()
