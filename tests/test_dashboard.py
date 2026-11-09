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


if __name__ == "__main__":
    unittest.main()
