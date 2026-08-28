"""声明式通用 HTML 适配器。

它只负责常见的“列表页→详情页→附件”结构。特殊站点继续使用自定义适配器，
避免把站点差异硬编码进通用层。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from tender_ai.config_loader import DEFAULT_CONFIG_DIR
from tender_ai.sources.adapters import HttpSourceAdapter, _clean, _date_from_text, _extract_attachments, _visible_text
from tender_ai.sources.contracts import RawListingItem
from tender_ai.sources.registry import SourceDefinition


class GenericAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    list_url: str
    search_url: str | None = None
    request_method: str = "GET"
    query_parameters: dict[str, Any] = Field(default_factory=dict)
    list_item_selector: str = "a[href]"
    title_selector: str | None = None
    url_selector: str | None = None
    publish_date_selector: str | None = None
    pagination_type: str = "none"
    page_parameter: str = "page"
    detail_content_selector: str | None = None
    attachment_selector: str = "a[href]"
    next_page_selector: str | None = None
    encoding: str = "utf-8"
    rate_limit: float = Field(default=0.2, ge=0.0, le=60.0)
    browser_required: bool = False


def load_generic_adapter_config(definition: SourceDefinition) -> GenericAdapterConfig:
    configured = definition.adapter_config or definition.adapter.removeprefix("generic:")
    target = Path(configured)
    if not target.is_absolute():
        target = DEFAULT_CONFIG_DIR / target if target.parts and target.parts[0] == "source_adapters" else DEFAULT_CONFIG_DIR / "source_adapters" / target
    if target.suffix.lower() not in {".yaml", ".yml", ".json"}:
        target = target.with_suffix(".yaml")
    with target.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if isinstance(payload, dict) and "adapter" in payload:
        payload = payload["adapter"]
    if not isinstance(payload, dict):
        raise ValueError(f"通用 Adapter 配置必须是对象: {target}")
    return GenericAdapterConfig(**payload)


class GenericSourceAdapter(HttpSourceAdapter):
    """通过 YAML selector 接入普通公开 HTML 站点，不启动浏览器。"""

    def __init__(self, definition: SourceDefinition):
        super().__init__(definition)
        self.generic_config = load_generic_adapter_config(definition)
        if self.generic_config.browser_required:
            raise ValueError(f"{definition.source_id} 标记 browser_required，不能使用 Generic HTTP Adapter")

    def _page_url(self, page: int, query: str) -> str:
        config = self.generic_config
        base = config.search_url or config.list_url
        params = dict(config.query_parameters)
        if query and config.search_url:
            params.setdefault("query", query)
        if config.pagination_type.lower() in {"page", "offset", "page_parameter"}:
            params[config.page_parameter] = page
        if not params:
            return base
        separator = "&" if "?" in base else "?"
        return base + separator + urlencode(params)

    def _response(self, url: str) -> Any:
        config = self.generic_config
        method = config.request_method.upper()
        if method == "POST":
            return self.http.request("POST", url, data=config.query_parameters, cache_namespace=f"source:{self.source_id}:generic", cache_expire=300.0)
        return self._get(url, namespace=f"source:{self.source_id}:generic", expire=300.0)

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        query = str(kwargs.get("query") or "")
        page = int(kwargs.get("page", 0))
        url = listing_url if listing_url != self.generic_config.list_url else self._page_url(page, query)
        response = self._response(url)
        soup = BeautifulSoup(response.text, "lxml")
        config = self.generic_config
        result: list[RawListingItem] = []
        for node in soup.select(config.list_item_selector):
            title_node = node.select_one(config.title_selector) if config.title_selector else node
            url_node = node.select_one(config.url_selector) if config.url_selector else node
            raw_url = url_node.get("href") if url_node else None
            if not raw_url and url_node:
                raw_url = url_node.get("data-url") or url_node.get("data-href")
            title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
            detail_url = urljoin(url, str(raw_url or ""))
            if not title or not raw_url or not detail_url:
                continue
            date_node = node.select_one(config.publish_date_selector) if config.publish_date_selector else node
            published = _date_from_text(_clean(date_node.get_text(" ", strip=True) if date_node else ""))
            result.append(RawListingItem(title=title, url=detail_url, published_at=published, metadata={"content": _clean(node.get_text(" ", strip=True))}))
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for page in range(max_pages):
            items = self.fetch_list(self.generic_config.list_url, query=query, page=page)
            for item in items:
                if item.url not in seen:
                    seen.add(item.url)
                    result.append(item)
            if self.generic_config.pagination_type == "none" or not items:
                break
            if self.generic_config.rate_limit:
                time.sleep(self.generic_config.rate_limit)
        return result

    def _detail_from_response(self, response: Any, detail_url: str):
        payload = super()._detail_from_response(response, detail_url)
        selector = self.generic_config.detail_content_selector
        if selector:
            soup = BeautifulSoup(response.text, "lxml")
            node = soup.select_one(selector)
            if node:
                payload.text = _clean(node.get_text(" ", strip=True))
                payload.html = str(node)
                payload.attachments = _extract_attachments(str(node), detail_url)
        return payload

    def fetch_attachments(self, detail: Any, **kwargs: Any):
        if not getattr(self.generic_config, "attachment_selector", None):
            return super().fetch_attachments(detail, **kwargs)
        return list(getattr(detail, "attachments", []) or [])


__all__ = ["GenericAdapterConfig", "GenericSourceAdapter", "load_generic_adapter_config"]
