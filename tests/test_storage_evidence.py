from datetime import datetime

from sqlalchemy import inspect, select

from tender_ai.cache import DiskCache
from tender_ai.evidence.models import EvidenceRecord
from tender_ai.models import TenderRecord
from tender_ai.sources.registry import SourceRegistry
from tender_ai.status.engine import TenderStatus
from tender_ai.status.time import SHANGHAI_TZ
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import ChangeHistory, Evidence, Project, Source, StatusHistory
from tender_ai.storage.repository import save_evidence, save_tender_record


def test_database_creates_required_tables(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    tables = set(inspect(engine).get_table_names())
    assert {"projects", "announcements", "sources", "project_sources", "attachments", "evidence", "status_history", "change_history", "crawl_runs", "crawl_errors", "discovered_sources", "search_queries"} <= tables


def test_tender_record_persists_and_status_history_is_kept(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    item = TenderRecord(project_id="p-1", project_name="陕西光伏项目", status=TenderStatus.OPEN, source_url="https://example.com/a")
    with session_scope(engine) as session:
        save_tender_record(session, item, status_reason="首次入库")
    with session_scope(engine) as session:
        row = session.get(Project, "p-1")
        history = list(session.scalars(select(StatusHistory)).all())
        assert row is not None
        assert row.status == "OPEN"
        assert len(history) == 1
        assert history[0].old_status is None


def test_evidence_hash_and_fields_are_persisted(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    item = TenderRecord(project_id="p-2", project_name="储能项目")
    evidence = EvidenceRecord(field_name="document_deadline", normalized_value="2026-09-05 17:00", raw_value="2026年9月5日17点", source_url="https://example.com/a.pdf", source_file="a.pdf", page_number=3, source_text="文件获取截止时间：2026年9月5日17点", extractor="rule.time", confidence=0.95)
    with session_scope(engine) as session:
        save_tender_record(session, item)
        save_evidence(session, evidence, project_id=item.project_id)
    with session_scope(engine) as session:
        row = session.scalar(select(Evidence).where(Evidence.project_id == "p-2"))
        assert row is not None
        assert row.normalized_value == "2026-09-05 17:00"
        assert row.raw_value == "2026年9月5日17点"
        assert row.page_number == 3
        assert len(row.content_hash) == 64


def test_deadline_change_keeps_old_and_new_values(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    original = TenderRecord(project_id="p-3", project_name="延期项目", registration_deadline="2026-08-20 17:00", status=TenderStatus.CLOSED)
    extension = TenderRecord(project_id="p-3", project_name="延期项目", registration_deadline="2026-09-05 17:00", status=TenderStatus.OPEN)
    with session_scope(engine) as session:
        save_tender_record(session, original, change_type="original")
        save_tender_record(session, extension, change_type="extension")
    with session_scope(engine) as session:
        rows = list(session.scalars(select(ChangeHistory)).all())
        assert len(rows) == 1
        assert rows[0].change_type == "extension"
        assert "2026-08-20" in rows[0].old_value
        assert "2026-09-05" in rows[0].new_value


def test_invalid_change_type_is_rejected(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    with session_scope(engine) as session:
        try:
            save_tender_record(session, TenderRecord(project_id="p-4", project_name="项目"), change_type="invalid")
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝未知变更类型")


def test_sources_can_be_seeded_from_registry(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    registry = SourceRegistry.from_file()
    with session_scope(engine) as session:
        session.add_all([Source(**item.model_dump()) for item in registry.definitions])
    with session_scope(engine) as session:
        assert session.scalar(select(Source).where(Source.source_id == "ccgp")) is not None
        assert session.scalar(select(Source).where(Source.source_id == "xinjiang_ggzy")).region == "新疆"


def test_disk_cache_supports_namespaces_and_short_failures(tmp_path):
    cache = DiskCache(tmp_path / "cache")
    try:
        cache.set("http", "https://example.com", {"status": 200})
        assert cache.get("http", "https://example.com")["status"] == 200
        cache.remember_short_failure("pdf", "a.pdf", "timeout")
        assert cache.get("failure:pdf", "a.pdf")["message"] == "timeout"
    finally:
        cache.close()
