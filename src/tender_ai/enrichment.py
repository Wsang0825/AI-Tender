"""Recursive candidate enrichment through replaceable public search providers.

The engine never calls an LLM and never treats a search snippet as an official
fact.  It preserves each result as a source pivot, records the query strategy,
and only upgrades verification when an authority-level source is found.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from tender_ai.candidates import CandidateStore, _published, classify_relevance, completeness_score, missing_fields, next_action_for
from tender_ai.candidate_documents import process_candidate_enrichment_result
from tender_ai.discovery.contracts import SearchResult
from tender_ai.discovery.providers import CustomSearchProvider, DDGSProvider, FallbackSearchProvider, SearXNGProvider, SearchProviderError
from tender_ai.identity import resolve_identity
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Candidate, CandidateEnrichmentQuery, CandidateEnrichmentResult, CandidateFact, CandidateSource, SourcePivot
from tender_ai.urls import canonicalize_url


ENRICHMENT_STRATEGIES = (
    "FULL_NAME",
    "PROJECT_CODE",
    "TENDER_CODE",
    "FULL_NAME_TENDERER",
    "FULL_NAME_AGENCY",
    "TENDERER_CORE_PHRASE",
    "AGENCY_CORE_PHRASE",
    "SITE_TENDERER",
    "SITE_AGENCY",
    "PROJECT_CODE_NOTICE",
    "PROJECT_CODE_PDF",
    "NAME_CHANGE",
    "NAME_CLARIFICATION",
    "NAME_EXTENSION",
    "NAME_BIDDER",
    "NAME_RESULT",
    "NAME_FAILED",
    "NAME_RE_TENDER",
)


@dataclass
class EnrichmentSummary:
    candidate_id: str | None = None
    query_count: int = 0
    skipped_cached_queries: int = 0
    result_count: int = 0
    candidate_hits: int = 0
    new_fact_count: int = 0
    new_source_count: int = 0
    new_candidate_count: int = 0
    rounds: int = 0
    state: str | None = None
    errors: list[str] = field(default_factory=list)
    blocked: bool = False
    exhausted: bool = False
    stop_reason: str | None = None
    completeness_score: float = 0.0
    missing_fields: list[str] = field(default_factory=list)
    attachment_count: int = 0
    document_count: int = 0
    evidence_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "query_count": self.query_count,
            "skipped_cached_queries": self.skipped_cached_queries,
            "result_count": self.result_count,
            "candidate_hits": self.candidate_hits,
            "new_fact_count": self.new_fact_count,
            "new_source_count": self.new_source_count,
            "new_candidate_count": self.new_candidate_count,
            "rounds": self.rounds,
            "state": self.state,
            "errors": self.errors,
            "blocked": self.blocked,
            "exhausted": self.exhausted,
            "stop_reason": self.stop_reason,
            "completeness_score": self.completeness_score,
            "missing_fields": self.missing_fields,
            "attachment_count": self.attachment_count,
            "document_count": self.document_count,
            "evidence_count": self.evidence_count,
        }


def _values(candidate: Candidate) -> dict[str, Any]:
    try:
        value = json.loads(candidate.candidate_values_json or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _core_phrase(name: str) -> str:
    phrase = name
    for token in ("招标公告", "采购公告", "中标候选人公示", "结果公示", "项目", "工程", "光伏", "储能"):
        phrase = phrase.replace(token, " ")
    return _clean(phrase)[:80] or name[:80]


def _quote(value: str) -> str:
    return f'"{_clean(value)}"'


def _result_source_level(result: SearchResult) -> str:
    metadata = result.metadata or {}
    configured = str(metadata.get("source_level") or metadata.get("sourceLevel") or "").upper()
    if configured in {"A", "B", "C", "D", "E"}:
        return configured
    domain = (urlparse(result.url).netloc or "").lower().split(":", 1)[0]
    if domain.endswith((".gov.cn", ".gov", ".mil.cn")):
        return "A"
    if domain == "mp.weixin.qq.com":
        return "B"
    if any(token in f"{result.title} {result.snippet}" for token in ("招标代理", "代理机构")):
        return "C"
    return "E"


def generate_enrichment_queries(candidate: Candidate) -> list[tuple[str, str]]:
    """Generate the documented 18 strategies from currently known facts."""

    values = _values(candidate)
    name = _clean(values.get("project_name") or candidate.title)
    code = _clean(values.get("project_code"))
    tender = _clean(values.get("tender_code"))
    tenderer = _clean(values.get("owner") or values.get("tenderer"))
    agency = _clean(values.get("agency"))
    core = _core_phrase(name) if name else ""
    if len(name) < 4 and not (code or tender):
        return []
    queries: list[tuple[str, str]] = []

    def add(strategy: str, text: str) -> None:
        text = _clean(text)
        if strategy not in ENRICHMENT_STRATEGIES or not text:
            return
        if (text, strategy) not in queries:
            queries.append((text, strategy))

    if name:
        add("FULL_NAME", _quote(name))
    if code:
        add("PROJECT_CODE", _quote(code))
    if tender:
        add("TENDER_CODE", _quote(tender))
    if name and tenderer:
        add("FULL_NAME_TENDERER", f"{_quote(name)} {_quote(tenderer)}")
    if name and agency:
        add("FULL_NAME_AGENCY", f"{_quote(name)} {_quote(agency)}")
    if tenderer and core:
        add("TENDERER_CORE_PHRASE", f"{_quote(tenderer)} {core}")
    if agency and core:
        add("AGENCY_CORE_PHRASE", f"{_quote(agency)} {core}")
    if tenderer and (code or name):
        add("SITE_TENDERER", f"site:{tenderer} {_quote(code or name)}")
    if agency and (code or name):
        add("SITE_AGENCY", f"site:{agency} {_quote(code or name)}")
    if code:
        add("PROJECT_CODE_NOTICE", f"{_quote(code)} 招标公告")
        add("PROJECT_CODE_PDF", f"{_quote(code)} PDF")
    if name:
        add("NAME_CHANGE", f"{_quote(name)} 变更")
        add("NAME_CLARIFICATION", f"{_quote(name)} 澄清")
        add("NAME_EXTENSION", f"{_quote(name)} 延期")
        add("NAME_BIDDER", f"{_quote(name)} 中标候选人")
        add("NAME_RESULT", f"{_quote(name)} 中标结果")
        add("NAME_FAILED", f"{_quote(name)} 流标")
        add("NAME_RE_TENDER", f"{_quote(name)} 重新招标")
    return queries


def _provider_default() -> FallbackSearchProvider:
    return FallbackSearchProvider([DDGSProvider(), SearXNGProvider(), CustomSearchProvider()])


class EnrichmentEngine:
    def __init__(self, *, database: str | None = None, provider: Any | None = None):
        self.engine = initialize_database(create_engine_for(database))
        self.provider = provider or _provider_default()

    @staticmethod
    def _add_fact(session: Any, candidate: Candidate, field_name: str, value: str, *, source_url: str, source_level: str | None, confidence: float) -> bool:
        normalized = "".join(str(value).split()).casefold()
        existing = session.scalar(select(CandidateFact).where(CandidateFact.candidate_id == candidate.candidate_id, CandidateFact.field_name == field_name, CandidateFact.normalized_value == normalized))
        if existing is not None:
            return False
        session.add(CandidateFact(candidate_id=candidate.candidate_id, field_name=field_name, value=value, normalized_value=normalized, raw_value=value, source_url=source_url, source_level=source_level, confidence=confidence, is_current=True, created_at=now_shanghai()))
        return True

    @staticmethod
    def _merge_identity(candidate: Candidate, identity: Any, *, source_url: str, source_level: str | None, session: Any) -> int:
        values = _values(candidate)
        facts = {
            "project_name": identity.canonical_project_name,
            "project_code": identity.project_code,
            "tender_code": identity.tender_code,
            "owner": identity.owner or identity.tenderer,
            "agency": identity.agency,
            "project_location": identity.project_location,
        }
        new_count = 0
        for field_name, value in facts.items():
            value = _clean(value)
            if not value:
                continue
            current = _clean(values.get(field_name))
            if not current:
                values[field_name] = value
                new_count += 1
            if EnrichmentEngine._add_fact(session, candidate, field_name, value, source_url=source_url, source_level=source_level, confidence=identity.confidence):
                new_count += 1
        candidate.candidate_values_json = json.dumps(values, ensure_ascii=False, default=str)
        if identity.identity_status == "RESOLVED" and candidate.identity_status == "AMBIGUOUS":
            candidate.identity_status = "RESOLVED"
            candidate.identity_confidence = identity.confidence
        return new_count

    def _record_error(self, summary: EnrichmentSummary, query: CandidateEnrichmentQuery, exc: Exception) -> None:
        query.status = "BLOCKED" if isinstance(exc, SearchProviderError) and exc.manual_action_required else "FAILED"
        query.error = str(exc)[:2000]
        summary.errors.append(f"{query.strategy}: {exc}")
        if isinstance(exc, SearchProviderError) and exc.manual_action_required:
            summary.blocked = True

    def run(self, candidate_id: str, *, search_session_id: str | None = None, max_queries: int = 24, max_results: int = 5, max_rounds: int = 4, dry_run: bool = False, process_attachments: bool = False) -> EnrichmentSummary:
        summary = EnrichmentSummary(candidate_id=candidate_id)
        attachment_result_ids: list[int] = []
        try:
            with session_scope(self.engine) as session:
                candidate = session.get(Candidate, candidate_id)
                if candidate is None:
                    summary.errors.append(f"unknown candidate_id: {candidate_id}")
                    return summary
                if dry_run:
                    summary.query_count = len(generate_enrichment_queries(candidate))
                    summary.state = candidate.enrichment_state
                    return summary
                candidate.enrichment_state = "ENRICHING"
                initial_candidate_ids = set(session.scalars(select(Candidate.candidate_id)).all())
                queue: list[tuple[str, str, int, int | None]] = [(query, strategy, 1, None) for query, strategy in generate_enrichment_queries(candidate)]
                queued = {(query, strategy) for query, strategy, _, _ in queue}
                seen_urls: set[str] = set()
                while queue and summary.query_count < max(0, max_queries) and summary.rounds < max_rounds:
                    query_text, strategy, round_no, parent_id = queue.pop(0)
                    queued.discard((query_text, strategy))
                    current_hash = candidate.content_hash
                    query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
                    cached = session.scalar(select(CandidateEnrichmentQuery).where(CandidateEnrichmentQuery.candidate_id == candidate.candidate_id, CandidateEnrichmentQuery.query_hash == query_hash, CandidateEnrichmentQuery.content_hash == current_hash, CandidateEnrichmentQuery.status == "SUCCESS"))
                    if cached is not None:
                        summary.skipped_cached_queries += 1
                        continue
                    row = CandidateEnrichmentQuery(candidate_id=candidate.candidate_id, search_session_id=search_session_id, parent_query_id=parent_id, query_text=query_text, strategy=strategy, round_no=round_no, provider=getattr(self.provider, "name", "provider"), status="PENDING", query_hash=query_hash, content_hash=current_hash, executed_at=now_shanghai())
                    session.add(row)
                    session.flush()
                    summary.query_count += 1
                    try:
                        results = self.provider.search(query_text, max_results=max_results)
                    except Exception as exc:
                        self._record_error(summary, row, exc)
                        continue
                    row.status = "SUCCESS"
                    row.results_count = len(results)
                    summary.result_count += len(results)
                    round_new_facts = 0
                    round_new_sources = 0
                    for result in results:
                        if not isinstance(result, SearchResult):
                            continue
                        canonical = canonicalize_url(result.url) or result.url
                        if not canonical or canonical in seen_urls:
                            continue
                        seen_urls.add(canonical)
                        level = _result_source_level(result)
                        identity = resolve_identity(result.title, result.snippet, metadata=result.metadata or {})
                        relevance, _, _ = classify_relevance(f"{result.title} {result.snippet}")
                        if relevance != "IRRELEVANT" or identity.identity_status != "AMBIGUOUS":
                            summary.candidate_hits += 1
                            row.candidate_hits += 1
                        before = session.scalar(select(CandidateSource.id).where(CandidateSource.candidate_id == candidate.candidate_id, CandidateSource.canonical_url == canonical))
                        discovered = CandidateStore.upsert_search_result(session, result, search_session_id=search_session_id, source_level=level, region=identity.project_location)
                        result_row = session.scalar(select(CandidateEnrichmentResult).where(CandidateEnrichmentResult.query_id == row.id, CandidateEnrichmentResult.canonical_url == canonical))
                        if result_row is None:
                            result_row = CandidateEnrichmentResult(
                                query_id=row.id,
                                candidate_id=candidate.candidate_id,
                                discovered_candidate_id=discovered.candidate_id,
                                search_session_id=search_session_id,
                                title=(result.title or "未命名结果")[:500],
                                source_url=result.url,
                                canonical_url=canonical,
                                snippet=(result.snippet or "")[:10000],
                                provider=result.provider,
                                published_at=_published(result.published_at),
                                source_level=level,
                                content_hash=discovered.content_hash,
                                identity_status=identity.identity_status,
                                relevance_class=relevance,
                                is_official=level in {"A", "B"},
                                is_secondary=level not in {"A", "B"},
                                match_type="IDENTITY_MATCH" if identity.identity_status != "AMBIGUOUS" else "DISCOVERY_RESULT",
                                created_at=now_shanghai(),
                            )
                            session.add(result_row)
                            session.flush()
                        if process_attachments and level in {"A", "B", "C"} and relevance != "IRRELEVANT":
                            attachment_result_ids.append(result_row.id)
                        CandidateStore._link_source(session, candidate, source_url=result.url, original_url=result.url, source_level=level, source_type="enrichment", provider=result.provider, source_title=result.title, snippet=result.snippet, is_official=level in {"A", "B"}, is_secondary=level not in {"A", "B"}, content_hash_value=discovered.content_hash)
                        if before is None:
                            round_new_sources += 1
                        round_new_facts += self._merge_identity(candidate, identity, source_url=result.url, source_level=level, session=session)
                        if level in {"A", "B"}:
                            candidate.official_found = True
                            if candidate.verification_status in {"SECONDARY_ONLY", "DISCOVERY_LEAD", "UNVERIFIED"}:
                                candidate.verification_status = "OFFICIAL_PARTIAL"
                        if identity.tenderer or identity.owner or identity.agency or identity.project_code or identity.tender_code:
                            for entity_type, entity_value in (("tenderer", identity.tenderer or identity.owner), ("agency", identity.agency), ("project_code", identity.project_code), ("region", identity.project_location)):
                                if not entity_value:
                                    continue
                                exists = session.scalar(select(SourcePivot).where(SourcePivot.candidate_id == candidate.candidate_id, SourcePivot.entity_type == entity_type, SourcePivot.entity_value == entity_value, SourcePivot.discovered_url == result.url))
                                if exists is None:
                                    session.add(SourcePivot(candidate_id=candidate.candidate_id, entity_type=entity_type, entity_value=entity_value, discovered_url=result.url, domain=(urlparse(canonical).netloc or None), strategy=strategy, confidence=identity.confidence, status="DISCOVERED", created_at=now_shanghai()))
                    row.new_fact_count = round_new_facts
                    row.new_source_count = round_new_sources
                    summary.new_fact_count += round_new_facts
                    summary.new_source_count += round_new_sources
                    # New identity facts are allowed to create the next query
                    # round; this is the recursive part of enrichment.
                    if round_new_facts:
                        for next_query, next_strategy in generate_enrichment_queries(candidate):
                            if (next_query, next_strategy) not in queued and not session.scalar(select(CandidateEnrichmentQuery).where(CandidateEnrichmentQuery.candidate_id == candidate.candidate_id, CandidateEnrichmentQuery.query_text == next_query, CandidateEnrichmentQuery.status.in_(("SUCCESS", "PENDING")))):
                                queue.append((next_query, next_strategy, round_no + 1, row.id))
                                queued.add((next_query, next_strategy))
                    summary.rounds = max(summary.rounds, round_no)
                values = _values(candidate)
                values["source"] = candidate.source_url
                score = completeness_score(values, has_evidence=bool(candidate.evidence_ids_json))
                missing = missing_fields(values)
                summary.completeness_score = score
                summary.missing_fields = missing
                candidate.rank_score = score
                candidate.completeness_score = score
                candidate.missing_fields_json = json.dumps(missing, ensure_ascii=False)
                candidate.last_enriched_at = now_shanghai()
                if summary.blocked:
                    candidate.enrichment_state = "BLOCKED"
                    candidate.blocker = "ACCESS_BLOCKED"
                    summary.stop_reason = "BLOCKED"
                elif candidate.identity_status == "AMBIGUOUS":
                    candidate.enrichment_state = "AMBIGUOUS"
                    candidate.blocker = "IDENTITY_AMBIGUOUS"
                    summary.stop_reason = "IDENTITY_AMBIGUOUS"
                elif score >= 85:
                    candidate.enrichment_state = "COMPLETE"
                    candidate.blocker = None
                    summary.stop_reason = "COMPLETE"
                elif score >= 60:
                    candidate.enrichment_state = "USABLE"
                    candidate.blocker = None if candidate.official_found else candidate.blocker
                    summary.stop_reason = "USABLE"
                elif queue and summary.query_count >= max_queries:
                    candidate.enrichment_state = "EXHAUSTED"
                    candidate.blocker = "ENRICHMENT_EXHAUSTED"
                    summary.exhausted = True
                    summary.stop_reason = "BUDGET_EXHAUSTED"
                elif not queue:
                    candidate.enrichment_state = "PARTIAL"
                    summary.stop_reason = "NO_NEW_FACT" if not (summary.new_fact_count or summary.new_source_count) else "NO_MORE_SOURCES"
                    if not candidate.official_found and candidate.verification_status in {"SECONDARY_ONLY", "DISCOVERY_LEAD", "UNVERIFIED"}:
                        candidate.blocker = "OFFICIAL_NOT_FOUND"
                else:
                    candidate.enrichment_state = "PARTIAL"
                    summary.stop_reason = "NO_MORE_SOURCES"
                candidate.next_action = next_action_for(blocker=candidate.blocker, identity_status=candidate.identity_status, verification_status=candidate.verification_status, missing=missing)
                candidate.enrichment_stop_reason = summary.stop_reason
                candidate.updated_at = now_shanghai()
                summary.state = candidate.enrichment_state
                current_candidate_ids = set(session.scalars(select(Candidate.candidate_id)).all())
                summary.new_candidate_count = len(current_candidate_ids - initial_candidate_ids)
            if process_attachments and attachment_result_ids:
                for result_id in dict.fromkeys(attachment_result_ids):
                    closure = process_candidate_enrichment_result(candidate_id, result_id, database=str(self.engine.url))
                    summary.attachment_count += closure.attachment_count
                    summary.document_count += closure.parsed_count
                    summary.evidence_count += closure.evidence_count
                    if closure.error:
                        summary.errors.append(f"candidate document {result_id}: {closure.error}")
                    if closure.blocker == "ACCESS_BLOCKED":
                        summary.blocked = True
        finally:
            close = getattr(self.provider, "close", None)
            if close:
                close()
        return summary


__all__ = ["ENRICHMENT_STRATEGIES", "EnrichmentEngine", "EnrichmentSummary", "generate_enrichment_queries"]
