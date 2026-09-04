import json

from tender_ai.discovery.contracts import SearchResult
from tender_ai.discovery.leads import build_valuable_lead, classify_valuable_lead
from tender_ai.search import SearchRequest, SearchRunner
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import SearchSession


def _result(title: str, snippet: str) -> SearchResult:
    return SearchResult(
        title=title,
        url="https://secondary.example.test/notice/1",
        snippet=snippet,
        provider="ddgs",
        published_at="2026-08-29",
    )


def test_epc_project_is_reported_but_not_promoted_to_direct_rack_procurement():
    lead = classify_valuable_lead(
        _result(
            "当雄800MW光伏+100MW光热一体化EPC",
            "项目级工程总承包，支架采购可能嵌入EPC设备材料范围。",
        )
    )
    assert lead is not None
    assert lead["lead_type"] == "PROJECT_LEVEL_EPC"
    assert lead["direct_component_rack_procurement"] is False
    assert "设备材料" in lead["scope_warning"]


def test_early_stage_project_is_reported_for_follow_up():
    lead = classify_valuable_lead(
        _result(
            "昌都江达50MW光伏项目",
            "项目处于可研及地形测绘采购阶段，适合作为前期跟踪项目。",
        )
    )
    assert lead is not None
    assert lead["lead_type"] == "EARLY_STAGE_TRACKING"
    assert lead["follow_up_queries"]


def test_completed_project_installation_signal_is_not_current_procurement():
    lead = classify_valuable_lead(
        _result(
            "山南曲松罗布沙一期200MW光伏项目",
            "项目已竣工验收，公开消息提到支架安装，当前窗口未发现新的支架采购公告。",
        )
    )
    assert lead is not None
    assert lead["lead_type"] == "COMPLETED_INSTALLATION_SIGNAL"
    assert lead["direct_component_rack_procurement"] is False
    assert "新的支架采购" in lead["scope_warning"]


def test_negative_rack_procurement_phrase_does_not_hide_completed_lead():
    lead = classify_valuable_lead(
        _result(
            "某200MW光伏项目已竣工验收",
            "公开消息提到支架安装，未发现新的光伏支架采购公告。",
        )
    )
    assert lead is not None
    assert lead["lead_type"] == "COMPLETED_INSTALLATION_SIGNAL"


def test_adjacent_box_transformer_platform_is_separated_from_component_rack():
    lead = build_valuable_lead(
        _result(
            "班戈20MW、双湖10MW光伏项目箱变用钢结构平台采购",
            "采购内容为箱变平台，属于相近结构件，不是光伏组件支架。",
        ),
        canonical_url="https://secondary.example.test/notice/1",
        source_level="D",
        region="西藏自治区",
    )
    assert lead is not None
    assert lead["lead_type"] == "ADJACENT_STRUCTURE"
    assert lead["source_level"] == "D"
    assert lead["region"] == "西藏自治区"
    assert "不是光伏组件支架" in lead["reason"]


def test_explicit_component_rack_procurement_uses_normal_project_path():
    lead = classify_valuable_lead(
        _result(
            "哈密光伏支架采购招标公告",
            "采购内容为光伏组件固定支架。",
        )
    )
    assert lead is None


def test_unchanged_valuable_lead_is_suppressed_in_same_search_scope(tmp_path):
    database = tmp_path / "valuable-leads.db"
    engine = initialize_database(create_engine_for(database))
    request = SearchRequest(region="西藏自治区", industries=("solar",), days=30, deep=True)
    lead = {
        "lead_type": "ADJACENT_STRUCTURE",
        "canonical_url": "https://secondary.example.test/notice/1",
        "source_url": "https://secondary.example.test/notice/1",
        "source_text": "箱变用钢结构平台采购",
        "source_title": "班戈光伏项目箱变平台采购",
    }
    with session_scope(engine) as session:
        session.add(
            SearchSession(
                session_id="previous-lead-session",
                request_json=json.dumps(request.to_dict(), ensure_ascii=False),
                started_at=now_shanghai(),
                finished_at=now_shanghai(),
                status="COMPLETED",
                sources_json=json.dumps([{"provider": "discovery", "valuable_leads": [lead]}], ensure_ascii=False),
            )
        )

    runner = SearchRunner(database=str(database))
    with session_scope(runner.engine) as session:
        visible, suppressed = runner._filter_repeated_unchanged_valuable_leads(
            session, request, "current-lead-session", [lead]
        )
    assert visible == []
    assert suppressed == 1
