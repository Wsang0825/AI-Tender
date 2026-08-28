"""加载并校验本地 YAML 配置，不包含任何网站抓取逻辑。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = APP_ROOT / "config"


def _as_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} 必须是字符串列表")
    return tuple(item.strip() for item in value if item.strip())


def load_yaml(filename: str, config_dir: Path | None = None) -> dict[str, Any]:
    """读取 app/config 下的 YAML 文件。"""

    path = (config_dir or DEFAULT_CONFIG_DIR) / filename
    with path.open("r", encoding="utf-8") as handle:
        result = yaml.safe_load(handle) or {}
    if not isinstance(result, dict):
        raise ValueError(f"配置文件根节点必须是对象: {path}")
    return result


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s，。、；：:（）()【】\[\]「」《》/\\_-]+", "", value).lower()


def _name_and_aliases(node: Any) -> tuple[str, list[str]]:
    if isinstance(node, str):
        return node, []
    if not isinstance(node, dict) or not node.get("name"):
        raise ValueError(f"区域节点必须包含 name: {node!r}")
    aliases = node.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return str(node["name"]), [str(alias) for alias in aliases]


def _child_nodes(node: dict[str, Any], key: str) -> list[Any]:
    children = node.get(key) or []
    if not isinstance(children, list):
        raise ValueError(f"{key} 必须是列表: {node!r}")
    return children


@dataclass(frozen=True)
class RegionMatch:
    province: str
    city: str | None = None
    county: str | None = None
    matched_text: str = ""
    path: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegionEntry:
    region_code: str
    name: str
    short_name: str | None = None
    aliases: tuple[str, ...] = ()
    level: str = "province"
    parent_code: str | None = None


class RegionCatalog:
    """独立的行政区划目录；搜索范围由 SearchProfile 选择 code。"""

    def __init__(self, entries: Iterable[RegionEntry]):
        self.entries = tuple(entries)
        self._by_code = {entry.region_code: entry for entry in self.entries}
        if len(self._by_code) != len(self.entries):
            raise ValueError("region_catalog.yaml 存在重复 region_code")

    @classmethod
    def from_file(cls, path: Path | None = None) -> "RegionCatalog":
        target = path or (DEFAULT_CONFIG_DIR / "region_catalog.yaml")
        payload = load_yaml(target.name, target.parent)
        raw_entries = payload.get("regions") or []
        if not isinstance(raw_entries, list):
            raise ValueError("region_catalog.yaml 的 regions 必须是列表")
        entries = []
        for item in raw_entries:
            if not isinstance(item, Mapping):
                raise ValueError(f"地区目录项必须是对象: {item!r}")
            entries.append(
                RegionEntry(
                    region_code=str(item["region_code"]),
                    name=str(item["name"]),
                    short_name=str(item.get("short_name") or item["name"]),
                    aliases=_as_tuple(item.get("aliases"), field_name="aliases"),
                    level=str(item.get("level") or "province"),
                    parent_code=str(item["parent_code"]) if item.get("parent_code") else None,
                )
            )
        return cls(entries)

    def get(self, region_code: str) -> RegionEntry:
        try:
            return self._by_code[region_code]
        except KeyError as exc:
            raise KeyError(f"未知行政区划 code: {region_code}") from exc

    def descendants(self, region_code: str) -> tuple[RegionEntry, ...]:
        result: list[RegionEntry] = []
        pending = [region_code]
        while pending:
            parent = pending.pop()
            children = [entry for entry in self.entries if entry.parent_code == parent]
            result.extend(children)
            pending.extend(entry.region_code for entry in children)
        return tuple(result)

    def selected(self, codes: Iterable[str], excluded: Iterable[str] = ()) -> tuple[RegionEntry, ...]:
        excluded_set = set(excluded)
        selected_codes = set(codes)
        for code in tuple(selected_codes):
            selected_codes.update(item.region_code for item in self.descendants(code))
        return tuple(
            entry
            for entry in self.entries
            if entry.region_code in selected_codes and entry.region_code not in excluded_set
        )

    def match(self, text: str | None) -> RegionMatch | None:
        """按全国目录中的名称、简称和别名匹配省/市/县层级。"""

        if not text:
            return None
        normalized = _normalize_text(str(text))
        candidates: list[tuple[int, int, RegionEntry, str]] = []
        level_rank = {"province": 0, "special_region": 0, "city": 1, "prefecture": 1, "county": 2, "district": 2}
        for entry in self.entries:
            labels = (entry.name, entry.short_name or "", *entry.aliases)
            for label in labels:
                label_normalized = _normalize_text(label)
                if label_normalized and label_normalized in normalized:
                    candidates.append((level_rank.get(entry.level, 0), len(label_normalized), entry, label))
        if not candidates:
            return None
        _, _, matched, matched_text = max(candidates, key=lambda item: (item[0], item[1]))
        chain: list[RegionEntry] = [matched]
        parent_code = matched.parent_code
        while parent_code:
            parent = self._by_code.get(parent_code)
            if parent is None:
                break
            chain.append(parent)
            parent_code = parent.parent_code
        chain.reverse()
        province = chain[0].name if chain else matched.name
        city = matched.name if matched.level in {"city", "prefecture"} else next(
            (entry.name for entry in reversed(chain[:-1]) if entry.level in {"city", "prefecture"}), None
        )
        county = matched.name if matched.level in {"county", "district"} else None
        return RegionMatch(
            province=province,
            city=city,
            county=county,
            matched_text=matched_text,
            path=tuple(entry.name for entry in chain),
        )


@dataclass(frozen=True)
class IndustryProfile:
    group_id: str
    name: str
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    priority: int = 5

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.include, *self.synonyms, *self.aliases)))


@dataclass(frozen=True)
class SearchProfile:
    profile_id: str
    name: str
    enabled: bool = True
    regions: tuple[str, ...] = ()
    excluded_regions: tuple[str, ...] = ()
    industry_groups: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    source_categories: tuple[str, ...] = ()
    included_sources: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = ()
    announcement_types: tuple[str, ...] = ()
    lookback_days: int = 30
    discovery_enabled: bool = True
    wechat_discovery_enabled: bool = True
    max_search_queries_per_run: int = 48
    max_queries_per_run: int | None = None
    max_queries_per_day: int = 200
    max_results_per_query: int = 8
    only_active_opportunities: bool = False
    min_source_level: str = "E"
    schedule_enabled: bool = False

    @property
    def query_budget(self) -> int:
        return self.max_queries_per_run or self.max_search_queries_per_run

    def allows_source(self, source_id: str, category: str | None = None) -> bool:
        if self.included_sources and source_id not in self.included_sources:
            return False
        if source_id in self.excluded_sources:
            return False
        return not self.source_categories or (category in self.source_categories)


class IndustryCatalog:
    def __init__(self, profiles: Iterable[IndustryProfile]):
        self.profiles = tuple(profiles)
        self._by_id = {item.group_id: item for item in self.profiles}
        if len(self._by_id) != len(self.profiles):
            raise ValueError("industry_profiles.yaml 存在重复 group_id")

    def get(self, group_id: str) -> IndustryProfile:
        try:
            return self._by_id[group_id]
        except KeyError as exc:
            raise KeyError(f"未知行业关键词组: {group_id}") from exc

    def terms_for(self, group_ids: Iterable[str]) -> tuple[str, ...]:
        terms: list[str] = []
        for group_id in group_ids:
            terms.extend(self.get(group_id).terms)
        return tuple(dict.fromkeys(terms))


class SearchProfileRegistry:
    def __init__(self, profiles: Iterable[SearchProfile]):
        self.profiles = tuple(profiles)
        self._by_id = {item.profile_id: item for item in self.profiles}
        if len(self._by_id) != len(self.profiles):
            raise ValueError("search_profiles.yaml 存在重复 profile_id")

    def get(self, profile_id: str = "northwest_energy") -> SearchProfile:
        try:
            return self._by_id[profile_id]
        except KeyError as exc:
            raise KeyError(f"未知 Search Profile: {profile_id}") from exc

    def enabled(self) -> list[SearchProfile]:
        return [item for item in self.profiles if item.enabled]


class RegionRegistry:
    """面向文本的行政区划注册表，采用最长名称优先匹配。"""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self._paths = self._build_paths()

    @classmethod
    def from_file(cls, path: Path | None = None) -> "RegionRegistry":
        payload = load_yaml((path or (DEFAULT_CONFIG_DIR / "regions.yaml")).name, (path or (DEFAULT_CONFIG_DIR / "regions.yaml")).parent)
        return cls(payload)

    def _build_paths(self) -> list[tuple[str, str | None, str | None, str]]:
        paths: list[tuple[str, str | None, str | None, str]] = []
        groups = list(self.payload.get("provinces") or []) + list(self.payload.get("special_regions") or [])
        for province_node in groups:
            province, province_aliases = _name_and_aliases(province_node)
            province_names = [province, *province_aliases]
            for province_name in province_names:
                paths.append((province, None, None, province_name))
            for city_node in _child_nodes(province_node, "cities"):
                city, city_aliases = _name_and_aliases(city_node)
                for city_name in [city, *city_aliases]:
                    paths.append((province, city, None, city_name))
                for county_node in _child_nodes(city_node, "counties"):
                    county, county_aliases = _name_and_aliases(county_node)
                    for county_name in [county, *county_aliases]:
                        paths.append((province, city, county, county_name))
        return paths

    def counts(self) -> dict[str, int]:
        groups = list(self.payload.get("provinces") or []) + list(self.payload.get("special_regions") or [])
        city_count = 0
        county_count = 0
        for province_node in groups:
            cities = _child_nodes(province_node, "cities")
            city_count += len(cities)
            county_count += sum(len(_child_nodes(city, "counties")) for city in cities)
        return {"province_count": len(groups), "city_count": city_count, "county_count": county_count}

    def iter_regions(self) -> Iterable[tuple[str, str | None, str | None]]:
        groups = list(self.payload.get("provinces") or []) + list(self.payload.get("special_regions") or [])
        for province_node in groups:
            province, _ = _name_and_aliases(province_node)
            for city_node in _child_nodes(province_node, "cities"):
                city, _ = _name_and_aliases(city_node)
                counties = _child_nodes(city_node, "counties")
                if not counties:
                    yield province, city, None
                for county_node in counties:
                    county, _ = _name_and_aliases(county_node)
                    yield province, city, county

    def match(self, text: str | None) -> RegionMatch | None:
        if not text:
            return None
        normalized = _normalize_text(str(text))
        matches = [
            (0 if city is None else 1 if county is None else 2, len(_normalize_text(alias)), province, city, county, alias)
            for province, city, county, alias in self._paths
            if _normalize_text(alias) and _normalize_text(alias) in normalized
        ]
        if not matches:
            return None
        _, _, province, city, county, alias = max(matches, key=lambda item: (item[0], item[1]))
        return RegionMatch(province=province, city=city, county=county, matched_text=alias, path=tuple(item for item in (province, city, county) if item))


def load_keyword_catalog(config_dir: Path | None = None) -> dict[str, list[str]]:
    payload = load_yaml("keywords.yaml", config_dir)
    categories = payload.get("categories") or {}
    if not isinstance(categories, dict):
        raise ValueError("keywords.yaml 的 categories 必须是对象")
    result: dict[str, list[str]] = {}
    seen: set[str] = set()
    for category, terms in categories.items():
        if not isinstance(terms, list) or not all(isinstance(term, str) and term.strip() for term in terms):
            raise ValueError(f"关键词分类格式错误: {category}")
        result[str(category)] = [term.strip() for term in terms]
        seen.update(result[str(category)])
    result["all"] = sorted(seen)
    return result


def load_region_catalog(config_dir: Path | None = None) -> RegionCatalog:
    target_dir = config_dir or DEFAULT_CONFIG_DIR
    return RegionCatalog.from_file(target_dir / "region_catalog.yaml")


def load_industry_profiles(config_dir: Path | None = None) -> IndustryCatalog:
    payload = load_yaml("industry_profiles.yaml", config_dir)
    raw_profiles = payload.get("industry_groups") or payload.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise ValueError("industry_profiles.yaml 的 industry_groups 必须是列表")
    profiles = []
    for item in raw_profiles:
        if not isinstance(item, Mapping) or not item.get("group_id"):
            raise ValueError(f"行业关键词组必须包含 group_id: {item!r}")
        profiles.append(
            IndustryProfile(
                group_id=str(item["group_id"]),
                name=str(item.get("name") or item["group_id"]),
                include=_as_tuple(item.get("include"), field_name="include"),
                exclude=_as_tuple(item.get("exclude"), field_name="exclude"),
                synonyms=_as_tuple(item.get("synonyms"), field_name="synonyms"),
                aliases=_as_tuple(item.get("aliases"), field_name="aliases"),
                priority=int(item.get("priority", 5)),
            )
        )
    return IndustryCatalog(profiles)


def load_search_profiles(config_dir: Path | None = None) -> SearchProfileRegistry:
    payload = load_yaml("search_profiles.yaml", config_dir)
    raw_profiles = payload.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise ValueError("search_profiles.yaml 的 profiles 必须是列表")
    profiles = []
    tuple_fields = {
        "regions", "excluded_regions", "industry_groups", "include_keywords", "exclude_keywords",
        "source_categories", "included_sources", "excluded_sources", "announcement_types",
    }
    for item in raw_profiles:
        if not isinstance(item, Mapping) or not item.get("profile_id"):
            raise ValueError(f"Search Profile 必须包含 profile_id: {item!r}")
        normalized = dict(item)
        for field_name in tuple_fields:
            normalized[field_name] = _as_tuple(normalized.get(field_name), field_name=field_name)
        if "max_queries_per_run" in normalized and "max_search_queries_per_run" not in normalized:
            normalized["max_search_queries_per_run"] = normalized["max_queries_per_run"]
        allowed = set(SearchProfile.__dataclass_fields__)
        normalized = {key: value for key, value in normalized.items() if key in allowed}
        profiles.append(SearchProfile(**normalized))
    return SearchProfileRegistry(profiles)


__all__ = [
    "APP_ROOT",
    "DEFAULT_CONFIG_DIR",
    "IndustryCatalog",
    "IndustryProfile",
    "RegionCatalog",
    "RegionEntry",
    "RegionMatch",
    "RegionRegistry",
    "SearchProfile",
    "SearchProfileRegistry",
    "load_industry_profiles",
    "load_keyword_catalog",
    "load_region_catalog",
    "load_search_profiles",
    "load_yaml",
]
