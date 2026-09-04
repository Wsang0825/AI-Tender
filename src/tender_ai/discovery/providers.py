"""基于公开搜索引擎索引的 Discovery 提供者。"""

from __future__ import annotations

import re
import os
from time import monotonic
from urllib.parse import urlencode

import httpx

from ddgs import DDGS

from tender_ai.cache import DiskCache
from tender_ai.config_loader import APP_ROOT
from tender_ai.discovery.contracts import SearchResult


class SearchProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        manual_action_required: bool = False,
        manual_action_type: str | None = None,
        http_status: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.manual_action_required = manual_action_required
        self.manual_action_type = manual_action_type
        self.http_status = http_status
        self.url = url


def _manual_action_from_error(error: BaseException, http_status: int | None = None) -> str | None:
    """识别搜索服务的登录、验证码和人机验证错误。"""

    message = str(error).casefold()
    if any(token in message for token in ("验证码", "captcha")):
        return "CAPTCHA"
    if any(token in message for token in ("请登录", "登录后", "login required", "sign in", "session expired")):
        return "LOGIN_REQUIRED"
    if http_status == 412 or any(token in message for token in ("412", "challenge", "verify you are human", "人机验证", "安全验证")):
        return "VERIFICATION_REQUIRED"
    return None


class DDGSProvider:
    name = "ddgs"
    priority = 1
    enabled = True
    health = "UNKNOWN"
    daily_query_limit = 200
    cooldown = 0.0

    def __init__(self, *, cache: DiskCache | None = None, cache_expire: float = 86400.0):
        self.cache = cache or DiskCache(APP_ROOT.parent / "data" / "cache")
        self.cache_expire = cache_expire
        self._owns_cache = cache is None
        self._last_query_at = 0.0

    def close(self) -> None:
        if self._owns_cache:
            self.cache.close()

    def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        if self.cooldown and monotonic() - self._last_query_at < self.cooldown:
            raise SearchProviderError("DDGS 处于冷却期")
        self._last_query_at = monotonic()
        cache_key = {"query": query, "max_results": max_results}
        cached = self.cache.get("discovery:ddgs", cache_key)
        if cached is not None:
            return [SearchResult(**item) for item in cached]
        try:
            with DDGS() as client:
                rows = list(client.text(query, max_results=max_results))
        except Exception as exc:  # DDGS 后端会随搜索引擎变化，单个查询不能中断批次
            if "no results found" in str(exc).casefold():
                rows = []
            else:
                action = _manual_action_from_error(exc)
                raise SearchProviderError(
                    f"DDGS 搜索失败: {exc}",
                    manual_action_required=bool(action),
                    manual_action_type=action,
                ) from exc
        result: list[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("href") or row.get("url") or "").strip()
            title = str(row.get("title") or "").strip()
            if not url or not title:
                continue
            result.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=str(row.get("body") or row.get("snippet") or "").strip(),
                    published_at=str(row.get("date") or "") or None,
                    provider=self.name,
                    metadata=dict(row),
                )
            )
        self.cache.set("discovery:ddgs", cache_key, [item.__dict__ for item in result], expire=self.cache_expire)
        self.health = "ACTIVE"
        return result


class SearXNGProvider:
    """可选 SearXNG fallback；未配置 SEARXNG_URL 时保持禁用。"""

    name = "searxng"
    priority = 2
    daily_query_limit = 200
    cooldown = 0.0

    def __init__(self, base_url: str | None = None, *, cache: DiskCache | None = None):
        self.base_url = (base_url or os.environ.get("SEARXNG_URL") or "").rstrip("/")
        self.enabled = bool(self.base_url)
        self.health = "DISABLED" if not self.enabled else "UNKNOWN"
        self.cache = cache or DiskCache(APP_ROOT.parent / "data" / "cache")
        self._owns_cache = cache is None
        self._client = httpx.Client(timeout=30.0, follow_redirects=True, verify=False)

    def close(self) -> None:
        self._client.close()
        if self._owns_cache:
            self.cache.close()

    def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        if not self.enabled:
            raise SearchProviderError("SearXNG 未配置")
        cache_key = {"query": query, "max_results": max_results, "base_url": self.base_url}
        cached = self.cache.get("discovery:searxng", cache_key)
        if cached is not None:
            return [SearchResult(**item) for item in cached]
        try:
            response = self._client.get(f"{self.base_url}/search?{urlencode({'q': query, 'format': 'json'})}")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            action = _manual_action_from_error(exc, status)
            self.health = "DEGRADED"
            raise SearchProviderError(
                f"SearXNG 搜索失败: {exc}",
                manual_action_required=bool(action),
                manual_action_type=action,
                http_status=status,
                url=str(exc.response.url) if exc.response is not None else f"{self.base_url}/search",
            ) from exc
        except Exception as exc:
            action = _manual_action_from_error(exc)
            self.health = "DEGRADED"
            raise SearchProviderError(
                f"SearXNG 搜索失败: {exc}",
                manual_action_required=bool(action),
                manual_action_type=action,
            ) from exc
        result = [
            SearchResult(
                title=str(row.get("title") or "").strip(),
                url=str(row.get("url") or "").strip(),
                snippet=str(row.get("content") or "").strip(),
                published_at=None,
                provider=self.name,
                metadata=dict(row),
            )
            for row in (payload.get("results") or [])
            if isinstance(row, dict) and row.get("title") and row.get("url")
        ][:max_results]
        self.cache.set("discovery:searxng", cache_key, [item.__dict__ for item in result], expire=86400.0)
        self.health = "ACTIVE"
        return result


