"""Durable candidate recall, classification, and source-lineage services."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from sqlalchemy import select

from tender_ai.concepts import RELATION_TYPES
from tender_ai.discovery.contracts import SearchResult
from tender_ai.identity import IdentityResolution, resolve_identity
from tender_ai.status.time import as_shanghai, now_shanghai, parse_datetime
from tender_ai.storage.models import (
    Announcement,
    Attachment,
    Candidate,
    CandidateEnrichmentQuery,
    CandidateFact,
    CandidateSource,
    Evidence,
    Project,
    ProjectSource,
    Source,
    SourcePivot,
)
from tender_ai.urls import canonicalize_url, content_hash


RELEVANCE_CLASSES = ("DIRECT", "EMBEDDED", "STRUCTURAL_RELATED", "PARENT_PROJECT", "ADJACENT", "POSSIBLE", "IRRELEVANT")
VERIFICATION_STATUSES = ("OFFICIAL_VERIFIED", "OFFICIAL_PARTIAL", "MULTI_SOURCE_CONFIRMED", "SECONDARY_ONLY", "DISCOVERY_LEAD", "BLOCKED", "UNVERIFIED")
ENRICHMENT_STATES = ("NEW", "ENRICHING", "USABLE", "COMPLETE", "PARTIAL", "BLOCKED", "EXHAUSTED", "AMBIGUOUS")
BLOCKER_CATEGORIES = {
    "MISSING_ATTACHMENT", "MISSING_DOCUMENT", "MISSING_PARTICIPATION_DEADLINE", "SOURCE_INCOMPLETE",
    "SECONDARY_ONLY", "DATE_CONFLICT", "ACCESS_BLOCKED", "IDENTITY_AMBIGUOUS", "GEO_CONFLICT",
    "EXTRACTION_FAILED", "OFFICIAL_NOT_FOUND", "ENRICHMENT_EXHAUSTED",
}
_RELEVANCE_RANK = {name: index for index, name in enumerate(("IRRELEVANT", "POSSIBLE", "PARENT_PROJECT", "ADJACENT", "STRUCTURAL_RELATED", "EMBEDDED", "DIRECT"))}
_IDENTITY_RANK = {"AMBIGUOUS": 0, "PARTIAL": 1, "RESOLVED": 2}

_DIRECT = ("光伏支架", "支架系统", "支架采购", "支架安装", "组件支架", "柔性支架", "固定支架", "水面光伏支架")
_COMPONENT = ("支架基础", "预埋件", "檩条", "连接件", "支架螺栓", "支架防腐")
_STRUCTURE = ("光伏车棚", "光伏雨棚", "钢结构", "支架加固", "支架防风改造")
_PARENT = ("光伏", "太阳能", "分布式", "屋顶光伏", "农光互补", "光伏电站", "光伏项目", "新能源")
_CONTRACT = ("EPC", "PC", "施工总承包", "专业分包", "设备材料")
_PROCUREMENT = ("招标", "采购", "询比", "资格预审", "中标", "项目", "工程", "公告", "公示")


def _text(value: Any, limit: int = 10000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load_json(value: str | None, default: Any) -> Any:
    try:
        result = json.loads(value or "")
        return result
    except (TypeError, ValueError):
        return default


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.lower().split(":", 1)[0] or None


def _contains(text: str, terms: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def classify_relevance(text: str, *, lead_type: str | None = None) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return relevance plus explicit relationship labels.

    This is intentionally conservative about DIRECT: a bare parent solar
    project is not a component procurement.
    """

    text = _text(text)
    lowered = text.casefold()
    relations: list[str] = []
    concepts: list[str] = []
    if _contains(text, _DIRECT) and _contains(text, _PROCUREMENT):
        relations.append("direct")
        concepts.append("direct")
        return "DIRECT", tuple(relations), tuple(concepts)
    if lead_type in {"PROJECT_LEVEL_EPC"} or (_contains(text, _PARENT) and _contains(text, _CONTRACT)):
        relations.extend(("embedded", "contract_scope"))
        concepts.extend(("parent_projects", "contract_modes"))
        return "EMBEDDED", tuple(relations), tuple(concepts)
    if lead_type in {"ADJACENT_STRUCTURE"} or _contains(text, _STRUCTURE) or _contains(text, _COMPONENT):
        relations.append("adjacent" if lead_type == "ADJACENT_STRUCTURE" else "structural_related")
        concepts.append("structures" if _contains(text, _STRUCTURE) else "components")
        return ("ADJACENT" if lead_type == "ADJACENT_STRUCTURE" else "STRUCTURAL_RELATED"), tuple(relations), tuple(concepts)
    if _contains(text, _PARENT):
        relations.append("parent_project")
        concepts.append("parent_projects")
        return "PARENT_PROJECT", tuple(relations), tuple(concepts)
    if _contains(text, _PROCUREMENT):
        relations.append("possible")
        return "POSSIBLE", tuple(relations), tuple(concepts)
    return "IRRELEVANT", (), ()


