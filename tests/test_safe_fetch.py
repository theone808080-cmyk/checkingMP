import unittest
from unittest.mock import patch

from collector.safe_fetch import fetch_public
from source_discovery.probe import probe_source


def address(host, ip):
    family = 2 if ":" not in ip else 23
    sockaddr = (ip, 443) if family == 2 else (ip, 443, 0, 0)
    return (family, 1, 6, "", sockaddr)


class FakeResponse:
    def __init__(self, status=200, body=b"ok", location=None):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self.body = body

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def close(self):
        pass


class SafeFetchTests(unittest.TestCase):
    def test_rejects_non_https_credentials_and_nonstandard_ports_before_request(self):
        opener = unittest.mock.Mock(return_value=FakeResponse())
        for url in (
            "http://public.example/", "https://user:pass@public.example/",
            "https://public.example:8443/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_public(url, opener=opener)
        opener.assert_not_called()

    @patch("collector.safe_fetch.socket.getaddrinfo")
    def test_rejects_private_or_mixed_dns_addresses_before_request(self, resolve):
        opener = unittest.mock.Mock(return_value=FakeResponse())
        for results in (
            [address("private.example", "127.0.0.1")],
            [address("mixed.example", "8.8.8.8"), address("mixed.example", "10.0.0.1")],
        ):
            resolve.return_value = results
            with self.assertRaises(ValueError):
                fetch_public("https://public.example/", opener=opener)
        opener.assert_not_called()

    @patch("collector.safe_fetch.socket.getaddrinfo")
    def test_pins_request_to_public_address_and_bounds_response(self, resolve):
        resolve.return_value = [address("public.example", "8.8.8.8")]
        seen = []

        def opener(url, ip, timeout):
            seen.append((url, ip, timeout))
            return FakeResponse(body=b"hello")

        self.assertEqual(fetch_public("https://public.example/news", timeout=2, max_bytes=5, opener=opener), b"hello")
        self.assertEqual(seen, [("https://public.example/news", "8.8.8.8", 2)])
        with self.assertRaises(ValueError):
            fetch_public("https://public.example/news", max_bytes=4, opener=opener)

    @patch("collector.safe_fetch.socket.getaddrinfo")
    def test_checks_redirect_target_and_refuses_private_redirect(self, resolve):
        resolve.side_effect = [
            [address("public.example", "8.8.8.8")],
            [address("internal.example", "192.168.1.8")],
        ]
        calls = []

        def opener(url, ip, timeout):
            calls.append((url, ip))
            return FakeResponse(status=302, location="https://internal.example/private")

        with self.assertRaises(ValueError):
            fetch_public("https://public.example/start", opener=opener)
        self.assertEqual(calls, [("https://public.example/start", "8.8.8.8")])


class SourceProbeTests(unittest.TestCase):
    @patch("source_discovery.probe.fetch_public")
    def test_accepts_direct_rss_feed(self, fetch):
        fetch.return_value = b"<rss><channel><item><title>Update</title><link>https://publisher.example/news</link></item></channel></rss>"

        result = probe_source("https://publisher.example/feed.xml")

        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "rss")
        self.assertEqual(result.collection_url, "https://publisher.example/feed.xml")

    @patch("source_discovery.probe.fetch_public")
    def test_discovers_and_validates_rss_link_from_html(self, fetch):
        fetch.side_effect = [
            b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>',
            b"<rss><channel><item><title>Update</title><link>https://publisher.example/news</link></item></channel></rss>",
        ]

        result = probe_source("https://publisher.example/newsroom")

        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "rss")
        self.assertEqual(result.collection_url, "https://publisher.example/feed.xml")

    @patch("source_discovery.probe.fetch_public")
    def test_plain_html_requires_explicit_owner_selection(self, fetch):
        fetch.return_value = b"<html><body><h1>Seller updates</h1></body></html>"

        result = probe_source("https://publisher.example/updates")

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_supported_feed")
        self.assertTrue(probe_source("https://publisher.example/updates", kind="html").ok)

    @patch("source_discovery.probe.fetch_public")
    def test_rejects_article_url_as_collection_source_and_accepts_telegram_preview(self, fetch):
        fetch.side_effect = [
            b"<html><body><article><p>A single article.</p></article></body></html>",
            b'<div class="tgme_widget_message" data-post="demo/123"></div>',
        ]

        article = probe_source("https://publisher.example/news/single-article")
        telegram = probe_source("https://t.me/s/demo")

        self.assertFalse(article.ok)
        self.assertTrue(telegram.ok)
        self.assertEqual(telegram.kind, "telegram")

    @patch("source_discovery.probe.fetch_public", return_value=b"<html><article>Single update</article></html>")
    def test_explicit_html_selection_still_rejects_single_article(self, fetch):
        result = probe_source("https://publisher.example/news/single-article", kind="html")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "article_url_not_source")


if __name__ == "__main__":
    unittest.main()
