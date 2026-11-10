import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from collector.main import fetch, read_html, read_telegram


class CollectorParsingTests(unittest.TestCase):
    def test_collection_uses_pinned_public_fetch_with_response_bound(self):
        with patch("collector.main.fetch_public", return_value=b"feed") as safe_fetch:
            self.assertEqual(fetch("https://example.test/feed", safe=True), b"feed")
        safe_fetch.assert_called_once_with("https://example.test/feed", timeout=30, max_bytes=2_000_000)

    def test_config_sources_keep_existing_fetch_path(self):
        with patch("collector.main.urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b"feed"
            self.assertEqual(fetch("https://example.test/feed"), b"feed")
        open_url.assert_called_once_with("https://example.test/feed", timeout=30)

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
        error, finished, processed = self.run_cycle([(1, "one", "rss", "https://example.test", "one", "config")], {"rss": failed})
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
        error, finished, processed = self.run_cycle([(1, "one", "bad", "https://example.test", "one", "config"), (2, "two", "good", "https://example.test", "two", "config")], {"bad": failed, "good": lambda url: []})
        self.assertIsNone(error)
        self.assertEqual(finished[2], "finished")
        self.assertEqual(processed, 1)


class CollectorTraceTests(unittest.TestCase):
    def test_material_collection_records_cycle_run_source_and_material_ids(self):
        from collector import main as collector
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            (3, "source", "rss", "https://example.test/feed", "source", "config")
        ]
        with patch.object(collector.db, "connect") as connect, \
             patch.object(collector.db, "init_schema"), patch.object(collector.db, "sync_sources"), \
             patch.object(collector.db, "start_run", return_value=12) as start_run, \
             patch.object(collector.db, "add_material", return_value=(True, 42)), \
             patch.object(collector.db, "record_trace_attempt") as trace, \
             patch.object(collector.db, "finish_run"), \
             patch.dict(collector.READERS, {"rss": lambda _url: [{"url": "https://example.test/story", "title": "Story", "text": "Text"}]}, clear=True):
            connect.return_value.__enter__.return_value = conn
            result = collector.main(cycle_id=9)
        self.assertEqual(result["items"], 1)
        start_run.assert_called_once_with(conn, "collector", cycle_id=9)
        trace.assert_called_once()
        args = trace.call_args.kwargs
        self.assertEqual((args["cycle_id"], args["run_id"], args["source_id"], args["material_id"]), (9, 12, 3, 42))
        self.assertEqual((args["stage"], args["status"]), ("collection", "success"))

    def test_source_failure_records_safe_trace_error_without_exception_text(self):
        from collector import main as collector
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            (3, "source", "rss", "https://example.test/feed", "source", "config")
        ]
        output = StringIO()
        with patch.object(collector.db, "connect") as connect, \
             patch.object(collector.db, "init_schema"), patch.object(collector.db, "sync_sources"), \
             patch.object(collector.db, "start_run", return_value=12), \
             patch.object(collector.db, "record_trace_attempt") as trace, \
             patch.object(collector.db, "finish_run") as finish_run, \
             patch.dict(collector.READERS, {"rss": lambda _url: (_ for _ in ()).throw(RuntimeError("Bearer secret prompt"))}, clear=True):
            connect.return_value.__enter__.return_value = conn
            with redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, "No sources"):
                    collector.main(cycle_id=9)
        self.assertEqual(trace.call_args.kwargs["error_code"], "source_fetch_failed")
        self.assertNotIn("secret", repr(trace.call_args.kwargs))
        self.assertNotIn("prompt", repr(trace.call_args.kwargs))
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("prompt", output.getvalue())
        self.assertNotIn("secret", repr(finish_run.call_args))