def completeness_score(values: Mapping[str, Any], *, has_attachment: bool = False, has_evidence: bool = False) -> float:
    weights = {
        "project_name": 15, "project_code": 5, "tender_code": 5, "owner": 10,
        "project_location": 10, "scope": 10, "publish_time": 5, "document_deadline": 10,
        "bid_deadline": 10, "open_time": 5, "participation_method": 5, "source": 5,
    }
    score = sum(weight for field_name, weight in weights.items() if values.get(field_name))
    if has_attachment:
        score += 3
    if has_evidence:
        score += 2
    return round(min(100.0, float(score)), 2)


def completeness_state(score: float) -> str:
    if score >= 85:
        return "COMPLETE"
    if score >= 60:
        return "USABLE"
    if score >= 30:
        return "PARTIAL"
    return "LEAD_ONLY"


def missing_fields(values: Mapping[str, Any]) -> list[str]:
    fields = ("project_name", "project_code_or_tender_code", "owner", "project_location", "document_deadline", "bid_deadline", "open_time", "participation_method", "source", "attachment")
    missing: list[str] = []
    for field_name in fields:
        if field_name == "project_code_or_tender_code":
            present = values.get("project_code") or values.get("tender_code")
        elif field_name == "attachment":
            present = values.get("has_attachment")
        else:
            present = values.get(field_name)
        if not present:
            missing.append(field_name)
    return missing


def next_action_for(*, blocker: str | None, identity_status: str | None, verification_status: str | None, missing: Iterable[str]) -> str:
    if blocker in {"ACCESS_BLOCKED", "LOGIN_REQUIRED", "CAPTCHA", "HTTP_412", "HTTP_403", "HTTP_429"}:
        return "请用户在Microsoft Edge完成人工验证后，定向重试受阻来源"
    if identity_status == "AMBIGUOUS":
        return "先由Codex确认项目身份，再生成精确追源查询"
    if blocker == "DATE_CONFLICT":
        return "读取最新变更/延期公告并保留新旧Evidence"
    if blocker in {"MISSING_ATTACHMENT", "EXTRACTION_FAILED"}:
        return "继续寻找并下载官方附件，离线解析后补充Evidence"
    if blocker == "MISSING_DOCUMENT":
        return "继续获取官方正文或附件，完成离线解析后补充Evidence"
    if blocker == "MISSING_PARTICIPATION_DEADLINE":
        return "补搜报名、文件获取、投标截止和变更公告；无法确认时交给Codex Review"
    if blocker == "OFFICIAL_NOT_FOUND":
        return "继续用项目全名、编号、招标人和代理机构追查官方来源"
    if blocker in {"ENRICHMENT_EXHAUSTED", "NO_MORE_SOURCES"}:
        return "已耗尽当前追源预算；保留候选，下一次按需重新核验"
    if blocker == "SOURCE_INCOMPLETE":
        return "补充正文或官方附件，确认参与窗口后再计算状态"
    if verification_status in {"SECONDARY_ONLY", "DISCOVERY_LEAD", "UNVERIFIED"}:
        return "用项目全名、编号、招标人和代理机构追查官方公告"
    values = list(missing)
    return "补充缺失字段: " + ", ".join(values[:5]) if values else "继续观察变更公告"


def candidate_id_for(candidate_key: str) -> str:
    return "candidate_" + hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()[:40]


