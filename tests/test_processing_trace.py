from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone

import psycopg
from unittest.mock import patch

import db
from worker import main as worker


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class ProcessingTraceTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "trace_test_" + uuid.uuid4().hex[:12]
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        self.addCleanup(self.conn.close)
        self.addCleanup(self._drop_schema)
        db.init_schema(self.conn)
        source_id = self.conn.execute(
            """INSERT INTO sources (code, name, kind, url, trust, scope)
               VALUES (%s, 'Trace source', 'rss', 'https://trace.example/feed', 'industry', 'test') RETURNING id""",
            ("trace_" + self.schema,),
        ).fetchone()[0]
        cycle_id = db.start_cycle(self.conn)
        run_id = db.start_run(self.conn, "collector", cycle_id=cycle_id)
        _, material_id = db.add_material(self.conn, source_id, run_id, {
            "url": "https://trace.example/item?api_key=private-token&campaign=weekly",
            "title": "Trace title", "text": "private full source body",
        }, return_id=True)
        event_id = self.conn.execute(
            """INSERT INTO events (event_key, platform, topic, importance, summary, recommended_action, verification)
               VALUES ('trace-event', 'Ozon', 'Trace topic', 3, 'Trace summary', 'Review', 'needs_check') RETURNING id"""
        ).fetchone()[0]
        self.conn.execute("INSERT INTO event_materials (event_id, material_id) VALUES (%s, %s)",
                          (event_id, material_id))
        self.cycle_id, self.run_id, self.source_id = cycle_id, run_id, source_id
        self.material_id, self.event_id = material_id, event_id

    def _drop_schema(self):
        if not self.conn.closed:
            self.conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(self.schema)))

    def test_finding_trace_links_source_material_cycle_and_preserves_attempts(self):
        start = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)
        db.record_trace_attempt(self.conn, cycle_id=self.cycle_id, run_id=self.run_id,
                                source_id=self.source_id, material_id=self.material_id,
                                stage="translation", status="failed", started_at=start,
                                finished_at=start, error_code="translation_failed")
        db.record_trace_attempt(self.conn, cycle_id=self.cycle_id, run_id=self.run_id,
                                source_id=self.source_id, material_id=self.material_id,
                                stage="translation", status="success", started_at=start,
                                finished_at=start, provider="openrouter", model="test-model")
        db.record_trace_attempt(self.conn, cycle_id=self.cycle_id, run_id=self.run_id,
                                source_id=self.source_id, material_id=self.material_id,
                                stage="analysis", status="success", started_at=start,
                                finished_at=start, provider="openrouter", model="test-model")

        result = db.finding_trace(self.conn, self.event_id)
        self.assertEqual(result["finding"]["id"], self.event_id)
        self.assertEqual(result["materials"][0]["material_id"], self.material_id)
        self.assertEqual(result["materials"][0]["source_code"], "trace_" + self.schema)
        self.assertIn("api_key=%5Bredacted%5D", result["materials"][0]["url"])
        self.assertNotIn("private-token", result["materials"][0]["url"])
        self.assertIn("campaign=weekly", result["materials"][0]["url"])
        self.assertEqual(len(result["materials"][0]["attempts"]), 3)
        self.assertEqual(result["materials"][0]["attempts"][0]["status"], "failed")
        self.assertNotIn("private full source body", repr(result))
        self.assertNotIn("prompt", repr(result).lower())

    def test_trace_error_is_allowlisted_and_does_not_store_secret_or_raw_message(self):
        db.record_trace_attempt(self.conn, cycle_id=self.cycle_id, run_id=self.run_id,
                                source_id=self.source_id, material_id=self.material_id,
                                stage="analysis", status="failed", error_code="analysis_failed")
        stored = self.conn.execute(
            "SELECT error_code, error_summary FROM processing_trace"
        ).fetchone()
        self.assertEqual(stored[0], "analysis_failed")
        self.assertNotIn("secret-token", stored[1])
        self.assertNotIn("private-token", stored[1])
        self.assertNotIn("full prompt", stored[1])

    def test_trace_rows_cannot_be_updated_or_deleted(self):
        trace_id = db.record_trace_attempt(
            self.conn, cycle_id=self.cycle_id, run_id=self.run_id, source_id=self.source_id,
            material_id=self.material_id, stage="analysis", status="success",
        )
        with self.assertRaises(psycopg.errors.RaiseException):
            self.conn.execute("UPDATE processing_trace SET status='failed' WHERE id=%s", (trace_id,))
        with self.assertRaises(psycopg.errors.RaiseException):
            self.conn.execute("DELETE FROM processing_trace WHERE id=%s", (trace_id,))

    def test_schema_migration_preserves_existing_material_and_cycles(self):
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE id=%s",
                                           (self.material_id,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM cycles WHERE id=%s",
                                           (self.cycle_id,)).fetchone()[0], 1)

    def test_worker_records_translation_relevance_and_analysis_for_each_material(self):
        payload = {"signals": [], "skipped_material_ids": [self.material_id],
                   "translations": [{"material_id": self.material_id, "title_ru": "Заголовок",
                                     "text_ru": "Перевод"}]}
        with patch.object(worker, "settings", return_value=("https://example.test", "fake", "test/model", 1, 10, 10)), \
             patch.object(worker, "assess_pending", return_value=1), \
             patch.object(worker, "infer", return_value=payload):
            result = worker.process(self.conn, cycle_id=self.cycle_id)
        self.assertEqual(result, (0, 0, 1))
        rows = self.conn.execute(
            "SELECT stage, status, cycle_id, run_id, source_id FROM processing_trace WHERE material_id=%s ORDER BY id",
            (self.material_id,),
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [
            ("relevance", "success"), ("analysis", "success"), ("translation", "success")
        ])
        self.assertTrue(all(row[2:] == (self.cycle_id, self.run_id, self.source_id) for row in rows))

    def test_worker_failure_records_safe_failed_attempts_and_retry_history(self):
        with patch.object(worker, "settings", return_value=("https://example.test", "fake", "test/model", 1, 10, 10)), \
             patch.object(worker, "assess_pending", return_value=1), \
             patch.object(worker, "infer", side_effect=RuntimeError("Bearer secret api_key=secret prompt")):
            with self.assertRaisesRegex(RuntimeError, "Bearer secret"):
                worker.process(self.conn, cycle_id=self.cycle_id)
        with patch.object(worker, "settings", return_value=("https://example.test", "fake", "test/model", 1, 10, 10)), \
             patch.object(worker, "assess_pending", return_value=1), \
             patch.object(worker, "infer", return_value={"signals": [], "skipped_material_ids": [self.material_id],
                 "translations": [{"material_id": self.material_id, "title_ru": "Заголовок", "text_ru": "Перевод"}]}):
            worker.process(self.conn, cycle_id=self.cycle_id)
        attempts = self.conn.execute(
            "SELECT stage, status, error_summary FROM processing_trace WHERE material_id=%s ORDER BY id",
            (self.material_id,),
        ).fetchall()
        analysis = [row for row in attempts if row[0] == "analysis"]
        translation = [row for row in attempts if row[0] == "translation"]
        self.assertEqual([row[1] for row in analysis], ["failed", "success"])
        self.assertEqual([row[1] for row in translation], ["failed", "success"])
        self.assertNotIn("secret", repr(attempts))
        self.assertNotIn("prompt", repr(attempts))


if __name__ == "__main__":
    unittest.main()
