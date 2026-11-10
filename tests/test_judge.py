import json
import unittest
import os
import uuid

import psycopg
import db
import main as cli

try:
    from judge.rules import classify
except ModuleNotFoundError:
    classify = None

try:
    from judge.main import assess_pending
except ModuleNotFoundError:
    assess_pending = None

try:
    from judge.review import compute_metrics, make_blind_export, validate_labels
except ModuleNotFoundError:
    compute_metrics = make_blind_export = validate_labels = None


class RuleClassifierTests(unittest.TestCase):
    def test_practical_marketplace_change_is_classified_with_rule_score(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({
            "title": "Ozon поднял комиссию для продавцов с 1 декабря",
            "text": "Новые тарифы начнут действовать с декабря.",
        })

        self.assertEqual("practical_change", result["classification"])
        self.assertIsInstance(result["score"], int)
        self.assertGreaterEqual(result["score"], 70)
        self.assertLessEqual(result["score"], 100)
        self.assertIn("platform:ozon", result["rule_codes"])
        self.assertIn("topic:tariffs", result["rule_codes"])
        self.assertIn("signal:change", result["rule_codes"])
        self.assertNotIn("probability", result)

    def test_seller_action_without_announced_change_is_classified_separately(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({
            "title": "Раздел тарифов доступен в кабинете продавца Ozon",
            "text": "Проверьте условия своего аккаунта.",
        })

        self.assertEqual("seller_action_or_risk", result["classification"])
        self.assertIn("platform:ozon", result["rule_codes"])
        self.assertIn("signal:seller", result["rule_codes"])

    def test_general_ecommerce_context_is_not_mistaken_for_actionable_news(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({"title": "Рынок электронной торговли растёт", "text": ""})

        self.assertEqual("industry_context", result["classification"])

    def test_explicit_unrelated_topic_is_classified_as_irrelevant(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({"title": "Футбольный клуб объявил расписание матчей", "text": None})

        self.assertEqual("irrelevant", result["classification"])

    def test_no_clear_match_is_uncertain(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({"title": "Интервью с руководителем компании о планах на год"})

        self.assertEqual("uncertain", result["classification"])
        self.assertEqual(0, result["score"])

    def test_conflicting_irrelevant_and_marketplace_signals_are_uncertain(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({
            "title": "Ozon поднял комиссию для продавцов; футбольный клуб сыграл матч",
        })

        self.assertEqual("uncertain", result["classification"])
        self.assertIn("topic:irrelevant", result["rule_codes"])

    def test_empty_material_is_safe_and_has_no_matched_rules(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        result = classify({"title": None, "text": ""})

        self.assertEqual("uncertain", result["classification"])
        self.assertEqual([], result["rule_codes"])

    def test_short_cyrillic_alias_requires_word_boundaries(self):
        self.assertIsNotNone(classify, "local relevance classifier is not implemented")
        embedded = classify({"title": "Терминовбвнутри произвольного слова"})
        standalone = classify({"title": "ВБ поднял комиссию для продавцов"})

        self.assertNotIn("platform:wildberries", embedded["rule_codes"])
        self.assertIn("platform:wildberries", standalone["rule_codes"])


class AssessmentRunnerTests(unittest.TestCase):
    def test_assess_pending_persists_only_materials_returned_as_unjudged(self):
        self.assertIsNotNone(assess_pending, "shadow assessment runner is not implemented")
        material = {"material_id": 17, "title": "Ozon обновил тарифы", "text": ""}
        result = {
            "classification": "practical_change",
            "score": 80,
            "rule_codes": ["platform:ozon", "topic:tariffs", "signal:change"],
            "platforms": ["Ozon"],
            "rules_version": "test-version",
            "explanation": "Совпали признаки: Ozon, tariffs, сигнал изменения.",
        }
        from unittest.mock import patch

        with patch("judge.main.db.pending_relevance_materials", return_value=[material]), \
             patch("judge.main.classify", return_value=result) as classify_mock, \
             patch("judge.main.db.save_relevance_assessments", return_value=1) as save_mock:
            count = assess_pending(object())

        self.assertEqual(1, count)
        classify_mock.assert_called_once_with(material)
        saved = save_mock.call_args.args[1]
        self.assertEqual(17, saved[0]["material_id"])
        self.assertEqual(result["classification"], saved[0]["classification"])

    def test_no_pending_materials_causes_no_classification_or_write(self):
        self.assertIsNotNone(assess_pending, "shadow assessment runner is not implemented")
        from unittest.mock import patch

        with patch("judge.main.db.pending_relevance_materials", return_value=[]), \
             patch("judge.main.classify") as classify_mock, \
             patch("judge.main.db.save_relevance_assessments") as save_mock:
            self.assertEqual(0, assess_pending(object()))

        classify_mock.assert_not_called()
        save_mock.assert_not_called()


class StorageValidationTests(unittest.TestCase):
    def test_invalid_assessments_are_rejected_before_database_access(self):
        invalid = [
            {"material_id": True, "rules_version": "1", "classification": "uncertain", "score": 0, "rule_codes": [], "platforms": [], "explanation": "x"},
            {"material_id": 1, "rules_version": "1", "classification": "uncertain", "score": 101, "rule_codes": [], "platforms": [], "explanation": "x"},
            {"material_id": 1, "rules_version": "1", "classification": "unknown", "score": 0, "rule_codes": [], "platforms": [], "explanation": "x"},
        ]
        for assessment in invalid:
            with self.subTest(assessment=assessment), self.assertRaises(ValueError):
                db.save_relevance_assessments(None, [assessment])

    def test_empty_assessment_and_label_batches_do_not_need_database(self):
        self.assertEqual(0, db.save_relevance_assessments(None, []))
        self.assertEqual(0, db.save_relevance_labels(None, [], "1"))


class BlindLabelTests(unittest.TestCase):
    def test_label_import_accepts_all_classes_and_normalizes_to_ids(self):
        self.assertIsNotNone(validate_labels, "blind label validation is not implemented")
        self.assertEqual(
            [
                {"material_id": 1, "label": "practical_change"},
                {"material_id": 2, "label": "seller_action_or_risk"},
                {"material_id": 3, "label": "industry_context"},
                {"material_id": 4, "label": "irrelevant"},
                {"material_id": 5, "label": "uncertain"},
            ],
            validate_labels({"materials": [
                {"material_id": 1, "label": "practical_change"},
                {"material_id": 2, "label": "seller_action_or_risk"},
                {"material_id": 3, "label": "industry_context"},
                {"material_id": 4, "label": "irrelevant"},
                {"material_id": 5, "label": "uncertain"},
            ]}),
        )

    def test_label_import_rejects_invalid_or_duplicate_ids_before_database(self):
        self.assertIsNotNone(validate_labels, "blind label validation is not implemented")
        invalid = [
            {"materials": [{"material_id": 1, "label": "maybe"}]},
            {"materials": [{"material_id": 1}]},
            {"materials": [{"material_id": True, "label": "uncertain"}]},
            {"materials": [{"material_id": 1, "label": "uncertain"}, {"material_id": 1, "label": "irrelevant"}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_labels(payload)

    def test_blind_export_removes_prediction_and_rule_details(self):
        self.assertIsNotNone(make_blind_export, "blind review export is not implemented")
        payload = make_blind_export([{
            "material_id": 5,
            "title": "Public title",
            "text": "Public source text",
            "source": "Example source",
            "classification": "irrelevant",
            "score": 0,
            "rule_codes": ["topic:irrelevant"],
        }])

        self.assertEqual("", payload["materials"][0]["label"])
        self.assertEqual({"material_id", "title", "text", "source", "label"}, set(payload["materials"][0]))


class MetricsTests(unittest.TestCase):
    def test_metrics_report_confusion_per_class_and_exclude_uncertain_owner_labels(self):
        self.assertIsNotNone(compute_metrics, "relevance metrics are not implemented")
        rows = [
            {"material_id": 1, "title": "A", "predicted": "practical_change", "label": "practical_change"},
            {"material_id": 2, "title": "B", "predicted": "seller_action_or_risk", "label": "practical_change"},
            {"material_id": 3, "title": "C", "predicted": "practical_change", "label": "seller_action_or_risk"},
            {"material_id": 4, "title": "D", "predicted": "irrelevant", "label": "uncertain"},
        ]

        report = compute_metrics(rows)

        self.assertEqual(4, report["labeled"])
        self.assertEqual(3, report["evaluated"])
        self.assertEqual(1, report["excluded_uncertain"])
        self.assertEqual(1, report["confusion_matrix"]["practical_change"]["practical_change"])
        self.assertEqual(1, report["confusion_matrix"]["practical_change"]["seller_action_or_risk"])
        self.assertEqual(0.5, report["per_class"]["practical_change"]["precision"])
        self.assertEqual(0.5, report["per_class"]["practical_change"]["recall"])
        self.assertTrue(report["false_positives"])
        self.assertTrue(report["false_negatives"])
        self.assertEqual("seller_action_or_risk", report["false_positives"][0]["for_class"])
        self.assertEqual("practical_change", report["false_negatives"][0]["for_class"])


class OwnerCommandsTests(unittest.TestCase):
    def test_blind_label_and_report_commands_are_registered(self):
        self.assertIn("prepare-judge-review", cli.COMMANDS)
        self.assertIn("import-judge-labels", cli.COMMANDS)
        self.assertIn("judge-report", cli.COMMANDS)

    def test_prepare_command_writes_only_blind_material_fields_to_work(self):
        from unittest.mock import patch

        payload = {"materials": [{"material_id": 8, "title": "Title", "text": "Body", "source": "Source", "label": ""}]}
        with patch.object(cli.db, "connect") as connect, \
             patch.object(cli.db, "init_schema") as init_schema, \
             patch.object(cli, "prepare_judge_sample", return_value=payload) as prepare, \
             patch.object(cli.Path, "write_text") as write_text:
            target = cli.prepare_judge_review(25)

        conn = connect.return_value.__enter__.return_value
        init_schema.assert_called_once_with(conn)
        prepare.assert_called_once_with(conn, 25)
        self.assertEqual(cli.WORK / "judge-review.json", target)
        exported = json.loads(write_text.call_args.args[0])
        self.assertEqual(payload, exported)
        self.assertNotIn("classification", str(exported))
        self.assertNotIn("score", str(exported))

    def test_container_packages_the_local_judge_module(self):
        dockerfile = cli.Path(__file__).resolve().parents[1] / "Dockerfile"
        self.assertIn("COPY judge ./judge", dockerfile.read_text(encoding="utf-8"))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class RelevancePostgresTests(unittest.TestCase):
    def setUp(self):
        schema = "task5_test_" + uuid.uuid4().hex
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        self.addCleanup(self.conn.execute, psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
        db.init_schema(self.conn)
        db.sync_sources(self.conn)
        self.source_id = self.conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]

    def add_material(self, suffix, status="new"):
        inserted = db.add_material(self.conn, self.source_id, None, {
            "url": f"https://example.test/task5-{suffix}",
            "title": f"Ozon changed seller tariffs {suffix}",
            "text": "New tariff rules for marketplace sellers.",
        })
        self.assertTrue(inserted)
        material_id = self.conn.execute("SELECT id FROM materials WHERE url = %s", (f"https://example.test/task5-{suffix}",)).fetchone()[0]
        if status != "new":
            self.conn.execute("UPDATE materials SET status = %s WHERE id = %s", (status, material_id))
        return material_id

    def assessment(self, material_id, version="1"):
        return {
            "material_id": material_id,
            "rules_version": version,
            "classification": "practical_change",
            "score": 80,
            "rule_codes": ["platform:ozon", "topic:tariffs", "signal:change"],
            "platforms": ["Ozon"],
            "explanation": "Совпали правила по площадке, теме и изменению.",
        }

    def test_new_assessments_are_idempotent_and_old_versions_are_preserved(self):
        self.assertTrue(callable(getattr(db, "pending_relevance_materials", None)), "pending relevance query is not implemented")
        self.assertTrue(callable(getattr(db, "save_relevance_assessments", None)), "assessment persistence is not implemented")
        material_id = self.add_material("versioned")

        first = db.pending_relevance_materials(self.conn, "1")
        self.assertEqual([material_id], [row["material_id"] for row in first])
        self.assertEqual(1, db.save_relevance_assessments(self.conn, [self.assessment(material_id)]))
        self.assertEqual([], db.pending_relevance_materials(self.conn, "1"))
        self.assertEqual(0, db.save_relevance_assessments(self.conn, [self.assessment(material_id)]))
        db.save_relevance_assessments(self.conn, [self.assessment(material_id, "2")])

        rows = self.conn.execute("SELECT rules_version FROM relevance_assessments WHERE material_id = %s ORDER BY rules_version", (material_id,)).fetchall()
        self.assertEqual([("1",), ("2",)], rows)
        self.assertEqual("new", self.conn.execute("SELECT status FROM materials WHERE id = %s", (material_id,)).fetchone()[0])
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM events").fetchone()[0])

    def test_blind_sample_and_owner_labels_are_separate_from_assessments(self):
        self.assertTrue(callable(getattr(db, "relevance_review_sample", None)), "blind sample query is not implemented")
        self.assertTrue(callable(getattr(db, "save_relevance_labels", None)), "label storage is not implemented")
        self.assertTrue(callable(getattr(db, "relevance_evaluation_rows", None)), "evaluation query is not implemented")
        material_id = self.add_material("labeled", "done")
        db.save_relevance_assessments(self.conn, [self.assessment(material_id)])

        sample = db.relevance_review_sample(self.conn, 10)
        self.assertEqual([material_id], [row["material_id"] for row in sample])
        self.assertNotIn("classification", sample[0])
        self.assertNotIn("score", sample[0])
        self.assertNotIn("rule_codes", sample[0])
        db.save_relevance_labels(self.conn, [{"material_id": material_id, "label": "practical_change"}], "1")
        self.assertEqual([], db.relevance_review_sample(self.conn, 10))
        with self.assertRaises(ValueError):
            db.save_relevance_labels(self.conn, [{"material_id": material_id + 100, "label": "irrelevant"}], "1")

        evaluated = db.relevance_evaluation_rows(self.conn, "1")
        self.assertEqual([{
            "material_id": material_id,
            "title": f"Ozon changed seller tariffs labeled",
            "predicted": "practical_change",
            "label": "practical_change",
        }], evaluated)


if __name__ == "__main__":
    unittest.main()