def _published(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_shanghai(value)
    return parse_datetime(value) if value else None


def _infer_source_level(url: str | None) -> str:
    domain = _domain(url) or ""
    if domain.endswith((".gov.cn", ".gov", ".mil.cn")):
        return "A"
    if domain == "mp.weixin.qq.com":
        return "B"
    return "E"


def _official(level: str | None) -> bool:
    return (level or "").upper() in {"A", "B"}


def _best_source_level(left: str | None, right: str | None) -> str | None:
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}
    values = [str(value).upper() for value in (left, right) if value]
    if not values:
        return None
    return min(values, key=lambda value: ranks.get(value, 9))


def _best_relevance(left: str | None, right: str | None) -> str:
    values = [str(value).upper() for value in (left, right) if value]
    if not values:
        return "POSSIBLE"
    return max(values, key=lambda value: _RELEVANCE_RANK.get(value, 0))


def _best_identity_status(left: str | None, right: str | None) -> str:
    values = [str(value).upper() for value in (left, right) if value]
    if not values:
        return "AMBIGUOUS"
    return max(values, key=lambda value: _IDENTITY_RANK.get(value, 0))


def _merge_candidate_values(previous: Mapping[str, Any], current: Mapping[str, Any], *, identity_status: str | None = None) -> dict[str, Any]:
    """Merge facts without allowing a sparse result to erase enrichment."""

    merged = dict(previous or {})
    identity_fields = {"project_name", "project_code", "tender_code", "owner", "tenderer", "agency", "project_location"}
    ambiguous = str(identity_status or "").upper() == "AMBIGUOUS"
    for name, value in (current or {}).items():
        if value in (None, "", [], {}, ()):
            continue
        if ambiguous and name in identity_fields and merged.get(name):
            continue
        merged[name] = value
    return merged


def _project_values(project: Project, announcement: Announcement | None = None) -> dict[str, Any]:
    return {
        "project_name": project.project_name,
        "project_code": project.project_code,
        "tender_code": project.tender_code,
        "owner": project.owner or project.purchaser or project.tenderer,
        "agency": project.agency,
        "project_location": getattr(project, "project_location", None) or project.location or " / ".join(item for item in (project.province, project.city, project.county) if item),
        "scope": project.project_scale or project.project_type or project.industry,
        "publish_time": project.publish_time,
        "document_deadline": project.document_deadline,
        "bid_deadline": project.bid_deadline,
        "open_time": project.open_time,
        "participation_method": project.participation_method,
        "source": project.source_url or (announcement.source_url if announcement else None),
        "has_attachment": False,
    }


