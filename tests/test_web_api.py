import base64
import http.client
import json
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from web import main as web
from test_telegram_auth import TOKEN, signed_data


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

    def request(self, method, path, auth=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = dict(headers or {})
        if auth:
            request_headers["Authorization"] = auth
        conn.request(method, path, body=body, headers=request_headers)
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
        for auth in (None, "Bearer token", "Bearer wrong"):
            self.assertEqual(self.request("GET", "/api/worker/pending", auth)[0], 401)
        self.assertEqual(self.request("GET", "/api/worker/pending", basic)[0], 404)
        self.assertEqual(self.request("POST", "/api/worker/results", basic, "{}")[0], 404)
        for path in ("/", "/api/worker/pending", "/health"):
            self.assertEqual(self.request("TRACE", path)[0], 401)
        self.assertEqual(self.request("TRACE", "/", basic)[0], 405)
        self.assertEqual(self.request("TRACE", "/api/worker/pending", "Bearer token")[0], 401)
        self.assertEqual(self.request("TRACE", "/api/worker/pending", basic)[0], 405)
        with patch.dict(web.os.environ, {"WEB_BASIC_PASSWORD": "", "WORKER_TOKEN": ""}):
            self.assertEqual(self.request("GET", "/", basic)[0], 401)
            self.assertEqual(self.request("GET", "/api/worker/pending", "Bearer token")[0], 401)

    def test_session_guards_private_feed_and_rejects_invalid_filters(self):
        with patch.dict(web.os.environ, {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}):
            self.assertEqual(self.request("GET", "/miniapp")[0], 200)
            self.assertEqual(self.request("GET", "/api/feed")[0], 401)
            self.assertEqual(self.request("POST", "/miniapp/session", body='{}')[0], 401)
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            conn.request("POST", "/miniapp/session", json.dumps({"initData": signed_data(timestamp=int(time.time()))}), {"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader("Set-Cookie")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=Strict", cookie)
            response.read()
            conn.close()
            headers = {"Cookie": cookie.split(";", 1)[0]}
            with patch.object(web.db, "feed_materials", return_value={"materials": [], "total": 0, "next_cursor": None}):
                conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
                conn.request("GET", "/api/feed", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                response.read()
                conn.close()
            basic = "Basic " + base64.b64encode(b"user:password").decode()
            self.assertEqual(self.request("GET", "/api/feed?period=all&period=today", basic)[0], 400)

    def test_findings_api_requires_auth_and_validates_combined_filters(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        self.assertEqual(self.request("GET", "/api/findings")[0], 401)
        with patch.object(web.db, "feed_findings", return_value={"findings": [], "total": 0, "next_cursor": None}) as feed:
            status, body = self.request(
                "GET",
                "/api/findings?period=week&platform=ozon&importance=4&topic=log&verification=needs_check&source_type=official&limit=10",
                basic,
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["findings"], [])
            self.assertEqual(feed.call_args.args[1:], ("week", "ozon", 4, "log", "needs_check", "official", None, 10))
        for query in (
            "importance=6",
            "verification=unknown",
            "platform=ozon&platform=wildberries",
            "unexpected=yes",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/findings?" + query, basic)[0], 400)

    def test_source_review_page_requires_basic_auth(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        self.assertEqual(self.request("GET", "/sources/review")[0], 401)
        self.assertEqual(self.request("GET", "/api/source-candidates")[0], 401)
        with patch.object(web, "render_source_review", return_value="review page", create=True):
            status, body = self.request("GET", "/sources/review", basic)
        self.assertEqual((status, body), (200, b"review page"))

    def test_processing_trace_is_basic_auth_only_and_read_only(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        path = "/api/findings/12/trace"
        self.assertEqual(self.request("GET", path)[0], 401)
        with patch.dict(web.os.environ, {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}):
            session = web.telegram_auth.new_session(TOKEN, "42")
            self.assertEqual(self.request("GET", path, headers={"Cookie": f"radar_session={session}"})[0], 401)
        result = {"finding": {"id": 12}, "materials": []}
        with patch.object(web.db, "finding_trace", return_value=result) as fetch:
            status, body = self.request("GET", path, basic)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), result)
        fetch.assert_called_once_with(self.conn, 12)
        with patch.object(web.db, "finding_trace", return_value=None):
            self.assertEqual(self.request("GET", "/api/findings/12/trace", basic)[0], 404)

    def test_source_approval_requires_same_origin_and_server_probe(self):
        from source_discovery.probe import ProbeResult

        basic = "Basic " + base64.b64encode(b"user:password").decode()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        body = json.dumps({"action": "approve", "fields": {
            "name": "Seller", "collection_url": "https://seller.example/",
            "kind": "rss", "category": "ozon", "trust": "industry", "scope": "ozon sellers",
        }})
        headers = {"Content-Type": "application/json", "Origin": "https://attacker.example"}
        with patch.object(web, "probe_source", return_value=ProbeResult(True, "rss", "https://seller.example/feed.xml"), create=True) as probe:
            self.assertEqual(self.request("POST", "/api/source-candidates/1", basic, body, headers)[0], 403)
            probe.assert_not_called()

        headers["Origin"] = origin
        candidate = {"id": 1, "kind": "rss", "collection_url": "https://seller.example/feed.xml"}
        with patch.object(web.db, "list_source_candidates", return_value=[candidate], create=True), \
             patch.object(web, "probe_source", return_value=ProbeResult(True, "rss", "https://seller.example/feed.xml"), create=True) as probe, \
             patch.object(web.db, "review_source_candidate", return_value={"status": "approved", "source_code": "discovered_test"}, create=True) as review:
            status, response = self.request("POST", "/api/source-candidates/1", basic, body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["status"], "approved")
        probe.assert_called_once_with("https://seller.example/", kind="rss")
        self.assertTrue(review.call_args.args[4]["ok"])
        self.assertEqual(review.call_args.args[3]["collection_url"], "https://seller.example/feed.xml")

    def test_client_cannot_supply_probe_result(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        body = json.dumps({"action": "approve", "fields": {}, "probe": {"ok": True}})
        headers = {"Content-Type": "application/json", "Origin": origin}
        with patch.object(web.db, "review_source_candidate", create=True) as review:
            self.assertEqual(self.request("POST", "/api/source-candidates/1", basic, body, headers)[0], 400)
            review.assert_not_called()



if __name__ == "__main__":
    unittest.main()
