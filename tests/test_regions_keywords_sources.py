from pathlib import Path

import pytest

from tender_ai.config_loader import RegionRegistry, load_keyword_catalog
from tender_ai.sources.registry import SourceRegistry


@pytest.fixture(scope="module")
def regions():
    return RegionRegistry.from_file()


@pytest.mark.parametrize(
    ("text", "province", "city", "county"),
    [
        ("陕西省西安市雁塔区光伏项目", "陕西", "西安", "雁塔区"),
        ("甘肃省张掖市甘州区储能", "甘肃", "张掖", "甘州区"),
        ("青海省海西州德令哈市", "青海", "海西蒙古族藏族自治州", "德令哈市"),
        ("宁夏回族自治区银川市金凤区", "宁夏", "银川", "金凤区"),
        ("新疆维吾尔自治区喀什地区喀什市", "新疆", "喀什地区", "喀什市"),
        ("新疆哈密伊州区新能源", "新疆", "哈密", "伊州区"),
        ("新疆生产建设兵团第一师阿拉尔市", "新疆生产建设兵团", "第一师", "阿拉尔市"),
        ("兵团第十四师昆玉市项目", "新疆生产建设兵团", "第十四师", "昆玉市"),
    ],
)
def test_region_identification(regions, text, province, city, county):
    match = regions.match(text)
    assert match is not None
    assert (match.province, match.city, match.county) == (province, city, county)


def test_unknown_region_returns_none(regions):
    assert regions.match("华东某项目") is None


def test_region_tree_has_all_six_groups(regions):
    counts = regions.counts()
    assert counts["province_count"] == 6
    assert counts["city_count"] >= 60
    assert counts["county_count"] >= 350


def test_keyword_categories_and_unique_union():
    catalog = load_keyword_catalog()
    assert {"industry", "engineering", "equipment", "procurement_actions", "change_types", "all"} <= set(catalog)
    assert "光伏" in catalog["industry"]
    assert "EPC" in catalog["engineering"]
    assert "电芯" in catalog["equipment"]
    assert "重新招标" in catalog["procurement_actions"]
    assert "延期公告" in catalog["change_types"]
    assert len(catalog["all"]) == len(set(catalog["all"]))


def test_source_registry_has_required_categories_and_fields():
    registry = SourceRegistry.from_file()
    names = {item.source_name for item in registry.definitions}
    assert {"中国政府采购网", "新疆公共资源交易网", "国家电投", "国网ECP"} <= names
    assert len(registry.definitions) >= 30
    assert {item.category for item in registry.definitions} >= {"national", "regional", "enterprise"}
    assert all(item.base_url.startswith("http") for item in registry.definitions)


def test_source_registry_rejects_unknown_source():
    registry = SourceRegistry.from_file()
    with pytest.raises(KeyError):
        registry.get("does-not-exist")


def test_configured_adapter_is_no_network_scaffold():
    registry = SourceRegistry.from_file()
    adapter = registry.adapters()[0]
    assert adapter.search("光伏") == []
    assert adapter.fetch_list("https://example.com") == []
    assert adapter.fetch_detail("https://example.com") is None
    assert adapter.fetch_attachments({}) == []
    assert adapter.health_check().status == "registry_only"
