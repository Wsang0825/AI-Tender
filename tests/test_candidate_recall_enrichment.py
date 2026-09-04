import json
from datetime import timedelta

from sqlalchemy import select

from tender_ai.crawlers.runner import CrawlSummary, SourceCrawlSummary
from tender_ai.candidates import CandidateStore
from tender_ai.discovery.contracts import SearchResult
from tender_ai.enrichment import ENRICHMENT_STRATEGIES, EnrichmentEngine, generate_enrichment_queries
from tender_ai.identity import canonicalize_project_name, resolve_identity
from tender_ai.models import TenderRecord
from tender_ai.search import SearchRequest, SearchRunner, build_coverage_manifest, parse_search_text
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, Candidate, CandidateEnrichmentQuery, CandidateEnrichmentResult, CandidateFact, CandidateSource, SearchSession, SearchSessionProject
from tender_ai.storage.repository import save_tender_record


def test_candidate_pool_preserves_closed_and_unknown_projects(monkeypatch, tmp_path):
    database = tmp_path / "candidate-pool.db"
    engine = initialize_database(create_engine_for(database))
    current = now_shanghai()
    request = SearchRequest(region="甘肃省", industries=("solar",), days=30, result_mode="FULL_RESULT", only_open=True)
    with session_scope(engine) as session:
        closed = save_tender_record(
            session,
            TenderRecord(
                project_id="closed-project",
                project_name="甘肃光伏支架采购项目",
                province="甘肃省",
                publish_time=current - timedelta(days=2),
                document_deadline=current - timedelta(days=1),
                bid_deadline=current + timedelta(days=5),
            ),
        )
        unknown = save_tender_record(
            session,
            TenderRecord(
                project_id="unknown-project",
                project_name="甘肃光伏EPC项目",
                province="甘肃省",
                publish_time=current - timedelta(days=2),
                bid_deadline=current + timedelta(days=5),
            ),
        )
        closed_announcement = Announcement(project_id=closed.project_id, title=closed.project_name, source_url="https://example.test/closed", published_at=current - timedelta(days=2), clean_text="招标文件获取截止已过")
        unknown_announcement = Announcement(project_id=unknown.project_id, title=unknown.project_name, source_url="https://example.test/unknown", published_at=current - timedelta(days=2), clean_text="投标截止时间待公告")
        session.add_all([closed_announcement, unknown_announcement])
        session.flush()
        # The same projects were already delivered by an earlier FULL run.
        # FULL must still return them; this guards the public delivery
        # contract instead of testing only the private suppression helper.
        previous = SearchSession(
            session_id="previous-full-session",
            request_json=json.dumps(request.to_dict(), ensure_ascii=False),
            started_at=current - timedelta(hours=2),
            finished_at=current - timedelta(hours=1),
            status="COMPLETED",
        )
        session.add(previous)
        session.flush()
        session.add_all([
            SearchSessionProject(session_id=previous.session_id, project_id=closed.project_id, announcement_id=closed_announcement.id, status_at_search=closed.status),
            SearchSessionProject(session_id=previous.session_id, project_id=unknown.project_id, announcement_id=unknown_announcement.id, status_at_search=unknown.status),
        ])

    def fake_crawl(self, **_kwargs):
        return CrawlSummary(sources=[SourceCrawlSummary("fixture", "Fixture", status="ACTIVE", query_count=1)])

    monkeypatch.setattr("tender_ai.search.CrawlRunner.run", fake_crawl)
    monkeypatch.setattr("tender_ai.search.ExtractionRunner.run", lambda self, **_kwargs: None)
    summary = SearchRunner(database=str(database)).run(request)

    assert summary.candidate_pool_count >= 2
    with session_scope(engine) as session:
        candidates = session.scalars(select(Candidate)).all()
        assert {item.project_id for item in candidates} >= {"closed-project", "unknown-project"}
        row = session.get(SearchSession, summary.session_id)
        assert row is not None
        assert row.candidate_pool_count >= 2
    payload = json.loads(open(summary.results_path, encoding="utf-8").read())
    assert {item.get("project_id") for item in payload["candidate_pool"]} >= {"closed-project", "unknown-project"}
    assert all(item.get("status") != "UNKNOWN" for item in payload["open_projects"])


