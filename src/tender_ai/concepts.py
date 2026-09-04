"""Configurable concept expansion for high-recall tender searches.

The search engine must not encode one industry's vocabulary in Python.  A
concept file describes relations between a user's intent and the terms that
should be searched.  The module intentionally returns small contextual query
groups instead of one large OR expression; this makes provenance and later
recall evaluation possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from tender_ai.config_loader import DEFAULT_CONFIG_DIR


RELATION_TYPES = (
    "direct",
    "component",
    "embedded",
    "structural_related",
    "parent_project",
    "contract_scope",
    "adjacent",
)


@dataclass(frozen=True)
class ConceptGroup:
    group_id: str
    terms: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()
    priority: int = 5


@dataclass(frozen=True)
class ConceptQuery:
    text: str
    concept_group: str
    relation: str
    priority: int = 5
    matched_concepts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConceptCatalog:
    groups: tuple[ConceptGroup, ...] = ()
    query_matrix: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_file(cls, path: Path | None = None) -> "ConceptCatalog":
        target = path or (DEFAULT_CONFIG_DIR / "concepts" / "photovoltaic_support.yaml")
        if not target.exists():
            return cls()
        with target.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"concept配置根节点必须是对象: {target}")
        raw_groups = payload.get("concept_groups") or payload.get("groups") or {}
        groups: list[ConceptGroup] = []
        if isinstance(raw_groups, Mapping):
            group_items = [dict(value, group_id=str(key)) if isinstance(value, Mapping) else {"group_id": key, "terms": value} for key, value in raw_groups.items()]
        elif isinstance(raw_groups, list):
            group_items = raw_groups
        else:
            raise ValueError(f"concept_groups必须是对象或列表: {target}")
        for item in group_items:
            if not isinstance(item, Mapping) or not item.get("group_id"):
                raise ValueError(f"concept group缺少group_id: {item!r}")
            raw_terms = item.get("terms") or item.get("include") or []
            if isinstance(raw_terms, str):
                raw_terms = [raw_terms]
            raw_relations = item.get("relations") or []
            if isinstance(raw_relations, str):
                raw_relations = [raw_relations]
            relations = tuple(str(value) for value in raw_relations if str(value) in RELATION_TYPES)
            groups.append(
                ConceptGroup(
                    group_id=str(item["group_id"]),
                    terms=tuple(dict.fromkeys(str(value).strip() for value in raw_terms if str(value).strip())),
                    relations=relations,
                    priority=int(item.get("priority", 5)),
                )
            )
        matrix = payload.get("query_matrix") or []
        if not isinstance(matrix, list):
            raise ValueError(f"query_matrix必须是列表: {target}")
        return cls(tuple(groups), tuple(item for item in matrix if isinstance(item, Mapping)))

    def group(self, group_id: str) -> ConceptGroup | None:
        return next((item for item in self.groups if item.group_id == group_id), None)

    def terms_for(self, group_ids: Iterable[str], *, relations: Iterable[str] = ()) -> tuple[str, ...]:
        wanted_relations = set(relations)
        terms: list[str] = []
        for group_id in group_ids:
            group = self.group(group_id)
            if group is None:
                continue
            if wanted_relations and group.relations and not wanted_relations.intersection(group.relations):
                continue
            terms.extend(group.terms)
        return tuple(dict.fromkeys(terms))

    def expand_queries(
        self,
        *,
        group_ids: Iterable[str],
        relation_types: Iterable[str] = (),
        extra_terms: Iterable[str] = (),
        area: str | None = None,
        max_queries: int | None = None,
    ) -> list[ConceptQuery]:
        """Expand configured concepts into contextual, attributable queries."""

        selected_ids = tuple(dict.fromkeys(str(value) for value in group_ids if str(value).strip()))
        wanted_relations = set(relation_types)
        selected = [group for group in self.groups if group.group_id in selected_ids]
        if not selected:
            selected = list(self.groups)
        selected_terms = list(dict.fromkeys(term for group in selected for term in group.terms))
        selected_terms.extend(str(value).strip() for value in extra_terms if str(value).strip())
        selected_terms = list(dict.fromkeys(selected_terms))
        queries: list[ConceptQuery] = []
        seen: set[str] = set()

        def add(text: str, group_id: str, relation: str, priority: int, concepts: Iterable[str]) -> None:
            text = " ".join(text.split()).strip()
            if not text or text in seen:
                return
            seen.add(text)
            queries.append(ConceptQuery(text, group_id, relation, priority, tuple(dict.fromkeys(concepts))))

        # Explicit matrix rows are the preferred representation.  A row may
        # refer to terms by literal text or by group id.
        for row in self.query_matrix:
            row_groups = row.get("groups") or row.get("concept_groups") or []
            if isinstance(row_groups, str):
                row_groups = [row_groups]
            if row_groups and not set(str(value) for value in row_groups).intersection(selected_ids):
                continue
            row_relations = row.get("relations") or [row.get("relation") or "parent_project"]
            if isinstance(row_relations, str):
                row_relations = [row_relations]
            for relation in row_relations:
                relation = str(relation)
                if wanted_relations and relation not in wanted_relations:
                    continue
                raw_terms = row.get("terms") or row.get("with") or []
                if isinstance(raw_terms, str):
                    raw_terms = [raw_terms]
                terms = [str(value) for value in raw_terms]
                if not terms:
                    terms = selected_terms[:2]
                text = " ".join(([area] if area else []) + terms)
                add(text, str(row.get("group_id") or row_groups[0] if row_groups else selected_ids[0] if selected_ids else "general"), relation, int(row.get("priority", 3)), terms)

        # Always retain direct and category queries, even when a matrix file is
        # incomplete.  This is a safe high-recall fallback, not a hard-coded
        # industry vocabulary.
        for group in sorted(selected, key=lambda value: (value.priority, value.group_id)):
            group_relations = wanted_relations.intersection(group.relations) if wanted_relations else set(group.relations)
            relations = tuple(group_relations) or ("parent_project",)
            for term in group.terms[:6]:
                for relation in relations[:2]:
                    add(" ".join(([area] if area else []) + [term]), group.group_id, relation, group.priority, (term,))

        queries.sort(key=lambda item: (item.priority, item.text))
        return queries[:max_queries] if max_queries is not None else queries


def load_concept_catalog(config_dir: Path | None = None, concept_id: str = "photovoltaic_support") -> ConceptCatalog:
    target_dir = config_dir or DEFAULT_CONFIG_DIR
    return ConceptCatalog.from_file(target_dir / "concepts" / f"{concept_id}.yaml")


__all__ = ["ConceptCatalog", "ConceptGroup", "ConceptQuery", "RELATION_TYPES", "load_concept_catalog"]
