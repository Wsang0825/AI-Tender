import io
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from tender_ai.candidate_documents import process_candidate_enrichment_result
from tender_ai.candidates import CandidateStore
from tender_ai.crawlers.http import HttpResponse
from tender_ai.discovery.contracts import SearchResult
from tender_ai.models import TenderRecord
from tender_ai.search import SearchRequest, normalize_result_mode, parse_search_text, resolve_result_mode
from tender_ai.status.engine import TenderStatus, evidence_gate, recalculate_status
from tender_ai.status.time import SHANGHAI_TZ
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import CandidateAttachment, CandidateEnrichmentQuery, CandidateEnrichmentResult, DocumentParse, SearchSession, Snapshot
from tender_ai.snapshots.store import SnapshotStore


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=SHANGHAI_TZ)


def ev(field_name: str, value: str, extractor_type: str = "DIRECT_TEXT") -> dict[str, str]:
    return {
        "field_name": field_name,
        "normalized_value": value,
        "raw_value": value,
        "source_url": "https://official.example/notice/1",
        "source_text": f"{field_name}：{value}",
        "extractor_type": extractor_type,
    }


def test_weak_deadline_evidence_cannot_force_closed():
    record = TenderRecord(project_name="项目", document_deadline="2026-08-20 17:00", bid_deadline="2026-09-10 09:00")
    decision = recalculate_status(record, NOW, evidences=[ev("document_deadline", "2026-08-20", "INFERRED"), ev("bid_deadline", "2026-09-10")], require_evidence=True)
    assert decision.status is TenderStatus.UNKNOWN
    assert decision.reason_code == "UNKNOWN_NEEDS_CODEX_REVIEW"
    assert "document_deadline" in decision.evidence_weak_fields


def test_strong_document_deadline_expiry_closes_before_bid_deadline():
    record = TenderRecord(project_name="项目", document_deadline="2026-08-20 17:00", bid_deadline="2026-09-10 09:00")
    decision = recalculate_status(record, NOW, evidences=[ev("document_deadline", "2026-08-20"), ev("bid_deadline", "2026-09-10")], require_evidence=True)
    assert decision.status is TenderStatus.CLOSED
    assert decision.reason_code == "CLOSED_DOCUMENT_DEADLINE_EXPIRED"


def test_document_window_with_strong_evidence_is_open():
    record = TenderRecord(project_name="项目", document_start="2026-08-20 09:00", document_deadline="2026-09-05 17:00", bid_deadline="2026-09-10 09:00")
    decision = recalculate_status(record, NOW, evidences=[ev("document_start", "2026-08-20"), ev("document_deadline", "2026-09-05"), ev("bid_deadline", "2026-09-10")], require_evidence=True)
    assert decision.status is TenderStatus.OPEN
    assert decision.reason_code == "OPEN_DOCUMENT_DOWNLOAD_ACTIVE"


def test_date_conflict_is_reported_before_evidence_gate():
    record = TenderRecord(project_name="项目", document_start="2026-09-05", document_deadline="2026-08-20")
    decision = recalculate_status(record, NOW, evidences=[ev("document_start", "2026-09-05"), ev("document_deadline", "2026-08-20")], require_evidence=True)
    assert decision.status is TenderStatus.UNKNOWN
    assert decision.reason_code == "UNKNOWN_CONFLICTING_DATES"


def test_result_mode_aliases_and_explicit_natural_language_contract():
    assert normalize_result_mode("full") == "FULL_RESULT"
    assert normalize_result_mode("delta") == "DELTA_RESULT"
    assert SearchRequest.from_dict({"result_mode": "full"}).result_mode == "FULL_RESULT"
    assert resolve_result_mode(SearchRequest(raw_query="给我完整结果", result_mode="AUTO")) == "FULL_RESULT"
    assert resolve_result_mode(SearchRequest(raw_query="最近新增项目", result_mode="AUTO")) == "DELTA_RESULT"
    assert parse_search_text("最近30天有哪些光伏项目").result_mode == "FULL_RESULT"


def test_evidence_gate_reports_missing_critical_fact_without_inventing_value():
    record = TenderRecord(project_name="项目", document_deadline="2026-09-05")
    result = evidence_gate(record, [ev("project_name", "项目")])
    assert result.missing_fields == ("document_deadline",)


class _AttachmentClient:
    def __init__(self, detail: bytes, pdf: bytes):
        self.detail = detail
        self.pdf = pdf

    def get(self, url: str, **_kwargs):
        is_pdf = url.endswith("notice.pdf")
        return HttpResponse(
            url=url,
            status_code=200,
            headers={"content-type": "application/pdf" if is_pdf else "text/html; charset=utf-8"},
            content=self.pdf if is_pdf else self.detail,
            fetched_at=NOW,
        )


class _ZipAttachmentClient:
    def __init__(self, detail: bytes, archive: bytes):
        self.detail = detail
        self.archive = archive

    def get(self, url: str, **_kwargs):
        is_zip = url.endswith("notice.zip")
        return HttpResponse(
            url=url,
            status_code=200,
            headers={"content-type": "application/zip" if is_zip else "text/html; charset=utf-8"},
            content=self.archive if is_zip else self.detail,
            fetched_at=NOW,
        )


