"""旧 LLM 缓存兼容模块，当前主路径不使用。

Codex Review 结果缓存由 ``codex_review_items.content_hash`` 管理；保留本文件
只为兼容早期代码，不能把它接回默认抽取流程。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_ai.storage.models import LLMExtractionCache


disabled_by_default = True


@dataclass(frozen=True)
class CachedExtraction:
    response: dict[str, Any]
    cache_hit: bool


def cached_extract_with_status(session: Session, provider: Any, *, content_hash: str, text: str, prompt: str, prompt_version: str, schema: dict[str, Any], schema_version: str) -> CachedExtraction:
    model = str(getattr(provider, "model", "unknown"))
    row = session.scalar(select(LLMExtractionCache).where(LLMExtractionCache.content_hash == content_hash, LLMExtractionCache.model == model, LLMExtractionCache.prompt_version == prompt_version))
    if row is not None:
        return CachedExtraction(json.loads(row.response_json), True)
    result = provider.extract(text=text, prompt=prompt, schema=schema, prompt_version=prompt_version, schema_version=schema_version)
    session.add(LLMExtractionCache(content_hash=content_hash, model=model, prompt_version=prompt_version, schema_version=schema_version, response_json=json.dumps(result, ensure_ascii=False, default=str)))
    session.flush()
    return CachedExtraction(result, False)


def cached_extract(session: Session, provider: Any, *, content_hash: str, text: str, prompt: str, prompt_version: str, schema: dict[str, Any], schema_version: str) -> dict[str, Any]:
    return cached_extract_with_status(session, provider, content_hash=content_hash, text=text, prompt=prompt, prompt_version=prompt_version, schema=schema, schema_version=schema_version).response


__all__ = ["CachedExtraction", "cached_extract", "cached_extract_with_status"]
