"""Tests for Live Queue Management and the prescription frequency dropdown.

Run from the project root:

    DISABLE_SCHEDULER=1 pytest tests/test_queue.py -v

The suite reuses the existing Patient/Doctor rows (read-only) and creates its
own Appointment rows on isolated future dates plus all QueueEntry/QueueSequence
rows, which are removed again in teardown so no application data is left
behind. Walk-in queue entries and touched sequence rows are cleaned by the
session fixtures; appointment-linked entries cascade with their Appointment.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['DISABLE_SCHEDULER'] = '1'

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, 'details.env'))

import pymysql
import pymysql.cursors
from werkzeug.datastructures import MultiDict

import app as app_module

PATIENT_ID = 1
OTHER_PATIENT_ID = 2
DOCTOR_ID = 1
OTHER_DOCTOR_ID = 2


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------

def db_conn():
    return pymysql.connect(
        host=os.environ['MYSQL_HOST'],
        user=os.environ['MYSQL_USER'],
        password=os.environ['MYSQL_PASSWORD'],
        database=os.environ['MYSQL_DB'],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def fetch_one(sql, params=()):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def fetch_all(sql, params=()):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _build_future_dates(start_key, end_key):
    """Unique weekday dates, one consecutive day per key, never a Sunday.

    Tests request their own key (40, 41, ...) so no two tests can ever share
    a queue session — a plain offset would drift when it hits a Sunday.
    """
    dates = {}
    day = date.today() + timedelta(days=40)
    while day.weekday() == 6:
        day += timedelta(days=1)
    for key in range(start_key, end_key + 1):
        dates[key] = day
        day += timedelta(days=1)
        while day.weekday() == 6:
            day += timedelta(days=1)
    return dates


_FUTURE_DATES = _build_future_dates(40, 80)


def future_date(key):
    """The reserved weekday date for this test's key."""
    return _FUTURE_DATES[key]


@pytest.fixture(scope='session')
def flask_app():
    app_module.app.config['TESTING'] = True
    return app_module.app


@pytest.fixture(scope='module', autouse=True)
def _queue_session_cleanup():
    """Remove everything this test module writes afterwards.

    AuditLog rows and walk-in QueueEntry rows are matched against the
    database clock (what INSERTs are stamped with), Notifications against the
    highest id seen before the module ran, and QueueSequence rows against
    updated_at, so real application data is never touched.
    """
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute('SELECT NOW() AS now')
        session_start = cur.fetchone()['now']
        cur.execute(
            'SELECT COALESCE(MAX(notification_id), 0) AS max_id '
            'FROM Notification')
        notification_floor = cur.fetchone()['max_id']
    conn.close()
    yield
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM AuditLog WHERE created_at >= %s',
                        (session_start,))
            cur.execute('DELETE FROM Notification WHERE notification_id > %s',
                        (notification_floor,))
            cur.execute(
                'DELETE FROM QueueEntry '
                'WHERE appointment_id IS NULL AND created_at >= %s',
                (session_start,))
            cur.execute('DELETE FROM QueueSequence WHERE updated_at >= %s',
                        (session_start,))
    finally:
        conn.close()


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


def login(client, user_type, user_id, email='qa@example.com', name='QA User'):
    with client.session_transaction() as sess:
        sess.clear()
        sess['loggedin'] = True
        sess['user_type'] = user_type
        sess['id'] = user_id
        sess['email'] = email
        sess['name'] = name


def flash_messages(client):
    with client.session_transaction() as sess:
        return [text for _, text in sess.get('_flashes', [])]


@pytest.fixture(autouse=True)
def email_outbox(monkeypatch):
    """Record every outbound email instead of calling the Brevo API."""
    outbox = []

    def fake_send(to_email, subject, body):
        outbox.append({'to': to_email, 'subject': subject, 'body': body})
        return True

    monkeypatch.setattr(app_module, 'send_email_via_sendgrid', fake_send)
    return outbox


