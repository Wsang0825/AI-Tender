"""跨采集器、文档解析器和 Web 层使用的统一记录模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tender_ai.status.engine import TenderStatus
from tender_ai.status.time import as_shanghai, now_shanghai, parse_datetime


TIME_FIELDS = (
    "publish_time",
    "status_evaluated_at",
    "qualification_start",
    "qualification_deadline",
    "registration_start",
    "registration_deadline",
    "document_start",
    "document_deadline",
    "bid_deadline",
    "open_time",
    "first_seen_at",
    "last_seen_at",
)


class TenderRecord(BaseModel):
    """招投标项目的统一内部数据契约。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    project_id: str = Field(default_factory=lambda: uuid4().hex)
    project_name: str
    province: str | None = None
    city: str | None = None
    county: str | None = None
    location: str | None = None
    owner: str | None = None
    purchaser: str | None = None
    tenderer: str | None = None
    agency: str | None = None
    industry: str | None = None
    sub_industry: str | None = None
    project_type: str | None = None
    announcement_type: str | None = None
    project_scale: str | None = None
    capacity_mw: float | None = None
    capacity_mwh: float | None = None
    budget: Decimal | None = None
    project_code: str | None = None
    tender_code: str | None = None
    publish_time: datetime | None = None
    qualification_start: datetime | None = None
    qualification_deadline: datetime | None = None
    registration_start: datetime | None = None
    registration_deadline: datetime | None = None
    document_start: datetime | None = None
    document_deadline: datetime | None = None
    bid_deadline: datetime | None = None
    open_time: datetime | None = None
    qualification_summary: str | None = None
    participation_method: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    source_level: str | None = None
    source_url: str | None = None
    original_url: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    status: TenderStatus = TenderStatus.UNKNOWN
    status_reason: str | None = None
    status_evaluated_at: datetime | None = None
    lifecycle_state: str = "NEW"
    last_change_at: datetime | None = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator(*TIME_FIELDS, mode="before")
    @classmethod
    def normalize_time_fields(cls, value: Any) -> Any:
        return parse_datetime(value)

    @field_validator("budget", mode="before")
    @classmethod
    def normalize_budget(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            value = value.replace(",", "").replace("元", "").replace("万元", "")
        return value

    def recalculate_status(self, now: datetime | None = None) -> TenderStatus:
        from tender_ai.status.engine import recalculate_status

        decision = recalculate_status(self, now)
        self.status = decision.status
        self.status_reason = decision.reason_code
        self.status_evaluated_at = as_shanghai(now) if now is not None else now_shanghai()
        return self.status
