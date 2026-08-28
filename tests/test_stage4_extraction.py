from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from tender_ai.config_loader import RegionRegistry, load_region_catalog
from tender_ai.discovery.contracts import SearchResult
from tender_ai.documents import parser as document_parser
from tender_ai.documents.parser import ParsedDocument, parse_docx, parse_path, parse_xlsx
from tender_ai.documents.quality import DocumentQuality, document_quality_score
from tender_ai.evidence.models import EvidenceRecord
from tender_ai.extractors.runner import _field_conflicts
from tender_ai.extractors.tender import _date_values, normalize_detail
from tender_ai.matching.dedupe import consolidate_projects
from tender_ai.models import TenderRecord
from tender_ai.review import ensure_review_item, resolve_review_item
from tender_ai.search import parse_search_text
from tender_ai.snapshots.store import SnapshotStore
from tender_ai.sources.contracts import DetailPayload
from tender_ai.sources.registry import SourceDefinition
from tender_ai.status.metadata import describe_time
from tender_ai.status.time import SHANGHAI_TZ, parse_datetime
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, CodexReviewItem, DocumentParse, Evidence, FieldConflict, Project, SearchSession, SearchSessionProject, VerificationTask
from tender_ai.storage.repository import clear_manual_override, save_evidence, save_manual_override, save_tender_record
from tender_ai.verification.runner import VerificationRunner


def _definition() -> SourceDefinition:
    return SourceDefinition(source_id="fixture", source_name="Fixture source", category="regional", base_url="https://example.com")


def test_rule_extraction_scopes_schedule_fields_and_records_evidence(tmp_path):
    html = (
        "<h1>哈密储能EPC项目招标公告</h1>"
        "<p>项目名称：哈密储能EPC项目</p>"
        "<p>招标人：新疆能源有限公司</p>"
        "<p>招标代理机构：西北招标代理有限公司</p>"
        "<p>报名时间：2026年8月27日09:00至2026年9月5日17:00</p>"
        "<p>文件获取截止时间：2026年9月6日17:00</p>"
        "<p>投标截止时间：2026年9月10日17:00</p>"
        "<p>开标时间：2026年9月10日17:00</p>"
    )
    fixture = tmp_path / "notice.html"
    fixture.write_text(html, encoding="utf-8")
    document = parse_path(fixture, source_url="https://example.com/notice", expected_terms=("报名", "截止"))
    result = normalize_detail(
        DetailPayload(title="哈密储能EPC项目招标公告", url="https://example.com/notice", html=html, text=document.text),
        _definition(),
        RegionRegistry.from_file(),
        document=document,
        source_file=str(fixture),
        parser="fixture.html",
    )

    assert result.record.agency == "西北招标代理有限公司"
    assert result.record.registration_start == datetime(2026, 8, 27, 9, tzinfo=result.record.registration_start.tzinfo)
    assert result.record.registration_deadline == datetime(2026, 9, 5, 17, tzinfo=result.record.registration_deadline.tzinfo)
    assert result.record.document_deadline == datetime(2026, 9, 6, 17, tzinfo=result.record.document_deadline.tzinfo)
    assert result.record.bid_deadline == datetime(2026, 9, 10, 17, tzinfo=result.record.bid_deadline.tzinfo)
    assert result.record.status.value == "OPEN"
    assert result.record.status_reason == "OPEN_REGISTRATION_ACTIVE"
    evidence = {item.field_name: item for item in result.evidences}
    assert {"registration_deadline", "document_deadline", "bid_deadline", "open_time"} <= evidence.keys()
    assert evidence["bid_deadline"].source_file == str(fixture)
    assert evidence["bid_deadline"].page_number == 1


def test_date_only_metadata_is_inferred_without_fabricating_explicit_clock():
    value = datetime(2026, 9, 5, 17, tzinfo=SHANGHAI_TZ)
    metadata = describe_time("document_deadline", value, "截至2026年9月5日")
    assert metadata is not None
    assert metadata.precision == "DATE_ONLY"
    assert metadata.explicit_or_inferred == "INFERRED"
    assert metadata.inference_rule


def test_partial_month_day_and_national_region_are_supported():
    parsed = parse_datetime("9月5日下午5时")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 9 and parsed.day == 5 and parsed.hour == 17
    values = _date_values("报名截止：9月5日；投标截止：9月10日")
    assert [value.date().isoformat() for value in values] == ["2026-09-05", "2026-09-10"]
    match = load_region_catalog().match("内蒙古自治区鄂尔多斯市")
    assert match is not None
    assert match.province == "内蒙古自治区"
    assert match.city == "鄂尔多斯市"