class CandidateStore:
    """Small persistence service used by Search, Discovery and Enrichment."""

    @staticmethod
    def _apply_project_dimensions(project: Project, *, relevance: str, relations: tuple[str, ...], concepts: tuple[str, ...], verification: str, state: str, blocker: str | None, next_action: str, missing: list[str], rank: float, identity: IdentityResolution | None = None) -> None:
        project.tender_status = project.status
        project.relevance_class = relevance
        project.verification_status = verification
        project.enrichment_state = state
        project.blocker = blocker
        project.next_action = next_action
        project.identity_status = (identity.identity_status if identity else getattr(project, "identity_status", None)) or "RESOLVED"
        if identity is not None:
            project.identity_confidence = identity.confidence
            project.project_location = identity.project_location or project.project_location or project.location
            project.province = project.province or identity.province
            project.city = project.city or identity.city
            project.county = project.county or identity.county
        project.relation_types_json = _json(list(relations))
        project.matched_concepts_json = _json(list(concepts))
        project.missing_fields_json = _json(missing)
        project.rank_score = rank

    @staticmethod
    def _link_source(session: Any, candidate: Candidate, *, source_url: str, original_url: str | None = None, source_id: str | None = None, source_name: str | None = None, source_level: str | None = None, source_type: str | None = None, provider: str | None = None, source_title: str | None = None, snippet: str | None = None, published_at: datetime | None = None, is_official: bool = False, is_secondary: bool = False, content_hash_value: str | None = None, source_location: str | None = None) -> CandidateSource:
        canonical = canonicalize_url(source_url) or source_url
        # The project session deliberately disables autoflush.  A project may
        # expose the same canonical URL through several ProjectSource rows;
        # querying only SQLite would miss a just-added pending row and enqueue
        # duplicate CandidateSource records for one flush.
        row = next(
            (
                item
                for item in session.new
                if isinstance(item, CandidateSource)
                and item.candidate_id == candidate.candidate_id
                and item.canonical_url == canonical
            ),
            None,
        )
        if row is None:
            row = session.scalar(select(CandidateSource).where(CandidateSource.candidate_id == candidate.candidate_id, CandidateSource.canonical_url == canonical))
        if row is None:
            row = CandidateSource(candidate_id=candidate.candidate_id, source_id=source_id, source_url=source_url, original_url=original_url or source_url, canonical_url=canonical, source_domain=_domain(canonical), source_name=source_name, source_level=source_level, source_type=source_type, provider=provider, source_title=source_title, snippet=snippet, source_location=source_location, published_at=published_at, content_hash=content_hash_value, is_official=is_official, is_secondary=is_secondary, access_status="DISCOVERED", first_seen_at=now_shanghai(), last_seen_at=now_shanghai())
            session.add(row)
        else:
            row.last_seen_at = now_shanghai()
            for name, value in {"source_id": source_id, "source_name": source_name, "source_type": source_type, "provider": provider, "source_title": source_title, "snippet": snippet, "published_at": published_at, "content_hash": content_hash_value, "source_location": source_location}.items():
                if value not in (None, ""):
                    setattr(row, name, value)
            row.source_level = _best_source_level(row.source_level, source_level)
            row.is_official = row.is_official or is_official
            row.is_secondary = row.is_secondary or is_secondary
        return row

    @staticmethod
    def _sync_project_sources(session: Any, candidate: Candidate) -> list[CandidateSource]:
        return list(session.scalars(select(CandidateSource).where(CandidateSource.candidate_id == candidate.candidate_id)).all())

    @classmethod
    def upsert_project(cls, session: Any, project: Project, *, announcement: Announcement | None = None, search_session_id: str | None = None, matched: Iterable[str] = (), source_location: str | None = None) -> Candidate:
        text = " ".join(_text(value) for value in (project.project_name, project.project_scale, project.project_type, project.industry, project.qualification_summary, project.participation_method, announcement.clean_text if announcement else ""))
        relevance, relations, concepts = classify_relevance(text)
        if relevance == "IRRELEVANT":
            relevance, relations, concepts = "POSSIBLE", ("possible",), tuple(matched)
        source_level = project.source_level or "E"
        if _official(source_level):
            verification = "OFFICIAL_VERIFIED" if announcement and (announcement.clean_text or announcement.raw_content) else "OFFICIAL_PARTIAL"
        else:
            verification = "SECONDARY_ONLY"
        values = _project_values(project, announcement)
        values["has_attachment"] = bool(session.scalar(select(Attachment.id).where(Attachment.project_id == project.project_id).limit(1)))
        missing = missing_fields(values)
        score = completeness_score(values, has_attachment=values["has_attachment"], has_evidence=bool(session.scalar(select(Evidence.id).where(Evidence.project_id == project.project_id).limit(1))))
        identity = resolve_identity(project.raw_project_name or project.project_name, announcement.clean_text if announcement else "", metadata={"project_name": project.canonical_project_name or project.project_name, "project_code": project.project_code, "tender_code": project.tender_code, "owner": project.owner, "agency": project.agency})
        blocker = None
        if project.status_reason == "UNKNOWN_CONFLICTING_DATES":
            blocker = "DATE_CONFLICT"
        elif project.status_reason == "UNKNOWN_NEEDS_CODEX_REVIEW":
            blocker = "SOURCE_INCOMPLETE"
        elif project.status == "UNKNOWN" and not any(getattr(project, field_name, None) for field_name in ("qualification_deadline", "registration_deadline", "document_deadline")):
            blocker = "MISSING_PARTICIPATION_DEADLINE"
        elif project.status == "UNKNOWN":
            blocker = "SOURCE_INCOMPLETE"
        elif str(project.source_level or "").upper() in {"D", "E"}:
            blocker = "SECONDARY_ONLY"
        state = "COMPLETE" if score >= 85 else "USABLE" if score >= 60 else "PARTIAL" if score >= 30 else "NEW"
        candidate_key = f"project:{project.project_id}"
        candidate = session.scalar(select(Candidate).where(Candidate.candidate_key == candidate_key))
        if candidate is not None:
            prior_values = _load_json(candidate.candidate_values_json, {})
            if not isinstance(prior_values, dict):
                prior_values = {}
            values = _merge_candidate_values(prior_values, values, identity_status=identity.identity_status)
            missing = missing_fields(values)
            score = completeness_score(
                values,
                has_attachment=bool(values.get("has_attachment")),
                has_evidence=bool(session.scalar(select(Evidence.id).where(Evidence.project_id == project.project_id).limit(1))),
            )
        candidate_id = candidate.candidate_id if candidate else candidate_id_for(candidate_key)
        old_hash = candidate.content_hash if candidate else None
        new_hash = project.content_hash or (announcement.content_hash if announcement else None)
        if candidate is None:
            candidate = Candidate(candidate_id=candidate_id, candidate_key=candidate_key, project_id=project.project_id, announcement_id=announcement.id if announcement else None, search_session_id=search_session_id, title=project.project_name, raw_title=project.raw_project_name, source_url=(announcement.source_url if announcement else None) or project.source_url, original_url=(announcement.original_url if announcement else None) or project.original_url, canonical_url=(announcement.canonical_url if announcement else None) or project.canonical_url, snippet=(announcement.clean_text if announcement else None), source_domain=_domain((announcement.source_url if announcement else None) or project.source_url), source_level=source_level, relevance_class=relevance, verification_status=verification, tender_status=project.status, enrichment_state=state, identity_status=identity.identity_status, identity_confidence=identity.confidence, blocker=blocker, next_action=next_action_for(blocker=blocker, identity_status=identity.identity_status, verification_status=verification, missing=missing), candidate_class="PERSISTED_PROJECT", relation_types_json=_json(list(relations)), matched_concepts_json=_json(list(concepts)), missing_fields_json=_json(missing), candidate_values_json=_json(values), source_ids_json=_json([]), official_found=_official(source_level), rank_score=score, completeness_score=score, review_priority=3 if project.status == "UNKNOWN" else 5, first_seen_at=now_shanghai(), last_seen_at=now_shanghai(), last_change_at=now_shanghai(), persisted_reason="项目已进入统一候选池；状态、来源等级或Evidence不影响保留")
            candidate.content_hash = new_hash
            session.add(candidate)
        else:
            content_changed = candidate.content_hash != new_hash
            changed = any(getattr(candidate, name) != value for name, value in {"title": project.project_name, "content_hash": new_hash, "tender_status": project.status, "relevance_class": relevance, "verification_status": verification, "enrichment_state": state}.items())
            candidate.project_id = project.project_id
            candidate.announcement_id = announcement.id if announcement else candidate.announcement_id
            candidate.search_session_id = search_session_id or candidate.search_session_id
            candidate.title = project.project_name
            candidate.raw_title = project.raw_project_name
            candidate.source_url = (announcement.source_url if announcement else None) or project.source_url
            candidate.original_url = (announcement.original_url if announcement else None) or project.original_url
            candidate.canonical_url = (announcement.canonical_url if announcement else None) or project.canonical_url
            candidate.snippet = (announcement.clean_text if announcement else None) or candidate.snippet
            candidate.source_level = _best_source_level(candidate.source_level, source_level)
            candidate.content_hash = new_hash
            candidate.relevance_class = relevance
            candidate.verification_status = "OFFICIAL_VERIFIED" if _official(candidate.source_level) and verification == "OFFICIAL_VERIFIED" else ("OFFICIAL_PARTIAL" if _official(candidate.source_level) else verification)
            candidate.tender_status = project.status
            if content_changed or candidate.enrichment_state not in {"COMPLETE", "USABLE", "BLOCKED", "EXHAUSTED"}:
                candidate.enrichment_state = state
            candidate.identity_status = _best_identity_status(candidate.identity_status, identity.identity_status)
            candidate.identity_confidence = max(float(candidate.identity_confidence or 0), float(identity.confidence or 0))
            candidate.blocker = blocker
            candidate.next_action = next_action_for(blocker=blocker, identity_status=identity.identity_status, verification_status=verification, missing=missing)
            candidate.relation_types_json = _json(list(relations))
            candidate.matched_concepts_json = _json(list(concepts))
            candidate.missing_fields_json = _json(missing)
            candidate.candidate_values_json = _json(values)
            candidate.official_found = candidate.official_found or _official(source_level)
            candidate.rank_score = score
            candidate.completeness_score = score
            candidate.review_priority = 3 if project.status == "UNKNOWN" else 5
            candidate.last_seen_at = now_shanghai()
            candidate.updated_at = now_shanghai()
            if changed or old_hash != new_hash:
                candidate.last_change_at = now_shanghai()
        session.flush()
        cls._apply_project_dimensions(project, relevance=relevance, relations=relations, concepts=concepts, verification=verification, state=state, blocker=blocker, next_action=candidate.next_action or "", missing=missing, rank=score, identity=identity)
        source_links = list(session.scalars(select(ProjectSource).where(ProjectSource.project_id == project.project_id)).all())
        for link in source_links:
            source = session.get(Source, link.source_id)
            if link.source_url:
                cls._link_source(session, candidate, source_url=link.source_url, source_id=link.source_id, source_name=source.source_name if source else None, source_level=source_level, source_type=source.category if source else None, source_title=project.project_name, is_official=_official(source_level), is_secondary=not _official(source_level), content_hash_value=link.content_hash, source_location=source_location or (source.region if source else None))
        if candidate.source_url:
            cls._link_source(session, candidate, source_url=candidate.source_url, source_id=None, source_name=project.source_name, source_level=source_level, source_type=project.source_type, source_title=project.project_name, snippet=candidate.snippet, is_official=_official(source_level), is_secondary=not _official(source_level), content_hash_value=new_hash, source_location=source_location)
        source_rows = cls._sync_project_sources(session, candidate)
        candidate.source_ids_json = _json(list(dict.fromkeys(row.source_id for row in source_rows if row.source_id)))
        candidate.evidence_ids_json = _json([row.id for row in session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all()])
        session.flush()
        return candidate

    @classmethod
    def upsert_search_result(cls, session: Any, result: SearchResult, *, search_session_id: str | None = None, source_level: str | None = None, source_id: str | None = None, region: str | None = None, lead_type: str | None = None) -> Candidate:
        canonical = canonicalize_url(result.url) or result.url
        key = f"url:{canonical}"
        identity = resolve_identity(result.title, result.snippet, metadata=result.metadata or {})
        text = f"{result.title} {result.snippet} {' '.join(str(value) for value in (result.metadata or {}).values())}"
        relevance, relations, concepts = classify_relevance(text, lead_type=lead_type)
        level = source_level or _infer_source_level(canonical)
        official = _official(level)
        existing = session.scalar(select(Candidate).where(Candidate.candidate_key == key))
        result_hash = content_hash(f"{result.title}\n{result.snippet}")
        values = {"project_name": identity.canonical_project_name, "project_code": identity.project_code, "tender_code": identity.tender_code, "owner": identity.owner or identity.tenderer, "agency": identity.agency, "project_location": identity.project_location or region, "scope": text[:500], "publish_time": result.published_at, "source": result.url, "has_attachment": False}
        missing = missing_fields(values)
        score = completeness_score(values, has_evidence=False)
        verification = "OFFICIAL_PARTIAL" if official else ("SECONDARY_ONLY" if level in {"D", "E"} else "DISCOVERY_LEAD")
        blocker = "IDENTITY_AMBIGUOUS" if identity.identity_status == "AMBIGUOUS" else ("SOURCE_INCOMPLETE" if not result.snippet else ("SECONDARY_ONLY" if level in {"D", "E"} else None))
        state = "AMBIGUOUS" if identity.identity_status == "AMBIGUOUS" else "PARTIAL"
        if existing is None:
            candidate = Candidate(candidate_id=candidate_id_for(key), candidate_key=key, project_id=None, announcement_id=None, search_session_id=search_session_id, source_id=source_id, title=result.title[:500] or "未命名候选", raw_title=result.title, source_url=result.url, original_url=result.url, canonical_url=canonical, snippet=result.snippet[:10000], source_domain=_domain(canonical), source_level=level, relevance_class=relevance, verification_status=verification, tender_status="UNKNOWN", enrichment_state=state, identity_status=identity.identity_status, identity_confidence=identity.confidence, blocker=blocker, next_action=next_action_for(blocker=blocker, identity_status=identity.identity_status, verification_status=verification, missing=missing), candidate_class="SECONDARY_LEAD" if not official else "DISCOVERY_RESULT", relation_types_json=_json(list(relations)), matched_concepts_json=_json(list(concepts)), missing_fields_json=_json(missing), candidate_values_json=_json(values), evidence_ids_json=_json([]), source_ids_json=_json([source_id] if source_id else []), official_found=official, rank_score=score, completeness_score=score, review_priority=5, first_seen_at=now_shanghai(), last_seen_at=now_shanghai(), last_change_at=now_shanghai(), persisted_reason="Discovery候选先保存，再进行官方追源；不因官方未找到或预算耗尽删除")
            session.add(candidate)
        else:
            content_changed = existing.content_hash != result_hash
            changed = content_changed or existing.title != result.title
            candidate = existing
            prior_values = _load_json(existing.candidate_values_json, {})
            if not isinstance(prior_values, dict):
                prior_values = {}
            # Enrichment facts are cumulative.  A later sparse snippet must
            # not erase an owner/code learned from an earlier official result.
            values = _merge_candidate_values(prior_values, values, identity_status=identity.identity_status)
            missing = missing_fields(values)
            score = completeness_score(values, has_evidence=False)
            candidate.search_session_id = search_session_id or candidate.search_session_id
            candidate.source_id = source_id or candidate.source_id
            candidate.title = result.title[:500] or candidate.title
            candidate.raw_title = result.title
            candidate.snippet = result.snippet[:10000]
            candidate.source_level = _best_source_level(candidate.source_level, level)
            candidate.relevance_class = _best_relevance(candidate.relevance_class, relevance)
            candidate.verification_status = "OFFICIAL_PARTIAL" if official else candidate.verification_status
            candidate.identity_status = _best_identity_status(candidate.identity_status, identity.identity_status)
            candidate.identity_confidence = max(float(candidate.identity_confidence or 0), float(identity.confidence or 0))
            candidate.blocker = (
                "IDENTITY_AMBIGUOUS"
                if candidate.identity_status == "AMBIGUOUS"
                else "SOURCE_INCOMPLETE"
                if not result.snippet
                else None
            )
            candidate.next_action = next_action_for(blocker=candidate.blocker, identity_status=identity.identity_status, verification_status=candidate.verification_status, missing=missing)
            candidate.missing_fields_json = _json(missing)
            candidate.candidate_values_json = _json(values)
            candidate.rank_score = score
            candidate.completeness_score = score
            candidate.review_priority = 5
            prior_concepts = _load_json(existing.matched_concepts_json, [])
            prior_relations = _load_json(existing.relation_types_json, [])
            candidate.matched_concepts_json = _json(list(dict.fromkeys([*(prior_concepts if isinstance(prior_concepts, list) else []), *concepts])))
            candidate.relation_types_json = _json(list(dict.fromkeys([*(prior_relations if isinstance(prior_relations, list) else []), *relations])))
            candidate.official_found = candidate.official_found or official
            if content_changed:
                candidate.enrichment_state = state
                candidate.last_enriched_at = None
                candidate.blocker = blocker
            candidate.last_seen_at = now_shanghai()
            candidate.updated_at = now_shanghai()
            if changed:
                candidate.last_change_at = now_shanghai()
        candidate.content_hash = result_hash
        session.flush()
        cls._link_source(session, candidate, source_url=result.url, original_url=result.url, source_id=source_id, source_name=canonical, source_level=level, source_type="discovery", provider=result.provider, source_title=result.title, snippet=result.snippet, published_at=_published(result.published_at), is_official=official, is_secondary=not official, content_hash_value=result_hash, source_location=region)
        source_rows = cls._sync_project_sources(session, candidate)
        candidate.source_ids_json = _json(list(dict.fromkeys(row.source_id for row in source_rows if row.source_id)))
        # Facts are separate from the candidate headline so newly discovered
        # owner/code/agency values can trigger another enrichment round.
        facts = {"project_name": identity.canonical_project_name, "project_code": identity.project_code, "tender_code": identity.tender_code, "owner": identity.owner or identity.tenderer, "agency": identity.agency, "project_location": identity.project_location}
        for field_name, value in facts.items():
            if not value:
                continue
            normalized = re.sub(r"\s+", "", str(value)).casefold()
            found = session.scalar(select(CandidateFact).where(CandidateFact.candidate_id == candidate.candidate_id, CandidateFact.field_name == field_name, CandidateFact.normalized_value == normalized))
            if found is None:
                session.add(CandidateFact(candidate_id=candidate.candidate_id, field_name=field_name, value=str(value), normalized_value=normalized, raw_value=str(value), source_url=result.url, source_level=level, confidence=identity.confidence, is_current=True, created_at=now_shanghai()))
        for entity_type, entity_value in (("tenderer", identity.tenderer or identity.owner), ("agency", identity.agency), ("project_code", identity.project_code), ("region", identity.project_location or region)):
            if not entity_value:
                continue
            exists = session.scalar(select(SourcePivot).where(SourcePivot.candidate_id == candidate.candidate_id, SourcePivot.entity_type == entity_type, SourcePivot.entity_value == entity_value))
            if exists is None:
                session.add(SourcePivot(candidate_id=candidate.candidate_id, entity_type=entity_type, entity_value=entity_value, source_id=source_id, discovered_url=result.url, domain=_domain(canonical), strategy="discovery_result", confidence=identity.confidence, status="DISCOVERED", created_at=now_shanghai()))
        session.flush()
        return candidate