@pytest.fixture()
def queue_factory():
    """Create appointments owned by this test; cascade-cleans on teardown."""
    conn = db_conn()
    created = []

    def create(appt_date, patient_id=PATIENT_ID, doctor_id=DOCTOR_ID,
               status='Pending', time='11:00:00'):
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO Appointment '
                '(patient_id, doctor_id, date, time, status, reason) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (patient_id, doctor_id, appt_date, time, status,
                 'QA queue test visit'),
            )
            appointment_id = cur.lastrowid
        created.append(appointment_id)
        return appointment_id, appt_date

    yield create

    with conn.cursor() as cur:
        for appointment_id in created:
            cur.execute('DELETE FROM Appointment WHERE appointment_id = %s',
                        (appointment_id,))
    conn.close()


def queue_rows(doctor_id, queue_date):
    return fetch_all(
        'SELECT * FROM QueueEntry WHERE doctor_id = %s AND queue_date = %s '
        'ORDER BY queue_number', (doctor_id, queue_date))


def add_walk_in(client, queue_date, name, phone=None):
    data = {'queue_date': queue_date.isoformat(), 'walk_in_name': name}
    if phone:
        data['walk_in_phone'] = phone
    return client.post('/doctor/queue/walk-in', data=data)


# ---------------------------------------------------------------------------
# Approval -> queue assignment
# ---------------------------------------------------------------------------

def test_approval_assigns_first_queue_number_and_emails_it(
        client, queue_factory, email_outbox):
    appointment_id, appt_date = queue_factory(future_date(40),
                                              status='Pending',
                                              time='09:30:00')
    login(client, 'doctor', DOCTOR_ID)
    response = client.get(f'/doctor/approve/{appointment_id}')
    assert response.status_code == 302

    entry = fetch_one('SELECT * FROM QueueEntry WHERE appointment_id = %s',
                      (appointment_id,))
    assert entry is not None
    assert entry['doctor_id'] == DOCTOR_ID
    assert entry['patient_id'] == PATIENT_ID
    assert entry['queue_date'] == appt_date
    assert entry['queue_number'] == 1
    assert entry['status'] == 'Waiting'
    assert app_module.format_queue_number(entry['queue_number']) == 'Q001'

    appointment = fetch_one(
        'SELECT status FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))
    assert appointment['status'] == 'Approved'

    confirmations = [m for m in email_outbox
                     if 'Appointment Confirmed' in m['subject']]
    assert confirmations, 'approval must send a confirmation email'
    assert 'Queue Number: Q001' in confirmations[-1]['body']


def test_pending_and_rejected_appointments_never_take_a_number(
        client, queue_factory):
    pending_id, appt_date = queue_factory(future_date(41),
                                          status='Pending', time='09:00:00')
    rejected_id, _ = queue_factory(future_date(41), status='Rejected',
                                   time='10:00:00')

    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/reject/{pending_id}')

    for appointment_id in (pending_id, rejected_id):
        assert fetch_one(
            'SELECT queue_id FROM QueueEntry WHERE appointment_id = %s',
            (appointment_id,)) is None
    assert fetch_one(
        'SELECT COUNT(*) AS n FROM QueueEntry WHERE queue_date = %s',
        (appt_date,))['n'] == 0


def test_approving_twice_keeps_a_single_queue_entry(
        client, queue_factory, email_outbox):
    appointment_id, appt_date = queue_factory(future_date(42),
                                              status='Pending',
                                              time='09:00:00')
    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')
    client.get(f'/doctor/approve/{appointment_id}')

    rows = queue_rows(DOCTOR_ID, appt_date)
    assert len(rows) == 1
    assert rows[0]['queue_number'] == 1

    confirmations = [m for m in email_outbox
                     if 'Appointment Confirmed' in m['subject']]
    assert len(confirmations) == 1
    assert any('not found or already unavailable' in message
               for message in flash_messages(client))


def test_api_approval_reports_queue_assignment(client, queue_factory,
                                               email_outbox):
    appointment_id, appt_date = queue_factory(future_date(66),
                                              status='Pending',
                                              time='14:00:00')
    login(client, 'patient', PATIENT_ID)
    blocked = client.put('/api/approveAppointment',
                         json={'appointment_id': appointment_id,
                               'action': 'approve'})
    assert blocked.status_code == 403

    login(client, 'doctor', DOCTOR_ID)
    response = client.put('/api/approveAppointment',
                          json={'appointment_id': appointment_id,
                                'action': 'approve'})
    payload = response.get_json()
    assert response.status_code == 200
    assert payload['success'] is True
    assert payload['new_status'] == 'Approved'
    assert payload['queue_assigned'] is True

    entry = fetch_one('SELECT * FROM QueueEntry WHERE appointment_id = %s',
                      (appointment_id,))
    assert entry['queue_number'] == 1
    assert entry['queue_date'] == appt_date
    assert entry['status'] == 'Waiting'


# ---------------------------------------------------------------------------
# Concurrency-safe allocation
# ---------------------------------------------------------------------------

def test_concurrent_allocation_produces_unique_contiguous_numbers():
    queue_date = future_date(50)

    def open_connection():
        # The shared free host allows only 5 connections per user and the
        # Flask app keeps one open, so a transient 1203 just retries.
        last_error = None
        for _ in range(40):
            try:
                return pymysql.connect(
                    host=os.environ['MYSQL_HOST'],
                    user=os.environ['MYSQL_USER'],
                    password=os.environ['MYSQL_PASSWORD'],
                    database=os.environ['MYSQL_DB'],
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                )
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] == 1203:
                    time.sleep(0.25)
                    last_error = exc
                    continue
                raise
        raise last_error

    def allocate(index):
        conn = open_connection()
        try:
            cursor = conn.cursor()
            entry = app_module.create_queue_entry(
                cursor, DOCTOR_ID, queue_date,
                walk_in_name=f'Race Patient {index}', walk_in_phone='999')
            conn.commit()
            return entry['queue_number']
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        numbers = list(pool.map(allocate, range(6)))

    assert sorted(numbers) == [1, 2, 3, 4, 5, 6]
    rows = queue_rows(DOCTOR_ID, queue_date)
    assert [row['queue_number'] for row in rows] == [1, 2, 3, 4, 5, 6]
    assert all(row['status'] == 'Waiting' for row in rows)


