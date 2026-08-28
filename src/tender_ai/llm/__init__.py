"""已停用的历史 Provider 兼容层。

当前系统的智能层是 Codex，不是 Python 内部模型调用；主路径不会导入或调用
这里的 Provider。保留导出只用于兼容早期代码。
"""

from tender_ai.llm.contracts import LLMProvider
from tender_ai.llm.cache import CachedExtraction, cached_extract, cached_extract_with_status
from tender_ai.llm.providers import LocalProvider, OpenAIProvider, OtherProvider, build_openai_provider, disabled_by_default

__all__ = [
    "CachedExtraction", "LLMProvider", "LocalProvider", "OpenAIProvider", "OtherProvider", "build_openai_provider",
    "cached_extract", "cached_extract_with_status", "disabled_by_default",
]