class CustomSearchProvider:
    """自定义合法搜索 API 的占位实现，业务层不绑定具体服务商。"""

    name = "custom"
    priority = 9
    enabled = False
    health = "DISABLED"
    daily_query_limit = 0
    cooldown = 0.0

    def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        raise SearchProviderError("CustomSearchProvider 尚未配置")

    def close(self) -> None:
        return None


class FallbackSearchProvider:
    def __init__(self, providers: list[object]):
        self.providers = sorted(providers, key=lambda item: int(getattr(item, "priority", 9)))
        self.name = "fallback"

    def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        errors: list[str] = []
        manual_action_type: str | None = None
        manual_http_status: int | None = None
        manual_url: str | None = None
        for provider in self.providers:
            if not getattr(provider, "enabled", True):
                continue
            try:
                return provider.search(query, max_results=max_results)
            except SearchProviderError as exc:
                errors.append(f"{getattr(provider, 'name', type(provider).__name__)}: {exc}")
                if exc.manual_action_required and manual_action_type is None:
                    manual_action_type = exc.manual_action_type or "MANUAL_ACTION_REQUIRED"
                    manual_http_status = exc.http_status
                    manual_url = exc.url
        raise SearchProviderError(
            "所有 SearchProvider 均失败: " + "; ".join(errors),
            manual_action_required=manual_action_type is not None,
            manual_action_type=manual_action_type,
            http_status=manual_http_status,
            url=manual_url,
        )

    def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if close:
                close()


class WeixinSearchProvider:
    """只使用公开索引查找微信公众号文章，不尝试进入微信封闭环境。"""

    name = "weixin_public_index"

    def __init__(self, provider: object | None = None):
        self.provider = provider or DDGSProvider()
        self._owns_provider = provider is None

    def close(self) -> None:
        if self._owns_provider:
            self.provider.close()

    def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        rows = self.provider.search(f"site:mp.weixin.qq.com/s {query}", max_results=max_results)
        return [
            SearchResult(
                title=item.title,
                url=item.url,
                snippet=item.snippet,
                published_at=item.published_at,
                provider=self.name,
                metadata={**item.metadata, "original_query": query},
            )
            for item in rows
            if "mp.weixin.qq.com" in item.url
        ]

    @staticmethod
    def follow_up_queries(result: SearchResult) -> list[str]:
        text = f"{result.title} {result.snippet}"
        candidates: list[str] = []
        patterns = (
            r"(?:项目名称|项目名|工程名称)\s*[：:]\s*([^，。；;]{4,100})",
            r"(?:项目编号|招标编号|采购编号)\s*[：:]\s*([A-Za-z0-9][A-Za-z0-9_-]{3,50})",
            r"(?:招标人|采购人|建设单位|业主单位)\s*[：:]\s*([^，。；;]{3,80})",
            r"(?:招标代理机构|代理机构)\s*[：:]\s*([^，。；;]{3,80})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                value = match.group(1).strip()
                if value and value not in candidates:
                    candidates.append(value)
        return candidates[:4]


__all__ = [
    "CustomSearchProvider", "DDGSProvider", "FallbackSearchProvider", "SearchProviderError",
    "SearXNGProvider", "WeixinSearchProvider",
]
