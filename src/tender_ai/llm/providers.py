"""未来扩展的 Provider 兼容壳。

当前产品明确不使用任何外部模型 API。Codex 是顶层智能层，Python 只负责
搜索、抓取、规则解析、Evidence 和状态计算。保留这些名字仅为避免旧导入
破坏，但它们不会被主执行路径调用，也不会读取或要求 API Key。
"""

from __future__ import annotations

from typing import Any


disabled_by_default = True


class DisabledProvider:
    name = "disabled"
    configured = False

    def extract(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("当前项目已禁用内部 AI API；请使用 Codex Review 工作流")


class OpenAIProvider(DisabledProvider):
    """旧接口兼容名，不实现 SDK 调用。"""

    name = "openai-disabled"

    @classmethod
    def from_env(cls) -> None:
        return None


class LocalProvider(DisabledProvider):
    name = "local-disabled"


class OtherProvider(DisabledProvider):
    name = "other-disabled"


def build_openai_provider() -> None:
    """兼容旧调用方；当前永远不构造 Provider。"""

    return None


__all__ = ["LocalProvider", "OpenAIProvider", "OtherProvider", "build_openai_provider", "disabled_by_default"]