def test_search_text_sets_opportunity_mode_and_photovoltaic_concept():
    request = parse_search_text("深度搜索内蒙古最近30天光伏支架项目")
    assert request.search_mode in {"broad", "opportunity"}
    assert request.concept_id == "photovoltaic_support"
    assert request.days == 30
    assert request.deep is True


def test_identity_resolver_removes_price_prefix_without_making_meaningless_query_seed():
    title = "3.31元/瓦丨陕西82.5MW屋顶分布式光伏项目EPC总包中标候选人公示"
    identity = resolve_identity(title, "招标人：陕西能源有限公司")
    assert canonicalize_project_name(title) == "陕西82.5MW屋顶分布式光伏项目EPC总包"
    assert identity.canonical_project_name == "陕西82.5MW屋顶分布式光伏项目EPC总包"
    assert identity.query_seed != "项目"
    assert identity.identity_status == "RESOLVED"


def test_identity_resolver_marks_short_or_numeric_headline_ambiguous():
    identity = resolve_identity("3.31元/瓦丨项目", "")
    assert identity.identity_status == "AMBIGUOUS"
    assert identity.query_seed is None
    candidate = Candidate(candidate_id="short", candidate_key="short", title="项目")
    assert generate_enrichment_queries(candidate) == []


def test_identity_resolver_keeps_project_city_separate_from_agency_address():
    identity = resolve_identity(
        "渭南某光伏支架采购项目招标公告",
        "招标代理机构：西安某工程咨询有限公司。",
    )
    assert identity.province == "陕西省"
    assert identity.city == "渭南市"


def test_candidate_upsert_does_not_erase_enriched_identity_on_sparse_result(tmp_path):
    database = tmp_path / "candidate-merge.db"
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        session.add(SearchSession(session_id="merge-session", request_json="{}"))
        session.flush()
        first = CandidateStore.upsert_search_result(
            session,
            SearchResult(
                title="甘肃河西光伏项目招标公告",
                url="https://official.example.gov.cn/notice/1",
                snippet="项目编号：GS-001；招标人：甘肃能源有限公司",
                provider="fixture",
            ),
            search_session_id="merge-session",
            source_level="A",
        )
        CandidateStore.upsert_search_result(
            session,
            SearchResult(title="项目", url="https://secondary.example/notice/2", snippet="", provider="fixture"),
            search_session_id="merge-session",
            source_level="E",
        )
        # A different URL is intentionally a second candidate; the first
        # candidate's enriched facts must still survive a sparse refresh.
        CandidateStore.upsert_search_result(
            session,
            SearchResult(
                title="甘肃河西光伏项目招标公告",
                url="https://official.example.gov.cn/notice/1",
                snippet="",
                provider="fixture",
            ),
            search_session_id="merge-session",
            source_level="A",
        )
        refreshed = session.get(Candidate, first.candidate_id)
        assert refreshed is not None
        values = json.loads(refreshed.candidate_values_json or "{}")
        assert values["project_code"] == "GS-001"
        assert values["owner"] == "甘肃能源有限公司"
        assert refreshed.identity_status == "RESOLVED"


def test_candidate_source_deduplicates_pending_canonical_urls(tmp_path):
    database = tmp_path / "candidate-source-pending.db"
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        session.add(SearchSession(session_id="source-pending-session", request_json="{}"))
        session.flush()
        candidate = CandidateStore.upsert_search_result(
            session,
            SearchResult(title="待去重项目", url="https://official.example.gov.cn/seed", snippet="光伏招标", provider="fixture"),
            search_session_id="source-pending-session",
            source_level="A",
        )
        duplicate_url = "https://source.example/notice?id=1"
        CandidateStore._link_source(session, candidate, source_url=duplicate_url, source_level="E")
        CandidateStore._link_source(session, candidate, source_url=duplicate_url, source_level="E")
        session.flush()
        rows = list(session.scalars(select(CandidateSource).where(CandidateSource.candidate_id == candidate.candidate_id, CandidateSource.canonical_url == duplicate_url)).all())
        assert len(rows) == 1


