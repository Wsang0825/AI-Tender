from datetime import datetime

from tender_ai.crawlers.http import HttpResponse
from tender_ai.discovery.contracts import SearchResult
from tender_ai.discovery.runner import DiscoveryRunner, DiscoverySummary, official_trace_queries
from tender_ai.sources.adapters import ChnEnergyEZhaoAdapter, DatangAdapter, ShanxiChangzhiGGZYAdapter
from tender_ai.sources.registry import SourceDefinition
from tender_ai.status.time import SHANGHAI_TZ


def _definition(source_id: str, base_url: str) -> SourceDefinition:
    return SourceDefinition(
        source_id=source_id,
        source_name=source_id,
        category="enterprise",
        base_url=base_url,
        adapter="configured",
    )


def _response(url: str, body: str, content_type: str = "text/html; charset=utf-8") -> HttpResponse:
    return HttpResponse(
        url=url,
        status_code=200,
        headers={"content-type": content_type},
        content=body.encode("utf-8"),
        fetched_at=datetime(2026, 8, 29, 12, tzinfo=SHANGHAI_TZ),
    )


def test_chnenergy_static_list_parser_extracts_detail_and_code(monkeypatch):
    adapter = ChnEnergyEZhaoAdapter(_definition("ceic_e_zhao", "https://www.chnenergybidding.com.cn/bidweb/"))
    html = """
    <ul class="right-items">
      <li class="right-item"><a class="infolink" title="哈密光伏项目组件采购招标公告" href="/bidweb/001/001002/001002001/20260829/a.html">占位</a><span class="author">CEZB260801</span><span>2026-08-29</span></li>
    </ul>
    """
    monkeypatch.setattr(adapter, "_get", lambda *args, **kwargs: _response(args[0], html))
    rows = adapter.fetch_list("https://www.chnenergybidding.com.cn/bidweb/001/001002/001002001/moreinfo.html")
    assert len(rows) == 1
    assert rows[0].title == "哈密光伏项目组件采购招标公告"
    assert rows[0].url.endswith("/20260829/a.html")
    assert rows[0].metadata["tendercode"] == "CEZB260801"
    assert rows[0].published_at is not None


def test_datang_public_api_list_and_detail_parser(monkeypatch):
    adapter = DatangAdapter(_definition("china_cdt", "https://www.cweme.cn/"))
    list_payload = '{"data":[{"id":"1001","message_title":"甘肃储能项目招标公告","publish_time":"2026-08-29 10:00:00","deadline":"2026-09-10 09:00:00","message_no":"DT-1001","bid_tenderer":"大唐甘肃公司"}]}'
    detail_payload = '{"data":{"message_title":"甘肃储能项目招标公告","publish_time":"2026-08-29 10:00:00","deadline":"2026-09-10 09:00:00","message_no":"DT-1001","bid_tenderer":"大唐甘肃公司","pro_overvier":"储能系统采购","pdf_url":"https://www.cweme.cn/files/1001.pdf"}}'
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("data")))
        return _response(url, detail_payload if url.endswith("fzggDetail") else list_payload, "application/json; charset=utf-8")

    monkeypatch.setattr(adapter.http, "request", fake_request)
    rows = adapter.fetch_list(adapter.detail_page, query="储能", page=1, since_days=30, message_type="0")
    detail = adapter.fetch_detail(rows[0].url)
    assert rows[0].metadata["tendercode"] == "DT-1001"
    assert detail is not None
    assert detail.metadata["owner"] == "大唐甘肃公司"
    assert any(link.url.endswith("1001.pdf") for link in detail.attachments)
    assert [item[2]["id"] for item in calls if item[2] and "id" in item[2]] == ["1001"]


def test_changzhi_html_parser_uses_public_detail_links(monkeypatch):
    adapter = ShanxiChangzhiGGZYAdapter(_definition("shanxi_changzhi_ggzy", "https://ggzy.changzhi.gov.cn/"))
    html = "<ul><li><a href='/front/notice/detail?id=9&xmlx=JSGC'>长治光伏工程招标公告</a><span>2026-08-28</span></li></ul>"
    monkeypatch.setattr(adapter, "_get", lambda *args, **kwargs: _response(args[0], html))
    rows = adapter.fetch_list("https://ggzy.changzhi.gov.cn/front/notice/list?type=ZBGG&xmlx=JSGC")
    assert len(rows) == 1
    assert rows[0].url.startswith("https://ggzy.changzhi.gov.cn/front/notice/detail?id=9")


def test_secondary_trace_queries_include_project_identity_and_official_intent():
    result = SearchResult(
        title="某某储能项目招标公告 - 招标网",
        url="https://www.example-secondary.cn/a",
        snippet="项目编号：ABC-2026-001；招标人：某某能源公司",
        provider="ddgs",
    )
    queries = official_trace_queries(result)
    assert queries
    assert any("ABC-2026-001" in item and "招标公告" in item for item in queries)
    assert any("某某储能项目" in item for item in queries)


def test_secondary_lead_trace_records_official_match_without_promoting_lead(tmp_path):
    class Session:
        def __init__(self):
            self.rows = []

        def scalar(self, _query):
            return None

        def add(self, row):
            self.rows.append(row)

    class Provider:
        name = "fake"

        def search(self, query, *, max_results=10):
            return [SearchResult(
                title="某某储能项目招标公告",
                url="https://official.example.gov.cn/notice/1",
                snippet="项目编号 ABC-2026-001 招标公告",
                provider="fake",
            )]

    lead = SearchResult(
        title="某某储能项目招标公告 - 招标网",
        url="https://secondary.example.com/a",
        snippet="项目编号：ABC-2026-001",
        provider="fake",
    )
    summary = DiscoverySummary()
    session = Session()
    runner = DiscoveryRunner(database=tmp_path / "trace.db")
    payload = runner._trace_secondary_lead(
        session,
        Provider(),
        lead,
        "甘肃省",
        {lead.url},
        summary,
        set(),
        {"official.example.gov.cn"},
        5,
    )
    assert payload["status"] == "FOUND_OFFICIAL"
    assert summary.secondary_official_match_count == 1
    assert summary.secondary_trace_matches[0]["url"].startswith("https://official.example.gov.cn")
    assert summary.secondary_unresolved_count == 0
