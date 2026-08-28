"""二次核验编排：先生成精确查询，再把搜索结果作为候选证据保存。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from tender_ai.discovery.contracts import SearchResult
from tender_ai.discovery.providers import CustomSearchProvider, DDGSProvider, FallbackSearchProvider, SearXNGProvider
from tender_ai.models import TenderRecord
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Evidence, Project, VerificationResult, VerificationTask
from tender_ai.storage.repository import project_to_record, save_evidence
from tender_ai.evidence.models import EvidenceRecord
from tender_ai.status.time import now_shanghai


def verification_reasons(project: Project) -> tuple[str, ...]:
    reasons: list[str] = []
    if project.status == "UNKNOWN":
        reasons.append("UNKNOWN_STATUS")
    if (project.source_level or "").upper() in {"D", "E"}:
        reasons.append("LOW_SOURCE_LEVEL")
    if not any(getattr(project, field_name, None) for field_name in ("registration_deadline", "document_deadline", "bid_deadline")):
        reasons.append("MISSING_DEADLINE")
    if not getattr(project, "registration_deadline", None):
        reasons.append("REGISTRATION_UNCLEAR")
    if project.status_reason == "UNKNOWN_CONFLICTING_DATES":
        reasons.append("CONFLICTING_DATES")
    return tuple(dict.fromkeys(reasons))


def build_verification_queries(project: Project | TenderRecord) -> tuple[str, ...]:
    project_name = " ".join(str(getattr(project, "project_name", "") or "").split()).strip()
    tender_code = " ".join(str(getattr(project, "tender_code", "") or "").split()).strip()
    project_code = " ".join(str(getattr(project, "project_code", "") or "").split()).strip()
    owner = " ".join(str(getattr(project, "owner", None) or getattr(project, "purchaser", None) or "").split()).strip()
    agency = " ".join(str(getattr(project, "agency", "") or "").split()).strip()
    values = [project_name, tender_code, project_code, owner, agency]
    queries: list[str] = []
    for value in values:
        if value and value not in queries:
            queries.append(value)
    if project_name:
        queries.extend((f'"{project_name}" 招标', f'"{project_name}" 延期', f'"{project_name}" 变更', f'"{project_name}" 澄清'))
    if tender_code:
        queries.extend((f'"{tender_code}" 延期', f'"{tender_code}" 变更'))
    if owner and project_name:
        queries.append(f'"{owner}" "{project_name}"')
    if agency and project_name:
        queries.append(f'"{agency}" "{project_name}"')
    return tuple(dict.fromkeys(queries))


@dataclass
class VerificationSummary:
    tasks_examined: int = 0
    tasks_created: int = 0
    searches: int = 0
    result_count: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks_examined": self.tasks_examined,
            "tasks_created": self.tasks_created,
            "searches": self.searches,
            "result_count": self.result_count,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


class VerificationRunner:
    def __init__(self, *, database: str | None = None, provider: Any | None = None):
        self.engine = initialize_database(create_engine_for(database))
        self.provider = provider or FallbackSearchProvider([DDGSProvider(), SearXNGProvider(), CustomSearchProvider()])

    def _candidate_projects(self, session: Any, *, max_tasks: int | None, project_id: str | None = None) -> list[Project]:
        if project_id:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(f"unknown project_id: {project_id}")
            # 显式指定项目时，用户要求的是该项目的完整核验，而不是只核验
            # 当前已被规则标记为 UNKNOWN 的项目。
            return [project]
        projects = list(session.scalars(select(Project).where(Project.status == "UNKNOWN").order_by(Project.updated_at.desc())).all())
        candidates = [project for project in projects if verification_reasons(project)]
        return candidates[:max_tasks] if max_tasks else candidates

    def _get_or_create_tasks(self, session: Any, projects: list[Project], summary: VerificationSummary, *, dry_run: bool) -> list[VerificationTask]:
        tasks: list[VerificationTask] = []
        for project in projects:
            for reason in verification_reasons(project):
                summary.tasks_examined += 1
                existing = session.scalar(select(VerificationTask).where(VerificationTask.project_id == project.project_id, VerificationTask.reason == reason, VerificationTask.status.in_(("PENDING", "RUNNING"))))
                if existing is not None:
                    tasks.append(existing)
                    continue
                task = VerificationTask(project_id=project.project_id, reason=reason, status="PENDING", query_texts_json=json.dumps(build_verification_queries(project), ensure_ascii=False), created_at=now_shanghai(), updated_at=now_shanghai())
                tasks.append(task)
                if not dry_run:
                    session.add(task)
                    session.flush()
                    summary.tasks_created += 1
        return tasks

    def run(self, *, project_id: str | None = None, max_tasks: int | None = None, max_results: int = 5, dry_run: bool = False) -> VerificationSummary:
        summary = VerificationSummary(dry_run=dry_run)
        with session_scope(self.engine) as session:
            projects = self._candidate_projects(session, max_tasks=max_tasks, project_id=project_id)
            tasks = self._get_or_create_tasks(session, projects, summary, dry_run=dry_run)
            if dry_run:
                return summary
            for task in tasks:
                task.status = "RUNNING"
                task.attempts = (task.attempts or 0) + 1
                task.last_run_at = now_shanghai()
                queries = json.loads(task.query_texts_json or "[]")
                found = 0
                errors: list[str] = []
                for query in queries:
                    summary.searches += 1
                    try:
                        results = self.provider.search(query, max_results=max_results)
                    except Exception as exc:
                        errors.append(f"{query}: {exc}")
                        continue
                    for result in results:
                        if not isinstance(result, SearchResult):
                            continue
                        exists = session.scalar(select(VerificationResult).where(VerificationResult.task_id == task.id, VerificationResult.url == result.url, VerificationResult.query_text == query))
                        if exists is not None:
                            continue
                        session.add(VerificationResult(task_id=task.id, project_id=task.project_id, query_text=query, title=result.title, url=result.url, snippet=result.snippet, provider=result.provider, published_at=result.published_at, confidence=0.5, created_at=now_shanghai()))
                        found += 1
                        summary.result_count += 1
                task.result_count = found
                task.status = "COMPLETED" if not errors else "COMPLETED_WITH_ERRORS"
                task.notes = "; ".join(errors)[:2000] or "已完成精确项目名、编号、业主和代理机构搜索"
                task.updated_at = now_shanghai()
                project = session.get(Project, task.project_id)
                if project is not None:
                    project.verification_required = True
                    project.verification_reason = ",".join(verification_reasons(project))
                    for result in session.scalars(select(VerificationResult).where(VerificationResult.task_id == task.id)).all():
                        evidence = EvidenceRecord(field_name="verification_candidate", normalized_value=result.url, raw_value=result.title, source_url=result.url, source_text=result.snippet or result.title, extractor=f"verification.{result.provider or 'search'}", confidence=result.confidence)
                        if session.scalar(select(Evidence).where(Evidence.project_id == project.project_id, Evidence.field_name == evidence.field_name, Evidence.source_url == evidence.source_url, Evidence.normalized_value == evidence.normalized_value)) is None:
                            save_evidence(session, evidence, project_id=project.project_id)
            session.flush()
        close = getattr(self.provider, "close", None)
        if close:
            close()
        return summary


__all__ = ["VerificationRunner", "VerificationSummary", "build_verification_queries", "verification_reasons"]
