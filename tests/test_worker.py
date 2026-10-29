import io
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import main as cli
from worker import main as worker


class Response(io.BytesIO):
    pass


class Connection:
    def __init__(self):
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


BATCH = [{"material_id": 1, "title": "Example"}, {"material_id": 2, "title": "Other"}]
SIGNAL = {"material_ids": [1], "platform": "Ozon", "topic": "rules", "severity": "info", "summary": "Summary", "recommended_action": "Check", "verification": "needs_check"}


def response(content):
    return Response(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = Connection()
        self.pending = patch.object(worker.db, "pending_materials", return_value=BATCH)
        self.importer = patch.object(worker.db, "import_signals", return_value=(1, 1, 1))
        self.pending.start()
        self.import_mock = self.importer.start()
        self.addCleanup(self.pending.stop)
        self.addCleanup(self.importer.stop)
        env = patch.dict(worker.os.environ, {"LM_STUDIO_BASE_URL": "http://127.0.0.1:1234/v1", "LM_STUDIO_MODEL": "test", "LM_STUDIO_TIMEOUT": "5", "LM_STUDIO_BATCH_SIZE": "2"})
        env.start()
        self.addCleanup(env.stop)

    def test_valid_completion_and_request(self):
        payload = {"signals": [SIGNAL], "skipped_material_ids": [2]}
        with patch.object(worker.urllib.request, "urlopen", return_value=response(json.dumps(payload))) as http:
            self.assertEqual(worker.process(self.conn), (1, 1, 1))
        req = http.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(json.loads(req.data)["model"], "test")
        self.import_mock.assert_called_once_with(self.conn, payload["signals"], [2])
        self.assertEqual(self.conn.transactions, 1)

    def test_model_verification_is_always_unconfirmed_locally(self):
        for value in ("confirmed", "unexpected", None):
            signal = {**SIGNAL, "verification": value}
            payload = {"signals": [signal], "skipped_material_ids": [2]}
            with self.subTest(value=value), patch.object(worker.urllib.request, "urlopen", return_value=response(json.dumps(payload))):
                worker.process(self.conn)
                self.assertEqual(self.import_mock.call_args.args[1][0]["verification"], "needs_check")
        self.assertEqual(signal["verification"], None)

    def test_remote_model_confirmed_is_sent_as_needs_check(self):
        payload = {"signals": [{**SIGNAL, "verification": "confirmed"}], "skipped_material_ids": [2]}
        with patch.object(worker.urllib.request, "build_opener") as factory, patch.object(worker.urllib.request, "urlopen", return_value=response(json.dumps(payload))):
            api = factory.return_value.open
            api.side_effect = [Response(json.dumps({"materials": BATCH}).encode()), Response(b'{"created":1,"linked":1,"skipped":1}')]
            worker.process_remote("https://radar.example", "secret")
            self.assertEqual(json.loads(api.call_args_list[1].args[0].data)["signals"][0]["verification"], "needs_check")

    def test_manual_review_import_preserves_confirmed(self):
        signal = {**SIGNAL, "verification": "confirmed"}
        payload = {"signals": [signal], "skipped_material_ids": []}
        with patch.object(cli.Path, "read_text", return_value=json.dumps(payload)), patch.object(cli.db, "connect") as connect, patch.object(cli.db, "init_schema"):
            conn = connect.return_value.__enter__.return_value
            cli.import_review("review.json")
            self.import_mock.assert_called_once_with(conn, [signal], [])

    def test_failures_never_import(self):
        invalid = ["```json\n{}\n```", '{"signals": [], "signals": [], "skipped_material_ids": []}', json.dumps({"signals": [SIGNAL], "skipped_material_ids": [1]}), json.dumps({"signals": [{**SIGNAL, "material_ids": [3]}], "skipped_material_ids": []}), json.dumps({"signals": [{**SIGNAL, "severity": "urgent"}], "skipped_material_ids": []})]
        for content in invalid:
            with self.subTest(content=content), patch.object(worker.urllib.request, "urlopen", return_value=response(content)):
                with self.assertRaises(RuntimeError):
                    worker.process(self.conn)
        for error in (TimeoutError("timeout"), OSError("offline")):
            with patch.object(worker.urllib.request, "urlopen", side_effect=error), self.assertRaises(RuntimeError):
                worker.process(self.conn)
        self.import_mock.assert_not_called()
        self.assertEqual(self.conn.transactions, 0)

    def test_empty_batch_does_not_call_model(self):
        with patch.object(worker.db, "pending_materials", return_value=[]), patch.object(worker.urllib.request, "urlopen") as http:
            self.assertEqual(worker.process(self.conn), (0, 0, 0))
        http.assert_not_called()

    def test_probe(self):
        with patch.object(worker.urllib.request, "urlopen", return_value=Response(b'{"data":[{"id":"local"}]}')):
            self.assertEqual(worker.probe(), {"available": True, "models": ["local"]})
        with patch.object(worker.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(worker.probe()["available"])

    def test_remote_round_trip(self):
        payload = {"signals": [SIGNAL], "skipped_material_ids": [2]}
        with patch.object(worker.urllib.request, "build_opener") as factory, patch.object(worker.urllib.request, "urlopen", return_value=response(json.dumps(payload))):
            api = factory.return_value.open
            api.side_effect = [Response(json.dumps({"materials": BATCH}).encode()), Response(b'{"created":1,"linked":1,"skipped":1}')]
            self.assertEqual(worker.process_remote("https://radar.example", "secret"), (1, 1, 1))
            first, second = [call.args[0] for call in api.call_args_list]
            self.assertEqual(first.full_url, "https://radar.example/api/worker/pending?limit=2")
            self.assertEqual(first.get_header("Authorization"), "Bearer secret")
            self.assertEqual(second.full_url, "https://radar.example/api/worker/results")
            self.assertEqual(json.loads(second.data)["batch_material_ids"], [1, 2])
        self.import_mock.assert_not_called()

    def test_remote_rejects_insecure_and_partial_configuration(self):
        with self.assertRaises(ValueError):
            worker.process_remote("http://radar.example", "secret")
        with patch.dict(worker.os.environ, {"WORKER_API_URL": "https://radar.example", "WORKER_TOKEN": ""}), patch.object(worker.db, "connect") as connect:
            with self.assertRaises(ValueError):
                worker.main()
            connect.assert_not_called()

    def test_remote_model_failure_never_posts(self):
        with patch.object(worker.urllib.request, "build_opener") as factory, patch.object(worker.urllib.request, "urlopen", side_effect=OSError("offline")):
            api = factory.return_value.open
            api.return_value = Response(json.dumps({"materials": BATCH}).encode())
            with self.assertRaises(RuntimeError):
                worker.process_remote("https://radar.example", "secret")
            self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
