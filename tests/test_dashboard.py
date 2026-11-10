import unittest
from unittest.mock import patch

from web import main as web


class Rows:
    def __init__(self, values):
        self.values = values

    def fetchall(self):
        return self.values

    def fetchone(self):
        return self.values[0]


class DashboardTests(unittest.TestCase):
    def test_dashboard_renders_numeric_importance_filters_and_five_levels(self):
        class Connection:
            def execute(self, query, params=None):
                if "SELECT e.platform" in query:
                    self.event_query = query
                    self.event_params = params
                    return Rows([("Ozon", "rules", 5, "Changed", "Review", "needs_check", "https://example.org/source")])
                if "SELECT DISTINCT platform" in query:
                    return Rows([("Ozon",)])
                if "SELECT count(*) FROM sources WHERE" in query:
                    return Rows([(0,)])
                return Rows([])

        conn = Connection()
        with patch.object(web.db, "health", return_value={"sources": 1, "materials": 1, "events": 1, "pending": 0}):
            body = web.render(conn, {"importance": "5"})
        self.assertIn('name="importance"', body)
        for level in range(1, 6):
            self.assertIn(f'value="{level}"', body)
        self.assertIn('importance-5', body)
        self.assertIn('Важность 5', body)
        self.assertIn('https://example.org/source', body)
        self.assertEqual(conn.event_params, [5])

    def test_source_review_escapes_candidate_text_and_links(self):
        from datetime import datetime, timezone

        candidate = {
            "id": 7, "publisher_domain": "publisher.example", "name": '<script>alert("x")</script>',
            "collection_url": "https://publisher.example/feed", "kind": "rss", "category": "ozon",
            "trust": "industry", "scope": "marketplaces", "status": "pending",
            "probe_status": "pending", "probe_error": None,
            "example_urls": ['https://publisher.example/news?x=" onmouseover="alert(1)'],
        }

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Rows([("finished", datetime(2026, 11, 10, tzinfo=timezone.utc),
                              datetime(2026, 11, 10, tzinfo=timezone.utc), 1,
                              '{"providers":{"google_news":"ok","exa":"not_configured"}}')])

        with patch.object(web.db, "list_source_candidates", return_value=[candidate]), \
             patch.object(web.db, "exa_pause_until", return_value=None):
            body = web.render_source_review(Connection())

        self.assertNotIn('<script>alert("x")</script>', body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&quot; onmouseover=&quot;", body)
        self.assertIn("Google News RSS: работает", body)
        self.assertIn("Exa: не настроен", body)


if __name__ == "__main__":
    unittest.main()
