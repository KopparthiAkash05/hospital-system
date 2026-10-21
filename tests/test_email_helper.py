"""Focused tests for the Brevo email helper and cancellation-email wiring.

Run from the project root:

    DISABLE_SCHEDULER=1 pytest tests/test_email_helper.py -v

Network is never touched: every HTTP call is mocked. The wiring tests reuse
the live database the same way tests/test_queue.py does (own Appointment rows
on isolated future dates, removed again in teardown).
"""
import os
import sys
from datetime import date, timedelta
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['DISABLE_SCHEDULER'] = '1'

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, 'details.env'))

import pymysql
import pymysql.cursors
import requests

import app as app_module


# ---------------------------------------------------------------------------
# Helpers for the mocked HTTP layer
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else str(payload)


def fake_post_factory(response=None, exception=None):
    """Build a stand-in for requests.post that records every call."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append({'url': url, 'headers': headers, 'json': json,
                      'timeout': timeout})
        if exception is not None:
            raise exception
        return response

    fake_post.calls = calls
    return fake_post


# ---------------------------------------------------------------------------
# send_email_via_sendgrid (the Brevo helper) — mocked HTTP
# ---------------------------------------------------------------------------

def test_success_returns_true_and_posts_to_brevo(monkeypatch):
    monkeypatch.setenv('BREVO_API_KEY', 'test-key-123')
    monkeypatch.setenv('EMAIL_FROM', 'sender@example.com')
    fake_post = fake_post_factory(FakeResponse(200, '{"messageId": "abc"}'))
    monkeypatch.setattr(requests, 'post', fake_post)

    assert app_module.send_email_via_sendgrid(
        'to@example.com', 'Hello', 'Body text') is True

    assert len(fake_post.calls) == 1
    call = fake_post.calls[0]
    assert call['url'] == 'https://api.brevo.com/v3/smtp/email'
    assert call['headers']['api-key'] == 'test-key-123'
    assert call['headers']['content-type'] == 'application/json'
    assert call['timeout'] == 20
    assert call['json']['sender']['email'] == 'sender@example.com'
    assert call['json']['to'][0]['email'] == 'to@example.com'
    assert call['json']['subject'] == 'Hello'
    assert call['json']['textContent'] == 'Body text'


def test_accepted_201_counts_as_success(monkeypatch):
    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.setenv('EMAIL_FROM', 's@example.com')
    monkeypatch.setattr(requests, 'post',
                        fake_post_factory(FakeResponse(201, '{}')))
    assert app_module.send_email_via_sendgrid('t@example.com', 'S', 'B') is True


@pytest.mark.parametrize('status', [400, 401, 403, 429, 500])
def test_api_errors_return_false(monkeypatch, status):
    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.setenv('EMAIL_FROM', 's@example.com')
    monkeypatch.setattr(requests, 'post',
                        fake_post_factory(FakeResponse(status, 'error')))
    assert app_module.send_email_via_sendgrid(
        't@example.com', 'S', 'B') is False


def test_network_timeout_returns_false(monkeypatch):
    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.setenv('EMAIL_FROM', 's@example.com')
    monkeypatch.setattr(requests, 'post',
                        fake_post_factory(exception=requests.Timeout('boom')))
    assert app_module.send_email_via_sendgrid(
        't@example.com', 'S', 'B') is False


def test_connection_error_returns_false(monkeypatch):
    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.setenv('EMAIL_FROM', 's@example.com')
    monkeypatch.setattr(requests, 'post',
                        fake_post_factory(exception=requests.ConnectionError()))
    assert app_module.send_email_via_sendgrid(
        't@example.com', 'S', 'B') is False


def test_missing_api_key_returns_false_without_http(monkeypatch):
    monkeypatch.delenv('BREVO_API_KEY', raising=False)
    monkeypatch.setenv('EMAIL_FROM', 's@example.com')
    fake_post = fake_post_factory(FakeResponse(200, '{}'))
    monkeypatch.setattr(requests, 'post', fake_post)
    assert app_module.send_email_via_sendgrid(
        't@example.com', 'S', 'B') is False
    assert fake_post.calls == []


def test_missing_sender_returns_false_without_http(monkeypatch):
    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.delenv('EMAIL_FROM', raising=False)
    fake_post = fake_post_factory(FakeResponse(200, '{}'))
    monkeypatch.setattr(requests, 'post', fake_post)
    assert app_module.send_email_via_sendgrid(
        't@example.com', 'S', 'B') is False
    assert fake_post.calls == []


def test_helper_failure_never_raises(monkeypatch):
    """Every failure mode must surface as False, never an exception."""
    monkeypatch.setenv('BREVO_API_KEY', 'k')
    monkeypatch.setenv('EMAIL_FROM', 's@example.com')
    monkeypatch.setattr(requests, 'post',
                        fake_post_factory(exception=RuntimeError('weird')))
    assert app_module.send_email_via_sendgrid(
        't@example.com', 'S', 'B') is False


# ---------------------------------------------------------------------------
# Cancellation email wiring — real routes, mocked Brevo API
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def flask_app():
    app_module.app.config['TESTING'] = True
    return app_module.app


def db_conn():
    return pymysql.connect(
        host=os.environ['MYSQL_HOST'],
        user=os.environ['MYSQL_USER'],
        password=os.environ['MYSQL_PASSWORD'],
        database=os.environ['MYSQL_DB'],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


def login(client, user_type, user_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess['loggedin'] = True
        sess['user_type'] = user_type
        sess['id'] = user_id
        sess['email'] = 'qa@example.invalid'
        sess['name'] = 'QA User'


@pytest.fixture()
def pending_appointment():
    """Create an isolated Pending appointment; removed again on teardown."""
    day = date.today() + timedelta(days=60)
    while day.weekday() == 6:
        day += timedelta(days=1)
    conn = db_conn()
    created = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO Appointment '
                '(patient_id, doctor_id, date, time, status, reason) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (1, 1, day, '11:00:00', 'Pending', 'QA cancel-email test'),
            )
            appointment_id = cur.lastrowid
        created.append(appointment_id)
        yield appointment_id
    finally:
        with conn.cursor() as cur:
            for appointment_id in created:
                cur.execute(
                    'DELETE FROM Appointment WHERE appointment_id = %s',
                    (appointment_id,))
        conn.close()


def _record_outbox(monkeypatch):
    outbox = []

    def fake_send(to_email, subject, body):
        outbox.append({'to': to_email, 'subject': subject, 'body': body})
        return True

    monkeypatch.setattr(app_module, 'send_email_via_sendgrid', fake_send)
    return outbox


def test_patient_cancel_sends_cancellation_email(client, pending_appointment,
                                                 monkeypatch):
    outbox = _record_outbox(monkeypatch)
    login(client, 'patient', 1)

    resp = client.get(f'/patient/cancel/{pending_appointment}',
                      follow_redirects=False)
    assert resp.status_code in (302, 303)

    cancelled = [m for m in outbox if 'cancel' in m['subject'].lower()]
    assert len(cancelled) == 1
    assert cancelled[0]['to']  # a real recipient address was used
    assert 'MediCare' in cancelled[0]['subject']


def test_doctor_cancel_sends_cancellation_email(client, pending_appointment,
                                                monkeypatch):
    outbox = _record_outbox(monkeypatch)
    login(client, 'doctor', 1)

    resp = client.get(f'/doctor/cancel/{pending_appointment}',
                      follow_redirects=False)
    assert resp.status_code in (302, 303)

    cancelled = [m for m in outbox if 'cancel' in m['subject'].lower()]
    assert len(cancelled) == 1


def test_admin_cancel_sends_cancellation_email(client, pending_appointment,
                                               monkeypatch):
    outbox = _record_outbox(monkeypatch)
    login(client, 'admin', 1)

    resp = client.post(f'/admin/appointments/{pending_appointment}/cancel',
                       follow_redirects=False)
    assert resp.status_code in (302, 303)

    cancelled = [m for m in outbox if 'cancel' in m['subject'].lower()]
    assert len(cancelled) == 1


def test_cancel_email_failure_does_not_break_route(client, pending_appointment,
                                                   monkeypatch):
    """A Brevo outage must still leave the appointment cancelled."""
    def failing_send(to_email, subject, body):
        return False

    monkeypatch.setattr(app_module, 'send_email_via_sendgrid', failing_send)
    login(client, 'patient', 1)

    resp = client.get(f'/patient/cancel/{pending_appointment}',
                      follow_redirects=True)
    assert resp.status_code == 200

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM Appointment WHERE appointment_id = %s',
                        (pending_appointment,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row['status'] == 'Cancelled'
