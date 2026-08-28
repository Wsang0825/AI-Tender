from datetime import datetime

from sqlalchemy import select

from tender_ai.config_loader import load_industry_profiles, load_region_catalog, load_search_profiles
from tender_ai.sources.generic import load_generic_adapter_config
from tender_ai.sources.registry import SourceDefinition
from tender_ai.status.engine import recalculate_status
from tender_ai.status.time import SHANGHAI_TZ
from tender_ai.storage.database import create_engine_for, initialize_database, search_projects, session_scope
from tender_ai.storage.models import ManualOverride, Project, Snapshot
from tender_ai.storage.repository import save_manual_override, save_tender_record
from tender_ai.models import TenderRecord
from tender_ai.urls import canonicalize_url
from tender_ai.snapshots.store import SnapshotStore


def test_search_profile_and_region_catalog_are_configurable():
    profile = load_search_profiles().get("northwest_energy")
    assert "CN-65" in profile.regions
    assert load_region_catalog().get("CN-65").short_name == "新疆"
    assert load_industry_profiles().get("solar").terms


def test_url_canonicalization_removes_tracking_and_pagination():
    assert canonicalize_url("http://Example.com//a/?utm_source=x&page=2#top") == "https://example.com/a"


def test_generic_adapter_config_is_declarative():
    definition = SourceDefinition(source_id="example", source_name="example", category="regional", base_url="https://example.com", adapter="generic:example_static", adapter_level="GENERIC_HTML")
    config = load_generic_adapter_config(definition)
    assert config.list_item_selector
    assert config.page_parameter == "page"


def test_snapshot_dedupe_fts_and_manual_override(tmp_path):
    engine = initialize_database(create_engine_for(tmp_path / "tender.db"))
    with session_scope(engine) as session:
        project = save_tender_record(session, TenderRecord(project_id="p-arch", project_name="哈密储能 EPC", owner="业主"))
        save_manual_override(session, project.project_id, "owner", "人工确认业主", reason="人工核验")
        snapshot = SnapshotStore(tmp_path / "snapshots").save_text(session, source_url="https://example.com/a", text="<h1>哈密储能 EPC</h1>")
        same = SnapshotStore(tmp_path / "snapshots").save_text(session, source_url="https://example.com/b", text="<h1>哈密储能 EPC</h1>")
        assert snapshot.snapshot_id == same.snapshot_id
    with session_scope(engine) as session:
        assert search_projects(session, "哈密储能") == ["p-arch"]
        assert session.scalar(select(ManualOverride).where(ManualOverride.project_id == "p-arch")).new_value == "人工确认业主"
        assert session.scalar(select(Snapshot)).sha256


def test_status_reason_code_is_exposed():
    now = datetime(2026, 8, 28, 12, tzinfo=SHANGHAI_TZ)
    decision = recalculate_status(TenderRecord(project_name="项目", registration_deadline="2026-08-20 17:00"), now)
    assert decision.reason_code == "CLOSED_REGISTRATION_EXPIRED"
