from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "docs" / "data-flow.md"
README = ROOT / "README.md"


class DataFlowDocumentTests(unittest.TestCase):
    def document(self) -> str:
        if not FLOW.is_file():
            self.fail("data-flow map must be stored as Markdown")
        return FLOW.read_text(encoding="utf-8")

    def test_mermaid_map_is_editable_and_linked_from_readme(self):
        document = self.document()
        readme = README.read_text(encoding="utf-8")

        self.assertRegex(document, r"```mermaid\s+flowchart\s+(?:TD|LR|TB)")
        self.assertIn("docs/data-flow.md", readme)

    def test_map_explains_actual_flow_failures_review_and_statuses(self):
        document = self.document()
        required_terms = (
            "Источник", "collector", "materials", "Перевод", "relevance_assessments",
            "ИИ-анализ", "events", "Лента", "notifier", "watchdog", "cycles",
            "Владелец", "Ручная проверка", "Ошибка",
        )
        for term in required_terms:
            with self.subTest(term=term):
                self.assertIn(term, document)
        for status in ("Реализовано", "В `main`", "Запланировано"):
            with self.subTest(status=status):
                self.assertIn(status, document)

    def test_map_has_no_secret_or_private_infrastructure_templates(self):
        document = self.document()
        forbidden = (
            r"(?i)api[_-]?key", r"(?i)password", r"(?i)token\s*=",
            r"postgres(?:ql)?://[^\s<]+@", r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.",
        )
        for pattern in forbidden:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, document))

    def test_relevance_is_shadow_only_and_ai_handles_material_translation(self):
        document = self.document()

        self.assertIn("теневом режиме", document)
        self.assertIn("не фильтрует", document)
        self.assertRegex(
            document,
            r"material\s+-->\|[^\n]*\|\s*analysis",
            "each material must reach AI analysis without relevance routing",
        )
        self.assertRegex(
            document,
            r"analysis\s+-->\|[^\n]*перевод[^\n]*\|\s*translation",
            "AI analysis must return the translation alongside its result",
        )
        self.assertRegex(
            document,
            r"translation\s+-->\|[^\n]*\|\s*material",
            "the returned translation must be saved next to its original material",
        )

    def test_cycle_starts_shadow_assessment_before_ai_without_pre_ai_translation(self):
        document = self.document()

        self.assertRegex(
            document,
            r"material\s+-->\|[^\n]*\|\s*relevance",
            "materials must be assessed by the shadow relevance stage",
        )
        self.assertRegex(
            document,
            r"cycle\s+-->\|[^\n]*\|\s*relevance",
            "the processing cycle must start the relevance assessment",
        )
        self.assertNotRegex(
            document,
            r"cycle\s+-->\|[^\n]*\|\s*translation",
            "translation is returned by AI processing rather than started before it",
        )
        self.assertNotRegex(
            document,
            r"material\s+-->\|[^\n]*\|\s*translation",
            "materials must not be routed through a pre-AI translation stage",
        )


if __name__ == "__main__":
    unittest.main()
