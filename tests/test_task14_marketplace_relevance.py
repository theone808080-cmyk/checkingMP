"""Acceptance tests for marketplace seller relevance and impact."""

import os
import unittest
import uuid
from unittest.mock import patch

import psycopg

import db
from judge.main import assess_pending, prepare_review, report
from judge.review import compute_metrics, make_blind_export, validate_labels
from judge.rules import classify, rules_version
from worker import main as worker


class MarketplaceRelevanceTests(unittest.TestCase):
    def test_commission_increase_is_relevant_and_negative_for_sellers(self):
        result = classify({
            "title": "Ozon повысит комиссию для продавцов",
            "text": "С 1 декабря комиссия для продавцов Ozon увеличится с 10 до 15 процентов.",
        })

        self.assertEqual(result["relevance"], "relevant")
        self.assertIn("Ozon", result["platforms"])
        self.assertTrue(result["topic"])
        self.assertEqual(result["seller_impact"], "negative")
        self.assertTrue(result["explanation"])

    def test_seller_impact_uses_the_change_not_the_authors_emotional_tone(self):
        result = classify({
            "title": "Прекрасная новость для продавцов Ozon",
            "text": "Ozon повысит комиссию для продавцов с 10 до 15 процентов с декабря.",
        })

        self.assertEqual(result["relevance"], "relevant")
        self.assertEqual(result["seller_impact"], "negative")

    def test_commission_reduction_is_a_seller_benefit(self):
        result = classify({
            "title": "Wildberries снизит комиссию для продавцов",
            "text": "С декабря комиссия Wildberries для продавцов снизится с 15 до 10 процентов.",
        })

        self.assertEqual(result["relevance"], "relevant")
        self.assertIn("Wildberries", result["platforms"])
        self.assertEqual(result["seller_impact"], "positive")

    def test_regulatory_marking_with_explicit_marketplace_seller_link_is_relevant(self):
        result = classify({
            "title": "Честный знак: обязательная маркировка для продавцов Ozon",
            "text": "Продавцы бытовой химии на Ozon должны наносить коды маркировки с 1 декабря.",
        })

        self.assertEqual(result["relevance"], "relevant")
        self.assertIn("Ozon", result["platforms"])
        self.assertTrue(result["topic"])

    def test_generic_marketplace_and_seller_words_are_not_sufficient(self):
        for title in (
            "Интервью продавца: как он отдыхает в выходные",
            "Рынок маркетплейсов и электронной торговли растёт",
        ):
            with self.subTest(title=title):
                result = classify({"title": title, "text": ""})
                self.assertNotEqual(result["relevance"], "relevant")

    def test_unrelated_and_incomplete_materials_are_not_confident_findings(self):
        unrelated = classify({"title": "Футбольный клуб объявил расписание матчей", "text": ""})
        incomplete = classify({"title": "Ozon обновил комиссию", "text": ""})
        self.assertEqual(unrelated["relevance"], "irrelevant")
        self.assertEqual(incomplete["relevance"], "uncertain")
        self.assertEqual(incomplete["seller_impact"], "unclear")

    def test_conflicting_unrelated_and_seller_signals_are_uncertain(self):
        result = classify({
            "title": "Ozon повысил комиссию продавцам; футбольный клуб сыграл матч",
            "text": "Комиссия для продавцов Ozon вырастет с 10 до 15 процентов.",
        })
        self.assertEqual(result["relevance"], "uncertain")

    def test_local_classification_makes_no_network_calls(self):
        with patch("socket.create_connection", side_effect=AssertionError("unexpected network call")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network call")):
            result = classify({
                "title": "Ozon повысит комиссию для продавцов",
                "text": "С 1 декабря комиссия продавцов Ozon вырастет с 10 до 15 процентов.",
            })
        self.assertEqual(result["relevance"], "relevant")


class MarketplaceReviewTests(unittest.TestCase):
    def test_blind_export_never_exposes_machine_verdict(self):
        export = make_blind_export([{
            "material_id": 7, "title": "Заголовок", "text": "Текст", "source": "Источник",
            "classification": "practical_change", "relevance": "relevant", "topic": "tariffs",
            "seller_impact": "negative", "score": 95, "rule_codes": ["signal:change"],
        }])
        item = export["materials"][0]
        self.assertEqual(item["label"], "")
        self.assertNotIn("classification", item)
        self.assertNotIn("relevance", item)
        self.assertNotIn("score", item)
        self.assertNotIn("rule_codes", item)
        self.assertNotEqual(item.get("seller_impact"), "negative")

    def test_owner_can_label_relevance_platform_and_impact(self):
        labels = validate_labels({"materials": [{
            "material_id": 7, "label": "relevant", "platforms": ["Ozon"], "seller_impact": "negative",
        }]})
        self.assertEqual(labels[0]["material_id"], 7)
        self.assertEqual(labels[0]["label"], "relevant")
        self.assertEqual(labels[0]["platforms"], ["Ozon"])
        self.assertEqual(labels[0]["seller_impact"], "negative")

    def test_binary_precision_recall_f1_exclude_uncertain_owner_labels(self):
        rows = [
            {"material_id": 1, "title": "A", "predicted": "relevant", "label": "relevant"},
            {"material_id": 2, "title": "B", "predicted": "relevant", "label": "irrelevant"},
            {"material_id": 3, "title": "C", "predicted": "irrelevant", "label": "relevant"},
            {"material_id": 4, "title": "D", "predicted": "irrelevant", "label": "irrelevant"},
            {"material_id": 5, "title": "E", "predicted": "relevant", "label": "uncertain"},
            {"material_id": 6, "title": "F", "predicted": "uncertain", "label": "irrelevant"},
        ]

        binary = compute_metrics(rows)["binary"]
        self.assertEqual(binary["evaluated"], 5)
        self.assertEqual(binary["excluded_uncertain"], 1)
        self.assertEqual(binary["uncertain_predictions"], 1)
        self.assertAlmostEqual(binary["precision"], 0.5)
        self.assertAlmostEqual(binary["recall"], 0.5)
        self.assertAlmostEqual(binary["f1"], 0.5)

    def test_report_breaks_down_metrics_and_sample_sizes_by_platform_and_source_type(self):
        rows = [
            {"material_id": 1, "predicted": "relevant", "label": "relevant", "platforms": ["Ozon"], "source_type": "telegram"},
            {"material_id": 2, "predicted": "relevant", "label": "irrelevant", "platforms": ["Ozon"], "source_type": "telegram"},
            {"material_id": 3, "predicted": "irrelevant", "label": "relevant", "platforms": ["Ozon"], "source_type": "telegram"},
            {"material_id": 4, "predicted": "relevant", "label": "uncertain", "platforms": ["Ozon"], "source_type": "telegram"},
            {"material_id": 5, "predicted": "relevant", "label": "relevant", "platforms": ["Wildberries"], "source_type": "rss"},
            {"material_id": 6, "predicted": "irrelevant", "label": "irrelevant", "platforms": ["Wildberries"], "source_type": "rss"},
        ]

        result = compute_metrics(rows)
        for section, group in (("by_platform", "Ozon"), ("by_source_type", "telegram")):
            with self.subTest(section=section, group=group):
                values = result[section][group]
                self.assertEqual(values["labeled"], 4)
                self.assertEqual(values["evaluated"], 3)
                self.assertEqual(values["excluded_uncertain"], 1)
                self.assertAlmostEqual(values["precision"], 0.5)
                self.assertAlmostEqual(values["recall"], 0.5)
                self.assertAlmostEqual(values["f1"], 0.5)
        for section, group in (("by_platform", "Wildberries"), ("by_source_type", "rss")):
            with self.subTest(section=section, group=group):
                values = result[section][group]
                self.assertEqual(values["labeled"], 2)
                self.assertEqual(values["evaluated"], 2)
                self.assertAlmostEqual(values["f1"], 1.0)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class MarketplaceFilterPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "task14_test_" + uuid.uuid4().hex[:12]
        self.conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(self.schema)))
        self.conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(self.schema)))
        self.addCleanup(self.conn.close)
        self.addCleanup(self._drop_schema)
        db.init_schema(self.conn)
        db.sync_sources(self.conn)
        self.relevant = self.add_material("relevant", "Ozon повысит комиссию для продавцов",
                                          "С декабря комиссия Ozon для продавцов вырастет с 10 до 15 процентов.")
        self.irrelevant = self.add_material("irrelevant", "Футбольный клуб объявил расписание матчей", "Спортивные новости.")
        self.uncertain = self.add_material("uncertain", "Ozon обновил комиссию", "")

    def _drop_schema(self):
        if not self.conn.closed:
            self.conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(self.schema)))

    def add_material(self, slug, title, body):
        return self.conn.execute(
            """INSERT INTO materials (source_id,url,title,text,published_at,fingerprint,translation_status)
               SELECT id,%s,%s,%s,now(),%s,'legacy' FROM sources WHERE code='ozon_telegram' RETURNING id""",
            (f"https://example.test/task14-{slug}", title, body, f"task14-{slug}"),
        ).fetchone()[0]

    def infer_all_as_skipped(self, captured):
        def inference(batch, *, cycle_id=None):
            captured.extend(item["material_id"] for item in batch)
            return {"signals": [], "skipped_material_ids": [item["material_id"] for item in batch], "translations": []}
        return inference

    def test_filter_is_off_by_default_and_does_not_change_existing_batch(self):
        captured = []
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(worker, "settings", return_value=("https://example.test", "placeholder", "model", 1, 10, 100)), \
             patch.object(worker, "infer", side_effect=self.infer_all_as_skipped(captured)):
            worker.process(self.conn)
        self.assertEqual(set(captured), {self.relevant, self.irrelevant, self.uncertain})

    def test_filter_keeps_only_relevant_in_processing_and_preserves_source_feed(self):
        captured = []
        def inference(batch, *, cycle_id=None):
            captured.extend(item["material_id"] for item in batch)
            return {"signals": [{
                "material_ids": [self.relevant], "platform": "Ozon", "topic": "Комиссия",
                "importance": 4, "summary": "Комиссия для продавцов вырастет.",
                "recommended_action": "Проверить тарифы.", "verification": "needs_check",
            }], "skipped_material_ids": [], "translations": []}

        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "1"}), \
             patch.object(worker, "settings", return_value=("https://example.test", "placeholder", "model", 1, 10, 100)), \
             patch.object(worker, "infer", side_effect=inference):
            worker.process(self.conn)
            self.assertEqual(captured, [self.relevant])
            source_ids = {item["id"] for item in db.feed_materials(self.conn)["materials"]}
            self.assertEqual(source_ids, {self.relevant, self.irrelevant, self.uncertain})
            findings = db.feed_findings(self.conn)["findings"]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["importance"], 4)
            self.assertEqual(findings[0]["seller_impact"], "negative")
            sample = prepare_review(self.conn, 100)
            self.assertIn(self.uncertain, {item["material_id"] for item in sample["materials"]})
            self.assertNotIn("relevance", next(item for item in sample["materials"] if item["material_id"] == self.uncertain))

    def test_classifier_error_never_creates_unassessed_findings_or_loses_sources(self):
        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "1"}), \
             patch.object(worker, "settings", return_value=("https://example.test", "placeholder", "model", 1, 10, 100)), \
             patch.object(worker, "assess_pending", side_effect=RuntimeError("classifier unavailable")), \
             patch.object(worker, "infer") as inference:
            try:
                worker.process(self.conn)
            except RuntimeError:
                pass
            inference.assert_not_called()
            self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            self.assertEqual(db.feed_materials(self.conn)["total"], 3)

    def test_enabled_filter_applies_to_existing_findings_and_counts(self):
        event_ids = {}
        for key, material_id in (("relevant", self.relevant), ("irrelevant", self.irrelevant), ("uncertain", self.uncertain)):
            event_id = self.conn.execute(
                """INSERT INTO events(event_key,platform,topic,importance,summary,recommended_action,verification)
                   VALUES (%s,'Ozon','rules',4,%s,'Проверить','needs_check') RETURNING id""", (key, key),
            ).fetchone()[0]
            self.conn.execute("INSERT INTO event_materials(event_id,material_id) VALUES (%s,%s)", (event_id, material_id))
            event_ids[key] = event_id
        assess_pending(self.conn)

        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "1"}):
            result = db.feed_findings(self.conn)
            self.assertEqual([item["id"] for item in result["findings"]], [event_ids["relevant"]])
            self.assertEqual(db.finding_filter_counts(self.conn)["period"]["all"], 1)
            self.assertEqual(db.feed_materials(self.conn)["total"], 3)

    def test_filter_waits_for_full_current_version_backfill_of_historical_findings(self):
        event_ids = {}
        for key, material_id in (("relevant", self.relevant), ("irrelevant", self.irrelevant), ("uncertain", self.uncertain)):
            event_id = self.conn.execute(
                """INSERT INTO events(event_key,platform,topic,importance,summary,recommended_action,verification)
                   VALUES (%s,'Ozon','rules',4,%s,'Проверить','needs_check') RETURNING id""", (key, key),
            ).fetchone()[0]
            self.conn.execute("INSERT INTO event_materials(event_id,material_id) VALUES (%s,%s)", (event_id, material_id))
            self.conn.execute("UPDATE materials SET status='done' WHERE id=%s", (material_id,))
            event_ids[key] = event_id

        relevant_material = {
            "title": "Ozon повысит комиссию для продавцов",
            "text": "С декабря комиссия Ozon для продавцов вырастет с 10 до 15 процентов.",
        }
        db.save_relevance_assessments(self.conn, [{"material_id": self.relevant, **classify(relevant_material)}])

        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "1"}):
            before = db.feed_findings(self.conn)
            self.assertEqual({item["id"] for item in before["findings"]}, set(event_ids.values()))
            self.assertEqual(db.finding_filter_counts(self.conn)["period"]["all"], 3)
            assess_pending(self.conn)
            after = db.feed_findings(self.conn)
            self.assertEqual([item["id"] for item in after["findings"]], [event_ids["relevant"]])
            self.assertEqual(db.finding_filter_counts(self.conn)["period"]["all"], 1)
            self.assertEqual(db.feed_materials(self.conn)["total"], 3)

    def test_report_keeps_labeled_platform_and_source_type_from_database(self):
        assess_pending(self.conn)
        db.save_relevance_labels(self.conn, [
            {"material_id": self.relevant, "label": "relevant", "platforms": ["Ozon"], "seller_impact": "negative"},
            {"material_id": self.irrelevant, "label": "irrelevant", "platforms": [], "seller_impact": "unclear"},
            {"material_id": self.uncertain, "label": "uncertain", "platforms": ["Ozon"], "seller_impact": "unclear"},
        ], rules_version())

        result = report(self.conn)
        self.assertEqual(result["by_platform"]["Ozon"]["labeled"], 2)
        self.assertEqual(result["by_platform"]["Ozon"]["evaluated"], 1)
        self.assertEqual(result["by_platform"]["Ozon"]["excluded_uncertain"], 1)
        self.assertEqual(result["by_source_type"]["telegram"]["labeled"], 3)
        self.assertEqual(result["by_source_type"]["telegram"]["evaluated"], 2)

    def test_cursor_created_before_enabling_filter_cannot_expose_irrelevant_findings(self):
        event_ids = {}
        for key, material_id, seconds in (("relevant", self.relevant, 3),
                                          ("irrelevant", self.irrelevant, 2),
                                          ("uncertain", self.uncertain, 1)):
            event_id = self.conn.execute(
                """INSERT INTO events(event_key,platform,topic,importance,summary,recommended_action,verification,updated_at)
                   VALUES (%s,'Ozon','rules',4,%s,'Проверить','needs_check',now() + %s * interval '1 second')
                   RETURNING id""", (key, key, seconds),
            ).fetchone()[0]
            self.conn.execute("INSERT INTO event_materials(event_id,material_id) VALUES (%s,%s)", (event_id, material_id))
            event_ids[key] = event_id
        assess_pending(self.conn)

        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "0"}):
            first = db.feed_findings(self.conn, limit=1)
        self.assertIsNotNone(first["next_cursor"])

        with patch.dict(os.environ, {"RELEVANCE_FILTER_ENABLED": "1"}):
            try:
                page = db.feed_findings(self.conn, cursor=first["next_cursor"], limit=1)
            except ValueError:
                # Invalidating the old snapshot is also a safe response.
                return
        self.assertLessEqual({item["id"] for item in page["findings"]}, {event_ids["relevant"]})

    def test_migration_twice_preserves_existing_assessment_and_material(self):
        self.conn.execute("DROP TABLE relevance_assessments")
        self.conn.execute("""CREATE TABLE relevance_assessments (
            id BIGSERIAL PRIMARY KEY, material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
            rules_version TEXT NOT NULL, classification TEXT NOT NULL CHECK (classification IN
              ('practical_change','seller_action_or_risk','industry_context','irrelevant','uncertain')),
            score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100), rule_codes TEXT[] NOT NULL DEFAULT '{}',
            platforms TEXT[] NOT NULL DEFAULT '{}', explanation TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(material_id,rules_version))""")
        self.conn.execute("""INSERT INTO relevance_assessments(material_id,rules_version,classification,score,explanation)
                             VALUES (%s,'old','practical_change',80,'existing assessment')""", (self.relevant,))

        db.init_schema(self.conn)
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT classification, score, explanation FROM relevance_assessments WHERE material_id=%s AND rules_version='old'", (self.relevant,)).fetchone(),
                         ("practical_change", 80, "existing assessment"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE id=%s", (self.relevant,)).fetchone()[0], 1)

    def test_old_schema_migration_adds_idempotent_checks_for_new_fields(self):
        # Simulate pre-task-14 tables: dropping columns also removes their inline checks.
        self.conn.execute("ALTER TABLE relevance_assessments DROP COLUMN relevance")
        self.conn.execute("ALTER TABLE relevance_assessments DROP COLUMN seller_impact")
        self.conn.execute("ALTER TABLE relevance_labels DROP COLUMN seller_impact")
        self.conn.execute("ALTER TABLE events DROP COLUMN seller_impact")

        db.init_schema(self.conn)
        db.init_schema(self.conn)

        for table, column in (("relevance_assessments", "relevance"),
                              ("relevance_assessments", "seller_impact"),
                              ("relevance_labels", "seller_impact"),
                              ("events", "seller_impact")):
            with self.subTest(table=table, column=column):
                count = self.conn.execute(
                    """SELECT count(*) FROM pg_constraint
                       WHERE conrelid=%s::regclass AND contype='c' AND pg_get_constraintdef(oid) ILIKE %s""",
                    (table, f"%{column}%"),
                ).fetchone()[0]
                self.assertEqual(count, 1)

        assessment_id = self.conn.execute(
            """INSERT INTO relevance_assessments(material_id,rules_version,classification,score,explanation)
               VALUES (%s,'old','practical_change',80,'existing assessment') RETURNING id""", (self.relevant,),
        ).fetchone()[0]
        self.conn.execute("INSERT INTO relevance_labels(material_id,label) VALUES (%s,'relevant')", (self.relevant,))
        event_id = self.conn.execute(
            """INSERT INTO events(event_key,platform,topic,importance,summary,recommended_action,verification)
               VALUES ('legacy-event','Ozon','rules',4,'summary','action','needs_check') RETURNING id"""
        ).fetchone()[0]
        for query, key in (("UPDATE relevance_assessments SET relevance='invalid' WHERE id=%s", assessment_id),
                           ("UPDATE relevance_assessments SET seller_impact='invalid' WHERE id=%s", assessment_id),
                           ("UPDATE relevance_labels SET seller_impact='invalid' WHERE material_id=%s", self.relevant),
                           ("UPDATE events SET seller_impact='invalid' WHERE id=%s", event_id)):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.CheckViolation):
                self.conn.execute(query, (key,))


if __name__ == "__main__":
    unittest.main()