def test_coverage_manifest_does_not_call_unconfigured_source_success():
    manifest = build_coverage_manifest(
        [
            {"source_id": "ready", "source_name": "Ready", "selected": True, "reason": "READY"},
            {"source_id": "missing", "source_name": "Missing", "selected": False, "reason": "ADAPTER_NOT_CONFIGURED", "reason_detail": "not configured"},
        ],
        [{"source_id": "ready", "status": "ACTIVE", "query_count": 2}],
        query_count=2,
        candidate_yield=3,
    )
    assert [item["source_id"] for item in manifest["successful"]] == ["ready"]
    assert [item["source_id"] for item in manifest["adapter_not_configured"]] == ["missing"]
    assert manifest["candidate_yield"] == 3


class _Provider:
    name = "fixture-provider"
    priority = 1
    enabled = True

    def __init__(self):
        self.calls = []

    def search(self, query, *, max_results=10):
        self.calls.append(query)
        return [
            SearchResult(
                title="甘肃河西光伏项目招标公告",
                url="https://official.gov.cn/notice/solar-1",
                snippet="项目名称：甘肃河西光伏项目；招标人：甘肃能源有限公司；招标编号：GS-2026-001",
                provider=self.name,
            )
        ]

    def close(self):
        return None


def test_recursive_enrichment_persists_queries_facts_sources_and_cache(tmp_path):
    database = tmp_path / "enrichment.db"
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        # Candidate and enrichment-query rows intentionally reference a real
        # SearchSession.  Keep the FK contract strict instead of weakening it
        # merely to make a fixture accept an arbitrary session id.
        session.add_all([
            SearchSession(session_id="seed-session", request_json="{}"),
            SearchSession(session_id="enrich-session", request_json="{}"),
            SearchSession(session_id="enrich-session-2", request_json="{}"),
        ])
        session.flush()
        seed = CandidateStore.upsert_search_result(
            session,
            SearchResult(
                title="甘肃河西光伏项目",
                url="https://secondary.example/lead/1",
                snippet="光伏项目招标线索",
                provider="fixture",
            ),
            search_session_id="seed-session",
            source_level="E",
            region="甘肃省",
        )
        candidate_id = seed.candidate_id

    provider = _Provider()
    first = EnrichmentEngine(database=str(database), provider=provider).run(candidate_id, search_session_id="enrich-session", max_queries=24, max_results=2)
    assert first.query_count >= 3
    assert first.result_count >= 1
    assert first.new_fact_count >= 1
    assert set(ENRICHMENT_STRATEGIES) >= {"FULL_NAME", "NAME_CHANGE", "NAME_EXTENSION"}
    with session_scope(engine) as session:
        candidate = session.get(Candidate, candidate_id)
        assert candidate is not None
        assert candidate.official_found is True
        query_rows = session.scalars(select(CandidateEnrichmentQuery).where(CandidateEnrichmentQuery.candidate_id == candidate_id)).all()
        result_rows = session.scalars(select(CandidateEnrichmentResult).where(CandidateEnrichmentResult.candidate_id == candidate_id)).all()
        fact_rows = session.scalars(select(CandidateFact).where(CandidateFact.candidate_id == candidate_id)).all()
        source_rows = session.scalars(select(CandidateSource).where(CandidateSource.candidate_id == candidate_id)).all()
        assert len(query_rows) == first.query_count
        assert result_rows
        assert fact_rows
        assert any(row.is_official for row in source_rows)

    second_provider = _Provider()
    second = EnrichmentEngine(database=str(database), provider=second_provider).run(candidate_id, search_session_id="enrich-session-2", max_queries=3, max_results=2)
    assert second.skipped_cached_queries >= 1
    assert second_provider.calls == []
