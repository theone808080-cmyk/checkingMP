import http.client
import threading
import unittest
import subprocess
import shutil
from http.server import ThreadingHTTPServer
from pathlib import Path

from web.main import Handler


class MiniAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_public_shell_contains_navigation_and_no_private_data(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('GET', '/miniapp')
        response = conn.getresponse()
        body = response.read().decode('utf-8')
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertIn('Лента', body)
        self.assertIn('Как собрано', body)
        self.assertIn('Сегодня', body)
        self.assertIn('Показать ещё', body)
        self.assertNotIn('E-pepper — Telegram', body)

    def test_shell_is_packaged_with_web_directory(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / 'web' / 'miniapp.html').is_file())
        self.assertIn('COPY web ./web', (root / 'Dockerfile').read_text())

    @unittest.skipUnless(shutil.which('node'), 'Node is required for JavaScript interaction test')
    def test_failed_load_more_can_retry_same_cursor(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', str(root / 'tests' / 'miniapp_retry_test.js')],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