# ---------------------------------------------------------------------------
# Walk-ins, daily reset, page-load sync
# ---------------------------------------------------------------------------

def test_walk_in_route_assigns_numbers_and_requires_a_name(client):
    queue_date = future_date(51)
    login(client, 'doctor', DOCTOR_ID)

    assert add_walk_in(client, queue_date, 'Walk In Alpha',
                       '555-0101').status_code == 302
    assert add_walk_in(client, queue_date, 'Walk In Beta').status_code == 302
    # A missing name must not consume a number.
    add_walk_in(client, queue_date, '')

    rows = queue_rows(DOCTOR_ID, queue_date)
    assert [row['queue_number'] for row in rows] == [1, 2]
    assert rows[0]['walk_in_name'] == 'Walk In Alpha'
    assert rows[0]['walk_in_phone'] == '555-0101'
    assert rows[1]['walk_in_name'] == 'Walk In Beta'
    assert rows[1]['appointment_id'] is None
    assert any('name is required' in message
               for message in flash_messages(client))

    page = client.get(f'/doctor/queue?date={queue_date.isoformat()}')
    html = page.get_data(as_text=True)
    assert 'Q001' in html and 'Q002' in html
    assert 'Walk In Alpha' in html


def test_queue_numbering_restarts_for_each_day(client):
    first_day = future_date(53)
    second_day = future_date(54)
    login(client, 'doctor', DOCTOR_ID)

    add_walk_in(client, first_day, 'Day One Patient')
    add_walk_in(client, second_day, 'Day Two Patient')

    assert queue_rows(DOCTOR_ID, first_day)[0]['queue_number'] == 1
    assert queue_rows(DOCTOR_ID, second_day)[0]['queue_number'] == 1

    sequence_rows = fetch_all(
        'SELECT last_number FROM QueueSequence '
        'WHERE doctor_id = %s AND queue_date IN (%s, %s)',
        (DOCTOR_ID, first_day, second_day))
    assert len(sequence_rows) == 2
    assert all(row['last_number'] == 1 for row in sequence_rows)


