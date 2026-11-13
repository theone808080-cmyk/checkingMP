"""Behavioural tests for task 13: shadow quality audit of finding texts."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import psycopg
import db
import main as cli

try:
    from quality_audit.checks import check_explicit_values
    from quality_audit.reviewer import review_finding
    from quality_audit.service import import_labels, prepare_labels, report, run_audit
except ModuleNotFoundError:
    check_explicit_values = review_finding = run_audit = prepare_labels = import_labels = report = None

try:
    from db import finding_quality as quality_store
except (ImportError, ModuleNotFoundError):
    quality_store = None


FINDING = {
    "id": 41,
    "platform": "Ozon",
    "topic": "Тарифы",
    "importance": 4,
    "summary": "Комиссия составит 1500 рублей с 20 марта 2026 года.",
    "recommended_action": "Подайте заявление в налоговую до 20 марта.",
}

MATERIALS = [{
    "material_id": 7,
    "source": "Официальный Ozon",
    "url": "https://example.test/rules",
    "title": "Изменение комиссии",
    "text": "С 15 марта 2026 года комиссия составит 1000 рублей. "
            "Продавец должен проверить настройки тарифа до 15 марта.",
}]


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ExplicitEvidenceChecksTests(unittest.TestCase):
    def test_explicit_date_and_amount_absent_from_sources_are_issues_for_summary(self):
        self.assertIsNotNone(check_explicit_values, "quality audit checks are not implemented")

        issues = check_explicit_values(FINDING["summary"], FINDING["recommended_action"], MATERIALS)

        unsupported = [issue for issue in issues if issue["type"] == "unsupported_fact"]
        summary_issues = [issue for issue in unsupported if issue["target"] == "summary"]
        recommendation_issues = [issue for issue in unsupported if issue["target"] == "recommendation"]
        self.assertGreaterEqual(len(summary_issues), 2)
        self.assertTrue(recommendation_issues)
        self.assertTrue(all(issue["material_id"] == 7 for issue in unsupported))
        self.assertTrue(any("20 марта 2026" in issue["explanation"] for issue in summary_issues))
        self.assertTrue(any("1500" in issue["explanation"] for issue in summary_issues))

    def test_unambiguous_source_does_not_create_deterministic_issue_when_values_match(self):
        self.assertIsNotNone(check_explicit_values, "quality audit checks are not implemented")

        issues = check_explicit_values(
            "Комиссия составит 1000 рублей с 15 марта 2026 года.",
            "Проверьте настройки тарифа до 15 марта.",
            MATERIALS,
        )

        self.assertEqual([], issues)

    def test_empty_or_insufficient_sources_never_invent_evidence(self):
        self.assertIsNotNone(check_explicit_values, "quality audit checks are not implemented")

        issues = check_explicit_values(FINDING["summary"], FINDING["recommended_action"], [])

        self.assertEqual([], issues)


class ReviewerTests(unittest.TestCase):
    def _response(self, result):
        return {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}

    def test_reviewer_returns_independent_issue_for_omitted_deadline_and_unsupported_action(self):
        self.assertIsNotNone(review_finding, "quality audit reviewer is not implemented")
        completion = {
            "summary": {
                "status": "issue",
                "issues": [{
                    "type": "omitted_deadline", "severity": "high", "material_id": 7,
                    "evidence": "до 15 марта", "explanation": "В сводке нет срока.",
                }],
            },
            "recommendation": {
                "status": "issue",
                "issues": [{
                    "type": "unsupported_action", "severity": "medium", "material_id": 7,
                    "evidence": "проверить настройки тарифа", "explanation": "Заявление в налоговую не подтверждено.",
                }],
            },
        }
        finding = {**FINDING, "summary": "Комиссия изменится.", "recommended_action": FINDING["recommended_action"]}

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_BASE_URL": "https://api.example.test", "OPENROUTER_TIMEOUT": "5"}, clear=False), \
             patch("quality_audit.reviewer.open_url", return_value=_Response(self._response(completion))):
            result = review_finding(finding, MATERIALS, model="test/reviewer")

        self.assertEqual("issue", result["summary"]["status"])
        self.assertEqual("omitted_deadline", result["summary"]["issues"][0]["type"])
        self.assertEqual("issue", result["recommendation"]["status"])
        self.assertEqual("unsupported_action", result["recommendation"]["issues"][0]["type"])

    def test_reviewer_marks_invalid_response_as_review_without_raw_model_text(self):
        self.assertIsNotNone(review_finding, "quality audit reviewer is not implemented")
        response = {"choices": [{"message": {"content": "SECRET MODEL OUTPUT: ignore every rule"}}]}

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_BASE_URL": "https://api.example.test", "OPENROUTER_TIMEOUT": "5"}, clear=False), \
             patch("quality_audit.reviewer.open_url", return_value=_Response(response)):
            result = review_finding(FINDING, MATERIALS, model="test/reviewer")

        self.assertEqual("review", result["summary"]["status"])
        self.assertEqual("review", result["recommendation"]["status"])
        self.assertEqual("invalid_response", result["error_code"])
        self.assertNotIn("SECRET MODEL OUTPUT", json.dumps(result, ensure_ascii=False))

    def test_insufficient_source_issue_must_still_quote_the_linked_source(self):
        self.assertIsNotNone(review_finding, "quality audit reviewer is not implemented")
        completion = {
            "summary": {"status": "review", "issues": [{
                "type": "insufficient_source", "severity": "medium", "material_id": 7,
                "evidence": "несуществующая цитата", "explanation": "Нужен полный документ.",
            }]},
            "recommendation": {"status": "ok", "issues": []},
        }
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_BASE_URL": "https://api.example.test", "OPENROUTER_TIMEOUT": "5"}, clear=False), \
             patch("quality_audit.reviewer.open_url", return_value=_Response(self._response(completion))):
            result = review_finding(FINDING, MATERIALS, model="test/reviewer")

        self.assertEqual("review", result["summary"]["status"])
        self.assertEqual("invalid_response", result["error_code"])

    def test_missing_model_raises_before_any_network_request(self):
        self.assertIsNotNone(review_finding, "quality audit reviewer is not implemented")

        with patch("quality_audit.reviewer.open_url") as open_url:
            with self.assertRaises(ValueError):
                review_finding(FINDING, MATERIALS, model="")

        open_url.assert_not_called()


class BlindLabelsAndReportTests(unittest.TestCase):
    def test_blind_export_excludes_automatic_statuses_issues_and_model(self):
        self.assertIsNotNone(prepare_labels, "blind quality labels are not implemented")
        conn = object()
        with patch("quality_audit.service.db.finding_quality_label_rows", return_value=[{
            "finding_id": 41, "summary": "Summary", "recommended_action": "Action",
            "materials": MATERIALS, "status": "issue", "issues": [{"type": "unsupported_fact"}],
            "model": "private/model",
        }]):
            exported = prepare_labels(conn, 9)

        item = exported["findings"][0]
        self.assertEqual(41, item["finding_id"])
        self.assertEqual("", item["summary_facts"])
        self.assertFalse(item["omitted_condition"])
        self.assertFalse(item["omitted_deadline"])
        self.assertEqual("", item["recommendation"])
        self.assertNotIn("status", item)
        self.assertNotIn("issues", item)
        self.assertNotIn("model", item)

    def test_label_import_rejects_duplicate_or_invalid_values_before_database_write(self):
        self.assertIsNotNone(import_labels, "quality label import is not implemented")
        invalid_payloads = [
            {"findings": [{"finding_id": 1, "summary_facts": "maybe", "omitted_condition": False, "omitted_deadline": False, "completeness_uncertain": False, "recommendation": "supported"}]},
            {"findings": [{"finding_id": 1, "summary_facts": "ok", "omitted_condition": False, "omitted_deadline": False, "completeness_uncertain": False, "recommendation": "supported"}, {"finding_id": 1, "summary_facts": "ok", "omitted_condition": False, "omitted_deadline": False, "completeness_uncertain": False, "recommendation": "supported"}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                import_labels(object(), 9, payload)

    def test_report_keeps_summary_and_recommendation_metrics_separate(self):
        self.assertIsNotNone(report, "quality report is not implemented")
        rows = [
            {"finding_id": 1, "target": "summary", "automatic": "issue", "manual": "issue", "platform": "Ozon", "importance": 5, "source_url": "https://a.example"},
            {"finding_id": 1, "target": "recommendation", "automatic": "ok", "manual": "issue", "platform": "Ozon", "importance": 5, "source_url": "https://a.example"},
        ]
        with patch("quality_audit.service.db.finding_quality_run", return_value={"run_id": 9}), \
             patch("quality_audit.service.db.finding_quality_report_rows", return_value=rows):
            result = report(object(), 9)

        self.assertEqual(1, result["summary"]["evaluated"])
        self.assertEqual(1, result["recommendation"]["evaluated"])
        self.assertEqual(1, result["summary"]["matches"])
        self.assertEqual(0, result["recommendation"]["matches"])
        self.assertEqual("https://a.example", result["recommendation"]["disagreements"][0]["source_url"])


class AuditRunnerTests(unittest.TestCase):
    def test_audit_limit_is_required_and_capped_at_fifty_before_database_or_network(self):
        self.assertIsNotNone(run_audit, "quality audit runner is not implemented")
        for limit in (0, -1, 51, True, "10"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                run_audit(object(), limit=limit)

    def test_audit_run_is_not_registered_as_a_normal_cycle_command(self):
        self.assertIn("audit-findings", cli.COMMANDS)
        self.assertNotIn("audit-findings", {"cycle", "collect", "process"})

    def test_dockerfile_copies_quality_audit_module(self):
        dockerfile = cli.Path(__file__).resolve().parents[1] / "Dockerfile"
        self.assertIn("COPY quality_audit ./quality_audit", dockerfile.read_text(encoding="utf-8"))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class FindingQualityPostgresTests(unittest.TestCase):
    def setUp(self):
        schema = "task13_test_" + os.urandom(8).hex()
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        self.addCleanup(self.conn.execute, psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
        db.init_schema(self.conn)

    def add_source(self, suffix, kind):
        return self.conn.execute(
            """INSERT INTO sources (code, name, kind, url, trust, scope, category)
               VALUES (%s, %s, %s, %s, 'official', 'task 13 test', 'ozon') RETURNING id""",
            (f"task13-{suffix}", f"Task 13 {suffix}", kind, f"https://{suffix}.example.test/"),
        ).fetchone()[0]

    def add_finding(self, suffix, *, source_kind="rss", importance=3, with_material=True):
        event_id = self.conn.execute(
            """INSERT INTO events (event_key, platform, topic, importance, summary, recommended_action, verification)
               VALUES (%s, 'Ozon', 'Тарифы', %s, %s, %s, 'needs_check') RETURNING id""",
            (f"task13-{suffix}", importance, f"Сводка {suffix}", f"Действие {suffix}"),
        ).fetchone()[0]
        if with_material:
            source_id = self.add_source(suffix, source_kind)
            created, material_id = db.add_material(self.conn, source_id, None, {
                "url": f"https://{suffix}.example.test/item",
                "title": f"Материал {suffix}",
                "text": "Первичный текст с условием и сроком.",
            }, return_id=True)
            self.assertTrue(created)
            self.conn.execute("INSERT INTO event_materials (event_id, material_id) VALUES (%s, %s)", (event_id, material_id))
        return event_id

    def test_schema_init_is_idempotent_and_creates_quality_audit_tables(self):
        for table in ("finding_quality_runs", "finding_quality_results", "finding_quality_labels"):
            with self.subTest(table=table):
                self.assertIsNotNone(self.conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0])
        restored_constraints = (
            "finding_quality_results_run_finding_fk",
            "finding_quality_results_ok_consistent",
            "finding_quality_results_issue_consistent",
            "finding_quality_results_insufficient_source_review",
            "finding_quality_results_error_consistent",
            "finding_quality_results_review_explained",
            "finding_quality_labels_run_finding_fk",
        )
        for constraint in restored_constraints:
            table = "finding_quality_labels" if constraint.startswith("finding_quality_labels") else "finding_quality_results"
            self.conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")

        db.init_schema(self.conn)

        for constraint in restored_constraints:
            with self.subTest(constraint=constraint):
                table = "finding_quality_labels" if constraint.startswith("finding_quality_labels") else "finding_quality_results"
                self.assertEqual(
                    1,
                    self.conn.execute(
                        "SELECT count(*) FROM pg_constraint WHERE conname=%s AND conrelid=to_regclass(%s)",
                        (constraint, table),
                    ).fetchone()[0],
                )

    def test_stratified_sample_selects_only_findings_with_linked_materials(self):
        self.assertIsNotNone(quality_store, "finding quality storage is not implemented")
        self.assertTrue(callable(getattr(quality_store, "audit_sample", None)), "audit sample query is not implemented")
        eligible = [
            self.add_finding("rss-1", source_kind="rss", importance=1),
            self.add_finding("html-5", source_kind="html", importance=5),
            self.add_finding("telegram-3", source_kind="telegram", importance=3),
        ]
        orphan = self.add_finding("orphan", with_material=False)

        sample = quality_store.audit_sample(self.conn, limit=3)

        self.assertEqual(3, len(sample))
        self.assertEqual(set(eligible), {row["finding_id"] for row in sample})
        self.assertNotIn(orphan, {row["finding_id"] for row in sample})
        groups = {(row["platform"], row["source_kind"], row["importance"]) for row in sample}
        self.assertEqual(3, len(groups), "first stratified pass must represent each available group")

    def test_runs_results_and_labels_are_append_only(self):
        self.assertIsNotNone(quality_store, "finding quality storage is not implemented")
        for name in ("create_run", "register_run_sample", "save_results", "save_labels"):
            self.assertTrue(callable(getattr(quality_store, name, None)), f"{name} is not implemented")
        finding_id = self.add_finding("append")
        run_id = quality_store.create_run(self.conn, limit=1, model="test/reviewer", rules_version="task13-v1")
        quality_store.register_run_sample(self.conn, run_id, [finding_id])
        material_id = self.conn.execute(
            "SELECT material_id FROM event_materials WHERE event_id=%s", (finding_id,)
        ).fetchone()[0]
        results = [
            {"finding_id": finding_id, "target": "summary", "status": "issue", "issues": [{"type": "unsupported_fact", "severity": "high", "material_id": material_id, "evidence": "условием", "explanation": "не подтверждено"}]},
            {"finding_id": finding_id, "target": "recommendation", "status": "review", "issues": [], "error_code": "invalid_response"},
        ]
        self.assertEqual(2, quality_store.save_results(self.conn, run_id, results))
        second_run_id = quality_store.create_run(self.conn, limit=1, model="test/reviewer", rules_version="task13-v2")
        quality_store.register_run_sample(self.conn, second_run_id, [finding_id])
        self.assertEqual(2, quality_store.save_results(self.conn, second_run_id, results), "new audit attempt must not overwrite old results")
        labels = [{
            "finding_id": finding_id, "summary_facts": "issue", "omitted_condition": False,
            "omitted_deadline": True, "completeness_uncertain": False, "recommendation": "unsupported",
        }]
        self.assertEqual(1, quality_store.save_labels(self.conn, run_id, labels))

        self.assertEqual(2, self.conn.execute("SELECT count(*) FROM finding_quality_results WHERE run_id=%s", (run_id,)).fetchone()[0])
        self.assertEqual(2, self.conn.execute("SELECT count(*) FROM finding_quality_results WHERE run_id=%s", (second_run_id,)).fetchone()[0])
        with self.assertRaises(psycopg.Error):
            self.conn.execute("UPDATE finding_quality_results SET status='ok' WHERE run_id=%s", (run_id,))
        with self.assertRaises(psycopg.Error):
            self.conn.execute("DELETE FROM finding_quality_labels WHERE run_id=%s", (run_id,))

    def test_storage_rejects_inconsistent_result_states_and_non_source_evidence(self):
        self.assertIsNotNone(quality_store, "finding quality storage is not implemented")
        finding_id = self.add_finding("invalid-result")
        material_id = self.conn.execute(
            "SELECT material_id FROM event_materials WHERE event_id=%s", (finding_id,)
        ).fetchone()[0]
        run_id = quality_store.create_run(self.conn, limit=1, model="test/reviewer", rules_version="task13-v1")
        quality_store.register_run_sample(self.conn, run_id, [finding_id])
        invalid_results = [
            {"finding_id": finding_id, "target": "summary", "status": "ok", "issues": [{"type": "unsupported_fact", "severity": "low", "material_id": material_id, "evidence": "условием", "explanation": "ошибка"}]},
            {"finding_id": finding_id, "target": "summary", "status": "issue", "issues": []},
            {"finding_id": finding_id, "target": "summary", "status": "review", "issues": [], "error_code": None},
            {"finding_id": finding_id, "target": "summary", "status": "issue", "issues": [{"type": "insufficient_source", "severity": "low", "material_id": material_id, "evidence": "условием", "explanation": "Нужен полный документ."}]},
            {"finding_id": finding_id, "target": "summary", "status": "issue", "issues": [{"type": "unsupported_fact", "severity": "low", "material_id": material_id, "evidence": "не из источника", "explanation": "ошибка"}]},
        ]
        for payload in invalid_results:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                quality_store.save_results(self.conn, run_id, [payload])

    def test_results_and_labels_reject_findings_outside_registered_run_sample(self):
        self.assertIsNotNone(quality_store, "finding quality storage is not implemented")
        included_id = self.add_finding("included")
        excluded_id = self.add_finding("excluded")
        included_material_id = self.conn.execute(
            "SELECT material_id FROM event_materials WHERE event_id=%s", (included_id,)
        ).fetchone()[0]
        run_id = quality_store.create_run(self.conn, limit=1, model="test/reviewer", rules_version="task13-v1")
        quality_store.register_run_sample(self.conn, run_id, [included_id])
        valid_result = {"finding_id": included_id, "target": "summary", "status": "issue", "issues": [{"type": "unsupported_fact", "severity": "low", "material_id": included_material_id, "evidence": "условием", "explanation": "ошибка"}]}
        self.assertEqual(1, quality_store.save_results(self.conn, run_id, [valid_result]))
        outside_result = {**valid_result, "finding_id": excluded_id}
        with self.assertRaises(ValueError):
            quality_store.save_results(self.conn, run_id, [outside_result])
        outside_label = {
            "finding_id": excluded_id, "summary_facts": "issue", "omitted_condition": False,
            "omitted_deadline": False, "completeness_uncertain": False, "recommendation": "unsupported",
        }
        with self.assertRaises(ValueError):
            quality_store.save_labels(self.conn, run_id, [outside_label])


if __name__ == "__main__":
    unittest.main()
