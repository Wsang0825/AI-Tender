"""运行时配置写入服务。

Web 设置页只通过本模块修改 YAML。配置仍然是人类可读的声明式文件，下一次
按需搜索会重新加载它；本模块不创建定时任务，也不触发网络访问。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml

from tender_ai.config_loader import APP_ROOT, DEFAULT_CONFIG_DIR, load_region_catalog


CONFIG_PATHS = {
    "profiles": DEFAULT_CONFIG_DIR / "search_profiles.yaml",
    "industries": DEFAULT_CONFIG_DIR / "industry_profiles.yaml",
    "sources": DEFAULT_CONFIG_DIR / "sources.yaml",
    "providers": DEFAULT_CONFIG_DIR / "search_providers.yaml",
}


def _csv(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in values:
        for token in str(item).replace("，", ",").replace("\n", ",").split(","):
            token = token.strip()
            if token and token not in result:
                result.append(token)
    return result


def _read(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"配置文件根节点必须是对象: {path}")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False, default_flow_style=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _resolve_region_code(value: str) -> str | None:
    catalog = load_region_catalog()
    if value in {entry.region_code for entry in catalog.entries}:
        return value
    normalized = "".join(str(value).casefold().split())
    candidates: list[tuple[int, str]] = []
    for entry in catalog.entries:
        labels = (entry.name, entry.short_name, *entry.aliases)
        for label in labels:
            label_normalized = "".join(str(label).casefold().split())
            if label_normalized and (label_normalized == normalized or label_normalized in normalized):
                candidates.append((len(label_normalized), entry.region_code))
    return max(candidates)[1] if candidates else None


def resolve_region_codes(values: str | Iterable[str] | None) -> list[str]:
    """把设置页中的全国地区名称/简称转换为 profile 使用的 region_code。"""
    result: list[str] = []
    for value in _csv(values):
        code = _resolve_region_code(value)
        if code and code not in result:
            result.append(code)
    return result


def config_snapshot() -> dict[str, Any]:
    return {name: _read(path) for name, path in CONFIG_PATHS.items() if path.exists()}


def save_search_profile(profile_id: str, values: dict[str, Any], *, copy_from: str | None = None) -> dict[str, Any]:
    profile_id = profile_id.strip()
    if not profile_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in profile_id):
        raise ValueError("profile_id 只能包含字母、数字、下划线和短横线")
    payload = _read(CONFIG_PATHS["profiles"])
    profiles = list(payload.get("profiles") or [])
    if copy_from:
        source = next((item for item in profiles if item.get("profile_id") == copy_from), None)
        if source is None:
            raise KeyError(f"复制源 Profile 不存在: {copy_from}")
        item = dict(source)
        item["profile_id"] = profile_id
    else:
        item = next((dict(row) for row in profiles if row.get("profile_id") == profile_id), {"profile_id": profile_id})
    list_fields = {
        "regions", "excluded_regions", "industry_groups", "include_keywords", "exclude_keywords",
        "source_categories", "included_sources", "excluded_sources", "announcement_types",
    }
    for key, value in values.items():
        if key in list_fields:
            item[key] = _csv(value)
        elif key in {"enabled", "discovery_enabled", "wechat_discovery_enabled", "only_active_opportunities", "schedule_enabled"}:
            item[key] = bool(value)
        elif key in {"lookback_days", "max_search_queries_per_run", "max_queries_per_run", "max_queries_per_day", "max_results_per_query", "coverage_budget", "discovery_budget", "enrichment_budget", "verification_budget", "max_verifications_per_session"}:
            item[key] = max(1, int(value))
        elif value is not None:
            item[key] = str(value).strip()
    if "regions" in values:
        item["regions"] = resolve_region_codes(values["regions"])
    if "excluded_regions" in values:
        item["excluded_regions"] = resolve_region_codes(values["excluded_regions"])
    item.setdefault("name", profile_id)
    item.setdefault("enabled", True)
    # Stage 5 is explicitly on-demand. Keep the legacy YAML key for
    # compatibility, but never allow Web settings to turn on a scheduler.
    item["schedule_enabled"] = False
    if not any(row.get("profile_id") == profile_id for row in profiles):
        profiles.append(item)
    else:
        profiles = [item if row.get("profile_id") == profile_id else row for row in profiles]
    payload["profiles"] = profiles
    payload["config_version"] = int(payload.get("config_version", 1)) + 1
    _write(CONFIG_PATHS["profiles"], payload)
    return item


def copy_search_profile(source_id: str, target_id: str, *, name: str | None = None) -> dict[str, Any]:
    return save_search_profile(target_id, {"name": name or target_id}, copy_from=source_id)


def toggle_search_profile(profile_id: str, enabled: bool) -> dict[str, Any]:
    return save_search_profile(profile_id, {"enabled": enabled})


def update_source(source_id: str, *, enabled: bool | None = None, crawl_enabled: bool | None = None) -> dict[str, Any]:
    payload = _read(CONFIG_PATHS["sources"])
    rows = list(payload.get("sources") or [])
    target = next((row for row in rows if row.get("source_id") == source_id), None)
    if target is None:
        raise KeyError(f"来源不存在: {source_id}")
    if enabled is not None:
        target["enabled"] = bool(enabled)
    if crawl_enabled is not None:
        target["crawl_enabled"] = bool(crawl_enabled)
    payload["sources"] = rows
    payload["version"] = int(payload.get("version", 1)) + 1
    _write(CONFIG_PATHS["sources"], payload)
    return target


def update_provider(provider_name: str, *, enabled: bool | None = None) -> dict[str, Any]:
    payload = _read(CONFIG_PATHS["providers"])
    rows = list(payload.get("providers") or [])
    target = next((row for row in rows if row.get("name") == provider_name), None)
    if target is None:
        raise KeyError(f"搜索 Provider 不存在: {provider_name}")
    if enabled is not None:
        target["enabled"] = bool(enabled)
    payload["providers"] = rows
    payload["version"] = int(payload.get("version", 1)) + 1
    _write(CONFIG_PATHS["providers"], payload)
    return target


def save_industry_profile(group_id: str, values: dict[str, Any]) -> dict[str, Any]:
    group_id = group_id.strip()
    if not group_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in group_id):
        raise ValueError("group_id 只能包含字母、数字、下划线和短横线")
    payload = _read(CONFIG_PATHS["industries"])
    rows = list(payload.get("industry_groups") or [])
    item = next((dict(row) for row in rows if row.get("group_id") == group_id), {"group_id": group_id})
    list_fields = {"include", "exclude", "synonyms", "aliases"}
    for key, value in values.items():
        item[key] = _csv(value) if key in list_fields else (int(value) if key == "priority" else str(value).strip())
    item.setdefault("name", group_id)
    item.setdefault("priority", 5)
    if not any(row.get("group_id") == group_id for row in rows):
        rows.append(item)
    else:
        rows = [item if row.get("group_id") == group_id else row for row in rows]
    payload["industry_groups"] = rows
    payload["config_version"] = int(payload.get("config_version", 1)) + 1
    _write(CONFIG_PATHS["industries"], payload)
    return item


__all__ = [
    "CONFIG_PATHS", "config_snapshot", "copy_search_profile", "resolve_region_codes", "save_industry_profile",
    "save_search_profile", "toggle_search_profile", "update_provider", "update_source",
]
