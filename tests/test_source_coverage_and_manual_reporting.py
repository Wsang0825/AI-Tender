from tender_ai.crawlers.runner import SourceCrawlSummary, _emit_manual_action_alert
from tender_ai.config_loader import load_yaml
from tender_ai.discovery.runner import DiscoveryRunner
from tender_ai.discovery.providers import FallbackSearchProvider, SearchProviderError
from tender_ai.search import SearchRequest, build_source_plan
from tender_ai.sources.registry import SourceRegistry


def test_requested_platform_families_are_registered_without_false_online_claims():
    registry = SourceRegistry.from_file()
    by_id = {item.source_id: item for item in registry.definitions}
    catalog_text = "\n".join(
        f"{item.source_name}\n{item.notes or ''}" for item in registry.definitions
    )

    required = {
        "军队采购网",
        "中央国家机关政府采购中心（央采网）",
        "中直机关采购网",
        "政采云",
        "税务采购网",
        "国家电投电子商务平台（电能 e 购）",
        "华能集团电子商务平台",
        "大唐集团电子商务平台",
        "国能 e 购（国家能源集团）",
        "国能 e 招",
        "华润守正电子招标平台",
        "中国电建阳光采购平台",
        "国网电子商务平台 ECP",
        "中国石油招标投标网",
        "中国石化电子招标投标交易平台",
        "中国海油采办业务管理与交易系统",
        "各省级公共资源交易平台",
        "各省级政府采购网",
        "各地市公共资源交易中心网站",
        "各区县公共资源交易中心网站",
        "各省市电子招标投标公共服务平台",
        "各省市交通工程招投标平台",
        "各省市水利工程招投标平台",
        "各省国企阳光采购平台",
        "各省属能源集团电子采购平台",
        "各省市城投集团阳光采购平台",
    }
    assert all(label in catalog_text for label in required)

    for source_id in (
        "provincial_ggzy_catalog",
        "provincial_govprocurement_catalog",
        "city_ggzy_catalog",
        "county_ggzy_catalog",
        "provincial_e_tender_catalog",
        "transport_tender_catalog",
        "water_tender_catalog",
        "provincial_soe_sunshine_catalog",
        "provincial_energy_enterprise_catalog",
        "city_investment_sunshine_catalog",
    ):
        assert by_id[source_id].status == "registry_only"
        assert by_id[source_id].crawl_enabled is False

    assert by_id["ceic_e_zhao"].adapter == "chnenergy_e_zhao"
    assert by_id["ceic_e_zhao"].status == "ACTIVE"
    assert by_id["ceic_e_zhao"].crawl_enabled is True
    assert by_id["china_cdt"].adapter == "datang"
    assert by_id["china_cdt"].status == "ACTIVE"
    assert by_id["powerchina"].adapter == "powerchina"
    assert by_id["powerchina"].status == "ACTIVE"
    assert by_id["shanxi_changzhi_ggzy"].adapter == "shanxi_changzhi_ggzy"


def test_weixin_public_index_is_configured_and_respects_toggle():
    rows = load_yaml("search_providers.yaml").get("providers") or []
    item = next(row for row in rows if row.get("name") == "weixin_public_index")
    assert item["enabled"] is True
    assert DiscoveryRunner._provider_enabled("weixin_public_index") is True


def test_manual_action_is_reported_immediately_once(capsys):
    summary = SourceCrawlSummary("test_source", "测试来源")
    summary.manual_action_required = True
    summary.manual_action_type = "CAPTCHA"
    summary.last_http_status = 412
    summary.error = "检测到验证页"

    _emit_manual_action_alert(summary)
    _emit_manual_action_alert(summary)

    captured = capsys.readouterr()
    assert "测试来源 (test_source)" in captured.err
    assert "CAPTCHA" in captured.err
    assert "HTTP 412" in captured.err
    assert "Microsoft Edge" in captured.err
    assert captured.err.count("人工处理提醒") == 1


def test_discovery_fallback_preserves_manual_verification_signal():
    class BlockedProvider:
        name = "blocked"
        enabled = True

        def search(self, query, *, max_results=10):
            raise SearchProviderError(
                "搜索服务返回验证页",
                manual_action_required=True,
                manual_action_type="VERIFICATION_REQUIRED",
                http_status=412,
            )

    provider = FallbackSearchProvider([BlockedProvider()])
    try:
        provider.search("光伏 招标")
    except SearchProviderError as exc:
        assert exc.manual_action_required is True
        assert exc.manual_action_type == "VERIFICATION_REQUIRED"
        assert exc.http_status == 412
    else:
        raise AssertionError("应保留 Discovery 人工验证信号")


def test_412_sources_are_actionable_manual_items_not_unconfigured():
    plan = build_source_plan(SearchRequest(region="全国", deep=True))
    rows = {item["source_id"]: item for item in plan}
    for source_id in ("chng", "chd"):
        assert rows[source_id]["reason"] == "MANUAL_ACTION_REQUIRED"
        assert rows[source_id]["manual_action_required"] is True
        assert rows[source_id]["manual_action_url"]
        assert rows[source_id]["manual_action_http_status"] == 412
        assert rows[source_id]["browser_profile_path"]
        assert rows[source_id]["manual_browser"] == "Microsoft Edge"
