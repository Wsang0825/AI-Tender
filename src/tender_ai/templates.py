"""按需搜索模板的持久化边界；模板只保存条件，不会自动运行。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from tender_ai.search import SearchRequest
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import SearchTemplate


def template_payload(row: SearchTemplate) -> dict[str, Any]:
    try:
        request = json.loads(row.request_json)
    except (TypeError, json.JSONDecodeError):
        request = {}
    return {
        "template_id": row.template_id,
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "request": request,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_templates(*, database: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        query = select(SearchTemplate).order_by(SearchTemplate.updated_at.desc(), SearchTemplate.name)
        if enabled_only:
            query = query.where(SearchTemplate.enabled.is_(True))
        return [template_payload(row) for row in session.scalars(query).all()]


def save_template(name: str, request: SearchRequest | dict[str, Any], *, description: str | None = None, template_id: str | None = None, database: str | None = None) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise ValueError("模板名称不能为空")
    request_payload = request.to_dict() if isinstance(request, SearchRequest) else dict(request)
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        row = session.get(SearchTemplate, template_id) if template_id else session.scalar(select(SearchTemplate).where(SearchTemplate.name == name))
        if row is None:
            row = SearchTemplate(template_id=template_id or f"template_{uuid4().hex[:16]}", name=name, request_json=json.dumps(request_payload, ensure_ascii=False), description=description, enabled=True)
            session.add(row)
        else:
            row.name = name
            row.request_json = json.dumps(request_payload, ensure_ascii=False)
            row.description = description
            row.updated_at = now_shanghai()
        session.flush()
        return template_payload(row)


def set_template_enabled(template_id: str, enabled: bool, *, database: str | None = None) -> dict[str, Any]:
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        row = session.get(SearchTemplate, template_id)
        if row is None:
            raise KeyError(f"模板不存在: {template_id}")
        row.enabled = enabled
        row.updated_at = now_shanghai()
        return template_payload(row)


def delete_template(template_id: str, *, database: str | None = None) -> None:
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        row = session.get(SearchTemplate, template_id)
        if row is not None:
            session.delete(row)


def request_from_template(payload: dict[str, Any]) -> SearchRequest:
    return SearchRequest.from_dict(payload.get("request", payload))


__all__ = ["delete_template", "list_templates", "request_from_template", "save_template", "set_template_enabled", "template_payload"]
