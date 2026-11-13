import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
            {"seller_impact": "unknown"},
            {"summary_audit": "unknown"},
            {"recommendation_audit": "unknown"},
            {"limit": 0},
            {"limit": True},
            {"cursor": "not-a-cursor"},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.feed_findings(None, **kwargs)

    def test_filter_counts_reject_invalid_filters_before_query(self):
        for filters in (
            {"period": "tomorrow"},
            {"platform": "unknown"},
            {"importance": True},
            {"importance": 6},
            {"verification": "unknown"},
            {"trust": "unknown"},
            {"seller_impact": "unknown"},
            {"summary_audit": "unknown"},
            {"recommendation_audit": "unknown"},
        ):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                db.finding_filter_counts(None, filters)


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
        result = db.feed_findings(
            conn, period="today", platform="ozon", importance=4, topic="log%", verification="needs_check",
            trust="official", seller_impact="negative", summary_audit="ok", recommendation_audit="review",
        )
        self.assertEqual(result, {"findings": [], "total": 0, "next_cursor": None})
        self.assertEqual(len(conn.calls), 2)
        query, params = conn.calls[0]
        self.assertIn("EXISTS (SELECT 1 FROM event_materials pem", query)
        self.assertIn("EXISTS (SELECT 1 FROM event_materials tem", query)
        self.assertIn("e.importance = %s", query)
        self.assertIn("strpos(lower(e.topic), lower(%s)) > 0", query)
        self.assertIn("COALESCE(ev.seller_impact,e.seller_impact) = %s", query)
        self.assertIn("finding_quality_results", query)
        self.assertEqual(params[4:9], (["ozon"], db._FINDING_PLATFORM_PATTERNS["ozon"], 4, "log%", "needs_check"))
        self.assertEqual(params[9:13], ("official", "negative", "ok", "review"))
        self.assertNotIn("log%", query)

    def test_cursor_is_filter_bound_and_preserves_keyset(self):
        fingerprint = db._finding_filter_hash(
            "all", "ozon", 4, "log", "needs_check", "official", "negative", "ok", "review"
        )
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
        self.assertIn("array_agg(id ORDER BY usefulness_rank ASC, importance DESC, updated_at DESC, id DESC)", snapshot_query)
        self.assertEqual(result["total"], 3)
        self.assertEqual(
            db._decode_findings_cursor(
                result["next_cursor"],
                db._finding_filter_hash("all", "all", None, None, None, None, None, None, None),
            )[1],
            2,
        )


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

    def add_assessment(self, material_id, classification, *, relevance=None, seller_impact="neutral"):
        relevance = relevance or {
            "practical_change": "relevant", "seller_action_or_risk": "relevant",
            "industry_context": "relevant", "irrelevant": "irrelevant", "uncertain": "uncertain",
        }[classification]
        self.conn.execute(
            """INSERT INTO relevance_assessments
               (material_id, rules_version, classification, relevance, topic, seller_impact, score, explanation)
               VALUES (%s, %s, %s, %s, 'rules', %s, 80, 'test assessment')""",
            (material_id, db.active_relevance_version(), classification, relevance, seller_impact),
        )

    def add_audit_result(self, finding_id, target, status, *, started_at):
        run_id = self.conn.execute(
            """INSERT INTO finding_quality_runs (rules_version, model, requested_limit, status, started_at, finished_at)
               VALUES ('test', 'test', 1, 'success', %s, %s) RETURNING id""",
            (started_at, started_at),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO finding_quality_run_findings (run_id, finding_id) VALUES (%s, %s)",
            (run_id, finding_id),
        )
        issues = '[]' if status == 'ok' else '[{"type":"unsupported_claim"}]'
        self.conn.execute(
            """INSERT INTO finding_quality_results (run_id, finding_id, target, status, issues, created_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s)""",
            (run_id, finding_id, target, status, issues, started_at),
        )

    def test_orders_by_practical_usefulness_then_importance_freshness_and_id(self):
        stamp = datetime.now(timezone.utc)
        practical_low = self.add_material("ozon_telegram", "practical-low", stamp)
        practical_high = self.add_material("ozon_telegram", "practical-high", stamp)
        seller_action = self.add_material("ozon_telegram", "seller-action", stamp)
        context = self.add_material("ozon_telegram", "context", stamp + timedelta(days=1))
        uncertain = self.add_material("ozon_telegram", "uncertain", stamp + timedelta(days=2))
        unassessed = self.add_material("ozon_telegram", "unassessed", stamp + timedelta(days=3))
        ids = [
            self.add_finding("practical-low", "Ozon", 3, stamp, [practical_low]),
            self.add_finding("practical-high", "Ozon", 5, stamp, [practical_high]),
            self.add_finding("seller-action", "Ozon", 5, stamp, [seller_action]),
            self.add_finding("context", "Ozon", 5, stamp + timedelta(days=1), [context]),
            self.add_finding("uncertain", "Ozon", 5, stamp + timedelta(days=2), [uncertain]),
            self.add_finding("unassessed", "Ozon", 5, stamp + timedelta(days=3), [unassessed]),
        ]
        self.add_assessment(practical_low, "practical_change")
        self.add_assessment(practical_high, "practical_change")
        self.add_assessment(seller_action, "seller_action_or_risk")
        self.add_assessment(context, "industry_context")
        self.add_assessment(uncertain, "uncertain")

        findings = db.feed_findings(self.conn, limit=20)["findings"]
        self.assertEqual([item["id"] for item in findings], [ids[1], ids[0], ids[2], ids[3], ids[4], ids[5]])
        self.assertEqual(findings[-1]["relevance"], "not_assessed")
        self.assertEqual(findings[-1]["classification"], "not_assessed")

    def test_returns_honest_assessment_and_latest_separate_audit_statuses(self):
        stamp = datetime.now(timezone.utc)
        assessed = self.add_material("ozon_telegram", "assessed", stamp)
        unassessed = self.add_material("ozon_telegram", "unassessed-audit", stamp)
        finding_id = self.add_finding("audited", "Ozon", 5, stamp, [assessed])
        unassessed_id = self.add_finding("not-audited", "Ozon", 5, stamp, [unassessed])
        self.add_assessment(assessed, "seller_action_or_risk", seller_impact="negative")
        self.add_audit_result(finding_id, "summary", "issue", started_at=stamp)
        self.add_audit_result(finding_id, "summary", "ok", started_at=stamp + timedelta(seconds=1))
        self.add_audit_result(finding_id, "recommendation", "review", started_at=stamp + timedelta(seconds=2))

        by_id = {item["id"]: item for item in db.feed_findings(self.conn)["findings"]}
        self.assertEqual(by_id[finding_id]["classification"], "seller_action_or_risk")
        self.assertEqual(by_id[finding_id]["relevance"], "relevant")
        self.assertEqual(by_id[finding_id]["summary_audit"], "ok")
        self.assertEqual(by_id[finding_id]["recommendation_audit"], "review")
        self.assertEqual(by_id[unassessed_id]["classification"], "not_assessed")
        self.assertEqual(by_id[unassessed_id]["summary_audit"], "not_checked")
        self.assertEqual(by_id[unassessed_id]["recommendation_audit"], "not_checked")

    def test_aggregates_disagreement_conservatively_and_filters_use_cursor_fingerprint(self):
        stamp = datetime.now(timezone.utc)
        practical = self.add_material("ozon_telegram", "mixed-practical", stamp)
        irrelevant = self.add_material("ozon_telegram", "mixed-irrelevant", stamp)
        negative = self.add_material("ozon_telegram", "negative", stamp)
        negative_second = self.add_material("ozon_telegram", "negative-second", stamp - timedelta(seconds=1))
        mixed_id = self.add_finding("mixed", "Ozon", 5, stamp, [practical, irrelevant])
        negative_id = self.add_finding("negative", "Ozon", 4, stamp, [negative])
        negative_second_id = self.add_finding("negative-second", "Ozon", 3, stamp - timedelta(seconds=1), [negative_second])
        self.add_assessment(practical, "practical_change")
        self.add_assessment(irrelevant, "irrelevant")
        self.add_assessment(negative, "seller_action_or_risk", seller_impact="negative")
        self.add_assessment(negative_second, "seller_action_or_risk", seller_impact="negative")
        self.add_audit_result(negative_id, "summary", "ok", started_at=stamp)
        self.add_audit_result(negative_second_id, "summary", "ok", started_at=stamp)

        by_id = {item["id"]: item for item in db.feed_findings(self.conn)["findings"]}
        self.assertEqual(by_id[mixed_id]["classification"], "uncertain")
        self.assertEqual(by_id[mixed_id]["relevance"], "uncertain")
        first = db.feed_findings(self.conn, seller_impact="negative", summary_audit="ok", limit=1)
        self.assertEqual([item["id"] for item in first["findings"]], [negative_id])
        self.assertIsNotNone(first["next_cursor"])
        second = db.feed_findings(
            self.conn, seller_impact="negative", summary_audit="ok", cursor=first["next_cursor"], limit=1
        )
        self.assertEqual([item["id"] for item in second["findings"]], [negative_second_id])
        with self.assertRaises(ValueError):
            db.feed_findings(self.conn, seller_impact="negative", summary_audit="issue", cursor=first["next_cursor"], limit=1)

    def test_counts_include_impact_and_audit_status_groups(self):
        stamp = datetime.now(timezone.utc)
        negative = self.add_material("ozon_telegram", "counts-negative", stamp)
        issue = self.add_material("ozon_telegram", "counts-issue", stamp)
        negative_id = self.add_finding("counts-negative", "Ozon", 5, stamp, [negative])
        issue_id = self.add_finding("counts-issue", "Ozon", 4, stamp, [issue])
        self.conn.execute("UPDATE events SET seller_impact='negative' WHERE id=%s", (negative_id,))
        self.conn.execute("UPDATE events SET seller_impact='negative' WHERE id=%s", (issue_id,))
        self.add_audit_result(negative_id, "summary", "ok", started_at=stamp)
        self.add_audit_result(issue_id, "summary", "issue", started_at=stamp)

        counts = db.finding_filter_counts(self.conn, {"seller_impact": "negative", "summary_audit": "ok"})
        self.assertEqual(counts["seller_impact"]["negative"], 1)
        self.assertEqual(counts["summary_audit"]["ok"], 1)
        self.assertEqual(counts["summary_audit"]["issue"], 1)
        self.assertEqual(counts["recommendation_audit"]["not_checked"], 1)

    def test_sorts_unassessed_findings_by_importance_freshness_and_id(self):
        stamp = datetime.now(timezone.utc)
        ozon = self.add_material("ozon_telegram", "ozon", stamp)
        wb = self.add_material("wb_telegram", "wb", stamp - timedelta(seconds=3))
        regulator = self.add_material("rospotrebnadzor_federal", "regulator", stamp - timedelta(seconds=1))
        industry = self.add_material("retail_ru_telegram", "industry", stamp - timedelta(seconds=1))
        ozon_second_source = self.add_material("ozon_telegram", "ozon-second", stamp)
        low_id = self.add_finding("ozon-low", "Ozon", 2, stamp, [ozon, ozon_second_source])
        wb_id = self.add_finding("wb-high", "Wildberries", 5, stamp - timedelta(seconds=3), [wb])
        regulator_id = self.add_finding("regulator", "industry", 5, stamp - timedelta(seconds=1), [regulator])
        industry_id = self.add_finding("industry", "industry", 5, stamp - timedelta(seconds=1), [industry])

        result = db.feed_findings(self.conn, limit=20)
        self.assertEqual([item["id"] for item in result["findings"]], [industry_id, regulator_id, wb_id, low_id])
        self.assertTrue(all(item["classification"] == "not_assessed" for item in result["findings"]))
        self.assertTrue(all(item["relevance"] == "not_assessed" for item in result["findings"]))
        self.assertEqual({source["url"] for source in result["findings"][-1]["sources"]},
                         {"https://example.org/ozon", "https://example.org/ozon-second"})

    def test_seller_impact_is_returned_without_changing_importance_filter(self):
        stamp = datetime.now(timezone.utc)
        material = self.add_material("ozon_telegram", "seller-impact", stamp)
        finding_id = self.add_finding("seller-impact", "Ozon", 4, stamp, [material])
        self.conn.execute("UPDATE events SET seller_impact='negative' WHERE id=%s", (finding_id,))
        legacy_material = self.add_material("ozon_telegram", "seller-impact-legacy", stamp)
        legacy_id = self.add_finding("seller-impact-legacy", "Ozon", 3, stamp, [legacy_material])

        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "0"}):
            findings = db.feed_findings(self.conn)["findings"]
            selected = db.feed_findings(self.conn, importance=4)["findings"]

        by_id = {item["id"]: item for item in findings}
        self.assertEqual(by_id[finding_id]["seller_impact"], "negative")
        self.assertEqual(by_id[finding_id]["importance"], 4)
        self.assertEqual(by_id[legacy_id]["seller_impact"], "unclear")
        self.assertEqual([item["id"] for item in selected], [finding_id])

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

    def test_filter_counts_use_other_groups_and_full_result_set(self):
        stamp = datetime.now(timezone.utc)
        for index in range(21):
            material = self.add_material("ozon_telegram", f"counter-ozon-{index}", stamp - timedelta(seconds=index))
            self.add_finding(f"counter-ozon-{index}", "Ozon", 5, stamp - timedelta(seconds=index), [material], topic="logistics")
        wildberries = self.add_material("wb_telegram", "counter-wb", stamp)
        self.add_finding("counter-wb", "Wildberries", 3, stamp, [wildberries], verification="confirmed", topic="returns")
        regulator = self.add_material("rospotrebnadzor_federal", "counter-regulator", stamp)
        self.add_finding("counter-regulator", "industry", 4, stamp, [regulator], topic="rules")

        filters = {"period": "week", "platform": "ozon", "importance": 5,
                   "topic": "logistics", "verification": "needs_check", "trust": "official"}
        counts = db.finding_filter_counts(self.conn, filters)

        self.assertEqual(db.feed_findings(self.conn, **filters, limit=1)["total"], 21)
        self.assertEqual(counts["period"]["all"], 21)
        self.assertEqual(counts["period"]["week"], 21)
        self.assertEqual(counts["platform"]["all"], 21)
        self.assertEqual(counts["platform"]["ozon"], 21)
        self.assertEqual(counts["importance"]["all"], 21)
        self.assertEqual(counts["importance"]["5"], 21)
        self.assertEqual(counts["source_type"]["all"], 21)
        self.assertEqual(counts["source_type"]["official"], 21)
        self.assertEqual(counts["source_type"]["regulator"], 0)
        self.assertEqual(counts["topic_status"]["verification"]["needs_check"], 21)
        self.assertEqual(counts["topic_status"]["topics"], [{"value": "logistics", "count": 21}])

    def test_filter_counts_show_zero_for_other_groups_when_current_filter_matches_nothing(self):
        stamp = datetime.now(timezone.utc)
        material = self.add_material("ozon_telegram", "counter-none", stamp)
        self.add_finding("counter-none", "Ozon", 4, stamp, [material], topic="logistics")

        counts = db.finding_filter_counts(self.conn, {"topic": "absent"})

        self.assertEqual(counts["period"]["all"], 0)
        self.assertEqual(counts["platform"]["all"], 0)
        self.assertEqual(counts["importance"]["all"], 0)
        self.assertEqual(counts["source_type"]["all"], 0)
        self.assertEqual(counts["topic_status"]["topics"], [{"value": "logistics", "count": 1}])

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
