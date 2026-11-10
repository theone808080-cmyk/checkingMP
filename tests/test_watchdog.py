from datetime import datetime, timedelta, timezone
import unittest

from watchdog.logic import evaluate, restart_due


class WatchdogLogicTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

    def test_restart_requires_three_failures_and_healthy_database(self):
        self.assertFalse(restart_due(app_available=False, database_available=True,
                                     failed_checks=2, restart_attempted=False))
        self.assertTrue(restart_due(app_available=False, database_available=True,
                                    failed_checks=3, restart_attempted=False))
        self.assertFalse(restart_due(app_available=False, database_available=False,
                                     failed_checks=3, restart_attempted=False))
        self.assertFalse(restart_due(app_available=False, database_available=True,
                                     failed_checks=3, restart_attempted=True))
        self.assertFalse(restart_due(app_available=True, database_available=True,
                                     failed_checks=3, restart_attempted=False))

    def test_detects_stale_cycle_and_repeated_ai_and_source_failures(self):
        cycles = [
            {"status": "failed", "processing_status": "failed", "error_stage": "processing", "error_message": "RuntimeError: x", "notification_status": "sent",
             "source_failures": ["source_a"], "finished_at": self.now - timedelta(minutes=5)},
            {"status": "failed", "processing_status": "failed", "error_stage": "processing", "error_message": "RuntimeError: x", "notification_status": "sent",
             "source_failures": ["source_a", "source_b"], "finished_at": self.now - timedelta(hours=12)},
            {"status": "success", "processing_status": "success", "notification_status": "sent",
             "source_failures": [], "finished_at": self.now - timedelta(hours=15)},
        ]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now - timedelta(days=1))
        self.assertIn("cycle_stale", incidents)
        self.assertIn("ai_processing", incidents)
        self.assertIn("source_failure:source_a", incidents)
        self.assertNotIn("source_failure:source_b", incidents)

    def test_delivery_failure_requires_database_outbox_confirmation(self):
        cycles = [{"status": "success", "processing_status": "success",
                   "notification_status": "failed", "notification_attempted": True,
                   "source_failures": [],
                   "finished_at": self.now - timedelta(minutes=1), "items": 0}]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertNotIn("telegram_delivery", incidents)
        self.assertNotIn("cycle_stale", incidents)

    def test_no_cycle_alert_uses_monitor_start_grace_period(self):
        fresh = evaluate([], database_available=True, app_available=True,
                         now=self.now, monitor_started_at=self.now - timedelta(hours=13))
        stale = evaluate([], database_available=True, app_available=True,
                         now=self.now, monitor_started_at=self.now - timedelta(hours=15))
        self.assertNotIn("cycle_stale", fresh)
        self.assertIn("cycle_stale", stale)

    def test_does_not_call_distinct_processing_errors_repeated(self):
        cycles = [
            {"finished_at": self.now, "processing_status": "failed", "error_stage": "processing", "error_message": "A"},
            {"finished_at": self.now - timedelta(minutes=1), "processing_status": "failed", "error_stage": "processing", "error_message": "B"},
        ]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertNotIn("ai_processing", incidents)

    def test_uses_last_success_even_when_it_is_older_than_recent_cycle_window(self):
        cycles = [{"finished_at": self.now - timedelta(minutes=i), "status": "failed"}
                  for i in range(1, 8)]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now - timedelta(days=3),
                             latest_success_at=self.now - timedelta(hours=1))
        self.assertNotIn("cycle_stale", incidents)

    def test_does_not_call_different_source_errors_repeated(self):
        cycles = [
            {"finished_at": self.now, "source_failures": ["source_a"],
             "source_failure_signatures": {"source_a": "hash-a"}},
            {"finished_at": self.now - timedelta(minutes=1), "source_failures": ["source_a"],
             "source_failure_signatures": {"source_a": "hash-b"}},
        ]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertNotIn("source_failure:source_a", incidents)

    def test_pending_notification_is_not_a_delivery_failure(self):
        cycles = [{"finished_at": self.now, "notification_status": "pending"}]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertNotIn("telegram_delivery", incidents)
        empty_target = [{"finished_at": self.now, "notification_status": "failed",
                         "notification_attempted": True, "notification_target_material_ids": []}]
        incidents = evaluate(empty_target, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertNotIn("telegram_delivery", incidents)
        failed_setup = [{"finished_at": self.now, "notification_status": "failed",
                         "notification_attempted": False}]
        incidents = evaluate(failed_setup, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertNotIn("telegram_delivery", incidents)

    def test_stale_unfinished_cycle_is_checked_separately_from_recent_cycles(self):
        cycles = [{"finished_at": self.now - timedelta(minutes=i), "status": "success"}
                  for i in range(5)]
        stuck = [{"status": "started", "started_at": self.now - timedelta(hours=15)}]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now,
                             stuck_cycles=stuck)
        self.assertIn("cycle_stuck", incidents)

    def test_stuck_cycle_is_reported_after_fourteen_hours(self):
        cycles = [{"status": "started", "started_at": self.now - timedelta(hours=15),
                   "finished_at": None}]
        incidents = evaluate(cycles, database_available=True, app_available=True,
                             now=self.now, monitor_started_at=self.now)
        self.assertIn("cycle_stuck", incidents)
    def test_database_and_application_incidents_are_independent(self):
        incidents = evaluate([], database_available=False, app_available=False,
                             now=self.now, monitor_started_at=self.now)
        self.assertIn("database_unavailable", incidents)
        self.assertNotIn("application_unavailable", incidents)


if __name__ == "__main__":
    unittest.main()