def test_queue_page_syncs_appointments_approved_before_the_feature(
        client, queue_factory):
    appointment_id, appt_date = queue_factory(future_date(55),
                                              status='Approved',
                                              time='10:30:00')
    # Legacy approval: no queue seat exists yet.
    assert fetch_one('SELECT queue_id FROM QueueEntry WHERE appointment_id = %s',
                     (appointment_id,)) is None

    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/queue?date={appt_date.isoformat()}')

    rows = queue_rows(DOCTOR_ID, appt_date)
    assert len(rows) == 1
    assert rows[0]['appointment_id'] == appointment_id
    assert rows[0]['queue_number'] == 1
    assert rows[0]['status'] == 'Waiting'

    # Every later page view is idempotent.
    client.get(f'/doctor/queue?date={appt_date.isoformat()}')
    client.get(f'/doctor/queue?date={appt_date.isoformat()}')
    assert len(queue_rows(DOCTOR_ID, appt_date)) == 1


# ---------------------------------------------------------------------------
# Lifecycle hooks: cancel / reschedule / complete
# ---------------------------------------------------------------------------

def test_patient_cancellation_frees_the_queue_seat(client, queue_factory):
    appointment_id, appt_date = queue_factory(future_date(56),
                                              status='Pending',
                                              time='11:30:00')
    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')

    login(client, 'patient', PATIENT_ID)
    response = client.get(f'/patient/cancel/{appointment_id}')
    assert response.status_code == 302

    entry = fetch_one('SELECT status FROM QueueEntry WHERE appointment_id = %s',
                      (appointment_id,))
    assert entry['status'] == 'Cancelled'
    appointment = fetch_one(
        'SELECT status FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))
    assert appointment['status'] == 'Cancelled'
    assert queue_rows(DOCTOR_ID, appt_date)[0]['status'] == 'Cancelled'


def test_doctor_and_admin_cancellation_free_queue_seats(client, queue_factory):
    doctor_appt, appt_date = queue_factory(future_date(57),
                                           status='Pending', time='09:00:00')
    admin_appt, _ = queue_factory(future_date(57), status='Pending',
                                  time='09:30:00')

    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{doctor_appt}')
    client.get(f'/doctor/approve/{admin_appt}')
    assert len(queue_rows(DOCTOR_ID, appt_date)) == 2

    client.get(f'/doctor/cancel/{doctor_appt}')
    rows = queue_rows(DOCTOR_ID, appt_date)
    assert rows[0]['status'] == 'Cancelled'
    assert rows[1]['status'] == 'Waiting'

    login(client, 'admin', 1)
    client.post(f'/admin/appointments/{admin_appt}/cancel')
    rows = queue_rows(DOCTOR_ID, appt_date)
    assert all(row['status'] == 'Cancelled' for row in rows)


def test_reschedule_voids_old_seat_and_reapproval_issues_a_new_one(
        client, queue_factory):
    old_date = future_date(45)
    new_date = future_date(65)
    appointment_id, _ = queue_factory(old_date, status='Pending',
                                      time='10:00:00')

    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')
    assert queue_rows(DOCTOR_ID, old_date)[0]['status'] == 'Waiting'

    login(client, 'patient', PATIENT_ID)
    client.post(f'/patient/reschedule/{appointment_id}',
                data={'date': new_date.isoformat(), 'time': '15:30'})

    appointment = fetch_one(
        'SELECT status, date, time FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))
    assert appointment['status'] == 'Pending'
    assert appointment['date'] == new_date
    assert queue_rows(DOCTOR_ID, old_date)[0]['status'] == 'Cancelled'

    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')

    old_rows = queue_rows(DOCTOR_ID, old_date)
    new_rows = queue_rows(DOCTOR_ID, new_date)
    assert len(old_rows) == 1 and old_rows[0]['status'] == 'Cancelled'
    assert len(new_rows) == 1
    assert new_rows[0]['queue_number'] == 1
    assert new_rows[0]['status'] == 'Waiting'
    assert new_rows[0]['appointment_id'] == appointment_id


def test_completing_an_appointment_marks_its_queue_entry_served(
        client, queue_factory, email_outbox):
    appointment_id, appt_date = queue_factory(future_date(61),
                                              status='Pending',
                                              time='12:00:00')
    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')

    response = client.post('/doctor/complete_appointment',
                           data={'appointment_id': appointment_id,
                                 'follow_up_days': 0})
    assert response.status_code == 302

    entry = fetch_one('SELECT * FROM QueueEntry WHERE appointment_id = %s',
                      (appointment_id,))
    assert entry['status'] == 'Completed'
    assert entry['started_at'] is not None
    assert entry['completed_at'] is not None
    appointment = fetch_one(
        'SELECT status FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))
    assert appointment['status'] == 'Completed'


# ---------------------------------------------------------------------------
# Doctor queue dashboard: actions, scoping, JSON
# ---------------------------------------------------------------------------

def test_queue_actions_walk_the_lifecycle_and_enforce_one_in_progress(
        client):
    queue_date = future_date(62)
    login(client, 'doctor', DOCTOR_ID)
    add_walk_in(client, queue_date, 'Queue Alpha')
    add_walk_in(client, queue_date, 'Queue Beta')
    add_walk_in(client, queue_date, 'Queue Gamma')

    rows = queue_rows(DOCTOR_ID, queue_date)
    assert [row['queue_number'] for row in rows] == [1, 2, 3]
    first, second, third = (row['queue_id'] for row in rows)

    client.post(f'/doctor/queue/entry/{first}/start')
    entry = fetch_one('SELECT * FROM QueueEntry WHERE queue_id = %s',
                      (first,))
    assert entry['status'] == 'In Progress'
    assert entry['started_at'] is not None

    # Only one patient may be in progress at a time.
    client.post(f'/doctor/queue/entry/{second}/start')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (second,))['status'] == 'Waiting'
    assert any('already in progress' in message
               for message in flash_messages(client))

    client.post(f'/doctor/queue/entry/{first}/complete')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (first,))['status'] == 'Completed'

    # Now the next waiting patient can be started.
    client.post(f'/doctor/queue/entry/{second}/start')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (second,))['status'] == 'In Progress'

    client.post(f'/doctor/queue/entry/{third}/skip')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (third,))['status'] == 'Skipped'

    client.post(f'/doctor/queue/entry/{second}/cancel')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (second,))['status'] == 'Cancelled'

    # Finishing an already finished entry is a harmless no-op.
    client.post(f'/doctor/queue/entry/{first}/complete')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (first,))['status'] == 'Completed'
    assert any('already finished' in message
               for message in flash_messages(client))


