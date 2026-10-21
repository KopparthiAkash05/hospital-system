"""Focused tests for IST handling and 12-hour AM/PM formatting.

Run from the project root:

    DISABLE_SCHEDULER=1 pytest tests/test_timezone_format.py -v

No network or database access: every fixture here is pure formatting logic.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['DISABLE_SCHEDULER'] = '1'

import app as app_module


# ---------------------------------------------------------------------------
# get_12hr_time — the single AM/PM formatter used across UI and email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('00:00', '12:00 AM'),
    ('06:15', '06:15 AM'),
    ('09:30', '09:30 AM'),
    ('12:00', '12:00 PM'),
    ('12:30', '12:30 PM'),
    ('15:45', '03:45 PM'),
    ('18:30', '06:30 PM'),
    ('23:59', '11:59 PM'),
    ('09:30:00', '09:30 AM'),
    ('15:45:59', '03:45 PM'),
])
def test_time_string_formats(raw, expected):
    assert app_module.get_12hr_time(raw) == expected


@pytest.mark.parametrize('seconds,expected', [
    (0, '12:00 AM'),
    (9 * 3600 + 30 * 60, '09:30 AM'),
    (12 * 3600, '12:00 PM'),
    (12 * 3600 + 30 * 60, '12:30 PM'),
    (15 * 3600 + 45 * 60, '03:45 PM'),
    (18 * 3600 + 30 * 60, '06:30 PM'),
    (23 * 3600 + 59 * 60, '11:59 PM'),
])
def test_timedelta_formats(seconds, expected):
    """TIME columns come back from PyMySQL as timedelta, never strftime-able."""
    assert app_module.get_12hr_time(timedelta(seconds=seconds)) == expected


def test_none_is_blank():
    assert app_module.get_12hr_time(None) == ''


# ---------------------------------------------------------------------------
# get_datetime_12h — UTC database timestamps rendered as IST
# ---------------------------------------------------------------------------

def test_dt12_converts_utc_to_ist():
    # 04:00 UTC is 09:30 IST on the same day.
    assert (app_module.get_datetime_12h('2026-10-21 04:00:00')
            == '2026-10-21 09:30 AM')


def test_dt12_afternoon_ist():
    # 10:00 UTC is 03:30 PM IST.
    assert (app_module.get_datetime_12h(datetime(2026, 10, 21, 10, 0, 0))
            == '2026-10-21 03:30 PM')


def test_dt12_crosses_midnight_to_next_ist_day():
    # 20:00 UTC is 01:30 AM IST on the *next* calendar day.
    assert (app_module.get_datetime_12h('2026-10-21 20:00:00')
            == '2026-10-22 01:30 AM')


def test_dt12_aware_value_is_not_double_shifted():
    aware = datetime(2026, 10, 21, 9, 30, tzinfo=app_module.HOSPITAL_TZ)
    assert app_module.get_datetime_12h(aware) == '2026-10-21 09:30 AM'


def test_dt12_blank():
    assert app_module.get_datetime_12h(None) == ''


# ---------------------------------------------------------------------------
# local_now — IST wall clock, not UTC
# ---------------------------------------------------------------------------

def test_local_now_is_ist():
    now = app_module.local_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=5, minutes=30)
    # Within a few seconds of the true IST clock.
    true_ist = datetime.now(timezone.utc).astimezone(app_module.HOSPITAL_TZ)
    assert abs((now - true_ist).total_seconds()) < 5


def test_local_now_matches_hospital_zoneinfo():
    assert app_module.HOSPITAL_TZ.key == 'Asia/Kolkata'