def test_candidate_enrichment_result_attachment_document_evidence_closure(tmp_path):
    fitz = __import__("fitz")
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "项目名称：甘肃河西光伏支架采购项目\n投标截止时间：2026年9月10日17时")
    pdf_bytes = pdf.tobytes()
    pdf.close()
    detail_url = "https://official.example/notice/1"
    detail = f'<html><h1>甘肃河西光伏支架采购项目</h1><a href="{detail_url}/notice.pdf">招标文件 PDF</a></html>'.encode("utf-8")
    database = tmp_path / "candidate-documents.db"
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        session.add(SearchSession(session_id="doc-session", request_json="{}"))
        session.flush()
        candidate = CandidateStore.upsert_search_result(
            session,
            SearchResult(title="甘肃河西光伏支架采购项目", url=detail_url, snippet="支架采购招标公告", provider="fixture"),
            search_session_id="doc-session",
            source_level="E",
            region="甘肃省",
        )
        query = CandidateEnrichmentQuery(candidate_id=candidate.candidate_id, search_session_id="doc-session", query_text="项目", strategy="FULL_NAME", status="SUCCESS")
        session.add(query)
        session.flush()
        result = CandidateEnrichmentResult(query_id=query.id, candidate_id=candidate.candidate_id, search_session_id="doc-session", title="项目详情", source_url=detail_url, canonical_url=detail_url, provider="fixture", source_level="E")
        session.add(result)
        session.flush()
        candidate_id, result_id = candidate.candidate_id, result.id
    summary = process_candidate_enrichment_result(
        candidate_id,
        result_id,
        database=str(database),
        client=_AttachmentClient(detail, pdf_bytes),
        snapshot_store=SnapshotStore(tmp_path / "snapshots"),
        download_destination=tmp_path / "downloads",
    )
    assert summary.status == "COMPLETED"
    assert summary.attachment_count == 1
    assert summary.downloaded_count == 1
    assert summary.parsed_count == 1
    assert summary.evidence_count >= 1
    with session_scope(engine) as session:
        assert session.scalars(select(CandidateAttachment).where(CandidateAttachment.candidate_id == candidate_id)).first() is not None
        assert session.scalars(select(DocumentParse).where(DocumentParse.candidate_id == candidate_id)).first() is not None
        assert session.scalars(select(Snapshot).where(Snapshot.candidate_id == candidate_id)).first() is not None


def test_candidate_zip_attachment_safely_extracts_and_parses_supported_member(tmp_path):
    fitz = __import__("fitz")
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text(
        (72, 72),
        "项目名称：甘肃光伏支架采购项目\n投标截止时间：2026年9月10日17时",
    )
    pdf_bytes = pdf.tobytes()
    pdf.close()
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("docs/notice.pdf", pdf_bytes)
        archive.writestr("../outside.pdf", pdf_bytes)
        archive.writestr("notes.txt", b"unsupported member")
    detail_url = "https://official.example/notice/zip"
    zip_url = f"{detail_url}/notice.zip"
    detail = (
        f'<html><h1>甘肃光伏支架采购项目</h1>'
        f'<a href="{zip_url}">采购文件 ZIP</a></html>'
    ).encode("utf-8")
    database = tmp_path / "candidate-zip.db"
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        session.add(SearchSession(session_id="zip-session", request_json="{}"))
        session.flush()
        candidate = CandidateStore.upsert_search_result(
            session,
            SearchResult(
                title="甘肃光伏支架采购项目",
                url=detail_url,
                snippet="支架采购招标公告",
                provider="fixture",
            ),
            search_session_id="zip-session",
            source_level="E",
            region="甘肃省",
        )
        query = CandidateEnrichmentQuery(
            candidate_id=candidate.candidate_id,
            search_session_id="zip-session",
            query_text="项目",
            strategy="FULL_NAME",
            status="SUCCESS",
        )
        session.add(query)
        session.flush()
        result = CandidateEnrichmentResult(
            query_id=query.id,
            candidate_id=candidate.candidate_id,
            search_session_id="zip-session",
            title="项目详情",
            source_url=detail_url,
            canonical_url=detail_url,
            provider="fixture",
            source_level="E",
        )
        session.add(result)
        session.flush()
        candidate_id, result_id = candidate.candidate_id, result.id

    destination = tmp_path / "downloads"
    summary = process_candidate_enrichment_result(
        candidate_id,
        result_id,
        database=str(database),
        client=_ZipAttachmentClient(detail, archive_buffer.getvalue()),
        snapshot_store=SnapshotStore(tmp_path / "snapshots"),
        download_destination=destination,
    )

    assert summary.status == "COMPLETED"
    assert summary.attachment_count == 1
    assert summary.downloaded_count == 1
    assert summary.parsed_count == 1
    assert summary.evidence_count >= 1
    assert not (destination / "outside.pdf").exists()
    with session_scope(engine) as session:
        attachment = session.scalars(
            select(CandidateAttachment).where(CandidateAttachment.candidate_id == candidate_id)
        ).first()
        assert attachment is not None
        metadata = __import__("json").loads(attachment.metadata_json or "{}")
        assert len(metadata["inner_documents"]) == 1
        assert session.scalars(
            select(DocumentParse).where(DocumentParse.candidate_id == candidate_id)
        ).first() is not None
        assert session.scalars(
            select(Snapshot).where(Snapshot.candidate_id == candidate_id)
        ).first() is not None
