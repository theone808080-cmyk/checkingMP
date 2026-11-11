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
                if "SELECT e.id, e.platform" in query:
                    self.event_query = query
                    self.event_params = params
                    return Rows([(42, "Ozon", "rules", 5, "Changed", "Review", "needs_check", "https://example.org/source")])
                if "SELECT DISTINCT platform" in query:
                    return Rows([("Ozon",)])
                if "SELECT count(*) FROM sources WHERE" in query:
                    return Rows([(0,)])
                return Rows([])

        conn = Connection()
        with patch.object(web.db, "health", return_value={"sources": 1, "materials": 1, "events": 1, "pending": 0}), \
             patch.object(web.db, "latest_backup_status", return_value={"last_status": "success", "last_success_size_bytes": 2048}):
            body = web.render(conn, {"importance": "5"})
        self.assertIn('name="importance"', body)
        for level in range(1, 6):
            self.assertIn(f'value="{level}"', body)
        self.assertIn('importance-5', body)
        self.assertIn('Важность 5', body)
        self.assertIn('https://example.org/source', body)
        self.assertIn('/api/findings/42/trace', body)
        self.assertIn("успешных копий пока нет", body)
        self.assertIn("проверка восстановления ещё не выполнялась", body)
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

    def test_source_review_shows_access_requirements_only_in_protected_queue(self):
        candidate = {
            "id": 8, "publisher_domain": "publisher.example", "name": "Publisher", "collection_url": "https://publisher.example/feed",
            "kind": "rss", "category": "ozon", "trust": "official", "scope": "marketplaces", "status": "pending",
            "probe_status": "passed", "probe_error": None, "example_urls": [],
            "access_public": True, "access_requires_credentials": False, "access_rate_limit": "60/hour",
            "access_cost": "unknown", "access_checked_at": None, "access_check_result": "passed",
        }

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Rows([None])

        with patch.object(web.db, "list_source_candidates", return_value=[candidate]), \
             patch.object(web.db, "exa_pause_until", return_value=None):
            body = web.render_source_review(Connection())

        for label in (
            "\u041f\u0443\u0431\u043b\u0438\u0447\u043d\u044b\u0439 \u0434\u043e\u0441\u0442\u0443\u043f",
            "\u0423\u0447\u0451\u0442\u043d\u044b\u0435 \u0434\u0430\u043d\u043d\u044b\u0435",
            "\u041e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0435 \u0447\u0430\u0441\u0442\u043e\u0442\u044b",
            "\u0421\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c",
            "\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u0430\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430",
        ):
            self.assertIn(label, body)
        self.assertIn("unknown", body)


if __name__ == "__main__":
    unittest.main()
