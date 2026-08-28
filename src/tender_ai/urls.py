"""公开来源 URL 规范化与内容摘要工具。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "gclid", "fbclid", "msclkid", "spm", "from", "ref", "referrer", "share_token",
}
PAGINATION_QUERY_KEYS = {
    "page", "pageno", "page_no", "pageindex", "page_index", "pagesize", "page_size",
    "offset", "limit", "size", "rn", "pn",
}


def canonicalize_url(url: str | None, *, strip_pagination: bool = True) -> str:
    if not url:
        return ""
    value = str(url).strip()
    parts = urlsplit(value)
    if not parts.netloc:
        return value
    scheme = parts.scheme.lower() or "https"
    host = parts.hostname.lower() if parts.hostname else ""
    if scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        scheme = "https"
    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/") or "/"
    pairs = []
    for key, query_value in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower.startswith("utm_") or key_lower in TRACKING_QUERY_KEYS:
            continue
        if strip_pagination and key_lower in PAGINATION_QUERY_KEYS:
            continue
        pairs.append((key, query_value))
    query = urlencode(sorted(pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def content_hash(content: bytes | str) -> str:
    raw = content if isinstance(content, bytes) else str(content).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


__all__ = ["canonicalize_url", "content_hash"]
