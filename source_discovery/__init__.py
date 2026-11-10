"""Search and review of candidate public marketplace news sources."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SearchHit:
    title: str
    article_url: str
    published_at: datetime | None
    publisher: str
    publisher_url: str | None
    provider: str


@dataclass(frozen=True)
class SearchBatch:
    hits: list[SearchHit] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    exa_limited: bool = False


@dataclass(frozen=True)
class SourceCandidate:
    publisher_domain: str
    name: str
    collection_url: str
    kind: str
    category: str
    trust: str
    scope: str
    example_urls: list[str] = field(default_factory=list)
