"""已停用的历史抽取扩展兼容模块。

Stage 4 最终架构不调用任何外部模型 API。复杂字段由 Codex 读取 Review
文件后通过 Evidence 写回；该模块只保留旧导入所需的返回形状。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


disabled_by_default = True
PROMPT_VERSION = "disabled"
SCHEMA_VERSION = "disabled"
LLM_FIELDS: tuple[str, ...] = ()
LLM_DATE_FIELDS: set[str] = set()


@dataclass(frozen=True)
class LLMEnhancement:
    extraction: Any
    calls: int = 0
    cache_hits: int = 0
    filled_fields: tuple[str, ...] = ()
    error: str | None = None


def tender_extraction_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": {}}


def build_extraction_prompt(*_: Any, **__: Any) -> str:
    return "内部模型调用已禁用；请使用 Codex Review。"


def enhance_extraction(extraction: Any, *_: Any, **__: Any) -> LLMEnhancement:
    return LLMEnhancement(extraction=extraction, error="DISABLED_USE_CODEX_REVIEW")


__all__ = [
    "LLMEnhancement", "LLM_DATE_FIELDS", "LLM_FIELDS", "PROMPT_VERSION", "SCHEMA_VERSION",
    "build_extraction_prompt", "disabled_by_default", "enhance_extraction", "tender_extraction_schema",
]
