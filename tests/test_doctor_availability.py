"""Tests for the schedule based availability engine.

Run from the project root:

    DISABLE_SCHEDULER=1 pytest tests/test_doctor_availability.py -v

No network or database access: every fixture here is pure logic and the
current time is always passed in explicitly.
"""
import os
import sys
from datetime import datetime, timedelta, time, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['DISABLE_SCHEDULER'] = '1'

import app as app_module


IST = app_module.HOSPITAL_TZ


def moment(text):
    """A hospital local datetime, e.g. '2026-10-21 16:45' (a Wednesday)."""
    return datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=IST)


def schedule_row(day_of_week, start, end, slot_duration=30, is_active=1):
    return {
        'day_of_week': day_of_week,
        'start_time': start,
        'end_time': end,
        'slot_duration': slot_duration,
        'is_active': is_active,
    }


# Monday to Saturday, 09:00 - 17:00
FULL_WEEK = [schedule_row(day, '09:00', '17:00') for day in range(1, 7)]
# Monday to Friday, 10:00 - 16:30
SHORT_WEEK = [schedule_row(day, '10:00', '16:30') for day in range(1, 6)]
# A night shift: 22:00 - 02:00 (ends the next morning)
NIGHT_SHIFT = [schedule_row(3, '22:00', '02:00')]


def doctor(**overrides):
    record = {
        'doctor_id': 1,
        'name': 'Test Doctor',
        'is_active': 1,
        'availability_status': 'Available',
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value,expected', [
    ('09:00', 540),
    ('09:00:00', 540),
    (timedelta(hours=9, minutes=30), 570),
    (time(17, 0), 1020),
    ('00:00', 0),
    (None, None),
    ('not-a-time', None),
])
def test_schedule_time_to_minutes(value, expected):
    assert app_module.schedule_time_to_minutes(value) == expected


def test_minutes_to_12hr():
    assert app_module.minutes_to_12hr(540) == '09:00 AM'
    assert app_module.minutes_to_12hr(1020) == '05:00 PM'
    assert app_module.minutes_to_12hr(0) == '12:00 AM'


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def test_schedule_windows_normalises_night_shift():
    windows = app_module.schedule_windows(NIGHT_SHIFT)
    assert windows == [(3, 22 * 60, 26 * 60)]


def test_schedule_windows_skips_inactive_rows():
    windows = app_module.schedule_windows(
        [schedule_row(1, '09:00', '17:00', is_active=0)]
    )
    assert windows == []


# ---------------------------------------------------------------------------
# Is the doctor working right now?
# ---------------------------------------------------------------------------

def test_start_minute_is_inclusive():
    windows = app_module.schedule_windows(FULL_WEEK)
    assert app_module.schedule_covers_moment(windows, moment('2026-10-21 09:00')) is True


def test_end_minute_is_exclusive():
    windows = app_module.schedule_windows(FULL_WEEK)
    assert app_module.schedule_covers_moment(windows, moment('2026-10-21 16:59')) is True
    assert app_module.schedule_covers_moment(windows, moment('2026-10-21 17:00')) is False


def test_before_the_shift_starts():
    windows = app_module.schedule_windows(FULL_WEEK)
    assert app_module.schedule_covers_moment(windows, moment('2026-10-21 08:59')) is False


def test_day_without_a_window_is_not_working():
    # 2026-10-25 is a Sunday, 2026-10-19 is a Monday.
    windows = app_module.schedule_windows(FULL_WEEK)
    assert app_module.schedule_covers_moment(windows, moment('2026-10-25 11:00')) is False
    assert app_module.schedule_covers_moment(windows, moment('2026-10-19 11:00')) is True


def test_night_shift_carries_over_midnight():
    windows = app_module.schedule_windows(NIGHT_SHIFT)
    # Wednesday 23:30 is inside the window.
    assert app_module.schedule_covers_moment(windows, moment('2026-10-21 23:30')) is True
    # Thursday 01:30 is still the same shift.
    assert app_module.schedule_covers_moment(windows, moment('2026-10-22 01:30')) is True
    # Thursday 03:00 is not.
    assert app_module.schedule_covers_moment(windows, moment('2026-10-22 03:00')) is False


