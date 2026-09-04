import json
from datetime import timedelta

from tender_ai.crawlers.http import HttpResponse, _response_action
from tender_ai.crawlers.runner import SourceCrawlSummary, _record_source_error
from tender_ai.search import SearchRequest, SearchRunner, parse_search_text, search_scope_key
from tender_ai.result_layers import result_bucket
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, Project, SearchSession, SearchSessionProject
from tender_ai.storage.repository import save_tender_record
from tender_ai.models import TenderRecord


def test_http_412_javascript_challenge_requires_manual_action():
    marker = bytes([36]) + b"_ts"
    response = HttpResponse(
        url="https://example.test/",
        status_code=412,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<html><script>" + marker + b"=1</script></html>",
        fetched_at=now_shanghai(),
    )
    assert _response_action(response) == ("VERIFICATION_REQUIRED", "VERIFICATION_REQUIRED")

    summary = SourceCrawlSummary("fixture", "Fixture")
    from tender_ai.crawlers.http import HttpFetchError

    _record_source_error(
        summary,
        HttpFetchError(
            "HTTP 状态码 412；检测到VERIFICATION_REQUIRED",
            url=response.url,
            status_code=412,
            manual_action_required=True,
            manual_action_type="VERIFICATION_REQUIRED",
            health_reason="VERIFICATION_REQUIRED",
        ),
    )
    assert summary.manual_action_required is True
    assert summary.manual_action_type == "VERIFICATION_REQUIRED"
    assert summary.health_reason == "VERIFICATION_REQUIRED"
    assert "需要人工处理" in summary.error


def test_search_scope_ignores_execution_mode_but_not_search_content():
    base = SearchRequest(region="甘肃省", industries=("solar",), days=30)
    deep = SearchRequest(region="甘肃省", industries=("solar",), days=30, deep=True, discovery=True, wechat=True, only_open=True)
    storage = SearchRequest(region="甘肃省", industries=("storage",), days=30, deep=True)
    assert search_scope_key(base) == search_scope_key(deep)
    assert search_scope_key(base) != search_scope_key(storage)


def test_repeated_unchanged_project_is_suppressed_but_changed_project_is_returned(tmp_path):
    database = tmp_path / "repeat.db"
    engine = initialize_database(create_engine_for(database))
    request = SearchRequest(region="甘肃省", industries=("solar",), days=30, deep=True)
    finished_at = now_shanghai() - timedelta(hours=1)

    with session_scope(engine) as session:
        project = save_tender_record(
            session,
            TenderRecord(project_id="repeat-project", project_name="甘肃光伏项目", province="甘肃省"),
        )
        announcement = Announcement(
            project_id=project.project_id,
            title=project.project_name,
            source_url="https://example.test/repeat",
            canonical_url="https://example.test/repeat",
            content_hash="same-content",
        )
        session.add(announcement)
        session.flush()
        project.last_change_at = finished_at - timedelta(minutes=5)
        session.add(
            SearchSession(
                session_id="previous-session",
                request_json=json.dumps(request.to_dict(), ensure_ascii=False),
                started_at=finished_at - timedelta(minutes=5),
                finished_at=finished_at,
                status="COMPLETED",
            )
        )
        session.add(
            SearchSessionProject(
                session_id="previous-session",
                project_id=project.project_id,
                announcement_id=announcement.id,
                status_at_search=project.status,
            )
        )

    runner = SearchRunner(database=str(database))
    with session_scope(runner.engine) as session:
        selected = [(session.get(Project, "repeat-project"), session.get(Announcement, 1), [])]
        visible, suppressed = runner._filter_repeated_unchanged(session, request, "current-session", selected)
        assert visible == []
        assert suppressed == ["repeat-project"]

        project = session.get(Project, "repeat-project")
        project.last_change_at = now_shanghai()
        visible, suppressed = runner._filter_repeated_unchanged(session, request, "current-session-2", selected)
        assert [item[0].project_id for item in visible] == ["repeat-project"]
        assert suppressed == []


def test_recent_extension_recalls_project_with_old_original_publication(tmp_path):
    database = tmp_path / "extension.db"
    engine = initialize_database(create_engine_for(database))
    current = now_shanghai()
    with session_scope(engine) as session:
        project = save_tender_record(
            session,
            TenderRecord(
                project_id="old-project",
                project_name="甘肃光伏支架项目",
                province="甘肃省",
                publish_time=current - timedelta(days=90),
            ),
        )
        session.add(Announcement(
            project_id=project.project_id,
            title="甘肃光伏支架项目延期公告",
            announcement_type="延期公告",
            source_url="https://official.example.gov.cn/extension",
            published_at=current - timedelta(days=2),
            clean_text="投标截止时间延期至下月。光伏支架项目。",
        ))
    runner = SearchRunner(database=str(database))
    with session_scope(runner.engine) as session:
        recalled = runner._recall_projects(
            session,
            SearchRequest(region="甘肃省", industries=("solar",), days=30, result_mode="FULL_RESULT"),
        )
    assert [item[0].project_id for item in recalled] == ["old-project"]


def test_result_layers_keep_blocked_and_excluded_reasons_visible():
    assert result_bucket({"verification_status": "BLOCKED", "status": "UNKNOWN"}) == "J"
    assert result_bucket({"matched_keywords": ["EXCLUDED:太阳能热水器"], "status": "UNKNOWN"}) == "I"
    assert result_bucket({"title": "某项目中标结果公示", "status": "CLOSED"}) == "E"


def test_shortcut_parser_does_not_treat_non_region_query_as_region():
    assert parse_search_text("全国最近30天医院医疗设备采购").region == "全国"
    assert parse_search_text("医院医疗设备采购").region is None
