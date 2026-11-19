import base64
import http.client
import json
import os
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import psycopg

import db
from web import main as web


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class FindingFeedbackPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        self.schema = "finding_feedback_" + uuid.uuid4().hex
        self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn.execute(f'SET search_path TO "{self.schema}"')
        db.init_schema(self.conn)
        db.sync_sources(self.conn)
        source_id = self.conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        material_id = self.conn.execute(
            """INSERT INTO materials(source_id, url, title, text, fingerprint)
               VALUES (%s, 'https://example.test/news', 'News', 'Text', 'feedback-news') RETURNING id""",
            (source_id,),
        ).fetchone()[0]
        self.finding_id = self.conn.execute(
            """INSERT INTO events(event_key, platform, topic, importance, summary, recommended_action, verification, updated_at)
               VALUES ('feedback-event', 'Ozon', 'rules', 4, 'Summary', 'Review', 'needs_check', %s) RETURNING id""",
            (datetime.now(timezone.utc),),
        ).fetchone()[0]
        self.conn.execute("INSERT INTO event_materials(event_id, material_id) VALUES (%s, %s)", (self.finding_id, material_id))

    def tearDown(self):
        self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def test_latest_feedback_is_upserted_and_returned_by_feed_without_changing_verification(self):
        self.assertEqual(db.set_finding_feedback(self.conn, self.finding_id, "useful"),
                         {"id": self.finding_id, "feedback": "useful"})
        self.assertEqual(db.set_finding_feedback(self.conn, self.finding_id, "not_suitable"),
                         {"id": self.finding_id, "feedback": "not_suitable"})
        self.assertEqual(self.conn.execute("SELECT count(*), max(feedback) FROM finding_feedback").fetchone(),
                         (1, "not_suitable"))
        self.assertEqual(self.conn.execute("SELECT verification FROM events WHERE id=%s", (self.finding_id,)).fetchone()[0],
                         "needs_check")
        finding = db.feed_findings(self.conn)["findings"][0]
        self.assertEqual(finding["feedback"], "not_suitable")

    def test_rejects_unknown_finding_and_invalid_feedback(self):
        with self.assertRaises(ValueError):
            db.set_finding_feedback(self.conn, 999999, "useful")
        for value in ("", "maybe", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                db.set_finding_feedback(self.conn, self.finding_id, value)


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        yield


class FindingFeedbackApiTests(unittest.TestCase):
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
        self.env = patch.dict(web.os.environ, {"WEB_BASIC_USER": "user", "WEB_BASIC_PASSWORD": "password"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = _Connection()
        self.connect = patch.object(web.db, "connect", return_value=self.conn)
        self.connect.start()
        self.addCleanup(self.connect.stop)

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

    def test_feedback_api_requires_owner_authorization_and_same_origin(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        path = "/api/findings/7/feedback"
        self.assertEqual(self.request("POST", path, body='{"feedback":"useful"}', headers=headers)[0], 401)
        with patch.object(web.db, "set_finding_feedback", return_value={"id": 7, "feedback": "useful"}, create=True) as save:
            self.assertEqual(self.request("POST", path, basic, '{"feedback":"useful"}',
                                          {**headers, "Origin": "https://attacker.example"})[0], 403)
            status, body = self.request("POST", path, basic, '{"feedback":"useful"}', headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"id": 7, "feedback": "useful"})
        save.assert_called_once_with(self.conn, 7, "useful")

    def test_feedback_api_rejects_invalid_payload_and_unknown_finding(self):
        basic = "Basic " + base64.b64encode(b"user:password").decode()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        path = "/api/findings/7/feedback"
        for payload in ('{}', '{"feedback":"maybe"}', '{"feedback":"useful","extra":true}',
                        '{"feedback":"not_suitable","feedback":"useful"}', '[]'):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", path, basic, payload, headers)[0], 400)
        with patch.object(web.db, "set_finding_feedback", side_effect=ValueError("finding not found"), create=True):
            self.assertEqual(self.request("POST", "/api/findings/999/feedback", basic,
                                          '{"feedback":"not_suitable"}', headers)[0], 404)


if __name__ == "__main__":
    unittest.main()
