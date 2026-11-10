import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
import db
from watchdog.service import Settings, check_once, container_healthcheck, healthcheck, sync_incidents
from watchdog.integrations import restart_coolify, send_telegram_alert


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class WatchdogIncidentTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.conn.execute("DELETE FROM cycles")
        self.conn.execute("DELETE FROM watchdog_incidents")
        self.now = datetime.now(timezone.utc)

    def test_alert_is_deduplicated_and_recovery_is_sent_once(self):
        sent = []
        send = lambda key, text, recovered=False: sent.append((key, recovered)) or True
        active = {"source_failure:source_a": "Источник source_a не читается."}
        sync_incidents(self.conn, active, send, now=self.now)
        sync_incidents(self.conn, active, send, now=self.now)
        sync_incidents(self.conn, {}, send, now=self.now)
        sync_incidents(self.conn, {}, send, now=self.now)
        self.assertEqual(sent, [("source_failure:source_a", False),
                                ("source_failure:source_a", True)])

    def test_failed_notification_is_retried_until_confirmed(self):
        calls = []
        send = lambda key, text, recovered=False: calls.append(key) and len(calls) > 1
        active = {"telegram_delivery": "Telegram недоступен."}
        sync_incidents(self.conn, active, send, now=self.now)
        sync_incidents(self.conn, active, send, now=self.now)
        self.assertEqual(calls, ["telegram_delivery", "telegram_delivery"])

    def test_delivery_incident_stays_open_until_a_later_sent_cycle(self):
        sent = []
        send = lambda key, text, recovered=False: sent.append((key, recovered)) or True
        opened = self.now - timedelta(minutes=5)
        failed = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, failed, "notifications", "failed", error="failed")
        db.finish_cycle(self.conn, failed, "failed")
        self.conn.execute("""UPDATE cycles SET notification_target_material_ids = '[1]'::jsonb,
                          notification_attempted = true WHERE id = %s""",
                          (failed,))
        sync_incidents(self.conn, {"telegram_delivery": "Telegram delivery failed."},
                       send, now=opened)
        sync_incidents(self.conn, {}, send, now=self.now)
        state = db.watchdog_incidents(self.conn)["telegram_delivery"]
        self.assertTrue(state["active"])
        self.assertEqual(sent, [("telegram_delivery", False)])
        cycle_id = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, cycle_id, "notifications", "sent")
        db.finish_cycle(self.conn, cycle_id, "success")
        self.conn.execute("""UPDATE cycles SET finished_at = %s,
                          notification_delivered_material_ids = '[1]'::jsonb WHERE id = %s""",
                          (self.now + timedelta(minutes=1), cycle_id))
        sync_incidents(self.conn, {}, send, now=self.now + timedelta(minutes=1))
        self.assertFalse(db.watchdog_incidents(self.conn)["telegram_delivery"]["active"])
        self.assertEqual(sent[-1], ("telegram_delivery", True))

    def test_unresolved_delivery_failure_survives_newer_pending_cycle(self):
        earlier_delivery = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, earlier_delivery, "notifications", "sent")
        db.finish_cycle(self.conn, earlier_delivery, "success")
        self.conn.execute("UPDATE cycles SET finished_at = %s, notification_checked_at = %s WHERE id = %s",
                          (self.now + timedelta(minutes=5), self.now - timedelta(minutes=3), earlier_delivery))
        self.conn.execute("UPDATE cycles SET notification_delivered_material_ids = '[4]'::jsonb WHERE id = %s",
                          (earlier_delivery,))
        failed = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, failed, "notifications", "failed", error="failed")
        db.finish_cycle(self.conn, failed, "failed")
        self.conn.execute("UPDATE cycles SET finished_at = %s, notification_checked_at = %s WHERE id = %s",
                          (self.now - timedelta(minutes=2), self.now - timedelta(minutes=2), failed))
        self.conn.execute("""UPDATE cycles SET notification_target_material_ids = '[9]'::jsonb,
                          notification_attempted = true WHERE id = %s""",
                          (failed,))
        self.assertTrue(db.has_unresolved_notification_failure(self.conn))
        pending = db.start_cycle(self.conn)
        db.finish_cycle(self.conn, pending, "failed", error_stage="collection", error="collection failed")
        self.conn.execute("UPDATE cycles SET finished_at = %s WHERE id = %s",
                          (self.now - timedelta(minutes=1), pending))
        self.assertTrue(db.has_unresolved_notification_failure(self.conn))
        delivered = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, delivered, "notifications", "sent")
        db.finish_cycle(self.conn, delivered, "success")
        self.conn.execute("UPDATE cycles SET finished_at = %s, notification_checked_at = %s WHERE id = %s",
                          (self.now, self.now, delivered))
        self.conn.execute("UPDATE cycles SET notification_delivered_material_ids = '[9]'::jsonb WHERE id = %s", (delivered,))
        self.assertFalse(db.has_unresolved_notification_failure(self.conn))

    def test_delivery_after_target_snapshot_resolves_before_failed_status_is_written(self):
        captured = self.now - timedelta(minutes=5)
        delivered = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, delivered, "notifications", "sent",
                              notification_delivered_material_ids=[9])
        db.finish_cycle(self.conn, delivered, "success")
        self.conn.execute("""UPDATE cycles SET notification_checked_at = %s,
                          notification_delivered_material_ids = '[9]'::jsonb WHERE id = %s""",
                          (captured + timedelta(minutes=1), delivered))
        failed = db.start_cycle(self.conn)
        db.update_cycle_stage(self.conn, failed, "notifications", "failed", error="busy")
        db.finish_cycle(self.conn, failed, "failed")
        self.conn.execute("""UPDATE cycles SET notification_target_material_ids = '[9]'::jsonb,
                          notification_attempted = true,
                          notification_checked_at = %s WHERE id = %s""",
                          (captured + timedelta(minutes=2), failed))
        self.assertFalse(db.has_unresolved_notification_failure(self.conn))