def test_queue_actions_are_scoped_to_the_logged_in_doctor(client):
    queue_date = future_date(67)
    login(client, 'doctor', DOCTOR_ID)
    add_walk_in(client, queue_date, 'Scoped Walk In')
    entry = queue_rows(DOCTOR_ID, queue_date)[0]
    entry_id = entry['queue_id']
    original_status = entry['status']

    login(client, 'doctor', OTHER_DOCTOR_ID)
    client.post(f'/doctor/queue/entry/{entry_id}/start')
    assert fetch_one('SELECT status FROM QueueEntry WHERE queue_id = %s',
                     (entry_id,))['status'] == original_status
    assert any('not found' in message for message in flash_messages(client))

    # The other doctor's own queue page never shows these entries.
    page = client.get(f'/doctor/queue?date={queue_date.isoformat()}')
    assert 'Scoped Walk In' not in page.get_data(as_text=True)


def test_doctor_queue_status_endpoint_returns_polling_json(
        client, queue_factory):
    queue_date = future_date(63)
    appointment_id, _ = queue_factory(queue_date, status='Pending',
                                      time='10:00:00')

    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')
    add_walk_in(client, queue_date, 'Walk In JSON')

    response = client.get(f'/doctor/queue/status?date={queue_date.isoformat()}')
    payload = response.get_json()
    assert response.status_code == 200
    assert payload['date'] == queue_date.isoformat()
    assert payload['last_number'] == 2
    assert payload['counts']['Waiting'] == 2
    assert [entry['label'] for entry in payload['entries']] == ['Q001', 'Q002']

    appointment_entry = payload['entries'][0]
    assert appointment_entry['kind'] == 'appointment'
    assert appointment_entry['display_name']
    assert appointment_entry['appointment_time_12hr']
    walk_in_entry = payload['entries'][1]
    assert walk_in_entry['kind'] == 'walk-in'
    assert walk_in_entry['display_name'] == 'Walk In JSON'

    login(client, 'patient', PATIENT_ID)
    blocked = client.get(f'/doctor/queue/status?date={queue_date.isoformat()}')
    assert blocked.status_code == 302


