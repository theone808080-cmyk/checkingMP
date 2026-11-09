import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

import db


class FindingFilterValidationTests(unittest.TestCase):
    def test_rejects_invalid_filters_before_query(self):
        invalid = (
            {"period": "today' OR true--"},
            {"platform": "unknown"},
            {"importance": True},
            {"importance": 0},
            {"importance": 6},
            {"importance": "3"},
            {"verification": "maybe"},
            {"trust": "untrusted"},
            {"limit": 0},
            {"limit": True},
            {"cursor": "not-a-cursor"},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.feed_findings(None, **kwargs)


class FindingQueryTests(unittest.TestCase):
    def test_builds_parameterized_query_with_all_optional_filters(self):
        class Result:
            def fetchone(self):
                return ([],)

            def fetchall(self):
                return []

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, query, params):
                self.calls.append((query, params))
                return Result()

        conn = Connection()
        result = db.feed_findings(conn, period="today", platform="ozon", importance=4, topic="log%", verification="needs_check", trust="official")
        self.assertEqual(result, {"findings": [], "total": 0, "next_cursor": None})
        self.assertEqual(len(conn.calls), 2)
        query, params = conn.calls[0]
        self.assertIn("EXISTS (SELECT 1 FROM event_materials pem", query)
        self.assertIn("EXISTS (SELECT 1 FROM event_materials tem", query)
        self.assertIn("e.importance = %s", query)
        self.assertIn("strpos(lower(e.topic), lower(%s)) > 0", query)
        self.assertEqual(params[3:8], (["ozon"], db._FINDING_PLATFORM_PATTERNS["ozon"], 4, "log%", "needs_check"))
        self.assertEqual(params[8], "official")
        self.assertNotIn("log%", query)

    def test_cursor_is_filter_bound_and_preserves_keyset(self):
        fingerprint = db._finding_filter_hash("all", "ozon", 4, "log", "needs_check", "official")
        snapshot_id = uuid.uuid4()
        cursor = db._encode_findings_cursor(snapshot_id, 20, fingerprint)
        self.assertEqual(db._decode_findings_cursor(cursor, fingerprint), (snapshot_id, 20))
        with self.assertRaises(ValueError):
            db._decode_findings_cursor(cursor, "different-filter-hash")

    def test_findings_snapshot_keeps_result_order_for_later_pages(self):
        class Result:
            def fetchone(self):
                return ([9, 3, 7],)

            def fetchall(self):
                return []

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, query, params):
                self.calls.append((query, params))
                return Result()

        conn = Connection()
        result = db.feed_findings(conn, limit=2)
        snapshot_query, _ = conn.calls[0]
        self.assertIn("INSERT INTO finding_snapshots", snapshot_query)
        self.assertIn("array_agg(id ORDER BY specificity ASC, importance DESC, updated_at DESC, id DESC)", snapshot_query)
        self.assertEqual(result["total"], 3)
        self.assertEqual(db._decode_findings_cursor(result["next_cursor"], db._finding_filter_hash("all", "all", None, None, None, None))[1], 2)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class FindingPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        self.schema = "finding_test_" + uuid.uuid4().hex
        self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn.execute(f'SET search_path TO "{self.schema}"')
        db.init_schema(self.conn)
        db.sync_sources(self.conn)

    def tearDown(self):
        self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def add_material(self, source_code, slug, published):
        return self.conn.execute(
            """INSERT INTO materials(source_id, url, title, text, published_at, fingerprint)
               SELECT id, %s, %s, %s, %s, %s FROM sources WHERE code = %s RETURNING id""",
            (f"https://example.org/{slug}", slug, "source text", published, slug, source_code),
        ).fetchone()[0]

    def add_finding(self, key, platform, importance, updated, materials, verification="needs_check", topic="rules", effective_date=None):
        event_id = self.conn.execute(
            """INSERT INTO events(event_key, platform, topic, importance, summary, recommended_action,
                                  verification, effective_date, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (key, platform, topic, importance, key, "Review the change", verification, effective_date, updated),
        ).fetchone()[0]
        for material_id in materials:
            self.conn.execute("INSERT INTO event_materials(event_id, material_id) VALUES (%s, %s)", (event_id, material_id))
        return event_id

    def test_sorts_specific_findings_before_general_and_returns_all_sources(self):
        stamp = datetime.now(timezone.utc)
        ozon = self.add_material("ozon_telegram", "ozon", stamp)
        wb = self.add_material("wb_telegram", "wb", stamp)
        regulator = self.add_material("rospotrebnadzor_federal", "regulator", stamp)
        industry = self.add_material("retail_ru_telegram", "industry", stamp)
        ozon_second_source = self.add_material("ozon_telegram", "ozon-second", stamp)
        self.add_finding("ozon-low", "Ozon", 2, stamp, [ozon, ozon_second_source])
        self.add_finding("wb-high", "Wildberries", 5, stamp, [wb])
        self.add_finding("regulator", "industry", 5, stamp, [regulator])
        self.add_finding("industry", "industry", 5, stamp, [industry])

        result = db.feed_findings(self.conn, limit=20)
        self.assertEqual([item["summary"] for item in result["findings"]], ["wb-high", "ozon-low", "regulator", "industry"])
        self.assertEqual({source["url"] for source in result["findings"][1]["sources"]},
                         {"https://example.org/ozon", "https://example.org/ozon-second"})

    def test_platform_filter_matches_legacy_display_aliases(self):
        stamp = datetime.now(timezone.utc)
        material = self.add_material("yandex_market_telegram", "legacy-yandex", stamp)
        finding_id = self.add_finding("legacy-yandex", "Яндекс.Маркет", 4, stamp, [material])
        result = db.feed_findings(self.conn, platform="yandex")
        self.assertEqual([item["id"] for item in result["findings"]], [finding_id])

    def test_marketplace_filter_includes_cross_platform_finding(self):
        stamp = datetime.now(timezone.utc)
        material = self.add_material("ozon_telegram", "cross-market", stamp)
        finding_id = self.add_finding("cross-market", "Межплощадочная: Ozon, Wildberries", 4, stamp, [material])
        for platform in ("ozon", "wildberries"):
            with self.subTest(platform=platform):
                result = db.feed_findings(self.conn, platform=platform)
                self.assertEqual([item["id"] for item in result["findings"]], [finding_id])

    def test_combined_filters_and_cursor_are_bound_to_filter_values(self):
        stamp = datetime.now(timezone.utc)
        ozon = self.add_material("ozon_telegram", "today-ozon", stamp)
        ozon_second = self.add_material("ozon_telegram", "today-ozon-second", stamp - timedelta(seconds=2))
        wb = self.add_material("wb_telegram", "today-wb", stamp)
        event_ids = [
            self.add_finding("first", "Ozon", 5, stamp, [ozon], topic="logistics"),
            self.add_finding("second", "Wildberries", 5, stamp - timedelta(seconds=1), [wb], topic="logistics"),
            self.add_finding("third", "Ozon", 5, stamp - timedelta(seconds=2), [ozon_second], topic="logistics"),
        ]
        first = db.feed_findings(self.conn, period="today", platform="ozon", importance=5, topic="log", verification="needs_check", trust="official", limit=1)
        self.assertEqual([item["id"] for item in first["findings"]], [event_ids[0]])
        self.assertIsNotNone(first["next_cursor"])
        second = db.feed_findings(self.conn, period="today", platform="ozon", importance=5, topic="log", verification="needs_check", trust="official", cursor=first["next_cursor"], limit=1)
        self.assertEqual([item["id"] for item in second["findings"]], [event_ids[2]])
        with self.assertRaises(ValueError):
            db.feed_findings(self.conn, period="today", platform="wildberries", importance=5, topic="log", verification="needs_check", trust="official", cursor=first["next_cursor"], limit=1)

    def test_snapshot_keeps_order_when_new_findings_arrive_between_pages(self):
        stamp = datetime.now(timezone.utc)
        old_ids = []
        for index in range(3):
            material = self.add_material("ozon_telegram", f"snapshot-old-{index}", stamp - timedelta(seconds=index))
            old_ids.append(self.add_finding(f"snapshot-old-{index}", "Ozon", 5, stamp - timedelta(seconds=index), [material]))
        first = db.feed_findings(self.conn, limit=1)
        self.assertEqual([item["id"] for item in first["findings"]], [old_ids[0]])
        new_material = self.add_material("ozon_telegram", "snapshot-new", stamp + timedelta(minutes=1))
        self.add_finding("snapshot-new", "Ozon", 5, stamp + timedelta(minutes=1), [new_material])
        second = db.feed_findings(self.conn, cursor=first["next_cursor"], limit=1)
        self.assertEqual([item["id"] for item in second["findings"]], [old_ids[1]])
        self.assertEqual(second["total"], 3)

    def test_legacy_severity_migrates_twice_and_keeps_event_material_link(self):
        schema = "legacy_event_" + uuid.uuid4().hex
        self.conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            self.conn.execute(f'SET search_path TO "{schema}"')
            self.conn.execute("""CREATE TABLE events (
                id SERIAL PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, platform TEXT NOT NULL, topic TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (severity IN ('critical','important','info')), summary TEXT NOT NULL,
                recommended_action TEXT NOT NULL, verification TEXT NOT NULL CHECK (verification IN ('confirmed','needs_check')),
                effective_date DATE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            self.conn.execute("""CREATE TABLE sources (
                id SERIAL PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, kind TEXT NOT NULL,
                url TEXT NOT NULL, trust TEXT NOT NULL, scope TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT true,
                last_checked_at TIMESTAMPTZ, last_error TEXT)""")
            self.conn.execute("""CREATE TABLE materials (
                id SERIAL PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), run_id INTEGER,
                url TEXT NOT NULL, title TEXT NOT NULL, text TEXT, published_at TIMESTAMPTZ,
                collected_at TIMESTAMPTZ NOT NULL DEFAULT now(), fingerprint TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'done')""")
            self.conn.execute("CREATE TABLE event_materials (event_id INTEGER REFERENCES events(id), material_id INTEGER UNIQUE REFERENCES materials(id), PRIMARY KEY(event_id,material_id))")
            self.conn.execute("INSERT INTO sources(code,name,kind,url,trust,scope) VALUES ('ozon_telegram','Ozon','telegram','https://example.org','official','Ozon')")
            self.conn.execute("INSERT INTO materials(source_id,url,title,fingerprint) SELECT id,'https://example.org/one','one','one' FROM sources")
            self.conn.execute("""INSERT INTO events(event_key,platform,topic,severity,summary,recommended_action,verification) VALUES
                ('old-critical','Ozon','rules','critical','old critical','check','needs_check'),
                ('old-important','Ozon','rules','important','old important','check','needs_check'),
                ('old-info','Ozon','rules','info','old info','check','needs_check')""")
            self.conn.execute("INSERT INTO event_materials(event_id,material_id) VALUES (1,1)")
            db.init_schema(self.conn)
            db.init_schema(self.conn)
            self.assertEqual(dict(self.conn.execute("SELECT event_key, importance FROM events").fetchall()),
                             {"old-critical": 5, "old-important": 3, "old-info": 1})
            self.assertEqual(self.conn.execute("SELECT count(*) FROM event_materials WHERE event_id=1 AND material_id=1").fetchone()[0], 1)
        finally:
            self.conn.execute("SET search_path TO public")
            self.conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


if __name__ == "__main__":
    unittest.main()