class WatchdogIntegrationTests(unittest.TestCase):
    def test_coolify_restart_uses_bearer_token_and_scoped_application(self):
        response = type("Response", (), {"status": 200, "read": lambda self, size=-1: b'{}',
                                        "__enter__": lambda self: self,
                                        "__exit__": lambda self, *args: None})()
        with patch("watchdog.integrations.open_url", return_value=response) as open_url:
            restart_coolify("https://coolify.example", "synthetic-token", "app-uuid")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://coolify.example/api/v1/applications/app-uuid/restart")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")

    def test_telegram_alert_payload_contains_diagnostic_without_inline_secrets(self):
        response = type("Response", (), {"read": lambda self, size=-1: b'{"ok":true,"result":{"message_id":1}}',
                                        "__enter__": lambda self: self,
                                        "__exit__": lambda self, *args: None})()
        with patch("watchdog.integrations.open_url", return_value=response) as open_url:
            send_telegram_alert("123456:synthetic-token", "12345", "Сбой сбора")
        request = open_url.call_args.args[0]
        self.assertIn("synthetic-token", request.full_url)
        self.assertNotIn("synthetic-token".encode("utf-8"), request.data)
        self.assertIn("Сбой сбора".encode("utf-8"), request.data)
        self.assertIn("Сбой сбора".encode("utf-8"), request.data)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class WatchdogHealthcheckTests(unittest.TestCase):
    def test_healthcheck_accepts_fresh_and_rejects_stale_or_missing_heartbeat(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            path = Path(folder) / "heartbeat"
            now = datetime.now(timezone.utc)
            path.write_text((now - timedelta(minutes=5)).isoformat(), encoding="ascii")
            self.assertTrue(healthcheck(path, now=now))
            path.write_text((now - timedelta(minutes=11)).isoformat(), encoding="ascii")
            self.assertFalse(healthcheck(path, now=now))
            path.unlink()
            self.assertFalse(healthcheck(path, now=now))

class ContainerHealthcheckTests(unittest.TestCase):
    def test_watchdog_mode_uses_persisted_heartbeat(self):
        with patch.dict(os.environ, {"WATCHDOG_MODE": "1"}), \
             patch("watchdog.service.healthcheck", return_value=True) as heartbeat:
            self.assertTrue(container_healthcheck())
        heartbeat.assert_called_once()

    def test_web_mode_probes_local_health_endpoint(self):
        with patch.dict(os.environ, {"PORT": "8123"}, clear=True), \
             patch("watchdog.service.probe_app_health", return_value=True) as probe:
            self.assertTrue(container_healthcheck())
        probe.assert_called_once_with("http://127.0.0.1:8123/health")

class WatchdogServiceTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            database_url=os.environ["TEST_DATABASE_URL"],
            radar_health_url="https://radar.example/health",
            coolify_api_url="https://coolify.example",
            coolify_application_uuid="app-uuid",
            coolify_api_token="synthetic-token",
            telegram_token="123456:synthetic-token",
            telegram_chat_id="12345",
            state_path=Path(self.temp.name) / "fallback.json",
            heartbeat_path=Path(self.temp.name) / "heartbeat",
        )
        with psycopg.connect(self.settings.database_url, autocommit=True) as conn:
            db.init_schema(conn)
            conn.execute("DELETE FROM cycles")
            conn.execute("DELETE FROM watchdog_incidents")
        self.base_time = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.app_available = False
        self.restart = unittest.mock.Mock()
        self.send = unittest.mock.Mock(return_value=True)

    def check(self, minute):
        return check_once(
            self.settings, now=self.base_time + timedelta(minutes=minute),
            app_probe=lambda _url: self.app_available, restart=self.restart,
            send_notice=self.send,
            connect=lambda: psycopg.connect(self.settings.database_url, autocommit=True),
        )

    def test_restarts_once_after_three_failures_and_resets_after_recovery(self):
        from watchdog.service import check_once
        self.check(0)
        self.check(5)
        self.restart.assert_not_called()
        self.check(10)
        self.restart.assert_called_once_with("https://coolify.example", "synthetic-token", "app-uuid")
        self.check(15)
        self.restart.assert_called_once()
        self.app_available = True
        self.check(20)
        self.app_available = False
        self.check(25)
        self.check(30)
        self.check(35)
        self.assertEqual(self.restart.call_count, 2)
        self.assertTrue(any(call.args[2] for call in self.send.call_args_list))

    def test_failed_coolify_restart_is_not_retried_during_same_outage(self):
        self.restart.side_effect = RuntimeError("synthetic token should not leak")
        for minute in (0, 5, 10, 15, 20):
            self.check(minute)
        self.restart.assert_called_once()
        self.assertEqual(sum(1 for call in self.send.call_args_list
                             if call.args[0] == "coolify_restart_failed"), 1)
        self.assertNotIn("synthetic-token", " ".join(call.args[1]
                                                       for call in self.send.call_args_list))

    def test_service_reports_only_unresolved_notification_targets(self):
        with psycopg.connect(self.settings.database_url, autocommit=True) as conn:
            failed = db.start_cycle(conn)
            db.update_cycle_stage(conn, failed, "notifications", "failed", error="busy")
            db.finish_cycle(conn, failed, "failed")
            conn.execute("""UPDATE cycles SET notification_attempted = true,
                              notification_target_material_ids = '[42]'::jsonb,
                              notification_checked_at = now() WHERE id = %s""", (failed,))
        self.app_available = True
        self.check(0)
        self.assertTrue(any(call.args[0] == "telegram_delivery" and not call.args[2]
                            for call in self.send.call_args_list))
        with psycopg.connect(self.settings.database_url, autocommit=True) as conn:
            sent = db.start_cycle(conn)
            db.update_cycle_stage(conn, sent, "notifications", "sent",
                                  notification_delivered_material_ids=[42])
            db.finish_cycle(conn, sent, "success")
        self.check(5)
        self.assertTrue(any(call.args[0] == "telegram_delivery" and call.args[2]
                            for call in self.send.call_args_list))
    def test_database_outage_notice_includes_last_successful_cycle(self):
        with psycopg.connect(self.settings.database_url, autocommit=True) as conn:
            cycle_id = db.start_cycle(conn)
            db.finish_cycle(conn, cycle_id, "success")
            conn.execute("UPDATE cycles SET finished_at = %s WHERE id = %s",
                         (self.base_time - timedelta(hours=2), cycle_id))
        self.app_available = True
        self.check(0)
        connect = unittest.mock.Mock(side_effect=OSError("database offline"))
        check_once(self.settings, now=self.base_time + timedelta(minutes=5),
                   app_probe=lambda _url: True, restart=self.restart,
                   send_notice=self.send, connect=connect)
        alert = next(call.args[1] for call in self.send.call_args_list
                     if call.args[0] == "database_unavailable")
        self.assertIn("2026-11-09T10:00+00:00", alert)
    def test_database_outage_notifies_once_and_never_restarts_app(self):
        connect = unittest.mock.Mock(side_effect=OSError("database unavailable"))
        for minute in (0, 5, 10):
            check_once(self.settings, now=self.base_time + timedelta(minutes=minute),
                       app_probe=lambda _url: False, restart=self.restart,
                       send_notice=self.send, connect=connect)
        self.assertEqual(self.send.call_count, 1)
        self.restart.assert_not_called()
        with psycopg.connect(self.settings.database_url, autocommit=True) as conn:
            check_once(self.settings, now=self.base_time + timedelta(minutes=15),
                       app_probe=lambda _url: True, restart=self.restart,
                       send_notice=self.send,
                       connect=lambda: psycopg.connect(self.settings.database_url, autocommit=True))
        self.assertTrue(any(call.args[2] for call in self.send.call_args_list))

    def test_app_recovery_during_database_outage_resets_restart_counter(self):
        self.check(0)
        self.check(5)
        self.app_available = True
        unavailable_db = unittest.mock.Mock(side_effect=OSError("database unavailable"))
        check_once(self.settings, now=self.base_time + timedelta(minutes=10),
                   app_probe=lambda _url: True, restart=self.restart,
                   send_notice=self.send, connect=unavailable_db)
        self.app_available = False
        self.check(15)
        self.check(20)
        self.restart.assert_not_called()
        self.check(25)
        self.restart.assert_called_once()

if __name__ == "__main__":
    unittest.main()
