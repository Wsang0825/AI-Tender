from decimal import Decimal

import pytest
from pydantic import ValidationError

from tender_ai.matching.dedupe import DedupeOutcome, compare_records, normalize_identity
from tender_ai.models import TenderRecord
from tender_ai.status.engine import TenderStatus


def test_tender_record_contains_unified_core_fields():
    item = TenderRecord(project_name="项目", budget="1,000,000元", capacity_mw=100, status="OPEN")
    assert item.budget == Decimal("1000000")
    assert item.capacity_mw == 100
    assert item.status is TenderStatus.OPEN


def test_tender_record_rejects_unknown_field():
    with pytest.raises(ValidationError):
        TenderRecord(project_name="项目", unexpected="value")


def test_confidence_score_range_is_validated():
    with pytest.raises(ValidationError):
        TenderRecord(project_name="项目", confidence_score=1.1)


def test_tender_code_is_exact_dedupe_key():
    left = TenderRecord(project_name="甲项目", tender_code="T-001")
    right = TenderRecord(project_name="完全不同的标题", tender_code=" T-001 ")
    result = compare_records(left, right)
    assert result.outcome == DedupeOutcome.EXACT_MATCH


def test_project_code_is_exact_dedupe_key():
    left = TenderRecord(project_name="甲项目", project_code="P-001")
    right = TenderRecord(project_name="甲项目二次公告", project_code="P001")
    assert compare_records(left, right).outcome == DedupeOutcome.EXACT_MATCH


def test_similar_project_is_probable_match():
    left = TenderRecord(project_name="新疆某地集中式光伏发电项目", owner="甲能源公司", province="新疆", city="哈密", capacity_mw=100)
    right = TenderRecord(project_name="新疆某地集中式光伏发电项目招标公告", owner="甲能源公司", province="新疆", city="哈密", capacity_mw=100.4)
    assert compare_records(left, right).outcome == DedupeOutcome.PROBABLE_MATCH


def test_unrelated_project_is_no_match():
    left = TenderRecord(project_name="陕西风电项目", owner="甲公司", province="陕西")
    right = TenderRecord(project_name="青海储能电池采购", owner="乙公司", province="青海")
    assert compare_records(left, right).outcome == DedupeOutcome.NO_MATCH


def test_identity_normalization_removes_punctuation():
    assert normalize_identity(" 项目-（一） ") == "项目一"
