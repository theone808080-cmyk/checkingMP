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
