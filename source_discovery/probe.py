"""Validate public collection endpoints before owner approval."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from collector.safe_fetch import fetch_public

ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    kind: str | None
    collection_url: str
    error: str | None = None


class _FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.article = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "article":
            self.article = True
            return
        if tag.lower() != "link":
            values = {key.lower(): value for key, value in attrs if key and value is not None}
            if (tag.lower() == "meta" and values.get("property", "").casefold() == "og:type"
                    and values.get("content", "").casefold() in {"article", "newsarticle"}):
                self.article = True
            return
        values = {key.lower(): value for key, value in attrs if key and value is not None}
        rel = {part.casefold() for part in values.get("rel", "").split()}
        mime = values.get("type", "").split(";", 1)[0].strip().casefold()
        if "alternate" in rel and mime in {"application/rss+xml", "application/atom+xml"}:
            href = values.get("href", "").strip()
            if href:
                self.links.append(href)


def _is_feed(payload: bytes) -> bool:
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError):
        return False
    if root.tag.lower().endswith("rss") or root.tag.lower() == "rdf:rdf":
        items = root.findall(".//item")
        return any((item.findtext("title") or "").strip() and
                   (item.findtext("link") or "").strip() for item in items) or root.tag.lower() == "rdf:rdf"
    entries = root.findall(f"{ATOM}entry")
    return bool(entries and any((entry.findtext(f"{ATOM}title") or "").strip() for entry in entries))


def _telegram_preview(url: str) -> bool:
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    return parsed.hostname == "t.me" and len(parts) == 2 and parts[0] == "s" and bool(parts[1])


def _telegram_payload_valid(payload: bytes) -> bool:
    text = payload.decode("utf-8", errors="replace")
    return 'data-post="' in text and "tgme_widget_message" in text


def probe_source(url: str, *, kind: str | None = None) -> ProbeResult:
    if kind not in (None, "rss", "html", "telegram"):
        return ProbeResult(False, None, url if isinstance(url, str) else "", "unsupported_type")
    if not isinstance(url, str) or len(url) > 2048:
        return ProbeResult(False, None, "", "invalid_url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ProbeResult(False, None, url, "unsafe_url")
    if kind == "telegram" or (kind is None and _telegram_preview(url)):
        if not _telegram_preview(url):
            return ProbeResult(False, None, url, "unsupported_telegram_url")
        try:
            payload = fetch_public(url)
        except (OSError, ValueError, TimeoutError):
            return ProbeResult(False, None, url, "probe_failed")
        if _telegram_payload_valid(payload):
            return ProbeResult(True, "telegram", url)
        return ProbeResult(False, None, url, "invalid_telegram_preview")
    try:
        payload = fetch_public(url)
    except (OSError, ValueError, TimeoutError):
        return ProbeResult(False, None, url, "probe_failed")
    if _is_feed(payload):
        return ProbeResult(True, "rss", url)
    parser = _FeedLinks()
    try:
        parser.feed(payload.decode("utf-8", errors="replace"))
    except Exception:
        parser.links = []
    for href in parser.links[:5]:
        target = urljoin(url, href)
        if urlparse(target).scheme != "https":
            continue
        try:
            feed = fetch_public(target)
        except (OSError, ValueError, TimeoutError):
            continue
        if _is_feed(feed):
            return ProbeResult(True, "rss", target)
    if parser.article:
        return ProbeResult(False, None, url, "article_url_not_source")
    if kind == "html" and payload.strip():
        return ProbeResult(True, "html", url)
    return ProbeResult(False, None, url, "no_supported_feed")
