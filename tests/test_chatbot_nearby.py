"""Tests for the intelligent triage chatbot (/api/chatbot) and the
nearby-doctors listing (/patient/nearby_doctors).

Run from the project root:

    DISABLE_SCHEDULER=1 pytest tests/test_chatbot_nearby.py -v

The suite is read-only against existing Patient/Doctor rows: it asserts on
response shape and on the *absence* of fabricated data, never on specific
doctor/location values that would change between environments.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['DISABLE_SCHEDULER'] = '1'

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, 'details.env'))

import pymysql
import pymysql.cursors

import app as app_module


# ---------------------------------------------------------------------------
# Helpers
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


@pytest.fixture(scope='session')
def flask_app():
    app_module.app.config['TESTING'] = True
    return app_module.app


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


def ask(client, message, **extra):
    payload = {'input': message}
    payload.update(extra)
    resp = client.post('/api/chatbot', json=payload)
    assert resp.status_code == 200
    return resp.get_json()


# ---------------------------------------------------------------------------
# Chatbot: emergencies always short-circuit to a safety-first answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('message', [
    'I think I am having a heart attack',
    'my father is unconscious and not breathing',
    'severe bleeding that wont stop',
    'I want to die',
    'possible overdose',
    'emergency',
])
def test_emergency_keywords_return_emergency_intent(client, message):
    data = ask(client, message)
    assert data['intent'] == 'emergency'
    assert data['options'] == []
    lowered = data['message'].lower()
    assert 'emergency' in lowered
    # Never fabricates a number: either the stored hospital line or generic advice.
    hospital = fetch_one(
        'SELECT emergency_phone FROM HospitalInfo ORDER BY info_id LIMIT 1')
    if hospital and hospital.get('emergency_phone'):
        assert hospital['emergency_phone'] in data['message']


def test_emergency_reply_does_not_invent_a_hotline_when_none_stored():
    reply = app_module._chatbot_emergency_reply({'emergency_phone': None})
    assert '112' in reply  # generic real number only, clearly framed as an example
    assert 'emergency' in reply.lower()
    for fake in ('9876543210', '911', '+1 234'):
        assert fake not in reply


def test_emergency_reply_uses_stored_hospital_number():
    reply = app_module._chatbot_emergency_reply({'emergency_phone': '+1 234 567 911'})
    assert '+1 234 567 911' in reply
    assert '+1 234 567 890' not in reply


# ---------------------------------------------------------------------------
# Chatbot: triage intent uses the shared symptom checker
# ---------------------------------------------------------------------------

def test_triage_matches_department_from_free_text(client):
    data = ask(client, 'I have had a pounding headache and a migraine for two days')
    assert data['intent'] == 'triage'
    assert data['department']
    assert 'diagnosis' in data['message'].lower()  # explicit non-diagnosis disclaimer
    assert 'Book Appointment' in data['options']


def test_chest_pain_triggers_emergency_by_design(client):
    # "chest pain" is on the emergency red-flag list, so it must short-circuit
    # to safety guidance rather than being treated as routine triage.
    data = ask(client, 'sharp chest pain when I take a deep breath')
    assert data['intent'] == 'emergency'
    assert data['options'] == []


def test_triage_matches_a_department_for_non_red_flag_symptoms(client):
    data = ask(client, 'itchy red rash on my arm for a week')
    assert data['intent'] == 'triage'
    assert data['department'] in ('Dermatology', 'Allergy & Immunology')
    assert 'Book Appointment' in data['options']


def test_unrecognised_text_is_not_guessed(client):
    data = ask(client, 'asdfgh qwerty zxcvb')
    assert data['intent'] == 'clarify'
    # Must not claim a department it cannot justify.
    assert 'department' not in data


# ---------------------------------------------------------------------------
# Chatbot: navigation and informational intents
# ---------------------------------------------------------------------------

def test_nearby_intent_redirects_to_nearby_doctors(client):
    data = ask(client, 'show me doctors near me')
    assert data['intent'] == 'nearby'
    assert data['redirect'].endswith('/patient/nearby_doctors')


def test_book_intent_redirects_to_doctor_list(client):
    data = ask(client, 'I want to book an appointment', department='Cardiology')
    assert data['intent'] == 'book'
    assert 'Cardiology' in data['redirect']


def test_opening_hours_intent_uses_stored_value(client):
    data = ask(client, 'what are your opening hours?')
    assert data['intent'] == 'hours'
    hospital = fetch_one(
        'SELECT opening_hours FROM HospitalInfo ORDER BY info_id LIMIT 1')
    if hospital and hospital.get('opening_hours'):
        assert hospital['opening_hours'] in data['message']


def test_contact_intent_reports_hospital_details(client):
    data = ask(client, 'can I have your phone number and email')
    assert data['intent'] in ('contact',)
    assert 'reach' in data['message'].lower() or 'phone' in data['message'].lower()


def test_empty_input_returns_greeting(client):
    data = ask(client, '   ')
    assert data['intent'] == 'greeting'
    assert data['options']


def test_contact_text_never_invents_details_when_missing():
    text = app_module._hospital_contact_text({})
    assert 'verified' in text.lower() or "don't have" in text.lower()


def test_contact_text_uses_only_supplied_fields():
    text = app_module._hospital_contact_text({
        'name': 'Test Hospital',
        'phone': '+1 000 111 222',
        'email': None,
        'emergency_phone': None,
        'opening_hours': None,
        'address': None,
    })
    assert '+1 000 111 222' in text
    assert 'Email:' not in text
    assert 'Address:' not in text


# ---------------------------------------------------------------------------
# Nearby doctors
# ---------------------------------------------------------------------------

def _login_patient(client, patient_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess['loggedin'] = True
        sess['user_type'] = 'patient'
        sess['id'] = patient_id
        sess['email'] = 'qa@example.com'
        sess['name'] = 'QA Patient'


def test_nearby_doctors_requires_login(client):
    resp = client.get('/patient/nearby_doctors')
    assert resp.status_code in (302, 401, 403)


def test_nearby_doctors_lists_active_doctors_without_location(client):
    row = fetch_one("SELECT patient_id FROM Patient ORDER BY patient_id LIMIT 1")
    if not row:
        pytest.skip('no patient rows available')
    _login_patient(client, row['patient_id'])

    resp = client.get('/patient/nearby_doctors')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    active = fetch_one(
        "SELECT COUNT(*) c FROM Doctor WHERE (is_active IS NULL OR is_active = 1)")
    if active and active['c']:
        # The page must list available doctors rather than showing the old
        # "no doctors with location data" empty state for the whole listing.
        assert 'Doctors Near You' in body


def test_nearby_doctors_labels_straight_line_distance(client):
    row = fetch_one("SELECT patient_id FROM Patient ORDER BY patient_id LIMIT 1")
    if not row:
        pytest.skip('no patient rows available')
    _login_patient(client, row['patient_id'])

    body = client.get('/patient/nearby_doctors').get_data(as_text=True)
    # Whenever a distance is shown it must be labelled as approximate, never
    # presented as an exact road distance or travel time.
    if 'km away' in body:
        assert 'approximate straight-line distance' in body
    assert 'Travel time' not in body
    assert 'min away' not in body
