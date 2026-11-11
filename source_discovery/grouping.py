"""Group article search hits by their actual publisher domain."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse

from source_discovery import SearchHit, SourceCandidate

_AGGREGATORS = {"news.google.com"}


def group_candidates(hits: list[SearchHit]) -> list[SourceCandidate]:
    grouped: dict[str, list[SearchHit]] = defaultdict(list)
    for hit in hits:
        parsed = urlparse(hit.publisher_url or "")
        domain = (parsed.hostname or "").lower().rstrip(".")
        if not domain or domain in _AGGREGATORS or domain.endswith(".news.google.com"):
            continue
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            continue
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            continue
        grouped[domain].append(hit)
    candidates = []
    for domain, rows in sorted(grouped.items()):
        examples = list(dict.fromkeys(row.article_url for row in rows if row.article_url))[:10]
        name = next((row.publisher.strip() for row in rows if row.publisher.strip()), domain)
        candidates.append(SourceCandidate(
            publisher_domain=domain, name=name[:200], collection_url=f"https://{domain}/",
            kind="html", category="industry", trust="industry", scope="marketplaces",
            example_urls=examples,
        ))
    return candidates