def test_patient_queue_status_json_and_dashboard_section(client,
                                                          queue_factory):
    queue_date = future_date(64)
    appointment_id, _ = queue_factory(queue_date, status='Pending',
                                      time='10:30:00')
    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')

    login(client, 'patient', PATIENT_ID)
    payload = client.get('/patient/queue/status').get_json()
    mine = [entry for entry in payload['entries']
            if entry['queue_date_str'] == queue_date.strftime('%d-%m-%Y')]
    assert len(mine) == 1
    entry = mine[0]
    assert entry['label'] == 'Q001'
    assert entry['queue_number'] == 1
    assert entry['status'] == 'Waiting'
    assert entry['ahead_count'] == 0
    assert entry['doctor_name']
    assert entry['appointment_time_12hr']

    html = client.get('/patient/dashboard').get_data(as_text=True)
    assert 'My Queue Status' in html
    assert queue_date.strftime('%d-%m-%Y') in html
    assert 'Q001' in html

    # A patient without any queue seat sees an empty list.
    login(client, 'patient', OTHER_PATIENT_ID)
    other_payload = client.get('/patient/queue/status').get_json()
    assert other_payload['entries'] == []


# ---------------------------------------------------------------------------
# Appointment emails carry the queue number
# ---------------------------------------------------------------------------

