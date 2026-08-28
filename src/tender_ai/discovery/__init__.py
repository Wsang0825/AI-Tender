"""Discovery Provider 预留目录。"""
from tender_ai.discovery.contracts import SearchResult
from tender_ai.discovery.providers import DDGSProvider, WeixinSearchProvider
from tender_ai.discovery.runner import DiscoveryRunner, DiscoverySummary

__all__ = ["DDGSProvider", "DiscoveryRunner", "DiscoverySummary", "SearchResult", "WeixinSearchProvider"]