def test_utc_input_is_converted_to_hospital_time():
    windows = app_module.schedule_windows(FULL_WEEK)
    # 12:00 UTC is 17:30 IST, which is after the 17:00 close.
    after_close = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    assert app_module.schedule_covers_moment(windows, after_close) is False
    # 09:00 UTC is 14:30 IST, which is inside the shift.
    during_shift = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
    assert app_module.schedule_covers_moment(windows, during_shift) is True


# ---------------------------------------------------------------------------
# Next shift
# ---------------------------------------------------------------------------

def test_next_schedule_start_today():
    windows = app_module.schedule_windows(FULL_WEEK)
    assert app_module.next_schedule_start(
        windows, moment('2026-10-21 07:30')
    ) == moment('2026-10-21 09:00')


def test_next_schedule_start_next_working_day():
    windows = app_module.schedule_windows(FULL_WEEK)
    # Saturday 10:00 -> Saturday is worked, but 17:00 has passed.
    assert app_module.next_schedule_start(
        windows, moment('2026-10-24 18:00')
    ) == moment('2026-10-26 09:00')  # Monday


def test_next_schedule_start_is_none_without_schedule():
    assert app_module.next_schedule_start([], moment('2026-10-21 10:00')) is None


# ---------------------------------------------------------------------------
# Effective status
# ---------------------------------------------------------------------------

def test_available_during_the_shift():
    info = app_module.doctor_availability(
        doctor(), FULL_WEEK, moment('2026-10-21 10:00')
    )
    assert info['status'] == 'Available'
    assert info['source'] == 'schedule'
    assert info['on_shift'] is True
    assert info['until'] == '05:00 PM'


def test_unavailable_outside_the_shift():
    info = app_module.doctor_availability(
        doctor(), FULL_WEEK, moment('2026-10-21 18:00')
    )
    assert info['status'] == 'Unavailable'
    assert info['source'] == 'schedule'
    # 2026-10-21 is a Wednesday, so the next shift is Thursday morning.
    assert info['next'] == 'Thu 09:00 AM'


def test_unavailable_on_a_day_off():
    info = app_module.doctor_availability(
        doctor(), FULL_WEEK, moment('2026-10-25 11:00')
    )
    assert info['status'] == 'Unavailable'


def test_manual_unavailable_wins_over_the_schedule():
    info = app_module.doctor_availability(
        doctor(availability_status='Unavailable'), FULL_WEEK,
        moment('2026-10-21 10:00')
    )
    assert info['status'] == 'Unavailable'
    assert info['source'] == 'manual'


def test_inactive_doctor_is_never_available():
    info = app_module.doctor_availability(
        doctor(is_active=0), FULL_WEEK, moment('2026-10-21 10:00')
    )
    assert info['status'] == 'Unavailable'
    assert info['source'] == 'inactive'


def test_doctor_without_schedule_falls_back_to_the_manual_status():
    info = app_module.doctor_availability(
        doctor(), [], moment('2026-10-21 10:00')
    )
    assert info['status'] == 'Available'
    assert info['source'] == 'manual'
    assert info['on_shift'] is None


def test_missing_doctor():
    info = app_module.doctor_availability(None, FULL_WEEK, moment('2026-10-21 10:00'))
    assert info['status'] == 'Unavailable'


def test_labels_follow_the_status():
    on_shift = app_module.doctor_availability(
        doctor(), FULL_WEEK, moment('2026-10-21 10:00')
    )
    off_shift = app_module.doctor_availability(
        doctor(), FULL_WEEK, moment('2026-10-21 18:00')
    )
    assert on_shift['label'] == 'Available'
    assert off_shift['label'] == 'Not Available'
    assert off_shift['status'] == 'Unavailable'


# ---------------------------------------------------------------------------
# Working hours summary
# ---------------------------------------------------------------------------

def test_format_working_hours_groups_consecutive_days():
    summary = app_module.format_working_hours(FULL_WEEK)
    assert summary == 'Mon-Sat: 09:00 AM - 05:00 PM'


def test_format_working_hours_for_a_short_week():
    assert app_module.format_working_hours(SHORT_WEEK) == 'Mon-Fri: 10:00 AM - 04:30 PM'


def test_format_working_hours_without_schedule():
    assert app_module.format_working_hours([]) == ''