def test_explicit_participation_rule_can_open_without_registration_window(tmp_path):
    html = "<h1>储能设备采购公告</h1><p>投标截止时间：2099-09-10 17:00</p><p>招标文件可在投标截止时间前自行下载。</p>"
    fixture = tmp_path / "participation.html"
    fixture.write_text(html, encoding="utf-8")
    document = parse_path(fixture, source_url="https://example.com/participation", expected_terms=())
    result = normalize_detail(
        DetailPayload(title="储能设备采购公告", url="https://example.com/participation", html=html, text=html),
        _definition(),
        RegionRegistry.from_file(),
        document=document,
    )
    assert result.record.participation_method
    assert result.record.status.value == "OPEN"
    assert result.record.status_reason == "OPEN_PARTICIPATION_ACTIVE"


def test_docx_and_xlsx_parsers_return_structured_text(tmp_path):
    docx_path = tmp_path / "notice.docx"
    Document = __import__("docx").Document
    docx = Document()
    docx.add_paragraph("项目名称：风电设备采购")
    table = docx.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "投标截止时间"
    table.rows[0].cells[1].text = "2026-09-10 17:00"
    docx.save(docx_path)
    parsed_docx = parse_docx(docx_path, source_url="https://example.com/notice.docx")
    assert parsed_docx.parser == "python-docx"
    assert "投标截止时间" in parsed_docx.text
    assert parsed_docx.tables

    xlsx_path = tmp_path / "notice.xlsx"
    Workbook = __import__("openpyxl").Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "公告"
    sheet.append(["项目名称", "储能系统采购"])
    sheet.append(["开标时间", "2026-09-10 17:00"])
    workbook.save(xlsx_path)
    parsed_xlsx = parse_xlsx(xlsx_path, source_url="https://example.com/notice.xlsx")
    assert parsed_xlsx.parser == "openpyxl"
    assert "储能系统采购" in parsed_xlsx.text
    assert parsed_xlsx.page_count == 1


def test_pdf_quality_is_0_to_100_and_mineru_is_a_fallback(monkeypatch, tmp_path):
    assert 0 <= document_quality_score("项目名称：储能项目 招标文件获取截止时间") <= 100
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        document_parser,
        "extract_pdf",
        lambda *_args, **_kwargs: DocumentQuality(score=4, text="", parser="pymupdf4llm", needs_mineru=True, pages=()),
    )
    monkeypatch.setattr(
        document_parser,
        "parse_mineru",
        lambda *_args, **_kwargs: ParsedDocument(source_url="https://example.com/a.pdf", source_file=str(pdf_path), content_type="application/pdf", parser="mineru", text="项目名称：储能项目", pages=(), quality_score=70, used_mineru=True, content_hash="hash"),
    )
    parsed = document_parser.parse_path(pdf_path, source_url="https://example.com/a.pdf")
    assert parsed.used_mineru is True
    assert parsed.quality_score == 70


def test_snapshot_replay_reextracts_offline(tmp_path):
    database = tmp_path / "replay.db"
    engine = initialize_database(create_engine_for(database))
    html = "<h1>离线储能项目招标公告</h1><p>招标人：甲能源</p><p>投标截止时间：2099-09-10 17:00</p><p>开标时间：2099-09-10 17:00</p>"
    with session_scope(engine) as session:
        project = save_tender_record(session, TenderRecord(project_id="replay-project", project_name="离线储能项目"))
        announcement = Announcement(project_id=project.project_id, title="离线储能项目招标公告", source_url="https://example.com/replay", canonical_url="https://example.com/replay", raw_content=html, clean_text=html)
        session.add(announcement)
        session.flush()
        SnapshotStore(tmp_path / "snapshots").save_text(session, source_url=announcement.source_url, text=html, content_type="text/html", announcement_id=announcement.id)
    from tender_ai.replay import ReplayRunner

    summary = ReplayRunner(database=str(database)).run()
    assert summary.snapshot_count == 1
    assert summary.replayed_count == 1
    assert summary.failed_count == 0
    with session_scope(engine) as session:
        assert session.scalar(select(DocumentParse).where(DocumentParse.project_id == "replay-project")) is not None


def test_codex_review_item_is_cached_by_content_hash(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "review.db"))
    with session_scope(engine) as session:
        project = save_tender_record(session, TenderRecord(project_id="review-project", project_name="待确认项目"))
        announcement = Announcement(project_id=project.project_id, title="待确认项目公告", source_url="https://example.com/review", content_hash="content-1")
        session.add(announcement)
        session.flush()
        first = ensure_review_item(session, project, announcement=announcement)
        assert first is not None and first.status == "PENDING"
        resolve_review_item(session, first.review_id, status="RESOLVED", resolution="已阅读原文")
        second = ensure_review_item(session, project, announcement=announcement)
        assert second is not None
        assert second.review_id == first.review_id
        assert second.status == "RESOLVED"


