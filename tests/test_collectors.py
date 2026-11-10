import unittest
from unittest.mock import patch

from collector.main import read_html, read_telegram


class CollectorParsingTests(unittest.TestCase):
    def test_telegram_preview_extracts_post_and_text(self):
        page = '''<div data-post="demo_channel/42"><div class="tgme_widget_message_text js-message_text">Новое <b>правило</b></div></div>'''
        with patch("collector.main.fetch", return_value=page.encode("utf-8")):
            rows = list(read_telegram("https://t.me/s/demo_channel"))
        self.assertEqual(rows[0]["url"], "https://t.me/demo_channel/42")
        self.assertEqual(rows[0]["text"], "Новое правило")

    def test_html_page_is_one_snapshot_not_navigation_links(self):
        page = "<html><head><title>Маркировка</title></head><body><nav>Меню</nav><main>Важное изменение</main></body></html>"
        with patch("collector.main.fetch", return_value=page.encode("utf-8")):
            rows = list(read_html("https://example.test/page"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.test/page")
        self.assertIn("Важное изменение", rows[0]["text"])


class CollectorCycleTests(unittest.TestCase):
    def run_cycle(self, sources, readers):
        import main as cli
        from collector import main as collector
        from unittest.mock import MagicMock
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = sources
        with patch.object(collector.db, "connect") as connect, \
             patch.object(collector.db, "init_schema"), patch.object(collector.db, "sync_sources"), \
             patch.object(collector.db, "start_run", return_value=7), \
             patch.object(collector.db, "finish_run") as finish, \
             patch.object(cli.db, "start_cycle", return_value=88), \
             patch.object(cli, "_record_cycle_stage"), patch.object(cli, "_finish_cycle"), \
             patch.object(cli, "prepare_notifications", return_value=None), \
             patch.object(cli, "send_notifications", return_value="skipped"), \
             patch.dict(collector.READERS, readers, clear=True), patch.object(cli, "process") as process:
            connect.return_value.__enter__.return_value = conn
            try:
                cli.cycle()
                error = None
            except Exception as exc:
                error = exc
            return error, finish.call_args.args, process.call_count
    def test_all_failed_sources_stop_cycle_and_mark_run_failed(self):
        def failed(url):
            raise OSError("unavailable")
        error, finished, processed = self.run_cycle([(1, "one", "rss", "https://example.test", "one")], {"rss": failed})
        self.assertIsNotNone(error)
        self.assertEqual(finished[2], "failed")
        self.assertEqual(processed, 0)

    def test_zero_sources_stop_cycle_and_mark_run_failed(self):
        error, finished, processed = self.run_cycle([], {})
        self.assertIsNotNone(error)
        self.assertEqual(finished[2], "failed")
        self.assertEqual(processed, 0)

    def test_partial_failure_allows_cycle_when_source_read(self):
        def failed(url):
            raise OSError("unavailable")
        error, finished, processed = self.run_cycle([(1, "one", "bad", "https://example.test", "one"), (2, "two", "good", "https://example.test", "two")], {"bad": failed, "good": lambda url: []})
        self.assertIsNone(error)
        self.assertEqual(finished[2], "finished")
        self.assertEqual(processed, 1)
