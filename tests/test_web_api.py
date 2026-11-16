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
        finding = {"id": 7, "platform": "Ozon", "importance": 4, "seller_impact": "negative"}
        with patch.object(web.db, "feed_findings", return_value={"findings": [finding], "total": 1, "next_cursor": None}) as feed:
            status, body = self.request(
                "GET",
                "/api/findings?period=week&platform=ozon&importance=4&topic=log&verification=needs_check&source_type=official&seller_impact=negative&summary_audit=ok&recommendation_audit=review&limit=10",
                basic,
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["findings"], [finding])
            feed.assert_called_once_with(
                self.conn, period="week", platform="ozon", importance=4, topic="log", verification="needs_check",
                trust="official", seller_impact="negative", summary_audit="ok", recommendation_audit="review",
                cursor=None, limit=10,
            )
        for query in (
            "importance=6",
            "verification=unknown",
            "seller_impact=unknown",
            "summary_audit=unknown",
            "recommendation_audit=unknown",
            "platform=ozon&platform=wildberries",
            "unexpected=yes",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/findings?" + query, basic)[0], 400)

    def test_findings_read_filter_and_read_mutation_are_owner_only_and_same_origin(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        finding = {"id": 7, "platform": "Ozon", "read": False}
        with patch.object(web.db, "feed_findings", return_value={"findings": [finding], "total": 1, "next_cursor": None}) as feed:
            status, body = self.request("GET", "/api/findings?read=unread&period=fresh", basic)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["findings"], [finding])
        self.assertEqual(feed.call_args.kwargs["read"], "unread")
        self.assertEqual(feed.call_args.kwargs["period"], "fresh")
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        with patch.object(web.db, "set_finding_read", return_value={"id": 7, "read": True}, create=True) as set_read:
            self.assertEqual(self.request("POST", "/api/findings/7/read", basic, '{"read":true}', {**headers, "Origin": "https://attacker.example"})[0], 403)
            status, body = self.request("POST", "/api/findings/7/read", basic, '{"read":true}', headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["read"])
        set_read.assert_called_once_with(self.conn, 7, True)

    def test_finding_impacts_api_accepts_owner_access_and_rejects_foreign_session_cross_origin_or_invalid_payload(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        list_path = "/api/findings/7/impacts"
        create_path = list_path
        payload = {
            "platform": "Ozon", "object_type": "sorting_center", "object_name": "СЦ Казань",
            "change_type": "delivery_tariff", "effective_date": None, "processes": ["shipment"],
            "consequence": "Проверить экономику отгрузок.", "source_url": "https://example.test/ozon/news",
        }
        self.assertEqual(self.request("GET", list_path)[0], 401)
        with patch.object(web.db, "list_finding_impacts", return_value=[], create=True):
            with patch.dict(web.os.environ, {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}):
                foreign_session = web.telegram_auth.new_session(TOKEN, "43")
                owner_session = web.telegram_auth.new_session(TOKEN, "42")
                self.assertEqual(self.request("GET", list_path, headers={"Cookie": f"radar_session={foreign_session}"})[0], 401)
                self.assertEqual(self.request("GET", list_path, headers={"Cookie": f"radar_session={owner_session}"})[0], 200)
            self.assertEqual(self.request("GET", list_path, basic)[0], 200)
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        with patch.object(web.db, "create_finding_impact", return_value={"id": 73, **payload, "status": "needs_review"}, create=True) as created:
            self.assertEqual(self.request("POST", create_path, basic, json.dumps(payload), {**headers, "Origin": "https://attacker.example"})[0], 403)
            self.assertEqual(self.request("POST", create_path, basic, json.dumps({**payload, "unexpected": True}), headers)[0], 400)
            status, body = self.request("POST", create_path, basic, json.dumps(payload), headers)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["status"], "needs_review")
        created.assert_called_once_with(self.conn, 7, payload)

    def test_finding_impact_update_requires_same_origin_and_passes_complete_card_to_storage(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        path = "/api/finding-impacts/73"
        payload = {
            "platform": "Ozon", "object_type": "seller_warehouse", "object_name": "Склад продавца",
            "change_type": "acceptance", "effective_date": "2026-12-01", "processes": ["acceptance"],
            "consequence": "Сверить график приёмки.", "source_url": "https://example.test/ozon/acceptance",
        }
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        with patch.object(web.db, "update_finding_impact", return_value={"id": 73, **payload, "status": "needs_review"}, create=True) as update:
            self.assertEqual(self.request("PATCH", path, basic, json.dumps(payload), {**headers, "Origin": "https://attacker.example"})[0], 403)
            status, body = self.request("PATCH", path, basic, json.dumps(payload), headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["object_name"], "Склад продавца")
        update.assert_called_once_with(self.conn, 73, payload)

    def test_finding_impact_decision_uses_authorized_owner_and_keeps_error_safe(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        path = "/api/finding-impacts/73/decision"
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        with patch.object(web.db, "decide_finding_impact", return_value={"id": 73, "status": "confirmed", "decided_by": "user"}, create=True) as decide:
            self.assertEqual(self.request("POST", path, basic, json.dumps({"decision": "unsupported"}), headers)[0], 400)
            status, body = self.request("POST", path, basic, json.dumps({"decision": "confirmed"}), headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "confirmed")
        decide.assert_called_once_with(self.conn, 73, "confirmed", "user")

    def test_finding_counts_api_uses_findings_authorization_and_filters(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        path = "/api/findings/counts?period=week&platform=ozon&importance=4&topic=log&verification=needs_check&source_type=official&seller_impact=negative&summary_audit=ok&recommendation_audit=review"
        expected = {"period": {"all": 1}, "platform": {"all": 1}, "importance": {"all": 1},
                    "topic_status": {"topics": [], "verification": {"all": 1}}, "source_type": {"all": 1}}
        self.assertEqual(self.request("GET", "/api/findings/counts")[0], 401)
        with patch.object(web.db, "finding_filter_counts", return_value=expected, create=True) as count:
            status, body = self.request("GET", path, basic)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), expected)
        count.assert_called_once_with(self.conn, {
            "period": "week", "platform": "ozon", "importance": 4, "topic": "log",
            "verification": "needs_check", "trust": "official", "seller_impact": "negative",
            "summary_audit": "ok", "recommendation_audit": "review",
        })
        for query in (
            "importance=0", "seller_impact=unknown", "summary_audit=unknown", "recommendation_audit=unknown",
            "cursor=old", "unexpected=yes", "period=week&period=today",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/findings/counts?" + query, basic)[0], 400)

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

    def test_source_approval_passes_validated_access_metadata_to_the_safe_probe(self):
        from source_discovery.probe import ProbeResult

        basic = "Basic " + base64.b64encode(b"user:password").decode()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        fields = {
            "access_public": True, "access_requires_credentials": False,
            "access_rate_limit": "60/hour", "access_cost": "free",
        }
        body = json.dumps({"action": "approve", "fields": fields})
        headers = {"Content-Type": "application/json", "Origin": origin}
        candidate = {"id": 1, "kind": "rss", "collection_url": "https://seller.example/feed.xml"}
        with patch.object(web.db, "list_source_candidates", return_value=[candidate], create=True), \
             patch.object(web, "probe_source", return_value=ProbeResult(True, "rss", "https://seller.example/feed.xml"), create=True), \
             patch.object(web.db, "review_source_candidate", return_value={"status": "approved", "source_code": "discovered_test"}, create=True) as review:
            self.assertEqual(self.request("POST", "/api/source-candidates/1", basic, body, headers)[0], 200)
        self.assertEqual({key: review.call_args.args[3][key] for key in fields}, fields)

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
