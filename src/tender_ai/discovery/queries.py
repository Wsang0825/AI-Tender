"""有限预算、可轮换的 Discovery 查询计划。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tender_ai.config_loader import load_industry_profiles, load_region_catalog, load_search_profiles


@dataclass(frozen=True)
class DiscoveryQuery:
    text: str
    category: str
    region: str | None
    priority: int


ACTION_TERMS = ("招标", "采购", "询比", "资格预审", "EPC")
OBJECT_TERMS = ("项目", "组件", "逆变器", "储能系统", "光伏支架", "运维")


def generate_discovery_queries(
    *,
    max_queries: int = 96,
    rotation_day: int | None = None,
    profile_id: str = "northwest_energy",
) -> list[DiscoveryQuery]:
    """从 Profile 和关键词组生成有限查询，不展开无上限笛卡尔积。"""

    profile = load_search_profiles().get(profile_id)
    if not profile.discovery_enabled:
        return []
    catalog = load_region_catalog()
    industries = load_industry_profiles()
    selected = catalog.selected(profile.regions, profile.excluded_regions)
    regions = [item.short_name or item.name for item in selected if item.level in {"province", "city", "prefecture", "special_region"}]
    # 省级 Profile 默认只取省名和有限重点城市，长尾地区靠后续轮换覆盖。
    if not regions:
        regions = list(profile.regions) or ["全国"]
    region_deduped = list(dict.fromkeys(regions))
    terms = list(industries.terms_for(profile.industry_groups)) + list(profile.include_keywords)
    terms = [term for term in dict.fromkeys(terms) if term and term not in profile.exclude_keywords]
    if not terms:
        terms = ["新能源"]
    day = date.today().toordinal() if rotation_day is None else rotation_day
    offset = day % len(region_deduped)
    rotated = region_deduped[offset:] + region_deduped[:offset]
    budget = min(max_queries, profile.query_budget)
    # 为官方 PDF、公众号和公开站点保留预算，避免它们被基础组合耗尽。
    extras = (
        ('site:gov.cn "光伏" "招标" filetype:pdf', "official_pdf", None, 2),
        ('site:gov.cn "储能系统" "采购公告"', "official_site", None, 2),
        ('site:mp.weixin.qq.com "新能源" "招标"', "weixin_candidate", None, 3),
        ('"新能源" "招标公告" filetype:pdf', "regional_pdf", None, 3),
    )
    if not profile.wechat_discovery_enabled:
        extras = tuple(item for item in extras if item[1] != "weixin_candidate")
    base_limit = max(0, budget - len(extras))
    queries: list[DiscoveryQuery] = []
    seen: set[str] = set()
    for index, region in enumerate(rotated):
        for industry in terms[:8]:
            action = ACTION_TERMS[(index + day) % len(ACTION_TERMS)]
            obj = OBJECT_TERMS[(index + len(industry)) % len(OBJECT_TERMS)]
            text = f'"{region}" "{industry}" "{action}" "{obj}"'
            if any(excluded.casefold() in text.casefold() for excluded in profile.exclude_keywords):
                continue
            if text in seen:
                continue
            seen.add(text)
            queries.append(DiscoveryQuery(text, "region_industry_action_object", region, 1 + index // 5))
            if len(queries) >= base_limit:
                break
        if len(queries) >= base_limit:
            break
    for text, category, region, priority in extras:
        if len(queries) >= budget:
            break
        if text not in seen:
            seen.add(text)
            queries.append(DiscoveryQuery(text, category, region, priority))
    return queries


__all__ = ["DiscoveryQuery", "generate_discovery_queries"]
