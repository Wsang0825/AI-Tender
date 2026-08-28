"""历史 Provider 协议；当前主路径禁用，智能层由 Codex 承担。"""

from __future__ import annotations

from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str
    model: str

    def extract(self, *, text: str, prompt: str, schema: dict[str, Any], prompt_version: str, schema_version: str) -> dict[str, Any]:
        ...


__all__ = ["LLMProvider"]
