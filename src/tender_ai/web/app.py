"""Stage 5 本地数据浏览器。

Web 只负责查看、配置和人工写回。搜索必须由用户显式提交表单、CLI 或
Codex 调用，create_app() 本身绝不触发 crawl/discovery。
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from tender_ai.config_loader import APP_ROOT, load_industry_profiles, load_region_catalog, load_search_profiles
from tender_ai.config_store import (
    config_snapshot,
    copy_search_profile,
    save_industry_profile,
    save_search_profile,
    toggle_search_profile,
    update_provider,
    update_source,
)
from tender_ai.export import export_search_session
from tender_ai.review import review_item_dict
from tender_ai.search import SearchRequest, SearchRunner, parse_search_text
from tender_ai.sources.registry import SourceRegistry
from tender_ai.status.engine import recalculate_status, with_manual_evidence
from tender_ai.status.time import as_shanghai, now_shanghai
from tender_ai.storage.database import create_engine_for, fts5_available, initialize_database, refresh_tender_fts, search_projects, session_scope
from tender_ai.storage.models import (
    Announcement,
    Attachment,
    ChangeHistory,
    CodexReviewItem,
    DiscoveredSource,
    DocumentParse,
    Evidence,
    FieldConflict,
    ManualOverride,
    Project,
    ProjectSource,
    SearchSession,
    SearchSessionProject,
    Snapshot,
    Source,
    StatusHistory,
    TimelineEvent,
)
from tender_ai.storage.repository import _coerce_field_value, add_status_history, clear_manual_override, project_to_record, save_manual_override
from tender_ai.templates import list_templates, save_template, set_template_enabled
from tender_ai.versioning import APP_VERSION, CONFIG_VERSION, EXTRACTOR_VERSION, SCHEMA_VERSION, STATUS_RULE_VERSION


WEB_ROOT = Path(__file__).resolve().parent
TEMPLATES_ROOT = WEB_ROOT / "templates"
DEFAULT_DATABASE = str(APP_ROOT.parent / "data" / "tender.db")
EDITABLE_FIELDS = {
    "project_name", "province", "city", "county", "location", "owner", "purchaser", "tenderer", "agency",
    "industry", "sub_industry", "project_type", "project_scale", "capacity_mw", "capacity_mwh", "budget",
    "project_code", "tender_code", "qualification_start", "qualification_deadline", "registration_start",
    "registration_deadline", "document_start", "document_deadline", "bid_deadline", "open_time", "participation_method",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_shanghai(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _serialize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    return _json_value(value)


def _dt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return as_shanghai(value).strftime("%Y-%m-%d %H:%M")
    return str(value)


def _bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked", "启用", "是"}


def _csv(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for part in values:
        for token in str(part).replace("，", ",").replace("\n", ",").split(","):
            token = token.strip()
            if token and token not in result:
                result.append(token)
    return result


def _path_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return parsed if isinstance(parsed, list) else [str(parsed)]


def _project_payload(project: Project) -> dict[str, Any]:
    fields = (
        "project_id", "project_name", "raw_project_name", "canonical_project_name", "province", "city", "county", "location",
        "owner", "purchaser", "tenderer", "agency", "industry", "sub_industry", "project_type", "announcement_type",
        "project_scale", "capacity_mw", "capacity_mwh", "budget", "project_code", "tender_code", "publish_time",
        "qualification_start", "qualification_deadline", "registration_start", "registration_deadline", "document_start",
        "document_deadline", "bid_deadline", "open_time", "qualification_summary", "participation_method", "source_name",
        "source_type", "source_level", "source_url", "original_url", "canonical_url", "content_hash", "status", "status_reason",
        "status_evaluated_at", "status_rule_version", "lifecycle_state", "last_change_at", "favorite", "ignored", "ignore_reason",
        "document_quality_score", "extraction_version", "extraction_method", "last_extracted_at", "verification_required",
        "verification_reason", "field_confidence", "source_confidence", "project_match_confidence", "overall_confidence",
        "completeness_score", "needs_codex_review", "review_reason", "created_at", "updated_at",
    )
    return {field: _json_value(getattr(project, field, None)) for field in fields}


def _evidence_payload(row: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": row.id, "field_name": row.field_name, "normalized_value": row.normalized_value, "raw_value": row.raw_value,
        "source_url": row.source_url, "source_file": row.source_file, "snapshot_id": row.snapshot_id, "document_id": row.document_id,
        "page_number": row.page_number, "sheet_name": row.sheet_name, "cell_range": row.cell_range, "source_text": row.source_text,
        "extractor": row.extractor, "extractor_type": row.extractor_type, "extractor_version": row.extractor_version,
        "confidence": row.confidence, "captured_at": _json_value(row.captured_at), "content_hash": row.content_hash,
    }


def _request_payload(row: SearchSession) -> dict[str, Any]:
    try:
        return json.loads(row.request_json or "{}")
    except json.JSONDecodeError:
        return {}


def _errors_payload(row: SearchSession) -> list[Any]:
    try:
        value = json.loads(row.errors_json or "[]")
    except json.JSONDecodeError:
        value = []
    return value if isinstance(value, list) else [value]


def _sources_payload(session: Any, project_id: str) -> list[dict[str, Any]]:
    result = []
    for link in session.scalars(select(ProjectSource).where(ProjectSource.project_id == project_id)).all():
        source = session.get(Source, link.source_id)
        result.append({
            "source_id": link.source_id, "source_name": source.source_name if source else link.source_id,
            "category": source.category if source else None, "source_url": link.source_url or (source.base_url if source else None),
            "original_url": link.original_url, "canonical_url": link.canonical_url, "content_hash": link.content_hash,
            "last_seen_at": _json_value(link.last_seen_at),
        })
    return result


def _project_card(session: Any, project: Project) -> dict[str, Any]:
    announcements = list(session.scalars(select(Announcement).where(Announcement.project_id == project.project_id).order_by(Announcement.published_at.desc(), Announcement.id.desc())).all())
    newest = announcements[0] if announcements else None
    deadlines = [getattr(project, field) for field in ("qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline", "open_time") if isinstance(getattr(project, field), datetime)]
    next_deadline = min(deadlines) if deadlines else None
    payload = _project_payload(project)
    payload.update({
        "announcement_id": newest.id if newest else None, "announcement_title": newest.title if newest else None,
        "announcement_url": newest.source_url if newest else None, "announcement_published_at": _json_value(newest.published_at) if newest else None,
        "source_count": len(_sources_payload(session, project.project_id)),
        "remaining_hours": round((as_shanghai(next_deadline) - now_shanghai()).total_seconds() / 3600, 1) if next_deadline else None,
    })
    return payload


def _session_card(row: SearchSession) -> dict[str, Any]:
    return {
        "session_id": row.session_id, "request_id": row.request_id, "request": _request_payload(row),
        "started_at": _json_value(row.started_at), "finished_at": _json_value(row.finished_at), "status": row.status,
        "sources_planned": row.sources_planned, "sources_completed": row.sources_completed, "sources_failed": row.sources_failed,
        "query_count": row.query_count, "candidate_count": row.candidate_count, "projects_found": row.projects_found,
        "open_count": row.open_count, "unknown_count": row.unknown_count, "closed_count": row.closed_count,
        "review_count": row.review_count, "verification_count": row.verification_count, "errors": _errors_payload(row),
    }


def _build_request_from_form(form: Any) -> SearchRequest:
    raw_query = str(form.get("query") or "").strip() or None
    profile_id = str(form.get("profile") or "northwest_energy").strip()
    base = parse_search_text(raw_query, profile_id=profile_id) if raw_query else SearchRequest(profile_id=profile_id)
    region = str(form.get("region") or "").strip() or base.region
    city = str(form.get("city") or "").strip() or base.city
    county = str(form.get("county") or "").strip() or base.county
    try:
        days = int(form.get("days") or base.days or 30)
    except (TypeError, ValueError):
        days = 30
    industries = tuple(_csv(form.getlist("industry")) or base.industries)
    project_types = tuple(_csv(form.getlist("project_type")) or base.project_types)
    equipment = tuple(_csv(form.getlist("equipment")) or base.equipment)
    keywords = tuple(_csv(form.get("keywords")) or base.keywords)
    excludes = tuple(_csv(form.get("exclude_keywords")) or base.exclude_keywords)
    source_categories = tuple(_csv(form.get("source_category")) or base.source_categories)
    announcement_types = tuple(_csv(form.get("announcement_type")) or base.announcement_types)
    return replace(
        base, profile_id=profile_id, raw_query=raw_query, region=region, city=city, county=county,
        regions=(region,) if region else base.regions, cities=(city,) if city else base.cities, counties=(county,) if county else base.counties,
        days=max(1, min(days, 3650)), date_from=str(form.get("date_from") or "").strip() or base.date_from,
        date_to=str(form.get("date_to") or "").strip() or base.date_to, industries=industries, project_types=project_types,
        equipment=equipment, keywords=keywords, exclude_keywords=excludes, source_categories=source_categories,
        announcement_types=announcement_types, include_unknown=_bool(form.get("include_unknown")) or base.include_unknown,
        only_open=_bool(form.get("only_open")) or base.only_open, discovery=_bool(form.get("discovery")) or base.discovery,
        wechat=_bool(form.get("wechat")) or base.wechat, deep=_bool(form.get("deep")) or base.deep,
    )


def _config_context(database: str | None) -> dict[str, Any]:
    profiles = load_search_profiles()
    industries = load_industry_profiles()
    catalog = load_region_catalog()
    registry = SourceRegistry.from_file()
    providers = (config_snapshot().get("providers") or {}).get("providers") or []
    return {
        "profiles": [asdict(row) for row in profiles.profiles], "industries": [asdict(row) for row in industries.profiles],
        "regions": [asdict(row) for row in catalog.entries], "sources": [row.model_dump() for row in registry.definitions],
        "providers": providers, "templates": list_templates(database=database),
    }


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _system_payload(database: str | None, engine: Any) -> dict[str, Any]:
    registry = SourceRegistry.from_file()
    profile_registry = load_search_profiles()
    return {
        "app_version": APP_VERSION, "schema_version": SCHEMA_VERSION, "config_version": CONFIG_VERSION,
        "extractor_version": EXTRACTOR_VERSION, "status_rule_version": STATUS_RULE_VERSION,
        "database": database or DEFAULT_DATABASE, "fts5_available": fts5_available(engine),
        "profiles": len(profile_registry.profiles), "enabled_profiles": len(profile_registry.enabled()),
        "sources": len(registry.definitions), "enabled_sources": sum(1 for row in registry.definitions if row.enabled and row.crawl_enabled),
        "browser_profiles_root": str(APP_ROOT.parent / "data" / "browser_profiles"), "scheduling_mode": "ON_DEMAND_ONLY",
        "components": {name: _module_available(name) for name in ("ddgs", "scrapling", "crawl4ai", "pymupdf4llm", "mineru", "openpyxl")},
    }


def create_app(*, database: str | None = None) -> FastAPI:
    """创建 Web 应用。函数只建立连接和路由，不执行搜索。"""

    engine = initialize_database(create_engine_for(database))
    templates = Jinja2Templates(directory=str(TEMPLATES_ROOT))
    templates.env.filters["dt"] = _dt
    templates.env.filters["json"] = lambda value: json.dumps(_serialize(value), ensure_ascii=False, indent=2, default=str)
    templates.env.filters["join_path"] = lambda value: "; ".join(str(item) for item in (value or []))
    application = FastAPI(title="区域新能源招投标自动搜索系统", docs_url="/api/docs", redoc_url=None)
    application.state.engine = engine
    application.state.database = database or DEFAULT_DATABASE
    application.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")

    def render(request: Request, name: str, **values: Any) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name=name, context={"request": request, "now": now_shanghai(), **values})

    @application.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "scheduling_mode": "ON_DEMAND_ONLY", "database": application.state.database}

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        with session_scope(engine) as session:
            active_filter = or_(Project.ignored.is_(False), Project.ignored.is_(None))
            counts = {status: session.scalar(select(func.count()).select_from(Project).where(Project.status == status, active_filter)) or 0 for status in ("OPEN", "UNKNOWN", "CLOSED")}
            pending_reviews = session.scalar(select(func.count()).select_from(CodexReviewItem).where(CodexReviewItem.status == "PENDING")) or 0
            favorites = session.scalar(select(func.count()).select_from(Project).where(Project.favorite.is_(True), active_filter)) or 0
            reopened = session.scalar(select(func.count()).select_from(Project).where(Project.lifecycle_state == "REOPENED", active_filter)) or 0
            projects = [_project_card(session, row) for row in session.scalars(select(Project).where(active_filter).order_by(Project.updated_at.desc()).limit(12)).all()]
            recent_sessions = [_session_card(row) for row in session.scalars(select(SearchSession).order_by(SearchSession.started_at.desc()).limit(6)).all()]
        return render(request, "dashboard.html", title="总览", counts=counts, pending_reviews=pending_reviews, favorites=favorites, reopened=reopened, projects=projects, sessions=recent_sessions, system=_system_payload(application.state.database, engine))

    @application.get("/search", response_class=HTMLResponse)
    def search_page(request: Request) -> HTMLResponse:
        return render(request, "search.html", title="按需搜索", **_config_context(application.state.database), error=None)

    @application.post("/search/run")
    async def search_run(request: Request) -> Response:
        form = await request.form()
        try:
            summary = SearchRunner(database=application.state.database).run(_build_request_from_form(form), dry_run=False)
            return RedirectResponse(url=f"/sessions/{summary.session_id}", status_code=303)
        except Exception as exc:
            return render(request, "search.html", title="按需搜索", **_config_context(application.state.database), error=str(exc))

    @application.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, q: str = Query(""), status: str = Query(""), view: str = Query("all"), page: int = Query(1, ge=1), page_size: int = Query(30, ge=5, le=100)) -> HTMLResponse:
        with session_scope(engine) as session:
            filters = []
            if q.strip():
                ids = search_projects(session, q.strip(), limit=500)
                filters.append(Project.project_id.in_(ids or ["__no_match__"]))
            if status.upper() in {"OPEN", "UNKNOWN", "CLOSED"}:
                filters.append(Project.status == status.upper())
            today = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
            if view == "today-new":
                filters.extend((Project.lifecycle_state == "NEW", Project.created_at >= today))
            elif view == "today-changed":
                filters.append(Project.last_change_at >= today)
            elif view == "reopened":
                filters.append(Project.lifecycle_state == "REOPENED")
            elif view == "favorite":
                filters.append(Project.favorite.is_(True))
            if view != "ignored":
                filters.append(or_(Project.ignored.is_(False), Project.ignored.is_(None)))
            else:
                filters.append(Project.ignored.is_(True))
            statement = select(Project).where(*filters).order_by(Project.status, Project.updated_at.desc())
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.scalars(statement.offset((page - 1) * page_size).limit(page_size)).all()
            cards = [_project_card(session, row) for row in rows]
        return render(request, "projects.html", title="项目库", projects=cards, q=q, status=status, view=view, page=page, page_size=page_size, total=total, pages=max(1, (total + page_size - 1) // page_size))

    @application.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_detail(request: Request, project_id: str) -> HTMLResponse:
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                return render(request, "not_found.html", title="未找到", message=f"项目不存在：{project_id}")
            announcements = list(session.scalars(select(Announcement).where(Announcement.project_id == project_id).order_by(Announcement.published_at.desc(), Announcement.id.desc())).all())
            evidence = [_evidence_payload(row) for row in session.scalars(select(Evidence).where(Evidence.project_id == project_id).order_by(Evidence.id.desc())).all()]
            timeline = [{"id": row.id, "event_type": row.event_type, "event_at": _json_value(row.event_at), "title": row.title, "summary": row.summary, "source_url": row.source_url, "announcement_id": row.announcement_id, "evidence_ids": _path_list(row.evidence_ids_json)} for row in session.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project_id).order_by(TimelineEvent.event_at, TimelineEvent.id)).all()]
            documents = [{"document_id": row.document_id, "document_type": row.document_type or row.content_type, "file_path": row.file_path, "source_file": row.source_file, "source_url": row.source_url, "content_hash": row.content_hash, "parser": row.parser, "quality_score": row.quality_score, "page_count": row.page_count, "clean_text_path": row.clean_text_path, "markdown_path": row.markdown_path, "parse_status": row.parse_status, "parse_error": row.parse_error or row.error, "parsed_at": _json_value(row.parsed_at or row.extracted_at)} for row in session.scalars(select(DocumentParse).where(DocumentParse.project_id == project_id).order_by(DocumentParse.id.desc())).all()]
            attachments = [{"id": row.id, "file_name": row.file_name, "source_url": row.source_url, "local_path": row.local_path, "mime_type": row.mime_type, "content_hash": row.content_hash} for row in session.scalars(select(Attachment).where(Attachment.project_id == project_id).order_by(Attachment.id.desc())).all()]
            reviews = [review_item_dict(session, row) for row in session.scalars(select(CodexReviewItem).where(CodexReviewItem.project_id == project_id).order_by(CodexReviewItem.status, CodexReviewItem.priority, CodexReviewItem.created_at)).all()]
            conflicts = [{"id": row.id, "field_name": row.field_name, "candidate_values": _path_list(row.candidate_values_json), "evidence_ids": _path_list(row.evidence_ids_json), "resolution_status": row.resolution_status, "resolution": row.resolution, "detected_at": _json_value(row.detected_at)} for row in session.scalars(select(FieldConflict).where(FieldConflict.project_id == project_id).order_by(FieldConflict.id.desc())).all()]
            overrides = [{"id": row.id, "field_name": row.field_name, "old_value": row.old_value, "new_value": row.new_value, "automatic_value": row.automatic_value, "manual_value": row.manual_value, "changed_at": _json_value(row.changed_at), "reason": row.reason, "changed_by": row.changed_by, "active": row.active} for row in session.scalars(select(ManualOverride).where(ManualOverride.project_id == project_id).order_by(ManualOverride.id.desc())).all()]
            status_history = [{"old_status": row.old_status, "new_status": row.new_status, "reason": row.reason, "changed_at": _json_value(row.changed_at)} for row in session.scalars(select(StatusHistory).where(StatusHistory.project_id == project_id).order_by(StatusHistory.changed_at.desc())).all()]
            changes = [{"change_type": row.change_type, "field_name": row.field_name, "old_value": row.old_value, "new_value": row.new_value, "source_url": row.source_url, "updated_at": _json_value(row.updated_at)} for row in session.scalars(select(ChangeHistory).where(ChangeHistory.project_id == project_id).order_by(ChangeHistory.updated_at.desc())).all()]
            payload = {
                "project": _project_card(session, project),
                "announcements": [{"id": row.id, "title": row.title, "announcement_type": row.announcement_type, "source_url": row.source_url, "original_url": row.original_url, "published_at": _json_value(row.published_at), "content_hash": row.content_hash, "snapshot_id": row.snapshot_id, "clean_text": (row.clean_text or "")[:3000]} for row in announcements],
                "evidence": evidence, "timeline": timeline, "documents": documents, "attachments": attachments,
                "sources": _sources_payload(session, project_id), "reviews": reviews, "conflicts": conflicts, "overrides": overrides,
                "status_history": status_history, "changes": changes,
            }
        return render(request, "project_detail.html", title=payload["project"]["project_name"], **payload)

    @application.post("/projects/{project_id}/favorite")
    async def project_favorite(project_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is not None:
                project.favorite = _bool(form.get("value")) if "value" in form else not project.favorite
                project.updated_at = now_shanghai()
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @application.post("/projects/{project_id}/ignore")
    async def project_ignore(project_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is not None:
                project.ignored = True
                project.ignore_reason = str(form.get("reason") or "用户忽略")
                project.updated_at = now_shanghai()
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @application.post("/projects/{project_id}/ignore/clear")
    def project_ignore_clear(project_id: str) -> RedirectResponse:
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is not None:
                project.ignored = False
                project.ignore_reason = None
                project.updated_at = now_shanghai()
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @application.post("/projects/{project_id}/override")
    async def project_override(project_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        field_name = str(form.get("field_name") or "").strip()
        if field_name not in EDITABLE_FIELDS:
            return RedirectResponse(url=f"/projects/{project_id}?error=字段不可编辑", status_code=303)
        try:
            value = _coerce_field_value(field_name, str(form.get("value") or ""))
        except (TypeError, ValueError):
            return RedirectResponse(url=f"/projects/{project_id}?error=字段值无法解析", status_code=303)
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is not None:
                old_status = project.status
                save_manual_override(session, project_id, field_name, value, reason=str(form.get("reason") or "Web 人工修正"), changed_by="USER")
                if field_name in {"project_name", "owner", "agency", "qualification_summary", "participation_method"}:
                    refresh_tender_fts(session, project)
                gate_evidence = with_manual_evidence(
                    session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all(),
                    session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all(),
                    source_url=project.source_url,
                )
                decision = recalculate_status(project_to_record(project), evidences=gate_evidence, require_evidence=True)
                project.status, project.status_reason = decision.status.value, decision.reason_code
                project.status_evaluated_at, project.status_rule_version = now_shanghai(), STATUS_RULE_VERSION
                if old_status != project.status:
                    add_status_history(session, project_id, old_status, project.status, decision.reason)
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @application.post("/projects/{project_id}/override/remove")
    async def project_override_remove(project_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        field_name = str(form.get("field_name") or "").strip()
        if field_name:
            with session_scope(engine) as session:
                clear_manual_override(session, project_id, field_name)
                project = session.get(Project, project_id)
                if project is not None:
                    gate_evidence = with_manual_evidence(
                        session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all(),
                        session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all(),
                        source_url=project.source_url,
                    )
                    decision = recalculate_status(project_to_record(project), evidences=gate_evidence, require_evidence=True)
                    project.status, project.status_reason = decision.status.value, decision.reason_code
                    project.status_evaluated_at, project.status_rule_version = now_shanghai(), STATUS_RULE_VERSION
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @application.post("/projects/{project_id}/recalculate")
    def project_recalculate(project_id: str) -> RedirectResponse:
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is not None:
                gate_evidence = with_manual_evidence(
                    session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all(),
                    session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all(),
                    source_url=project.source_url,
                )
                decision = recalculate_status(project_to_record(project), evidences=gate_evidence, require_evidence=True)
                old_status = project.status
                project.status, project.status_reason = decision.status.value, decision.reason_code
                project.status_evaluated_at, project.status_rule_version = now_shanghai(), STATUS_RULE_VERSION
                if old_status != project.status:
                    add_status_history(session, project_id, old_status, project.status, decision.reason)
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @application.get("/evidence/{evidence_id}", response_class=HTMLResponse)
    def evidence_page(request: Request, evidence_id: int) -> HTMLResponse:
        with session_scope(engine) as session:
            row = session.get(Evidence, evidence_id)
            if row is None:
                return render(request, "not_found.html", title="未找到", message=f"Evidence 不存在：{evidence_id}")
            project = session.get(Project, row.project_id) if row.project_id else None
            payload = _evidence_payload(row)
        return render(request, "evidence.html", title=f"Evidence {evidence_id}", evidence=payload, project=_project_payload(project) if project else None)

    @application.get("/snapshots/{snapshot_id}")
    def snapshot_file(snapshot_id: str) -> Response:
        with session_scope(engine) as session:
            row = session.get(Snapshot, snapshot_id)
            path = Path(row.file_path) if row and row.file_path else None
        if path is None or not path.exists() or not path.is_file():
            return JSONResponse({"ok": False, "error": "本地 Snapshot 文件不存在"}, status_code=404)
        return FileResponse(path)

    @application.get("/attachments/{attachment_id}")
    def attachment_file(attachment_id: int) -> Response:
        with session_scope(engine) as session:
            row = session.get(Attachment, attachment_id)
            path = Path(row.local_path) if row and row.local_path else None
        if path is None or not path.exists() or not path.is_file():
            return JSONResponse({"ok": False, "error": "本地附件不存在"}, status_code=404)
        return FileResponse(path, filename=path.name)

    @application.get("/sessions", response_class=HTMLResponse)
    def sessions_page(request: Request) -> HTMLResponse:
        with session_scope(engine) as session:
            rows = [_session_card(row) for row in session.scalars(select(SearchSession).order_by(SearchSession.started_at.desc()).limit(100)).all()]
        return render(request, "sessions.html", title="搜索历史", sessions=rows)

    @application.get("/review", response_class=HTMLResponse)
    def review_page(request: Request) -> HTMLResponse:
        with session_scope(engine) as session:
            rows = [review_item_dict(session, row) for row in session.scalars(select(CodexReviewItem).where(CodexReviewItem.status == "PENDING").order_by(CodexReviewItem.priority, CodexReviewItem.created_at.desc()).limit(200)).all()]
        return render(request, "review.html", title="错误 / 待处理", reviews=rows)

    @application.get("/sessions/{session_id}", response_class=HTMLResponse)
    def session_detail(request: Request, session_id: str) -> HTMLResponse:
        with session_scope(engine) as session:
            row = session.get(SearchSession, session_id)
            if row is None:
                return render(request, "not_found.html", title="未找到", message=f"Session 不存在：{session_id}")
            links = list(session.scalars(select(SearchSessionProject).where(SearchSessionProject.session_id == session_id).order_by(SearchSessionProject.status_at_search, SearchSessionProject.id)).all())
            projects = []
            for link in links:
                project = session.get(Project, link.project_id)
                if project is not None:
                    card = _project_card(session, project)
                    card.update({"status_at_search": link.status_at_search, "is_new": link.is_new, "is_updated": link.is_updated, "is_reopened": link.is_reopened, "matched_keywords": _path_list(link.matched_keywords), "match_score": link.match_score})
                    projects.append(card)
            output_dir = APP_ROOT.parent / "output" / "sessions" / session_id
            files = sorted(str(path) for path in output_dir.glob("*") if path.is_file())
            payload = _session_card(row)
            payload.update({"source_plan": _path_list(row.source_plan_json), "source_health": json.loads(row.sources_json or "[]") if row.sources_json else [], "files": files, "review_path": str(output_dir / "codex_review.md") if (output_dir / "codex_review.md").exists() else None})
        return render(request, "session_detail.html", title=session_id, session=payload, projects=projects)

    @application.get("/sessions/{session_id}/export")
    def session_export(session_id: str, include_unknown: bool = Query(False)) -> Response:
        try:
            result = export_search_session(session_id, database=application.state.database, include_unknown=include_unknown)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return FileResponse(result["path"], filename=Path(result["path"]).name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @application.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request) -> HTMLResponse:
        registry = SourceRegistry.from_file()
        with session_scope(engine) as session:
            rows = []
            for definition in registry.definitions:
                runtime = session.get(Source, definition.source_id)
                item = definition.model_dump()
                if runtime is not None:
                    item.update({"runtime_status": runtime.runtime_status, "health_reason": runtime.health_reason, "last_success_at": _json_value(runtime.last_success_at), "last_failure_at": _json_value(runtime.last_failure_at), "consecutive_failures": runtime.consecutive_failures, "average_items": runtime.average_items, "latest_items": runtime.latest_items, "last_error": runtime.last_error})
                rows.append(item)
        return render(request, "sources.html", title="数据源", sources=rows)

    @application.post("/sources/{source_id}/toggle")
    async def source_toggle(source_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        enabled = _bool(form.get("enabled"))
        update_source(source_id, enabled=enabled, crawl_enabled=enabled)
        return RedirectResponse(url="/sources", status_code=303)

    @application.get("/discovered-sources", response_class=HTMLResponse)
    def discovered_sources_page(request: Request) -> HTMLResponse:
        with session_scope(engine) as session:
            rows = [{"id": row.id, "source_url": row.source_url, "domain": row.domain, "source_name": row.source_name, "region": row.region, "projects_found": row.projects_found, "source_level_guess": row.source_level_guess, "confidence": row.confidence, "status": row.status, "first_seen_at": _json_value(row.first_seen_at), "last_seen_at": _json_value(row.last_seen_at), "discovery_method": row.discovery_method} for row in session.scalars(select(DiscoveredSource).order_by(DiscoveredSource.projects_found.desc(), DiscoveredSource.last_seen_at.desc())).all()]
        return render(request, "discovered_sources.html", title="发现的新数据源", sources=rows)

    @application.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        return render(request, "settings.html", title="搜索设置", **_config_context(application.state.database))

    @application.post("/settings/profile")
    async def settings_profile(request: Request) -> RedirectResponse:
        form = await request.form()
        profile_id = str(form.get("profile_id") or "").strip()
        values = {"name": str(form.get("name") or profile_id), "enabled": "enabled" in form, "regions": _csv(form.get("regions")), "excluded_regions": _csv(form.get("excluded_regions")), "industry_groups": _csv(form.get("industry_groups")), "include_keywords": _csv(form.get("include_keywords")), "exclude_keywords": _csv(form.get("exclude_keywords")), "source_categories": _csv(form.get("source_categories")), "included_sources": _csv(form.get("included_sources")), "excluded_sources": _csv(form.get("excluded_sources")), "announcement_types": _csv(form.get("announcement_types")), "lookback_days": form.get("lookback_days") or 30, "discovery_enabled": "discovery_enabled" in form, "wechat_discovery_enabled": "wechat_discovery_enabled" in form, "max_search_queries_per_run": form.get("max_search_queries_per_run") or 48, "max_queries_per_day": form.get("max_queries_per_day") or 200, "max_results_per_query": form.get("max_results_per_query") or 8, "only_active_opportunities": "only_active_opportunities" in form, "min_source_level": str(form.get("min_source_level") or "E"), "schedule_enabled": False}
        if profile_id:
            save_search_profile(profile_id, values)
        return RedirectResponse(url="/settings", status_code=303)

    @application.post("/settings/profile/copy")
    async def settings_profile_copy(request: Request) -> RedirectResponse:
        form = await request.form()
        source_id, target_id = str(form.get("source_id") or "").strip(), str(form.get("target_id") or "").strip()
        if source_id and target_id:
            copy_search_profile(source_id, target_id, name=str(form.get("name") or "").strip() or None)
        return RedirectResponse(url="/settings", status_code=303)

    @application.post("/settings/profile/{profile_id}/toggle")
    async def settings_profile_toggle(profile_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        toggle_search_profile(profile_id, _bool(form.get("enabled")))
        return RedirectResponse(url="/settings", status_code=303)

    @application.post("/settings/industry")
    async def settings_industry(request: Request) -> RedirectResponse:
        form = await request.form()
        group_id = str(form.get("group_id") or "").strip()
        if group_id:
            save_industry_profile(group_id, {"name": form.get("name") or group_id, "include": _csv(form.get("include")), "exclude": _csv(form.get("exclude")), "synonyms": _csv(form.get("synonyms")), "aliases": _csv(form.get("aliases")), "priority": form.get("priority") or 5})
        return RedirectResponse(url="/settings", status_code=303)

    @application.post("/settings/provider/{provider_name}/toggle")
    async def settings_provider_toggle(provider_name: str, request: Request) -> RedirectResponse:
        form = await request.form()
        update_provider(provider_name, enabled=_bool(form.get("enabled")))
        return RedirectResponse(url="/settings", status_code=303)

    @application.post("/settings/template")
    async def settings_template(request: Request) -> RedirectResponse:
        form = await request.form()
        name, query = str(form.get("name") or "").strip(), str(form.get("query") or "").strip()
        profile = str(form.get("profile") or "northwest_energy")
        if name:
            payload = parse_search_text(query, profile_id=profile) if query else SearchRequest(profile_id=profile)
            save_template(name, payload, description=str(form.get("description") or ""), database=application.state.database)
        return RedirectResponse(url="/settings", status_code=303)

    @application.post("/settings/template/{template_id}/toggle")
    async def settings_template_toggle(template_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        set_template_enabled(template_id, _bool(form.get("enabled")), database=application.state.database)
        return RedirectResponse(url="/settings", status_code=303)

    @application.get("/system", response_class=HTMLResponse)
    def system_page(request: Request) -> HTMLResponse:
        return render(request, "system.html", title="系统状态", system=_system_payload(application.state.database, engine))

    @application.get("/api/projects")
    def api_projects(q: str = "", status: str = "", limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
        with session_scope(engine) as session:
            filters = [or_(Project.ignored.is_(False), Project.ignored.is_(None))]
            if status.upper() in {"OPEN", "UNKNOWN", "CLOSED"}:
                filters.append(Project.status == status.upper())
            if q.strip():
                ids = search_projects(session, q.strip(), limit=limit)
                filters.append(Project.project_id.in_(ids or ["__no_match__"]))
            rows = session.scalars(select(Project).where(*filters).order_by(Project.updated_at.desc()).limit(limit)).all()
            return JSONResponse({"projects": [_project_card(session, row) for row in rows], "count": len(rows), "fts5": fts5_available(engine)})

    @application.get("/api/projects/{project_id}")
    def api_project(project_id: str) -> JSONResponse:
        with session_scope(engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                return JSONResponse({"ok": False, "error": "project not found"}, status_code=404)
            return JSONResponse({"ok": True, "project": _project_card(session, project), "sources": _sources_payload(session, project_id)})

    @application.get("/api/sessions")
    def api_sessions(limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
        with session_scope(engine) as session:
            rows = session.scalars(select(SearchSession).order_by(SearchSession.started_at.desc()).limit(limit)).all()
            return JSONResponse({"sessions": [_session_card(row) for row in rows]})

    @application.get("/api/review")
    def api_review(limit: int = Query(100, ge=1, le=500)) -> JSONResponse:
        with session_scope(engine) as session:
            rows = [review_item_dict(session, row) for row in session.scalars(select(CodexReviewItem).where(CodexReviewItem.status == "PENDING").order_by(CodexReviewItem.priority, CodexReviewItem.created_at.desc()).limit(limit)).all()]
            return JSONResponse({"review_items": _serialize(rows), "count": len(rows)})

    @application.get("/api/sessions/{session_id}")
    def api_session(session_id: str) -> JSONResponse:
        with session_scope(engine) as session:
            row = session.get(SearchSession, session_id)
            if row is None:
                return JSONResponse({"ok": False, "error": "session not found"}, status_code=404)
            links = session.scalars(select(SearchSessionProject).where(SearchSessionProject.session_id == session_id)).all()
            projects = []
            for link in links:
                project = session.get(Project, link.project_id)
                if project:
                    card = _project_card(session, project)
                    card.update({"status_at_search": link.status_at_search, "is_new": link.is_new, "is_updated": link.is_updated, "is_reopened": link.is_reopened})
                    projects.append(card)
            return JSONResponse({"session": _session_card(row), "projects": projects, "source_plan": _path_list(row.source_plan_json), "source_health": json.loads(row.sources_json or "[]") if row.sources_json else []})

    @application.get("/api/sources")
    def api_sources() -> JSONResponse:
        registry = SourceRegistry.from_file()
        with session_scope(engine) as session:
            rows = []
            for definition in registry.definitions:
                payload = definition.model_dump()
                runtime = session.get(Source, definition.source_id)
                if runtime:
                    payload.update({"runtime_status": runtime.runtime_status, "health_reason": runtime.health_reason, "last_success_at": _json_value(runtime.last_success_at), "last_failure_at": _json_value(runtime.last_failure_at), "latest_items": runtime.latest_items, "consecutive_failures": runtime.consecutive_failures})
                rows.append(payload)
        return JSONResponse({"sources": rows})

    @application.get("/api/system")
    def api_system() -> JSONResponse:
        return JSONResponse(_system_payload(application.state.database, engine))

    return application


app = create_app()


__all__ = ["app", "create_app"]
