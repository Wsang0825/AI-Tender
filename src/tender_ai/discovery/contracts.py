"""全网 Discovery 的搜索提供者协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    provider: str = "ddgs"
    metadata: dict[str, Any] = field(default_factory=dict)


class SearchProvider(Protocol):
    name: str
    priority: int
    enabled: bool
    health: str
    daily_query_limit: int
    cooldown: float

    def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        ...

    def close(self) -> None:
        ...


__all__ = ["SearchProvider", "SearchResult"]
