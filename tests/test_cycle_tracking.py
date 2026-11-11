import os
import unittest
from unittest.mock import patch

import psycopg
import db
import main


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class CycleTrackingTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.conn.execute("DELETE FROM cycles")

    def test_cycle_records_all_stages_and_terminal_result(self):
        cycle_id = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, cycle_id, "collection", "success", items=4,
                              source_failures=["source_a"])
        db.update_cycle_stage(self.conn, cycle_id, "notifications", "skipped")
        db.update_cycle_stage(self.conn, cycle_id, "processing", "success", items=3)
        db.finish_cycle(self.conn, cycle_id, "success")
        row = self.conn.execute(
            """SELECT status, collection_status, notification_status, processing_status,
                      collected_items, processed_items, source_failures, error_stage
               FROM cycles WHERE id = %s""",
            (cycle_id,),
        ).fetchone()
        self.assertEqual(row, ("success", "success", "skipped", "success", 4, 3,
                               ["source_a"], None))

    def test_cycle_command_persists_successful_empty_run(self):
        env = {"DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(main, "collect", return_value={"items": 0, "source_failures": []}), \
             patch.object(main, "process", return_value={"items": 0, "events": 0}):
            main.cycle()
        row = self.conn.execute(
            """SELECT status, collection_status, notification_status, processing_status,
                      collected_items, processed_items FROM cycles ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(row, ("success", "success", "skipped", "success", 0, 0))

    def test_notification_failure_is_recorded_while_processing_continues(self):
        env = {"DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(main, "collect", return_value={"items": 2, "source_failures": []}), \
             patch.object(main, "send_notifications", return_value="failed"), \
             patch.object(main, "process", return_value={"items": 2, "events": 1}) as process:
            main.cycle()
        process.assert_called_once()
        row = self.conn.execute(
            """SELECT status, notification_status, processing_status, error_stage
               FROM cycles ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(row, ("failed", "failed", "success", "notifications"))
    def test_interrupted_cycle_stays_started_until_explicit_finish(self):
        cycle_id = db.start_cycle(self.conn)
        row = self.conn.execute("SELECT status, finished_at FROM cycles WHERE id = %s",
                                (cycle_id,)).fetchone()
        self.assertEqual(row, ("started", None))

    def test_monitor_snapshot_returns_recent_terminal_cycles_newest_first(self):
        old_id = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, old_id, "collection", "success")
        db.update_cycle_stage(self.conn, old_id, "processing", "success")
        db.finish_cycle(self.conn, old_id, "success")
        new_id = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, new_id, "collection", "failed",
                              source_failures=["source_b"], error="collector failure")
        db.finish_cycle(self.conn, new_id, "failed", error_stage="collection",
                        error="collector failure")
        rows = db.recent_cycles(self.conn, limit=2)
        self.assertEqual([row["id"] for row in rows], [new_id, old_id])
        self.assertEqual(rows[0]["source_failures"], ["source_b"])


if __name__ == "__main__":
    unittest.main()
