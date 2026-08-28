"""证据链模型：规范化字段必须保留可回溯的原文。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tender_ai.status.time import now_shanghai, parse_datetime


def evidence_hash(*parts: Any) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str
    normalized_value: str | None = None
    raw_value: str | None = None
    source_url: str | None = None
    source_file: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_text: str
    extractor: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    captured_at: datetime = Field(default_factory=now_shanghai)
    content_hash: str | None = None

    @field_validator("captured_at", mode="before")
    @classmethod
    def normalize_capture_time(cls, value: Any) -> datetime:
        return parse_datetime(value) or now_shanghai()

    @model_validator(mode="after")
    def fill_hash(self) -> "EvidenceRecord":
        if not self.content_hash:
            self.content_hash = evidence_hash(self.field_name, self.normalized_value, self.raw_value, self.source_url, self.source_file, self.page_number, self.source_text)
        return self
