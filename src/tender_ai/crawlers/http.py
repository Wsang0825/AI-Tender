"""低并发、可缓存的公开 HTTP 客户端。

固定来源优先使用本模块。只有实际确认需要 JavaScript 或动态令牌时，才应在上层切换浏览器抓取。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import httpx

from tender_ai.cache import DiskCache
from tender_ai.status.time import now_shanghai


USER_AGENT = "AI-Tender/0.1 (+personal local research; public-source crawler)"


class HttpFetchError(RuntimeError):
    def __init__(self, message: str, *, url: str, status_code: int | None = None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    fetched_at: datetime
    from_cache: bool = False

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "").lower()
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or encoding
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text.lstrip("\ufeff"))


class HttpClient:
    def __init__(self, *, cache: DiskCache | None = None, timeout: float = 45.0):
        self.cache = cache
        self.timeout = timeout
        self.last_status: int | None = None
        self.last_success_status: int | None = None
        self._client = httpx.Client(
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 20.0)),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _cache_value(response: HttpResponse) -> dict[str, Any]:
        return {
            "url": response.url,
            "status_code": response.status_code,
            "headers": response.headers,
            "content": response.content,
            "fetched_at": response.fetched_at.isoformat(),
        }

    @staticmethod
    def _from_cache(value: Mapping[str, Any]) -> HttpResponse:
        return HttpResponse(
            url=str(value["url"]),
            status_code=int(value["status_code"]),
            headers=dict(value.get("headers") or {}),
            content=bytes(value.get("content") or b""),
            fetched_at=datetime.fromisoformat(str(value["fetched_at"])),
            from_cache=True,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_data: Any | None = None,
        data: Any | None = None,
        cache_namespace: str | None = None,
        cache_expire: float | None = None,
        force: bool = False,
    ) -> HttpResponse:
        method = method.upper()
        cache_key = {"method": method, "url": url, "json": json_data, "data": data}
        if self.cache and cache_namespace and not force:
            cached = self.cache.get(cache_namespace, cache_key)
            if cached:
                response = self._from_cache(cached)
                self.last_status = response.status_code
                if response.status_code < 400:
                    self.last_success_status = response.status_code
                return response
        try:
            result = self._client.request(method, url, headers=dict(headers or {}), json=json_data, data=data)
        except httpx.HTTPError as exc:
            raise HttpFetchError(f"HTTP 请求失败: {exc}", url=url) from exc
        response = HttpResponse(
            url=str(result.url),
            status_code=result.status_code,
            headers={key.lower(): value for key, value in result.headers.items()},
            content=result.content,
            fetched_at=now_shanghai(),
        )
        self.last_status = response.status_code
        if response.status_code < 400:
            self.last_success_status = response.status_code
        if response.status_code >= 400:
            raise HttpFetchError(f"HTTP 状态码 {response.status_code}", url=url, status_code=response.status_code)
        if self.cache and cache_namespace:
            self.cache.set(cache_namespace, cache_key, self._cache_value(response), expire=cache_expire)
        return response

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        cache_namespace: str | None = "http",
        cache_expire: float | None = 180.0,
        force: bool = False,
    ) -> HttpResponse:
        return self.request("GET", url, headers=headers, cache_namespace=cache_namespace, cache_expire=cache_expire, force=force)

    def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
        cache_namespace: str | None = "api",
        cache_expire: float | None = 120.0,
        force: bool = False,
    ) -> HttpResponse:
        merged = {"Content-Type": "application/json;charset=UTF-8", **dict(headers or {})}
        return self.request("POST", url, headers=merged, json_data=payload, cache_namespace=cache_namespace, cache_expire=cache_expire, force=force)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
