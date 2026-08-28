"""可替换 LLM Provider 接口；业务层不直接依赖任何 SDK。"""

from __future__ import annotations

from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str
    model: str

    def extract(self, *, text: str, prompt: str, schema: dict[str, Any], prompt_version: str, schema_version: str) -> dict[str, Any]:
        ...


__all__ = ["LLMProvider"]
