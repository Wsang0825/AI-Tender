from datetime import date, datetime

import pytest

from tender_ai.models import TenderRecord
from tender_ai.status.engine import TenderStatus, recalculate_status
from tender_ai.status.time import SHANGHAI_TZ, parse_datetime


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=SHANGHAI_TZ)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-05", "2026-09-05T17:00:00+08:00"),
        ("2026-09-05 17:00", "2026-09-05T17:00:00+08:00"),
        ("2026年9月5日17点", "2026-09-05T17:00:00+08:00"),
        ("2026年9月5日17点30分", "2026-09-05T17:30:00+08:00"),
        ("2026年9月5日 下午5点", "2026-09-05T17:00:00+08:00"),
        ("2026年9月5日 上午9点", "2026-09-05T09:00:00+08:00"),
        ("2026年9月5日 24:00", "2026-09-06T00:00:00+08:00"),
        ("2026/09/05 17:00", "2026-09-05T17:00:00+08:00"),
        ("2026-09-05T09:00:00+00:00", "2026-09-05T17:00:00+08:00"),
        (date(2026, 9, 5), "2026-09-05T17:00:00+08:00"),
    ],
)
def test_parse_datetime_formats(raw, expected):
    assert parse_datetime(raw).isoformat() == expected


def record(**kwargs) -> TenderRecord:
    return TenderRecord(project_name="新能源项目", **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"registration_start": "2026-08-27 09:00", "registration_deadline": "2026-09-01 17:00"}, TenderStatus.OPEN),
        ({"document_start": "2026-08-28 09:00", "document_deadline": "2026-09-01 17:00"}, TenderStatus.OPEN),
        ({"registration_start": "2026-08-20", "registration_deadline": "2026-08-27 17:00", "open_time": "2026-09-01 09:00"}, TenderStatus.CLOSED),
        ({"qualification_deadline": "2026-08-27 17:00", "bid_deadline": "2026-09-10 17:00"}, TenderStatus.CLOSED),
        ({"document_deadline": "2026-08-27 17:00", "bid_deadline": "2026-09-10 17:00"}, TenderStatus.CLOSED),
        ({"open_time": "2026-08-27 17:00"}, TenderStatus.CLOSED),
        ({"bid_deadline": "2026-09-10 17:00", "open_time": "2026-09-11 09:00"}, TenderStatus.UNKNOWN),
        ({"registration_start": "2026-09-01 09:00", "registration_deadline": "2026-09-10 17:00"}, TenderStatus.UNKNOWN),
        ({"registration_deadline": "2026-08-28 12:00"}, TenderStatus.CLOSED),
        ({"qualification_start": "2026-08-28 09:00", "qualification_deadline": "2026-09-01 17:00"}, TenderStatus.UNKNOWN),
    ],
)
def test_status_rules(kwargs, expected):
    assert recalculate_status(record(**kwargs), NOW).status is expected


def test_record_recalculation_sets_status():
    item = record(registration_start="2026-08-27 09:00", registration_deadline="2026-09-01 17:00")
    assert item.status is TenderStatus.UNKNOWN
    assert item.recalculate_status(NOW) is TenderStatus.OPEN


def test_extension_reopens_closed_record():
    original = record(registration_deadline="2026-08-20 17:00")
    extension = record(registration_deadline="2026-09-05 17:00")
    assert recalculate_status(original, NOW).status is TenderStatus.CLOSED
    assert recalculate_status(extension, NOW).status is TenderStatus.OPEN


def test_not_opened_is_not_open_without_participation_window():
    item = record(open_time="2026-09-01 09:00", bid_deadline="2026-08-31 17:00")
    assert recalculate_status(item, NOW).status is TenderStatus.UNKNOWN