def test_evidence_conflict_creates_field_conflict(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "conflict.db"))
    with session_scope(engine) as session:
        project = save_tender_record(session, TenderRecord(project_id="conflict-project", project_name="冲突项目"))
        announcement = Announcement(project_id=project.project_id, title="冲突项目公告")
        session.add(announcement)
        session.flush()
        first = save_evidence(session, EvidenceRecord(field_name="bid_deadline", normalized_value="2026-09-05T17:00:00+08:00", raw_value="9月5日", source_text="投标截止：9月5日", extractor="rule"), project_id=project.project_id, announcement_id=announcement.id)
        second = save_evidence(session, EvidenceRecord(field_name="bid_deadline", normalized_value="2026-09-10T17:00:00+08:00", raw_value="9月10日", source_text="投标截止：9月10日", extractor="rule"), project_id=project.project_id, announcement_id=announcement.id)
        count, date_conflict = _field_conflicts(session, project_id=project.project_id, announcement_id=announcement.id, evidence_rows={"bid_deadline": [first, second]})
        assert count == 1
        assert date_conflict is True
        row = session.scalar(select(FieldConflict).where(FieldConflict.project_id == project.project_id))
        assert row is not None and row.resolution_status == "PENDING"


def test_same_evidence_is_idempotent_when_locator_metadata_improves(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "evidence.db"))
    with session_scope(engine) as session:
        project = save_tender_record(session, TenderRecord(project_id="evidence-project", project_name="证据项目"))
        announcement = Announcement(project_id=project.project_id, title="证据项目公告", source_url="https://example.com/a")
        session.add(announcement)
        session.flush()
        first = save_evidence(
            session,
            EvidenceRecord(field_name="bid_deadline", normalized_value="2026-09-10T17:00:00+08:00", raw_value="2026年9月10日17时", source_url="https://example.com/a", source_text="投标截止时间：2026年9月10日17时", extractor="rule"),
            project_id=project.project_id,
            announcement_id=announcement.id,
        )
        second = save_evidence(
            session,
            EvidenceRecord(field_name="bid_deadline", normalized_value="2026-09-10T17:00:00+08:00", raw_value="2026年9月10日17时", source_url="https://example.com/a", source_file="D:/snapshot.html", page_number=1, source_text="投标截止时间：2026年9月10日17时", extractor="rule"),
            project_id=project.project_id,
            announcement_id=announcement.id,
        )
        assert first.id == second.id
        assert second.page_number == 1
        assert second.source_file == "D:/snapshot.html"


def test_probable_match_is_not_automatically_merged(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "dedupe.db"))
    with session_scope(engine) as session:
        save_tender_record(session, TenderRecord(project_id="p1", project_name="新疆某地集中式光伏发电项目", owner="甲能源", province="新疆", city="哈密", capacity_mw=100))
        save_tender_record(session, TenderRecord(project_id="p2", project_name="新疆某地集中式光伏发电项目招标公告", owner="甲能源", province="新疆", city="哈密", capacity_mw=100.4))
        assert consolidate_projects(session) == 0
        assert session.get(Project, "p1") is not None
        assert session.get(Project, "p2") is not None


def test_search_parser_and_session_status_snapshot(tmp_path):
    request = parse_search_text("内蒙古最近30天光伏储能，只看还能参与的")
    assert request.region == "内蒙古自治区"
    assert request.days == 30
    assert request.industries == ("solar", "storage")
    assert request.only_open is True

    engine = initialize_database(create_engine_for(tmp_path / "session.db"))
    with session_scope(engine) as session:
        save_tender_record(session, TenderRecord(project_id="session-project", project_name="Session项目"))
        session.add(SearchSession(session_id="session-1", request_json="{}", status="COMPLETED"))
        session.add(SearchSessionProject(session_id="session-1", project_id="session-project", status_at_search="UNKNOWN", is_new=True, is_updated=False))
    with session_scope(engine) as session:
        row = session.scalar(select(SearchSessionProject).where(SearchSessionProject.session_id == "session-1"))
        assert row is not None and row.status_at_search == "UNKNOWN" and row.is_new is True


def test_manual_override_round_trip_and_verification(tmp_path):
    database = tmp_path / "verification.db"
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        project = save_tender_record(session, TenderRecord(project_id="verify-project", project_name="核验项目", owner="甲公司"))
        override = save_manual_override(session, project.project_id, "owner", "Codex确认业主", reason="原文明确")
        assert override.automatic_value == "甲公司"
        assert override.manual_value == "Codex确认业主"
        save_tender_record(session, TenderRecord(project_id="verify-project", project_name="核验项目", owner="新自动业主"))
        assert session.get(Project, "verify-project").owner == "Codex确认业主"
        assert override.automatic_value == "新自动业主"
    with session_scope(engine) as session:
        clear_manual_override(session, "verify-project", "owner")
        assert session.get(Project, "verify-project").owner == "新自动业主"

    class StubProvider:
        def search(self, query: str, *, max_results: int = 5):
            return [SearchResult(title="核验项目延期公告", url="https://example.com/extension", snippet="延期", provider="stub")]

        def close(self):
            return None

    summary = VerificationRunner(database=str(database), provider=StubProvider()).run(project_id="verify-project", max_results=2)
    assert summary.tasks_examined >= 1
    with session_scope(engine) as session:
        assert session.scalar(select(VerificationTask).where(VerificationTask.project_id == "verify-project")) is not None
