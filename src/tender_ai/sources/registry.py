"""来源注册表与无网络占位适配器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tender_ai.config_loader import DEFAULT_CONFIG_DIR, load_yaml
from tender_ai.models import TenderRecord
from tender_ai.sources.base import AdapterHealth, SourceAdapter


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    category: str
    base_url: str
    region: str | None = None
    enabled: bool = True
    priority: int = Field(default=3, ge=1, le=9)
    access_method: str = "http_public"
    requires_login: bool = False
    adapter: str = "configured"
    adapter_level: str = "CUSTOM_HTTP"
    adapter_config: str | None = None
    status: str = "registry_only"
    crawl_enabled: bool = False
    max_pages: int = Field(default=2, ge=1, le=100)
    request_delay_seconds: float = Field(default=0.2, ge=0.0, le=60.0)
    crawl_interval: int = Field(default=86400, ge=0)
    rate_limit: float = Field(default=0.2, ge=0.0, le=60.0)
    lookback_days: int = Field(default=30, ge=1, le=3650)
    browser_profile_path: str | None = None
    notes: str | None = None


class ConfiguredSourceAdapter(SourceAdapter):
    """只验证接口存在，不主动访问网站。"""

    def __init__(self, definition: SourceDefinition):
        self.definition = definition
        self.source_id = definition.source_id

    def search(self, query: str, **kwargs: Any) -> list[Any]:
        return []

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[Any]:
        return []

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> Any | None:
        return None

    def fetch_attachments(self, detail: Any, **kwargs: Any) -> list[Any]:
        return []

    def normalize(self, payload: Any, **kwargs: Any) -> TenderRecord | None:
        return payload if isinstance(payload, TenderRecord) else None

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(self.source_id, self.definition.status, "来源已登记；当前版本未启用在线适配")


class SourceRegistry:
    def __init__(self, definitions: list[SourceDefinition]):
        self.definitions = definitions
        self._by_id = {item.source_id: item for item in definitions}
        if len(self._by_id) != len(definitions):
            raise ValueError("来源注册表存在重复 source_id")

    @classmethod
    def from_file(cls, path: Path | None = None) -> "SourceRegistry":
        target = path or (DEFAULT_CONFIG_DIR / "sources.yaml")
        payload = load_yaml(target.name, target.parent)
        raw_sources = payload.get("sources") or []
        return cls([SourceDefinition(**item) for item in raw_sources])

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._by_id[source_id]
        except KeyError as exc:
            raise KeyError(f"未知来源: {source_id}") from exc

    def enabled(self) -> list[SourceDefinition]:
        return [item for item in self.definitions if item.enabled]

    def adapters(self, *, enabled_only: bool = True) -> list[SourceAdapter]:
        definitions = self.enabled() if enabled_only else self.definitions
        from tender_ai.sources.adapters import build_adapter

        # 保留旧版调用方对“仅登记来源”适配器的兼容性；真实抓取运行器按配置直接构造适配器。
        ordered = sorted(definitions, key=lambda item: (item.crawl_enabled, item.priority, item.source_id))
        return [build_adapter(item) for item in ordered]

    def health_check(self) -> list[AdapterHealth]:
        adapters = self.adapters()
        try:
            return [adapter.health_check() for adapter in adapters]
        finally:
            for adapter in adapters:
                close = getattr(adapter, "close", None)
                if close:
                    close()
