"""只保存候选公告和证据来源的原始快照，不缓存全网页面。"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_ai.config_loader import APP_ROOT
from tender_ai.storage.models import Snapshot
from tender_ai.urls import canonicalize_url, content_hash


SNAPSHOT_ROOT = APP_ROOT.parent / "data" / "snapshots"


@dataclass(frozen=True)
class SnapshotArtifact:
    snapshot_id: str
    source_url: str
    canonical_url: str
    captured_at: Any
    content_type: str
    sha256: str
    file_path: Path
    source_id: str | None = None
    announcement_id: int | None = None


def _extension(content_type: str, source_url: str = "") -> str:
    media = content_type.split(";", 1)[0].lower()
    mapping = {
        "text/html": ".html", "application/json": ".json", "text/json": ".json",
        "application/pdf": ".pdf", "application/msword": ".doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    if media in mapping:
        return mapping[media]
    suffix = Path(source_url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in {".html", ".htm", ".json", ".pdf", ".doc", ".docx", ".xls", ".xlsx"} else ".bin"


class SnapshotStore:
    def __init__(self, root: Path | None = None):
        self.root = root or SNAPSHOT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def save_bytes(
        self,
        session: Session,
        *,
        source_url: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        source_id: str | None = None,
        announcement_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Snapshot:
        digest = content_hash(content)
        existing = session.scalar(select(Snapshot).where(Snapshot.sha256 == digest))
        if existing is not None:
            if existing.announcement_id is None and announcement_id is not None:
                existing.announcement_id = announcement_id
            return existing
        snapshot_id = uuid4().hex
        target_dir = self.root / (source_id or "unknown")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest}{_extension(content_type, source_url)}"
        if not target.exists():
            target.write_bytes(content)
        row = Snapshot(
            snapshot_id=snapshot_id,
            source_url=source_url,
            canonical_url=canonicalize_url(source_url),
            content_type=content_type.split(";", 1)[0].lower(),
            sha256=digest,
            file_path=str(target),
            source_id=source_id,
            announcement_id=announcement_id,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
        )
        session.add(row)
        session.flush()
        return row

    def save_text(self, session: Session, *, source_url: str, text: str, content_type: str = "text/html", **kwargs: Any) -> Snapshot:
        return self.save_bytes(session, source_url=source_url, content=text.encode("utf-8", errors="replace"), content_type=content_type, **kwargs)


__all__ = ["SNAPSHOT_ROOT", "SnapshotArtifact", "SnapshotStore"]
