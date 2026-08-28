"""LLM Provider 实现与占位实现。"""

from __future__ import annotations

from typing import Any


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, model: str = "", client: Any | None = None):
        self.model = model
        self.client = client

    def extract(self, *, text: str, prompt: str, schema: dict[str, Any], prompt_version: str, schema_version: str) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("OpenAIProvider 未配置 client；调用方应先检查 LLM 缓存")
        response = self.client.responses.create(model=self.model, input=f"{prompt}\n{text}")
        output = getattr(response, "output_text", "")
        if not output:
            raise RuntimeError("LLM 未返回结构化内容")
        return {"raw": output, "prompt_version": prompt_version, "schema_version": schema_version, "schema": schema}


class LocalProvider:
    name = "local"

    def __init__(self, *, model: str = "local"):
        self.model = model

    def extract(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("LocalProvider 仅预留，尚未配置本地模型")


class OtherProvider(LocalProvider):
    name = "other"


__all__ = ["LocalProvider", "OpenAIProvider", "OtherProvider"]
