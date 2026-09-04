"""可追踪的应用、数据库、配置和抽取规则版本。"""

APP_VERSION = "0.2.0"
SCHEMA_VERSION = "0009_candidate_enrichment_closure"
CONFIG_VERSION = "search_profiles:2;industry_profiles:2;region_catalog:1;concepts:1"
EXTRACTOR_VERSION = "rule.candidate_recall.v1"
STATUS_RULE_VERSION = "status.v2"

__all__ = ["APP_VERSION", "CONFIG_VERSION", "EXTRACTOR_VERSION", "SCHEMA_VERSION", "STATUS_RULE_VERSION"]
