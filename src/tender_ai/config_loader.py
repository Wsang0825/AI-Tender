"""加载并校验本地 YAML 配置，不包含任何网站抓取逻辑。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = APP_ROOT / "config"


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
