"""来源适配器统一接口；业务核心不感知具体网站结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from tender_ai.models import TenderRecord


@dataclass(frozen=True)
class AdapterHealth:
    source_id: str
    status: str
    message: str


class SourceAdapter(ABC):
    """所有来源适配器必须实现的最小接口。"""

    source_id: str

    @abstractmethod
    def search(self, query: str, **kwargs: Any) -> list[Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch_detail(self, detail_url: str, **kwargs: Any) -> Any | None:
        raise NotImplementedError

    @abstractmethod
    def fetch_attachments(self, detail: Any, **kwargs: Any) -> list[Any]:
        raise NotImplementedError

    @abstractmethod
    def normalize(self, payload: Any, **kwargs: Any) -> TenderRecord | None:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> AdapterHealth:
        raise NotImplementedError