def test_reminder_email_carries_the_queue_number(client, queue_factory,
                                                 email_outbox):
    target = (app_module.get_server_time_with_offset()
              + timedelta(minutes=30, seconds=10))
    appointment_id, appt_date = queue_factory(
        target.date(), status='Approved',
        time=target.strftime('%H:%M:%S'))

    # Reserve a seat directly (the approval flow is covered above).
    conn = db_conn()
    try:
        cursor = conn.cursor()
        entry = app_module.create_queue_entry(
            cursor, DOCTOR_ID, appt_date,
            appointment_id=appointment_id, patient_id=PATIENT_ID)
        conn.commit()
    finally:
        conn.close()
    label = app_module.format_queue_number(entry['queue_number'])

    app_module.check_and_send_reminders()

    reminders = [m for m in email_outbox
                 if 'Reminder' in m['subject'] and label in m['body']]
    assert reminders, 'the 30-minute reminder must include the queue number'
    assert f'Queue Number: {label}' in reminders[-1]['body']

    appointment = fetch_one(
        'SELECT reminder_sent FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))
    assert appointment['reminder_sent'] == 1


def test_cancellation_email_carries_the_queue_number(client, queue_factory,
                                                     email_outbox):
    appointment_id, appt_date = queue_factory(future_date(68),
                                              status='Pending',
                                              time='11:00:00')
    login(client, 'doctor', DOCTOR_ID)
    client.get(f'/doctor/approve/{appointment_id}')

    login(client, 'patient', PATIENT_ID)
    client.get(f'/patient/cancel/{appointment_id}')

    assert app_module.send_appointment_cancelled_email(appointment_id) is True
    cancelled = [m for m in email_outbox
                 if 'Appointment Cancelled' in m['subject']]
    assert cancelled, 'cancellation email should be sent'
    assert 'Queue Number: Q001' in cancelled[-1]['body']


# ---------------------------------------------------------------------------
# Prescription frequency dropdown
# ---------------------------------------------------------------------------

def _medicine_form(**overrides):
    row = {'medicine_name': 'Amoxicillin', 'dosage': '500mg',
           'frequency': 'Twice daily', 'duration': '5 days'}
    row.update(overrides)
    return MultiDict((key, value) for key, value in row.items())


def test_frequency_dropdown_offers_presets_and_custom():
    assert app_module.PRESCRIPTION_FREQUENCY_OPTIONS == (
        'Once daily', 'Twice daily', 'Three times daily')
    assert app_module.PRESCRIPTION_CUSTOM_FREQUENCY == 'Custom'


def test_custom_frequency_value_merges_from_the_sibling_input():
    medicines, error = app_module.parse_prescription_medicines(
        _medicine_form(frequency='Custom', frequency_custom='Every 6 hours'))
    assert error is None
    assert medicines[0]['frequency'] == 'Every 6 hours'


def test_blank_custom_frequency_is_reported_as_missing():
    medicines, error = app_module.parse_prescription_medicines(
        _medicine_form(frequency='Custom', frequency_custom='   '))
    assert medicines == []
    assert error is not None
    assert 'row 1' in error and 'frequency' in error


def test_dropdown_preset_frequencies_are_accepted_verbatim():
    medicines, error = app_module.parse_prescription_medicines(
        _medicine_form(frequency='Once daily'))
    assert error is None
    assert medicines[0]['frequency'] == 'Once daily'


def test_legacy_free_text_frequency_is_still_accepted():
    medicines, error = app_module.parse_prescription_medicines(
        _medicine_form(frequency='Night'))
    assert error is None
    assert medicines[0]['frequency'] == 'Night'


def _completed_appointment(queue_factory):
    appointment_id, _ = queue_factory(
        date.today() - timedelta(days=3), status='Completed',
        time='10:00:00')
    return appointment_id


def test_prescription_form_renders_the_frequency_dropdown(
        client, queue_factory):
    appointment_id = _completed_appointment(queue_factory)
    login(client, 'doctor', DOCTOR_ID)

    response = client.get(
        f'/doctor/prescriptions/new?appointment_id={appointment_id}')
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="frequency"' in html
    assert 'value="Once daily"' in html
    assert 'value="Twice daily"' in html
    assert 'value="Three times daily"' in html
    assert 'value="Custom"' in html
    assert 'name="frequency_custom"' in html


def test_create_prescription_saves_the_custom_frequency(client,
                                                        queue_factory):
    appointment_id = _completed_appointment(queue_factory)
    login(client, 'doctor', DOCTOR_ID)

    response = client.post('/doctor/prescriptions/create', data=MultiDict([
        ('appointment_id', appointment_id),
        ('medicine_name', 'Azithromycin'),
        ('dosage', '250mg'),
        ('frequency', 'Custom'),
        ('frequency_custom', 'Every other day'),
        ('duration', '3 days'),
        ('instructions', ''),
    ]))
    assert response.status_code == 302

    medicine = fetch_one('''
        SELECT m.frequency FROM PrescriptionMedicine m
        JOIN Prescription p ON p.prescription_id = m.prescription_id
        WHERE p.appointment_id = %s''', (appointment_id,))
    assert medicine['frequency'] == 'Every other day'


def test_create_prescription_saves_dropdown_preset(client, queue_factory):
    appointment_id = _completed_appointment(queue_factory)
    login(client, 'doctor', DOCTOR_ID)

    response = client.post('/doctor/prescriptions/create', data=MultiDict([
        ('appointment_id', appointment_id),
        ('medicine_name', 'Paracetamol'),
        ('dosage', '500mg'),
        ('frequency', 'Twice daily'),
        ('duration', '5 days'),
        ('instructions', ''),
    ]))
    assert response.status_code == 302

    medicine = fetch_one('''
        SELECT m.frequency FROM PrescriptionMedicine m
        JOIN Prescription p ON p.prescription_id = m.prescription_id
        WHERE p.appointment_id = %s''', (appointment_id,))
    assert medicine['frequency'] == 'Twice daily'


def test_create_prescription_rejects_custom_option_with_blank_text(
        client, queue_factory):
    appointment_id = _completed_appointment(queue_factory)
    login(client, 'doctor', DOCTOR_ID)

    response = client.post('/doctor/prescriptions/create', data=MultiDict([
        ('appointment_id', appointment_id),
        ('medicine_name', 'Ibuprofen'),
        ('dosage', '200mg'),
        ('frequency', 'Custom'),
        ('frequency_custom', ''),
        ('duration', '5 days'),
        ('instructions', ''),
    ]))
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'please complete frequency' in html
    assert 'Ibuprofen' in html
    assert fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,)) is None
