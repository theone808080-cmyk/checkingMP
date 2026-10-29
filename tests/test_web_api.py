import base64
import http.client
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from web import main as web


class Connection:
    def __init__(self):
        self.pending = {1, 2}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, params=None):
        self.rows = [(i,) for i in params[0] if i in self.pending] if params else []
        return self

    def fetchall(self):
        return self.rows


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.env = patch.dict(web.os.environ, {"WEB_BASIC_USER": "user", "WEB_BASIC_PASSWORD": "password", "WORKER_TOKEN": "token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = Connection()
        self.connect = patch.object(web.db, "connect", return_value=self.conn)
        self.connect.start()
        self.addCleanup(self.connect.stop)
        self.pending = patch.object(web.db, "pending_materials", return_value=[{"material_id": 1}, {"material_id": 2}])
        self.pending.start()
        self.addCleanup(self.pending.stop)
        self.importer = patch.object(web.db, "import_signals", return_value=(0, 0, 1))
        self.import_mock = self.importer.start()
        self.addCleanup(self.importer.stop)

    def request(self, method, path, auth=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Authorization": auth} if auth else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_authentication_and_health(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        self.assertEqual(self.request("GET", "/health")[0], 200)
        for auth in (None, "Bearer token", "Basic invalid"):
            self.assertEqual(self.request("GET", "/", auth)[0], 401)
        with patch.object(web, "render", return_value="page"):
            self.assertEqual(self.request("GET", "/", basic), (200, b"page"))
        for auth in (None, basic, "Bearer wrong"):
            self.assertEqual(self.request("GET", "/api/worker/pending", auth)[0], 401)
        self.assertEqual(self.request("POST", "/api/worker/results", basic, "{}")[0], 401)
        for path in ("/", "/api/worker/pending", "/health"):
            self.assertEqual(self.request("TRACE", path)[0], 401)
        self.assertEqual(self.request("TRACE", "/", basic)[0], 405)
        self.assertEqual(self.request("TRACE", "/api/worker/pending", "Bearer token")[0], 405)
        self.assertEqual(self.request("TRACE", "/api/worker/pending", basic)[0], 401)
        with patch.dict(web.os.environ, {"WEB_BASIC_PASSWORD": "", "WORKER_TOKEN": ""}):
            self.assertEqual(self.request("GET", "/", basic)[0], 401)
            self.assertEqual(self.request("GET", "/api/worker/pending", "Bearer token")[0], 401)

    def test_batch_limits_and_valid_result(self):
        for limit in ("0", "51", "x", "1&limit=2"):
            self.assertEqual(self.request("GET", "/api/worker/pending?limit=" + limit, "Bearer token")[0], 400)
        status, body = self.request("GET", "/api/worker/pending?limit=2", "Bearer token")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"materials": [{"material_id": 1}, {"material_id": 2}]})
        payload = {"batch_material_ids": [1, 2], "signals": [], "skipped_material_ids": [1]}
        status, body = self.request("POST", "/api/worker/results", "Bearer token", json.dumps(payload))
        self.assertEqual((status, json.loads(body)), (200, {"created": 0, "linked": 0, "skipped": 1}))
        self.import_mock.assert_called_once_with(self.conn, [], [1])

    def test_worker_api_cannot_confirm_a_signal(self):
        signal = {"material_ids": [1], "platform": "Ozon", "topic": "rules", "severity": "info", "summary": "Summary", "recommended_action": "Check", "verification": "confirmed"}
        payload = {"batch_material_ids": [1, 2], "signals": [signal], "skipped_material_ids": [2]}
        self.assertEqual(self.request("POST", "/api/worker/results", "Bearer token", json.dumps(payload))[0], 200)
        self.assertEqual(self.import_mock.call_args.args[1][0]["verification"], "needs_check")

    def test_invalid_or_stale_results_never_import(self):
        base = {"batch_material_ids": [1, 2], "signals": [], "skipped_material_ids": []}
        signal = {"material_ids": [1], "platform": "Ozon", "topic": "rules", "severity": "info", "summary": "Summary", "recommended_action": "Check", "verification": "needs_check"}
        payloads = [dict(base, skipped_material_ids=[3]), dict(base, skipped_material_ids=[1, 1]), dict(base, batch_material_ids=[1, 1]), dict(base, batch_material_ids=[1, 3]), dict(base, signals=[{"material_ids": [1]}]), dict(base, signals=[signal, signal]), dict(base, signals=[signal], skipped_material_ids=[1])]
        for payload in payloads:
            self.assertEqual(self.request("POST", "/api/worker/results", "Bearer token", json.dumps(payload))[0], 400)
        self.assertEqual(self.request("POST", "/api/worker/results", "Bearer token", '{"signals":[],"signals":[]}')[0], 400)
        self.conn.pending.remove(2)
        self.assertEqual(self.request("POST", "/api/worker/results", "Bearer token", json.dumps(base))[0], 400)
        self.import_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
