import io
import json
import os
import unittest
from unittest.mock import patch

import psycopg
import db
import main
from notifier import telegram


CONFIG = {'TELEGRAM_TOKEN': '123456:synthetic_token', 'TELEGRAM_CHAT_ID': '12345',
          'RADAR_MINIAPP_URL': 'https://radar.example/miniapp'}


class NotificationTests(unittest.TestCase):
    def cycle_storage(self):
        from contextlib import ExitStack
        stack = ExitStack()
        connect = stack.enter_context(patch.object(main.db, "connect"))
        connect.return_value.__enter__.return_value = object()
        stack.enter_context(patch.object(main.db, "init_schema"))
        stack.enter_context(patch.object(main.db, "start_cycle", return_value=1))
        stack.enter_context(patch.object(main, "_record_cycle_stage"))
        stack.enter_context(patch.object(main, "_finish_cycle"))
        return stack

    def test_failed_initial_baseline_stops_collection_until_retry(self):
        with self.cycle_storage(), patch.dict(os.environ, CONFIG, clear=True), \
             patch.object(telegram, "initialize", side_effect=[OSError("database offline"), None]), \
             patch.object(main, "collect") as collect, patch.object(main, "send_notifications"), \
             patch.object(main, "process"):
            with self.assertRaises(RuntimeError):
                main.cycle()
            collect.assert_not_called()
            main.cycle()
            collect.assert_called_once()

    def test_unconfigured_notifications_do_not_stop_collection(self):
        with self.cycle_storage(), patch.dict(os.environ, {}, clear=True), \
             patch.object(main, "collect") as collect, patch.object(main, "process"), \
             patch.object(telegram, "initialize") as initialize:
            main.cycle()
        collect.assert_called_once()
        initialize.assert_not_called()

    def test_partial_configuration_does_not_stop_collection(self):
        with self.cycle_storage(), patch.dict(os.environ, {
                "TELEGRAM_TOKEN": CONFIG["TELEGRAM_TOKEN"],
                "TELEGRAM_CHAT_ID": CONFIG["TELEGRAM_CHAT_ID"]}, clear=True), \
             patch.object(main, "collect") as collect, patch.object(main, "process"), \
             patch.object(telegram, "initialize") as initialize:
            main.cycle()
        collect.assert_called_once()
        initialize.assert_not_called()

    def test_cycle_baselines_before_collection_and_notifies_before_processing(self):
        calls = []
        with self.cycle_storage(), \
             patch.object(main, "prepare_notifications", side_effect=lambda: calls.append("baseline")), \
             patch.object(main, "collect", side_effect=lambda **_kwargs: calls.append("collect")), \
             patch.object(main, "send_notifications", side_effect=lambda _config, **_kwargs: calls.append("notify") or "skipped"), \
             patch.object(main, "process", side_effect=lambda **_kwargs: calls.append("process")):
            main.cycle()
        self.assertEqual(calls, ["baseline", "collect", "notify", "process"])
    def test_payload_and_network_failure_do_not_expose_token(self):
        with patch.dict(os.environ, CONFIG, clear=True):
            config = telegram.settings()
            with patch.object(telegram, 'open_url', return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":42}}')) as http:
                self.assertEqual(telegram.send_message(config, 3), 42)
            payload = json.loads(http.call_args.args[0].data)
            self.assertEqual(payload['text'], 'Новых новостей: 3')
            self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['web_app']['url'], CONFIG['RADAR_MINIAPP_URL'])
            with patch.object(telegram, 'open_url', side_effect=OSError(CONFIG['TELEGRAM_TOKEN'])):
                with self.assertRaises(RuntimeError) as caught:
                    telegram.send_message(config, 3)
                self.assertNotIn(CONFIG['TELEGRAM_TOKEN'], str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_invalid_settings_rejected_without_network(self):
        for change in ({'TELEGRAM_CHAT_ID': '-1'}, {'RADAR_MINIAPP_URL': 'http://example.com'},
                       {'TELEGRAM_TOKEN': 'fake token'}, {'RADAR_MINIAPP_URL': 'https://user:pass@example.com'}):
            with self.subTest(change=change), patch.dict(os.environ, {**CONFIG, **change}, clear=True):
                with self.assertRaises(ValueError):
                    telegram.settings()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'TEST_DATABASE_URL is not configured')
class NotificationPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TEMP TABLE materials (id SERIAL PRIMARY KEY)')
        self.conn.execute('CREATE TEMP TABLE notification_state (id INT PRIMARY KEY, initialized BOOLEAN NOT NULL)')
        self.conn.execute('CREATE TEMP TABLE notification_batches (id BIGSERIAL PRIMARY KEY, count INT NOT NULL, sent_at TIMESTAMPTZ, telegram_message_id BIGINT)')
        self.conn.execute('CREATE TEMP TABLE notification_materials (material_id INT PRIMARY KEY, batch_id BIGINT)')

    def test_baseline_excludes_old_and_retry_preserves_batch(self):
        self.conn.execute('INSERT INTO materials DEFAULT VALUES')
        telegram.baseline(self.conn)
        self.assertIsNone(telegram.claim_batch(self.conn))
        self.conn.execute('INSERT INTO materials DEFAULT VALUES')
        first = telegram.claim_batch(self.conn)
        self.assertEqual(first[1], 1)
        self.conn.execute('INSERT INTO materials DEFAULT VALUES')
        self.assertEqual(telegram.claim_batch(self.conn), first)
        telegram.mark_sent(self.conn, first[0], 42)
        second = telegram.claim_batch(self.conn)
        self.assertEqual(second[1], 1)
        telegram.mark_sent(self.conn, second[0], 43)
        self.assertIsNone(telegram.claim_batch(self.conn))

    def test_failed_send_is_retried_and_confirmed_send_is_not_repeated(self):
        telegram.initialize(self.conn)
        self.conn.execute('INSERT INTO materials DEFAULT VALUES')
        with patch.object(telegram, 'send_message', side_effect=[RuntimeError('timeout'), 42]) as send:
            with self.assertRaises(RuntimeError):
                telegram.notify_new_materials(self.conn, ('token', '12345', 'https://example.org'))
            self.assertTrue(telegram.notify_new_materials(self.conn, ('token', '12345', 'https://example.org')))
            self.assertFalse(telegram.notify_new_materials(self.conn, ('token', '12345', 'https://example.org')))
        self.assertEqual(send.call_count, 2)

    def test_other_sender_cannot_claim_while_lock_is_held(self):
        telegram.initialize(self.conn)
        self.conn.execute('INSERT INTO materials DEFAULT VALUES')
        with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as holder:
            holder.execute('SELECT pg_advisory_lock(%s)', (telegram.LOCK_ID,))
            try:
                with patch.object(telegram, 'send_message') as send:
                    self.assertFalse(telegram.notify_new_materials(self.conn, CONFIG))
                    self.assertEqual(telegram.notify_new_materials_status(self.conn, CONFIG), ("busy", None))
                    send.assert_not_called()
            finally:
                holder.execute('SELECT pg_advisory_unlock(%s)', (telegram.LOCK_ID,))

    def test_sent_batch_and_cycle_delivery_proof_commit_together(self):
        telegram.initialize(self.conn)
        self.conn.execute('INSERT INTO materials DEFAULT VALUES')
        cycle_id = db.start_cycle(self.conn)
        target = db.set_notification_target(self.conn, cycle_id)
        with patch.object(telegram, 'send_message', return_value=42):
            status, delivered_ids = telegram.notify_new_materials_status(
                self.conn, CONFIG, cycle_id=cycle_id)
        self.assertEqual(status, "sent")
        self.assertEqual(delivered_ids, target)
        row = self.conn.execute("""SELECT notification_status, notification_delivered_material_ids
                                  FROM cycles WHERE id = %s""", (cycle_id,)).fetchone()
        self.assertEqual(row, ("sent", delivered_ids))
if __name__ == '__main__':
    unittest.main()
