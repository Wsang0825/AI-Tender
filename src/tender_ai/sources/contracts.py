"""来源适配器之间共享的轻量数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RawListingItem:
    title: str
    url: str
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttachmentLink:
    url: str
    file_name: str | None = None
    mime_type: str | None = None


@dataclass
class DetailPayload:
    title: str
    url: str
    html: str = ""
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[AttachmentLink] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "html": self.html,
            "text": self.text,
            "metadata": self.metadata,
            "attachments": self.attachments,
        }
