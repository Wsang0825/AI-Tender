"""离线 Snapshot Replay：不访问网站，重新运行字段解析和状态计算。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select

from tender_ai.config_loader import RegionRegistry
from tender_ai.extractors.tender import normalize_detail
from tender_ai.sources.contracts import DetailPayload
from tender_ai.sources.registry import SourceDefinition, SourceRegistry
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, Snapshot
from tender_ai.storage.repository import save_tender_record


@dataclass
class ReplaySummary:
    snapshot_count: int = 0
    replayed_count: int = 0
    failed_count: int = 0
    dry_run: bool = False
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_count": self.snapshot_count,
            "replayed_count": self.replayed_count,
            "failed_count": self.failed_count,
            "dry_run": self.dry_run,
            "errors": self.errors or [],
        }


def _payload_from_snapshot(snapshot: Snapshot) -> DetailPayload:
    content = Path(snapshot.file_path).read_bytes()
    content_type = snapshot.content_type.lower()
    if "json" in content_type:
        try:
            parsed = json.loads(content.decode("utf-8", errors="replace"))
            title = str(parsed.get("title") or parsed.get("projectname") or snapshot.source_url) if isinstance(parsed, dict) else snapshot.source_url
            text = json.dumps(parsed, ensure_ascii=False, default=str)
            return DetailPayload(title=title, url=snapshot.source_url, text=text, metadata=parsed if isinstance(parsed, dict) else {})
        except json.JSONDecodeError:
            pass
    html = content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    title = (soup.select_one("h1") or soup.select_one("title"))
    title_text = title.get_text(" ", strip=True) if title else snapshot.source_url
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return DetailPayload(title=title_text, url=snapshot.source_url, html=html, text=soup.get_text(" ", strip=True))


class ReplayRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))

    def run(self, *, announcement_id: int | None = None, source_id: str | None = None, dry_run: bool = False) -> ReplaySummary:
        registry = SourceRegistry.from_file()
        regions = RegionRegistry.from_file()
        summary = ReplaySummary(dry_run=dry_run, errors=[])
        with session_scope(self.engine) as session:
            query = select(Snapshot).order_by(Snapshot.captured_at)
            if announcement_id is not None:
                query = query.where(Snapshot.announcement_id == announcement_id)
            if source_id:
                query = query.where(Snapshot.source_id == source_id)
            snapshots = list(session.scalars(query).all())
            summary.snapshot_count = len(snapshots)
            for snapshot in snapshots:
                try:
                    payload = _payload_from_snapshot(snapshot)
                    if snapshot.source_id:
                        definition = registry.get(snapshot.source_id)
                    else:
                        definition = SourceDefinition(source_id="replay", source_name="Snapshot Replay", category="discovered", base_url=payload.url)
                    extraction = normalize_detail(payload, definition, regions)
                    if not dry_run:
                        announcement = session.get(Announcement, snapshot.announcement_id) if snapshot.announcement_id else None
                        if announcement is not None:
                            extraction.record.project_id = announcement.project_id
                            extraction.record.source_url = announcement.source_url or payload.url
                            extraction.record.original_url = announcement.original_url or payload.url
                            extraction.record.canonical_url = announcement.canonical_url
                            extraction.record.content_hash = snapshot.sha256
                        save_tender_record(session, extraction.record, status_reason=extraction.record.status_reason, change_type="change")
                        if announcement is not None:
                            announcement.clean_text = payload.text or payload.html
                            announcement.raw_content = announcement.clean_text[:2_000_000]
                            announcement.content_hash = snapshot.sha256
                            announcement.created_at = announcement.created_at or now_shanghai()
                    summary.replayed_count += 1
                except Exception as exc:
                    summary.failed_count += 1
                    summary.errors.append(f"{snapshot.snapshot_id}: {exc}")
        return summary


__all__ = ["ReplayRunner", "ReplaySummary"]
