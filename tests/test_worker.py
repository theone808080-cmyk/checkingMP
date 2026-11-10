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

    def execute(self, query, params):
        self.query = query
        self.ids = params[0]
        return self

    def fetchall(self):
        return [(i,) for i in self.ids]


BATCH = [
    {"material_id": 1, "title": "Example", "translation_status": "pending", "text": "English excerpt"},
    {"material_id": 2, "title": "Другая новость", "translation_status": "pending", "text": "Другой текст"},
]
TRANSLATIONS = [
    {"material_id": 1, "title_ru": "Пример", "text_ru": "Английский фрагмент"},
    {"material_id": 2, "title_ru": "Другая новость", "text_ru": "Другой текст"},
]
SIGNAL = {"material_ids": [1], "platform": "Ozon", "topic": "rules", "importance": 3, "summary": "Summary", "recommended_action": "Check", "verification": "needs_check"}


def response(content):
    return Response(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = Connection()
        self.actual_import = worker.db.import_signals
        self.pending = patch.object(worker.db, "pending_materials", return_value=BATCH)
        self.importer = patch.object(worker.db, "import_signals", return_value=(1, 1, 1))
        self.filterer = patch.object(worker, "assess_pending", return_value=0)
        self.pending.start()
        self.import_mock = self.importer.start()
        self.filter_mock = self.filterer.start()
        self.addCleanup(self.pending.stop)
        self.addCleanup(self.importer.stop)
        self.addCleanup(self.filterer.stop)
        env = patch.dict(worker.os.environ, {"OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY": "secret", "OPENROUTER_MODEL": "test", "OPENROUTER_TIMEOUT": "5", "OPENROUTER_BATCH_SIZE": "2"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_valid_completion_and_request(self):
        signal = {**SIGNAL, "platform": "WB/Ozon", "topic": "Логистика",
                  "summary": "Изменились условия доставки", "recommended_action": "Проверить отгрузки"}
        payload = {"signals": [signal], "skipped_material_ids": [2], "translations": TRANSLATIONS}
        with patch.object(worker, "open_url", return_value=response(json.dumps(payload))) as http:
            self.assertEqual(worker.process(self.conn), (1, 1, 1))
        req = http.call_args.args[0]
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data)["temperature"], 0)
        self.assertEqual(json.loads(req.data)["response_format"], {"type": "json_object"})
        self.assertEqual(json.loads(req.data)["max_tokens"], 2500)
        self.assertEqual(json.loads(req.data)["model"], "test")
        prompt = json.loads(req.data)["messages"][0]["content"]
        self.assertIn("importance (integer 1-5", prompt)
        self.assertIn("in Russian", prompt)
        imported_signal = {**signal, "platform": "Межплощадочная: Ozon, Wildberries", "verification": "needs_check"}
        self.import_mock.assert_called_once_with(self.conn, [imported_signal], [2], translations=TRANSLATIONS, reject_confirmed=True)
        self.assertEqual(self.conn.transactions, 1)

    def test_requires_complete_unique_translations_for_pending_materials(self):
        valid = {"signals": [SIGNAL], "skipped_material_ids": [2], "translations": TRANSLATIONS}
        result = worker.validate(valid, BATCH)
        self.assertEqual(result["translations"], TRANSLATIONS)
        long_title = "Н" * 501
        long_title_result = worker.validate({**valid, "translations": [
            {**TRANSLATIONS[0], "title_ru": long_title}, TRANSLATIONS[1]
        ]}, BATCH)
        self.assertEqual(long_title_result["translations"][0]["title_ru"], long_title)
        invalid = (
            [],
            [TRANSLATIONS[0]],
            [TRANSLATIONS[0], TRANSLATIONS[0]],
            [TRANSLATIONS[0], {**TRANSLATIONS[1], "material_id": 99}],
            [{**TRANSLATIONS[0], "title_ru": " "}, TRANSLATIONS[1]],
            [{**TRANSLATIONS[0], "text_ru": ""}, TRANSLATIONS[1]],
            [{**TRANSLATIONS[0], "text_ru": "x" * 401}, TRANSLATIONS[1]],
        )
        for translations in invalid:
            with self.subTest(translations=translations), self.assertRaises(ValueError):
                worker.validate({**valid, "translations": translations}, BATCH)

    def test_translation_only_call_uses_existing_model_and_validates_results(self):
        payload = {"translations": TRANSLATIONS}
        with patch.object(worker, "open_url", return_value=response(json.dumps(payload))) as http:
            self.assertEqual(worker.translate(BATCH), TRANSLATIONS)
        request = http.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertIn("Translate non-Russian content into Russian", body["messages"][0]["content"])
    def test_shadow_filter_runs_before_model_processing(self):
        calls = []
        payload = {"signals": [{**SIGNAL, "material_ids": [1]}], "skipped_material_ids": [2], "translations": TRANSLATIONS}
        with patch.object(worker, "assess_pending", create=True, side_effect=lambda _conn: calls.append("filter")), \
             patch.object(worker, "infer", side_effect=lambda _batch: calls.append("model") or payload):
            result = worker.process(self.conn)

        self.assertEqual(result, (1, 1, 1))
        self.assertEqual(calls, ["filter", "model"])

    def test_filter_failure_does_not_stop_model_processing(self):
        payload = {"signals": [{**SIGNAL, "material_ids": [1]}], "skipped_material_ids": [2], "translations": TRANSLATIONS}
        with patch.object(worker, "assess_pending", create=True, side_effect=RuntimeError("filter failed")) as filter_mock, \
             patch.object(worker, "infer", return_value=payload):
            result = worker.process(self.conn)

        self.assertEqual(result, (1, 1, 1))
        filter_mock.assert_called_once_with(self.conn)

    def test_model_importance_must_be_integer_between_one_and_five(self):
        for value in range(1, 6):
            with self.subTest(value=value):
                payload = {"signals": [{**SIGNAL, "importance": value}], "skipped_material_ids": [], "translations": TRANSLATIONS}
                self.assertEqual(worker.validate(payload, BATCH)["signals"][0]["importance"], value)
        invalid_values = (None, True, False, 0, 6, 2.5, "3")
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload = {"signals": [{**SIGNAL, "importance": value}], "skipped_material_ids": [], "translations": TRANSLATIONS}
                worker.validate(payload, BATCH)
        with self.assertRaises(ValueError):
            payload = {"signals": [{key: value for key, value in SIGNAL.items() if key != "importance"}], "skipped_material_ids": [], "translations": TRANSLATIONS}
            worker.validate(payload, BATCH)

    def test_model_platforms_are_canonicalized_and_unknown_values_rejected(self):
        for source, expected in (("Яндекс.Маркет", "Яндекс Маркет"), ("WB/Ozon", "Межплощадочная: Ozon, Wildberries")):
            payload = {"signals": [{**SIGNAL, "platform": source}], "skipped_material_ids": [], "translations": TRANSLATIONS}
            self.assertEqual(worker.validate(payload, BATCH)["signals"][0]["platform"], expected)
        payload = {"signals": [{**SIGNAL, "platform": "unknown marketplace"}], "skipped_material_ids": [], "translations": TRANSLATIONS}
        with self.assertRaises(ValueError):
            worker.validate(payload, BATCH)

    def test_default_model(self):
        with patch.dict(worker.os.environ, {"OPENROUTER_API_KEY": "secret"}, clear=True):
            self.assertEqual(worker.settings()[2], "openai/gpt-4.1-nano")

    def test_stale_batch_is_rejected_before_import(self):
        payload = {"signals": [SIGNAL], "skipped_material_ids": [2], "translations": TRANSLATIONS}
        with patch.object(self.conn, "fetchall", return_value=[(1,)]), patch.object(worker, "open_url", return_value=response(json.dumps(payload))):
            with self.assertRaisesRegex(ValueError, "stale"):
                worker.process(self.conn)
        self.import_mock.assert_not_called()
        self.assertIn("FOR UPDATE", self.conn.query)

    def test_confirmed_event_collision_is_rejected(self):
        class ConflictConnection:
            def execute(self, query, params):
                self.query = query
                return self
            def fetchone(self):
                return None
            def fetchall(self):
                return []
        conn = ConflictConnection()
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.actual_import(conn, [SIGNAL], reject_confirmed=True)
        self.assertIn("WHERE events.verification <> 'confirmed'", conn.query)

    def test_manual_signal_import_rejects_invalid_importance_before_database_write(self):
        class NoWriteConnection:
            def execute(self, *_args, **_kwargs):
                raise AssertionError("invalid importance must be rejected before SQL")
        for value in (True, False, 0, 6, 2.5, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.actual_import(NoWriteConnection(), [{**SIGNAL, "importance": value}])

    def test_openrouter_requests_reject_redirects(self):
        handler = worker.NoRedirect()
        req = worker.urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": "Bearer secret"})
        for target in ("https://other.example/steal", "http://openrouter.ai/steal"):
            self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, target))
        with patch.object(worker.urllib.request, "build_opener") as build:
            worker.open_url(req, timeout=5)
        self.assertIsInstance(build.call_args.args[0], worker.NoRedirect)
        build.return_value.open.assert_called_once_with(req, timeout=5)

    def test_model_verification_is_always_unconfirmed_locally(self):
        for value in ("confirmed", "unexpected", None):
            signal = {**SIGNAL, "verification": value}
            payload = {"signals": [signal], "skipped_material_ids": [2], "translations": TRANSLATIONS}
            with self.subTest(value=value), patch.object(worker, "open_url", return_value=response(json.dumps(payload))):
                worker.process(self.conn)
                self.assertEqual(self.import_mock.call_args.args[1][0]["verification"], "needs_check")
        self.assertEqual(signal["verification"], None)

    def test_missing_key_and_insecure_url_fail_before_import(self):
        for env in ({"OPENROUTER_API_KEY": "  "}, {"OPENROUTER_BASE_URL": "http://example.com/v1"}, {"OPENROUTER_MAX_TOKENS": "2501"}):
            with self.subTest(env=env), patch.dict(worker.os.environ, env), patch.object(worker, "open_url") as http, self.assertRaises(ValueError):
                worker.process(self.conn)
            http.assert_not_called()
        self.import_mock.assert_not_called()

    def test_cycle_collects_before_processing(self):
        calls = []
        with patch.object(cli.db, "connect"), patch.object(cli.db, "init_schema"), \
             patch.object(cli.db, "start_cycle", return_value=1), \
             patch.object(cli, "_record_cycle_stage"), patch.object(cli, "_finish_cycle"), \
             patch.object(cli, "collect", side_effect=lambda: calls.append("collect")), \
             patch.object(cli, "process", side_effect=lambda: calls.append("process")):
            cli.cycle()
        self.assertEqual(calls, ["collect", "process"])
    def test_manual_review_import_preserves_confirmed(self):
        signal = {**SIGNAL, "verification": "confirmed"}
        payload = {"signals": [signal], "skipped_material_ids": []}
        with patch.object(cli.Path, "read_text", return_value=json.dumps(payload)), patch.object(cli.db, "connect") as connect, patch.object(cli.db, "init_schema"), patch.object(cli, "translate", return_value=TRANSLATIONS[:1]) as translate:
            conn = connect.return_value.__enter__.return_value
            cli.import_review("review.json")
            translate.assert_called_once_with([BATCH[0]])
            self.import_mock.assert_called_once_with(conn, [signal], [], translations=TRANSLATIONS[:1])

    def test_failures_never_import(self):
        invalid = ["```json\n{}\n```", '{"signals": [], "signals": [], "skipped_material_ids": []}', json.dumps({"signals": [SIGNAL], "skipped_material_ids": [1]}), json.dumps({"signals": [{**SIGNAL, "material_ids": [3]}], "skipped_material_ids": []}), json.dumps({"signals": [{**SIGNAL, "importance": 9}], "skipped_material_ids": []})]
        for content in invalid:
            with self.subTest(content=content), patch.object(worker, "open_url", return_value=response(content)):
                with self.assertRaises(RuntimeError):
                    worker.process(self.conn)
        for error in (TimeoutError("timeout"), OSError("offline")):
            with patch.object(worker, "open_url", side_effect=error), self.assertRaises(RuntimeError):
                worker.process(self.conn)
        self.import_mock.assert_not_called()
        self.assertEqual(self.conn.transactions, 0)

    def test_empty_batch_does_not_call_model(self):
        with patch.object(worker.db, "pending_materials", return_value=[]), patch.object(worker, "open_url") as http:
            self.assertEqual(worker.process(self.conn), (0, 0, 0))
        http.assert_not_called()

    def test_invalid_key_rejected_without_network_or_disclosure(self):
        for key in ("synthetic-first\nsynthetic-second", "synthetic-first\rsynthetic-second", "synthetic key", " synthetic", "synthetic\x01key"):
            with self.subTest(key=repr(key)), patch.dict(worker.os.environ, {"OPENROUTER_API_KEY": key}), patch.object(worker, "open_url", side_effect=AssertionError("network must not be called")) as http:
                with self.assertRaises(ValueError) as caught:
                    worker.infer(BATCH)
                self.assertNotIn("synthetic", str(caught.exception))
                result = worker.probe()
                self.assertFalse(result["available"])
                self.assertNotIn("synthetic", str(result))
                http.assert_not_called()

    def test_transport_error_does_not_disclose_key_or_exception_chain(self):
        import traceback
        key = "synthetic-secret"
        with patch.dict(worker.os.environ, {"OPENROUTER_API_KEY": key}), patch.object(worker, "open_url", side_effect=ValueError("Bearer " + key)):
            with self.assertRaises(RuntimeError) as caught:
                worker.infer(BATCH)
            self.assertNotIn(key, "".join(traceback.format_exception(caught.exception)))
            self.assertNotIn(key, str(worker.probe()))

    def test_safe_processing_failure_categories_differ_by_failure_type(self):
        key = "synthetic-secret"
        errors = []
        for error in (OSError("offline"), ValueError("invalid response")):
            with patch.dict(worker.os.environ, {"OPENROUTER_API_KEY": key}), \
                 patch.object(worker, "open_url", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    worker.infer(BATCH)
                errors.append(caught.exception)
        self.assertNotEqual(errors[0].failure_signature, errors[1].failure_signature)
        self.assertNotIn(key, str(errors[0]))

    def test_probe(self):
        with patch.object(worker, "open_url", return_value=Response(b'{"data":[{"id":"local"}]}')):
            self.assertEqual(worker.probe(), {"available": True, "models": ["local"]})
        with patch.object(worker, "open_url", side_effect=OSError("offline")):
            self.assertFalse(worker.probe()["available"])



if __name__ == "__main__":
    unittest.main()