def candidate_dict(row: Candidate) -> dict[str, Any]:
    def value(item: Any) -> Any:
        return as_shanghai(item).isoformat() if isinstance(item, datetime) else item
    return {
        "candidate_id": row.candidate_id,
        "candidate_key": row.candidate_key,
        "project_id": row.project_id,
        "announcement_id": row.announcement_id,
        "search_session_id": row.search_session_id,
        "title": row.title,
        "raw_title": row.raw_title,
        "source_url": row.source_url,
        "original_url": row.original_url,
        "canonical_url": row.canonical_url,
        "snippet": row.snippet,
        "source_domain": row.source_domain,
        "source_level": row.source_level,
        "content_hash": row.content_hash,
        "relevance_class": row.relevance_class,
        "verification_status": row.verification_status,
        "tender_status": row.tender_status,
        "enrichment_state": row.enrichment_state,
        "identity_status": row.identity_status,
        "identity_confidence": row.identity_confidence,
        "blocker": row.blocker,
        "next_action": row.next_action,
        "candidate_class": row.candidate_class,
        "relation_types": _load_json(row.relation_types_json, []),
        "matched_concepts": _load_json(row.matched_concepts_json, []),
        "missing_fields": _load_json(row.missing_fields_json, []),
        "candidate_values": _load_json(row.candidate_values_json, {}),
        "evidence_ids": _load_json(row.evidence_ids_json, []),
        "source_ids": _load_json(row.source_ids_json, []),
        "official_found": row.official_found,
        "rank_score": row.rank_score,
        "completeness_score": row.completeness_score,
        "enrichment_stop_reason": row.enrichment_stop_reason,
        "review_priority": row.review_priority,
        "first_seen_at": value(row.first_seen_at),
        "last_seen_at": value(row.last_seen_at),
        "last_change_at": value(row.last_change_at),
        "last_enriched_at": value(row.last_enriched_at),
        "persisted_reason": row.persisted_reason,
    }


__all__ = [
    "CandidateStore", "BLOCKER_CATEGORIES", "ENRICHMENT_STATES", "RELEVANCE_CLASSES", "VERIFICATION_STATUSES",
    "candidate_dict", "candidate_id_for", "classify_relevance", "completeness_score",
    "completeness_state", "missing_fields", "next_action_for",
]
