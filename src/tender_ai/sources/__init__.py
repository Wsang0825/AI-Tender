"""来源注册和适配器契约。"""

from tender_ai.sources.base import AdapterHealth, SourceAdapter
from tender_ai.sources.registry import ConfiguredSourceAdapter, SourceDefinition, SourceRegistry

__all__ = ["AdapterHealth", "ConfiguredSourceAdapter", "SourceAdapter", "SourceDefinition", "SourceRegistry"]
