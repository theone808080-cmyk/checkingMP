"""Free-mode search adapters for Exa and Google News RSS."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlparse

from source_discovery import SearchBatch, SearchHit

EXA_URL = "https://api.exa.ai/search"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
RESULT_LIMIT = 10
MAX_RSS_BYTES = 2_000_000
MAX_RSS_ITEMS = 100


class ProviderError(RuntimeError):
    def __init__(self, provider: str, code: str):
        super().__init__(code)
        self.provider = provider
        self.code = code


def open_url(request: urllib.request.Request, timeout: float = 15):
    return urllib.request.urlopen(request, timeout=timeout)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publisher_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        return None
    host = parsed.hostname.rstrip(".").lower()
    if host == "news.google.com" or host.endswith(".news.google.com"):
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    path = parsed.path or "/"
    return urllib.parse.urlunparse(("https", host, path, "", "", ""))


def _publisher_origin(value: str | None) -> str | None:
    publisher_url = _publisher_url(value)
    if publisher_url is None:
        return None
    return f"https://{urlparse(publisher_url).hostname}/"


def parse_google_news_rss(data: bytes) -> list[SearchHit]:
    if not isinstance(data, bytes) or len(data) > MAX_RSS_BYTES:
        raise ValueError("invalid RSS payload")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise ValueError("invalid RSS XML") from None
    hits = []
    for item in root.findall(".//item")[:MAX_RSS_ITEMS]:
        title = (item.findtext("title") or "").strip()
        article_url = (item.findtext("link") or "").strip()
        parsed_article = urlparse(article_url)
        if (not title or not article_url or parsed_article.scheme not in ("http", "https")
                or not parsed_article.hostname or parsed_article.username or parsed_article.password):
            continue
        source = item.find("source")
        publisher = " ".join("".join(source.itertext()).split()) if source is not None else ""
        publisher_url = _publisher_url(source.get("url")) if source is not None else None
        if publisher.casefold() in {"google news", "news.google.com"}:
            publisher = ""
        if not publisher and publisher_url:
            publisher = urlparse(publisher_url).hostname or ""
        hits.append(SearchHit(
            title=title[:500], article_url=article_url[:2048],
            published_at=_date(item.findtext("pubDate")), publisher=publisher[:200],
            publisher_url=publisher_url, provider="google_news",
        ))
    return hits


def search_google_news_rss(query: str, *, open_url: Callable = open_url) -> list[SearchHit]:
    params = urllib.parse.urlencode({"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"})
    request = urllib.request.Request(f"{GOOGLE_NEWS_URL}?{params}", headers={"User-Agent": "CheckingMP/1.0"})
    try:
        with open_url(request, timeout=15) as response:
            payload = response.read(MAX_RSS_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ProviderError("google_news", f"http_{error.code}") from None
    except OSError:
        raise ProviderError("google_news", "transport") from None
    return parse_google_news_rss(payload)


def search_exa(query: str, api_key: str, *, open_url: Callable = open_url) -> list[SearchHit]:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api key is required")
    payload = json.dumps({"query": query, "type": "lite", "category": "news",
                          "numResults": RESULT_LIMIT}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(EXA_URL, data=payload, method="POST", headers={
        "Content-Type": "application/json", "x-api-key": api_key.strip(),
    })
    try:
        with open_url(request, timeout=20) as response:
            raw = response.read(MAX_RSS_BYTES + 1)
    except urllib.error.HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="ignore").casefold()
        error.close()
        exhausted = error.code == 402 or (error.code == 429 and any(
            marker in detail for marker in ("credit", "quota", "payment", "monthly limit", "usage limit")
        ))
        code = "limit_reached" if exhausted else "rate_limited" if error.code == 429 else f"http_{error.code}"
        raise ProviderError("exa", code) from None
    except OSError:
        raise ProviderError("exa", "transport") from None
    if len(raw) > MAX_RSS_BYTES:
        raise ProviderError("exa", "response_too_large")
    try:
        result = json.loads(raw.decode("utf-8"))
        rows = result["results"]
        if not isinstance(rows, list):
            raise ValueError
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise ProviderError("exa", "invalid_response") from None
    hits = []
    for row in rows[:RESULT_LIMIT]:
        if not isinstance(row, dict):
            continue
        title, article_url = row.get("title"), row.get("url")
        parsed = urlparse(article_url) if isinstance(article_url, str) else None
        if not title or not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        hits.append(SearchHit(
            title=str(title)[:500], article_url=article_url[:2048],
            published_at=_date(row.get("publishedDate")),
            publisher=str(row.get("author") or parsed.hostname)[:200],
            publisher_url=_publisher_origin(f"https://{parsed.hostname}/"), provider="exa",
        ))
    return hits


def search_all(*, api_key: str | None, queries: list[str]) -> SearchBatch:
    hits: list[SearchHit] = []
    errors: dict[str, str] = {}
    exa_limited = False
    for query in queries:
        if api_key and not exa_limited:
            try:
                hits.extend(search_exa(query, api_key))
            except ProviderError as error:
                errors.setdefault("exa", error.code)
                exa_limited = exa_limited or error.code == "limit_reached"
            except Exception:
                errors.setdefault("exa", "unavailable")
        try:
            hits.extend(search_google_news_rss(query))
        except ProviderError as error:
            errors.setdefault("google_news", error.code)
        except Exception:
            errors.setdefault("google_news", "unavailable")
    return SearchBatch(hits=hits, errors=errors, exa_limited=exa_limited)
