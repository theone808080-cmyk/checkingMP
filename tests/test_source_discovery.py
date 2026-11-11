import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import json
import io
import urllib.error

from source_discovery import SearchHit
from source_discovery.grouping import group_candidates
from source_discovery.providers import ProviderError, parse_google_news_rss, search_all, search_exa


class GoogleNewsRssTests(unittest.TestCase):
    def test_parses_article_and_publisher_separately(self):
        payload = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel><item>
          <title>New marketplace rules</title>
          <link>https://news.google.com/rss/articles/example</link>
          <pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate>
          <source url="https://seller.example.org/news">Seller News</source>
        </item></channel></rss>'''

        hits = parse_google_news_rss(payload)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "New marketplace rules")
        self.assertEqual(hits[0].article_url, "https://news.google.com/rss/articles/example")
        self.assertEqual(hits[0].publisher, "Seller News")
        self.assertEqual(hits[0].publisher_url, "https://seller.example.org/news")
        self.assertEqual(hits[0].published_at, datetime(2026, 11, 9, 8, tzinfo=timezone.utc))
        self.assertEqual(hits[0].provider, "google_news")

    def test_does_not_treat_aggregator_or_article_url_as_publisher(self):
        payload = b'''<rss><channel><item>
          <title>Unattributed result</title>
          <link>https://news.google.com/rss/articles/example</link>
          <source>Google News</source>
        </item></channel></rss>'''

        hits = parse_google_news_rss(payload)

        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].publisher_url)
        self.assertNotEqual(hits[0].publisher, "news.google.com")

    def test_rejects_invalid_xml_and_limits_result_count(self):
        with self.assertRaises(ValueError):
            parse_google_news_rss(b"<rss><channel>")

        items = b"".join(
            f"<item><title>Story {index}</title><link>https://news.google.com/a/{index}</link></item>".encode()
            for index in range(20)
        )
        hits = parse_google_news_rss(b"<rss><channel>" + items + b"</channel></rss>")
        self.assertLessEqual(len(hits), 100)

    def test_exa_uses_free_lite_news_search_and_ten_result_limit(self):
        response = Mock()
        response.read.return_value = json.dumps({"results": [{
            "title": "Marketplace update", "url": "https://seller.example.org/news/1",
            "publishedDate": "2026-11-09T08:00:00Z", "author": "Seller News",
        }]}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        requests = []

        def open_url(request, timeout):
            requests.append((request, timeout))
            return response

        hits = search_exa("новости маркетплейсов", "private-test-key", open_url=open_url)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].provider, "exa")
        self.assertEqual(hits[0].publisher_url, "https://seller.example.org/")
        request, _ = requests[0]
        self.assertEqual(request.full_url, "https://api.exa.ai/search")
        self.assertEqual(request.get_header("X-api-key"), "private-test-key")
        body = json.loads(request.data)
        self.assertEqual(body["type"], "lite")
        self.assertEqual(body["category"], "news")
        self.assertEqual(body["numResults"], 10)

    @patch("source_discovery.providers.search_google_news_rss")
    @patch("source_discovery.providers.search_exa")
    def test_search_all_keeps_rss_results_when_exa_fails(self, exa, google):
        google.return_value = [SearchHit(
            title="Update", article_url="https://news.google.com/a", published_at=None,
            publisher="Seller", publisher_url="https://seller.example.org/", provider="google_news",
        )]
        exa.side_effect = ProviderError("exa", "http_402")

        result = search_all(api_key="private-test-key", queries=["query"])

        self.assertEqual([hit.provider for hit in result.hits], ["google_news"])
        self.assertEqual(result.errors, {"exa": "http_402"})

    @patch("source_discovery.providers.search_google_news_rss", return_value=[])
    @patch("source_discovery.providers.search_exa")
    def test_search_all_skips_exa_without_key(self, exa, google):
        result = search_all(api_key=None, queries=["query"])
        exa.assert_not_called()
        google.assert_called_once_with("query")
        self.assertEqual(result.errors, {})

    @patch("source_discovery.providers.search_google_news_rss", return_value=[])
    @patch("source_discovery.providers.search_exa", side_effect=[ProviderError("exa", "limit_reached"), []])
    def test_search_all_stops_exa_after_limit_but_keeps_google_rss(self, exa, google):
        result = search_all(api_key="private-test-key", queries=["first", "second"])

        self.assertTrue(result.exa_limited)
        self.assertEqual(exa.call_count, 1)
        self.assertEqual(google.call_count, 2)

    def test_exa_rate_limit_is_not_mistaken_for_monthly_credit_exhaustion(self):
        error = urllib.error.HTTPError("https://api.exa.ai/search", 429, "rate limited", {},
                                       io.BytesIO(b'{"error":"Too many requests"}'))
        def raise_error(*_args, **_kwargs):
            raise error
        with self.assertRaises(ProviderError) as caught:
            search_exa("query", "private-test-key", open_url=raise_error)
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_groups_by_publisher_and_keeps_article_urls_as_examples(self):
        hits = [
            SearchHit("One", "https://publisher.example/news/1", None, "Publisher",
                      "https://publisher.example/about", "exa"),
            SearchHit("Two", "https://publisher.example/news/2", None, "Publisher",
                      "https://publisher.example/about", "google_news"),
            SearchHit("Aggregate", "https://news.google.com/a", None, "Google News",
                      "https://news.google.com/", "google_news"),
        ]

        candidates = group_candidates(hits)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].publisher_domain, "publisher.example")
        self.assertEqual(candidates[0].collection_url, "https://publisher.example/")
        self.assertEqual(candidates[0].example_urls, [
            "https://publisher.example/news/1", "https://publisher.example/news/2",
        ])


if __name__ == "__main__":
    unittest.main()
