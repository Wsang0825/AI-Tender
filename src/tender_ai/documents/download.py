"""公开附件下载、内容哈希和去重。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from tender_ai.cache import DiskCache
from tender_ai.config_loader import APP_ROOT
from tender_ai.crawlers.http import HttpClient, sha256_bytes


DOWNLOAD_ROOT = APP_ROOT.parent / "downloads"


@dataclass(frozen=True)
class DownloadedAttachment:
    source_url: str
    file_name: str
    local_path: Path
    content_hash: str
    mime_type: str | None
    size: int


def safe_filename(value: str, *, fallback: str = "attachment") -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = value.replace("\\", "_").replace("/", "_")
    value = re.sub(r"[<>:\"|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def _name_from_response(url: str, response_headers: dict[str, str], suggested: str | None) -> str:
    if suggested:
        return safe_filename(suggested)
    disposition = response_headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^;\"]+)", disposition, re.I)
    if match:
        return safe_filename(unquote(match.group(1) or match.group(2)))
    path_name = Path(unquote(urlparse(url).path)).name
    return safe_filename(path_name or "attachment")


def download_attachment(
    client: HttpClient,
    url: str,
    *,
    suggested_name: str | None = None,
    mime_type: str | None = None,
    cache: DiskCache | None = None,
    destination: Path = DOWNLOAD_ROOT,
    max_bytes: int = 50 * 1024 * 1024,
) -> DownloadedAttachment:
    destination.mkdir(parents=True, exist_ok=True)
    if cache:
        previous = cache.get("attachment-url", url)
        if previous and Path(previous.get("local_path", "")).exists():
            return DownloadedAttachment(
                source_url=url,
                file_name=previous["file_name"],
                local_path=Path(previous["local_path"]),
                content_hash=previous["content_hash"],
                mime_type=previous.get("mime_type"),
                size=int(previous.get("size", 0)),
            )
    response = client.get(url, cache_namespace="attachment-http", cache_expire=86400)
    if len(response.content) > max_bytes:
        raise ValueError(f"附件超过大小限制 {max_bytes} bytes: {url}")
    content_type = (response.headers.get("content-type") or "").lower()
    prefix = response.content[:512].lstrip().lower()
    if content_type.startswith("text/html") or prefix.startswith((b"<!doctype html", b"<html", b"<head")):
        raise ValueError("attachment response is HTML, not a document")
    digest = sha256_bytes(response.content)
    original_name = _name_from_response(url, response.headers, suggested_name)
    output_name = safe_filename(f"{digest[:16]}__{original_name}")
    output = destination / output_name
    if not output.exists():
        output.write_bytes(response.content)
    item = DownloadedAttachment(url, original_name, output, digest, mime_type or response.headers.get("content-type"), len(response.content))
    if cache:
        cache.set("attachment-url", url, {"file_name": item.file_name, "local_path": str(output), "content_hash": digest, "mime_type": item.mime_type, "size": item.size}, expire=None)
        cache.set("attachment-hash", digest, {"file_name": item.file_name, "local_path": str(output)}, expire=None)
    return item
