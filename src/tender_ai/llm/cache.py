"""LLM 抽取缓存的唯一键和持久化边界。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_ai.storage.models import LLMExtractionCache


def cached_extract(session: Session, provider: Any, *, content_hash: str, text: str, prompt: str, prompt_version: str, schema: dict[str, Any], schema_version: str) -> dict[str, Any]:
    model = str(getattr(provider, "model", "unknown"))
    row = session.scalar(select(LLMExtractionCache).where(LLMExtractionCache.content_hash == content_hash, LLMExtractionCache.model == model, LLMExtractionCache.prompt_version == prompt_version))
    if row is not None:
        return json.loads(row.response_json)
    result = provider.extract(text=text, prompt=prompt, schema=schema, prompt_version=prompt_version, schema_version=schema_version)
    session.add(LLMExtractionCache(content_hash=content_hash, model=model, prompt_version=prompt_version, schema_version=schema_version, response_json=json.dumps(result, ensure_ascii=False, default=str)))
    session.flush()
    return result


__all__ = ["cached_extract"]
