"""Public-source collector. Never bypasses authentication or access controls."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import db
from collector.safe_fetch import fetch_public

SERVICE = "collector"
MAX_PER_SOURCE = 30
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch(url: str, *, safe: bool = False) -> bytes:
    if safe:
        return fetch_public(url, timeout=30, max_bytes=2_000_000)
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def read_rss(url: str, *, safe: bool = False):
    root = ET.fromstring(fetch(url, safe=safe))
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        if link and title:
            yield {"url": link, "title": title, "text": (item.findtext("description") or "")[:4000], "published_at": parse_date(item.findtext("pubDate"))}
    for entry in root.iter(f"{ATOM}entry"):
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        link_node = entry.find(f"{ATOM}link[@rel='alternate']") or entry.find(f"{ATOM}link")
        link = link_node.get("href") if link_node is not None else entry.findtext(f"{ATOM}id")
        if link and title:
            yield {"url": link, "title": title, "text": " ".join((entry.findtext(f"{ATOM}summary") or "").split())[:4000], "published_at": parse_date(entry.findtext(f"{ATOM}updated") or entry.findtext(f"{ATOM}published"))}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            text = " ".join("".join(self._parts).split())
            if text:
                self.links.append((self._href, text))
            self._href = None


def read_html(url: str, *, safe: bool = False):
    page = fetch(url, safe=safe).decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))).strip() if title_match else url
    without_noncontent = re.sub(r"<(script|style|noscript)[^>]*>.*?</\\1>", " ", page, flags=re.IGNORECASE | re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]+>", " ", without_noncontent))
    text = " ".join(text.split())[:12000]
    if not text:
        raise RuntimeError("на публичной HTML-странице не найден текст")
    # Public reference pages are tracked as one changing document. The content hash
    # in db.add_material creates a new row only when the page actually changes.
    yield {"url": url, "title": title[:500], "text": text, "published_at": None}


def read_telegram(url: str, *, safe: bool = False):
    page = fetch(url, safe=safe).decode("utf-8", errors="replace")
    pattern = re.compile(
        r'data-post="([^"]+)"[\s\S]*?<div class="tgme_widget_message_text[^>]*>([\s\S]*?)</div></div>',
        re.DOTALL,
    )
    matches = pattern.findall(page)
    if not matches:
        raise RuntimeError("в preview Telegram не найдены публичные сообщения")
    for post_id, content in matches:
        link = f"https://t.me/{post_id}"
        text = re.sub(r"<[^>]+>", " ", content)
        text = html.unescape(" ".join(text.split()))
        if text:
            yield {"url": link, "title": text[:160], "text": text[:4000], "published_at": None}


READERS = {"rss": read_rss, "html": read_html, "telegram": read_telegram}


def main() -> dict:
    with db.connect() as conn:
        db.init_schema(conn)
        db.sync_sources(conn)
        run_id = db.start_run(conn, SERVICE)
        total = 0
        successful_sources = 0
        failed_sources = []
        failure_signatures = {}
        try:
            sources = conn.execute("SELECT id, name, kind, url, code, managed_by FROM sources WHERE active ORDER BY id").fetchall()
            for source_id, name, kind, url, source_code, managed_by in sources:
                try:
                    if managed_by == "owner":
                        items = list(READERS[kind](url, safe=True))[:MAX_PER_SOURCE]
                    else:
                        items = list(READERS[kind](url))[:MAX_PER_SOURCE]
                    new = sum(db.add_material(conn, source_id, run_id, item) for item in items)
                    total += new
                    conn.execute("UPDATE sources SET last_checked_at = now(), last_error = NULL WHERE id = %s", (source_id,))
                    successful_sources += 1
                    print(f"[{SERVICE}] {name}: {len(items)} прочитано, {new} новых", flush=True)
                except Exception as error:
                    failed_sources.append(source_code)
                    failure_signatures[source_code] = hashlib.sha256(
                        f"{type(error).__name__}:{error}".encode("utf-8", errors="replace")
                    ).hexdigest()[:16]
                    conn.execute("UPDATE sources SET last_checked_at = now(), last_error = %s WHERE id = %s", (str(error)[:500], source_id))
                    print(f"[{SERVICE}] {name}: недоступен: {error}", flush=True)
            if not successful_sources:
                failure = RuntimeError("No sources were successfully read")
                failure.source_failures = failed_sources
                failure.source_failure_signatures = failure_signatures
                raise failure
            db.finish_run(conn, run_id, "finished", total, f"New materials: {total}; sources read: {successful_sources}; source failures: {len(failed_sources)}")
            return {"items": total, "source_failures": failed_sources,
                    "source_failure_signatures": failure_signatures}
        except Exception as error:
            db.finish_run(conn, run_id, "failed", total, str(error))
            raise

