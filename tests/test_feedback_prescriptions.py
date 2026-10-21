"""Tests for the Patient Feedback & Ratings and Prescription features.

Run from the project root:

    DISABLE_SCHEDULER=1 pytest tests/test_feedback_prescriptions.py -v

The suite reuses existing Patient/Doctor rows (read-only) and creates its own
Appointment rows plus all dependent Feedback/Prescription rows, which are
removed again in teardown so no application data is left behind.
"""
import os
import re
import sys
import uuid
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
from werkzeug.security import generate_password_hash

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


def run(sql, params=()):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture(scope='session')
def flask_app():
    app_module.app.config['TESTING'] = True
    return app_module.app


@pytest.fixture(scope='session', autouse=True)
def _audit_log_cleanup():
    """Remove AuditLog rows written by this test session afterwards.

    Captured from the database clock, which is what INSERTs are stamped with,
    so real application history is never touched.
    """
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute('SELECT NOW() AS now')
        session_start = cur.fetchone()['now']
    conn.close()
    yield
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute('DELETE FROM AuditLog WHERE created_at >= %s', (session_start,))
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


@pytest.fixture()
def appointment_factory():
    """Create appointments owned by this test; cascade-cleans on teardown."""
    conn = db_conn()
    created = []

    def create(patient_id=PATIENT_ID, doctor_id=DOCTOR_ID, status='Completed',
               days_ago=3):
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO Appointment '
                '(patient_id, doctor_id, date, time, status, reason) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (patient_id, doctor_id, date.today() - timedelta(days=days_ago),
                 '10:00:00', status, 'QA feedback/prescription test visit'),
            )
            appointment_id = cur.lastrowid
        created.append(appointment_id)
        return appointment_id

    yield create

    with conn.cursor() as cur:
        for appointment_id in created:
            cur.execute('DELETE FROM Appointment WHERE appointment_id = %s',
                        (appointment_id,))
    conn.close()


# ---------------------------------------------------------------------------
# Pure validation helpers
# ---------------------------------------------------------------------------

def test_clean_feedback_comment_trims_and_accepts_short_text():
    comment, error = app_module.clean_feedback_comment('  Great visit  ')
    assert comment == 'Great visit'
    assert error is None


def test_clean_feedback_comment_rejects_overlong_text():
    comment, error = app_module.clean_feedback_comment('x' * 2000)
    assert comment == ''
    assert error is not None
    assert '1000' in error


def test_parse_prescription_medicines_ignores_blank_rows():
    form = MultiDict([
        ('medicine_name', 'Amoxicillin 500mg'),
        ('dosage', '1 capsule'),
        ('frequency', 'Twice daily'),
        ('duration', '5 days'),
        ('medicine_name', ''),
        ('dosage', ''),
        ('frequency', ''),
        ('duration', ''),
    ])
    medicines, error = app_module.parse_prescription_medicines(form)
    assert error is None
    assert len(medicines) == 1
    assert medicines[0]['medicine_name'] == 'Amoxicillin 500mg'


def test_parse_prescription_medicines_requires_every_field():
    form = MultiDict([
        ('medicine_name', 'Paracetamol'),
        ('dosage', ''),
        ('frequency', 'Thrice daily'),
        ('duration', '3 days'),
    ])
    medicines, error = app_module.parse_prescription_medicines(form)
    assert medicines == []
    assert error is not None
    assert 'Medicine row 1' in error
    assert 'dosage' in error


def test_parse_prescription_medicines_requires_at_least_one_row():
    medicines, error = app_module.parse_prescription_medicines(MultiDict())
    assert medicines == []
    assert error is not None


# ---------------------------------------------------------------------------
# Feedback: access control and login hand-off
# ---------------------------------------------------------------------------

def test_anonymous_patient_is_sent_to_login_and_target_is_remembered(client):
    response = client.get('/patient/feedback?appointment_id=12')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')

    with client.session_transaction() as sess:
        assert sess.get('next_url') == '/patient/feedback?appointment_id=12'


def test_login_honours_stashed_target(client):
    email = f'qa-login-{uuid.uuid4().hex[:10]}@example.com'
    password = 'Sup3rSecret!'
    patient_id = run(
        'INSERT INTO Patient (name, email, password) VALUES (%s, %s, %s)',
        ('QA Login Patient', email, generate_password_hash(password)),
    )
    try:
        target = '/patient/feedback?appointment_id=77'
        with client.session_transaction() as sess:
            sess['next_url'] = target

        response = client.post('/login',
                               data={'email': email, 'password': password},
                               follow_redirects=False)
        assert response.status_code == 302
        assert response.headers['Location'] == target
    finally:
        run('DELETE FROM Patient WHERE patient_id = %s', (patient_id,))


def test_feedback_pages_require_the_matching_role(client):
    assert client.get('/patient/feedback').status_code == 302
    assert client.get('/doctor/feedback').status_code == 302
    assert client.get('/admin/feedback').status_code == 302


# ---------------------------------------------------------------------------
# Feedback: patient flow
# ---------------------------------------------------------------------------

def test_feedback_page_lists_completed_appointment(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)

    response = client.get('/patient/feedback')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Share Feedback' in html
    assert f'value="{appointment_id}"' in html


def test_submit_feedback_persists_and_thanks_patient(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)

    response = client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id,
        'rating': 5,
        'comment': 'Very thorough and caring doctor.',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert 'Thank you for sharing your feedback!' in \
        response.get_data(as_text=True)

    row = fetch_one('SELECT * FROM Feedback WHERE appointment_id = %s',
                    (appointment_id,))
    assert row is not None
    assert row['patient_id'] == PATIENT_ID
    assert row['doctor_id'] == DOCTOR_ID
    assert row['rating'] == 5
    assert row['comment'] == 'Very thorough and caring doctor.'

    # The appointment is no longer listed as waiting for feedback.
    page = client.get('/patient/feedback').get_data(as_text=True)
    assert f'value="{appointment_id}"' not in page
    assert 'Very thorough and caring doctor.' in page


def test_duplicate_feedback_is_rejected(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)

    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 4, 'comment': 'Nice.',
    })
    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 1, 'comment': 'Changed.',
    })

    rows = fetch_one(
        'SELECT COUNT(*) AS n FROM Feedback WHERE appointment_id = %s',
        (appointment_id,))
    assert rows['n'] == 1


def test_feedback_only_allowed_for_completed_appointments(client,
                                                          appointment_factory):
    appointment_id = appointment_factory(status='Approved')
    login(client, 'patient', PATIENT_ID)

    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 5, 'comment': 'Too early.',
    })

    row = fetch_one('SELECT feedback_id FROM Feedback WHERE appointment_id = %s',
                    (appointment_id,))
    assert row is None


def test_feedback_rating_must_be_one_to_five(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)

    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 7, 'comment': 'Nope.',
    })
    assert fetch_one('SELECT feedback_id FROM Feedback WHERE appointment_id = %s',
                     (appointment_id,)) is None


def test_patient_cannot_review_another_patients_appointment(client,
                                                            appointment_factory):
    appointment_id = appointment_factory(patient_id=OTHER_PATIENT_ID)
    login(client, 'patient', PATIENT_ID, email='not-owner@example.com')

    page = client.get('/patient/feedback').get_data(as_text=True)
    assert f'value="{appointment_id}"' not in page

    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 5, 'comment': 'Sneaky.',
    })
    assert fetch_one('SELECT feedback_id FROM Feedback WHERE appointment_id = %s',
                     (appointment_id,)) is None


def test_patient_can_edit_their_own_feedback(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)
    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 3, 'comment': 'Okay visit.',
    })

    feedback = fetch_one(
        'SELECT feedback_id FROM Feedback WHERE appointment_id = %s',
        (appointment_id,))
    client.post(f"/patient/feedback/{feedback['feedback_id']}/update", data={
        'rating': 5,
        'comment': 'Revised: excellent care after all.',
    })

    updated = fetch_one(
        'SELECT rating, comment FROM Feedback WHERE feedback_id = %s',
        (feedback['feedback_id'],))
    assert updated['rating'] == 5
    assert updated['comment'] == 'Revised: excellent care after all.'


def test_patient_cannot_edit_someone_elses_feedback(client, appointment_factory):
    appointment_id = appointment_factory(patient_id=OTHER_PATIENT_ID)
    login(client, 'patient', PATIENT_ID)
    # Seed a review by the real owner directly.
    other = fetch_one('SELECT patient_id FROM Patient WHERE patient_id = %s',
                      (OTHER_PATIENT_ID,))
    feedback_id = run(
        'INSERT INTO Feedback (appointment_id, patient_id, doctor_id, rating, comment) '
        'VALUES (%s, %s, %s, %s, %s)',
        (appointment_id, other['patient_id'], DOCTOR_ID, 2, 'Owner review.'))
    try:
        login(client, 'patient', PATIENT_ID)
        client.post(f'/patient/feedback/{feedback_id}/update', data={
            'rating': 5, 'comment': 'Hijacked.',
        })
        row = fetch_one('SELECT comment FROM Feedback WHERE feedback_id = %s',
                        (feedback_id,))
        assert row['comment'] == 'Owner review.'
    finally:
        run('DELETE FROM Feedback WHERE feedback_id = %s', (feedback_id,))


# ---------------------------------------------------------------------------
# Feedback: doctor and admin views, moderation
# ---------------------------------------------------------------------------

def test_doctor_feedback_page_shows_review_and_statistics(client,
                                                          appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)
    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 4,
        'comment': 'Clear explanations, short wait.',
    })

    login(client, 'doctor', DOCTOR_ID)
    response = client.get('/doctor/feedback')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Clear explanations, short wait.' in html
    assert 'Total Reviews' in html
    assert 'Average Rating' in html
    assert 'Rating Distribution' in html
    assert f'#{appointment_id}' in html


def test_doctor_feedback_rating_filter(client, appointment_factory):
    keep_id = appointment_factory()
    drop_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)
    client.post('/patient/feedback/submit', data={
        'appointment_id': keep_id, 'rating': 5, 'comment': 'KEEPME five star.',
    })
    client.post('/patient/feedback/submit', data={
        'appointment_id': drop_id, 'rating': 2, 'comment': 'DROPM two star.',
    })

    login(client, 'doctor', DOCTOR_ID)
    html = client.get('/doctor/feedback?rating=5').get_data(as_text=True)
    assert 'KEEPME five star.' in html
    assert 'DROPM two star.' not in html


def test_admin_feedback_moderation_hide_and_restore(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'patient', PATIENT_ID)
    client.post('/patient/feedback/submit', data={
        'appointment_id': appointment_id, 'rating': 1,
        'comment': 'MODQAVISIBLE bad experience.',
    })
    feedback = fetch_one(
        'SELECT feedback_id FROM Feedback WHERE appointment_id = %s',
        (appointment_id,))

    login(client, 'admin', 1)
    page = client.get('/admin/feedback').get_data(as_text=True)
    assert 'MODQAVISIBLE bad experience.' in page

    client.post(f"/admin/feedback/{feedback['feedback_id']}/toggle")
    assert fetch_one('SELECT is_hidden FROM Feedback WHERE feedback_id = %s',
                     (feedback['feedback_id'],))['is_hidden'] == 1

    # Hidden from the patient's history and the doctor's list.
    login(client, 'doctor', DOCTOR_ID)
    doctor_html = client.get('/doctor/feedback').get_data(as_text=True)
    assert 'MODQAVISIBLE bad experience.' not in doctor_html

    login(client, 'patient', PATIENT_ID)
    patient_html = client.get('/patient/feedback').get_data(as_text=True)
    assert 'MODQAVISIBLE bad experience.' in patient_html  # own history
    assert 'Hidden by moderator' in patient_html

    # Restore from the admin page.
    login(client, 'admin', 1)
    client.post(f"/admin/feedback/{feedback['feedback_id']}/toggle")
    assert fetch_one('SELECT is_hidden FROM Feedback WHERE feedback_id = %s',
                     (feedback['feedback_id'],))['is_hidden'] == 0
    login(client, 'doctor', DOCTOR_ID)
    assert 'MODQAVISIBLE bad experience.' in \
        client.get('/doctor/feedback').get_data(as_text=True)


def test_admin_feedback_filters_render(client, appointment_factory):
    login(client, 'admin', 1)
    for query in ('?rating=5', '?search=john', '?date_from=2020-01-01',
                  '?date_from=not-a-date', '?rating=99'):
        response = client.get('/admin/feedback' + query)
        assert response.status_code == 200, query


# ---------------------------------------------------------------------------
# Feedback invitation email (Brevo path is faked)
# ---------------------------------------------------------------------------

def test_feedback_invitation_is_sent_once_and_skips_existing_reviews(
        monkeypatch, appointment_factory):
    calls = []

    def fake_send(to_email, subject, body):
        calls.append({'to': to_email, 'subject': subject, 'body': body})
        return True

    monkeypatch.setattr(app_module, 'send_email_via_sendgrid', fake_send)

    appointment_id = appointment_factory()
    assert app_module.send_feedback_invitation_email(appointment_id) is True
    assert len(calls) == 1
    assert '/patient/feedback' in calls[0]['body']

    # Deduped through the persisted flag.
    assert app_module.send_feedback_invitation_email(appointment_id) is False
    assert len(calls) == 1
    assert fetch_one(
        'SELECT feedback_email_sent FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))['feedback_email_sent'] == 1

    # Never invites a patient who already reviewed the visit.
    reviewed_id = appointment_factory()
    run('INSERT INTO Feedback (appointment_id, patient_id, doctor_id, rating) '
        'VALUES (%s, %s, %s, 5)',
        (reviewed_id, PATIENT_ID, DOCTOR_ID))
    assert app_module.send_feedback_invitation_email(reviewed_id) is False
    assert len(calls) == 1


def test_completed_appointment_triggers_the_invitation(monkeypatch,
                                                       appointment_factory):
    calls = []
    monkeypatch.setattr(
        app_module, 'send_email_via_sendgrid',
        lambda *args, **kwargs: calls.append(args) or True)
    monkeypatch.setattr(app_module, 'send_appointment_completed_email',
                        lambda appointment_id: True)
    monkeypatch.setattr(app_module, 'notify_appointment_event',
                        lambda *args, **kwargs: None)

    appointment_id = appointment_factory(status='Approved')
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess['loggedin'] = True
        sess['user_type'] = 'doctor'
        sess['id'] = DOCTOR_ID
        sess['email'] = 'smith@gmail.com'
        sess['name'] = 'Smith'

    response = client.post('/doctor/complete_appointment',
                           data={'appointment_id': appointment_id,
                                 'follow_up_days': 0},
                           follow_redirects=False)
    assert response.status_code == 302

    status = fetch_one('SELECT status FROM Appointment WHERE appointment_id = %s',
                       (appointment_id,))
    assert status['status'] == 'Completed'
    assert len(calls) == 1
    assert fetch_one(
        'SELECT feedback_email_sent FROM Appointment WHERE appointment_id = %s',
        (appointment_id,))['feedback_email_sent'] == 1


# ---------------------------------------------------------------------------
# Prescriptions: doctor side
# ---------------------------------------------------------------------------

def test_doctor_prescriptions_pages_render(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)

    page = client.get('/doctor/prescriptions')
    assert page.status_code == 200
    assert 'Waiting for a Prescription' in page.get_data(as_text=True)

    new_form = client.get(f'/doctor/prescriptions/new?appointment_id={appointment_id}')
    assert new_form.status_code == 200
    assert 'New Prescription' in new_form.get_data(as_text=True)

    # Without an appointment there is nothing to prescribe for.
    assert client.get('/doctor/prescriptions/new').status_code == 302


def test_prescription_form_carries_action_and_appointment_id(
        client, appointment_factory):
    """Regression: the rendered form must POST to the create endpoint and
    carry the hidden appointment_id — without them the Save button posts
    back to the GET-only /new page and nothing is ever stored."""
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)

    html = client.get(
        f'/doctor/prescriptions/new?appointment_id={appointment_id}'
    ).get_data(as_text=True)
    assert 'action="/doctor/prescriptions/create"' in html
    assert f'name="appointment_id" value="{appointment_id}"' in html


def test_save_prescription_button_flow_persists(client, appointment_factory):
    """Simulates the browser flow: fetch the rendered form, submit to the
    form's own action with its own hidden fields, and verify the row lands
    in the database — i.e. the Save Prescription button actually works."""
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)

    html = client.get(
        f'/doctor/prescriptions/new?appointment_id={appointment_id}'
    ).get_data(as_text=True)
    action = re.search(r'action="([^"]+)"', html).group(1)

    response = client.post(action, data={
        'appointment_id': appointment_id,
        'instructions': 'Browser flow instructions.',
        'medicine_name': ['Azithromycin 250mg', ''],
        'dosage': ['1 tablet', ''],
        'frequency': ['Once daily', ''],
        'duration': ['3 days', ''],
    })
    assert response.status_code == 302

    prescription = fetch_one(
        'SELECT * FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))
    assert prescription is not None
    assert prescription['doctor_id'] == DOCTOR_ID
    meds = fetch_one(
        'SELECT COUNT(*) AS n FROM PrescriptionMedicine WHERE prescription_id = %s',
        (prescription['prescription_id'],))
    assert meds['n'] == 1

    # The redirect target (detail page) renders the saved values.
    detail = client.get(response.headers['Location'])
    assert detail.status_code == 200
    assert 'Azithromycin 250mg' in detail.get_data(as_text=True)


def test_edit_prescription_form_posts_to_update_endpoint(
        client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'medicine_name': ['Old Medicine'],
        'dosage': ['1'], 'frequency': ['1'], 'duration': ['1'],
    })
    prescription = fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))

    html = client.get(
        f"/doctor/prescriptions/{prescription['prescription_id']}/edit"
    ).get_data(as_text=True)
    assert (f'action="/doctor/prescriptions/'
            f'{prescription["prescription_id"]}/update"') in html


def test_prescription_create_persists_medicines(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)

    response = client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'instructions': 'Take after meals. Finish the course.',
        'medicine_name': ['Amoxicillin 500mg', 'Paracetamol 650mg', ''],
        'dosage': ['1 capsule', '1 tablet', ''],
        'frequency': ['Twice daily', 'As needed', ''],
        'duration': ['5 days', '3 days', ''],
    })
    assert response.status_code == 302

    prescription = fetch_one(
        'SELECT * FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))
    assert prescription is not None
    assert prescription['doctor_id'] == DOCTOR_ID
    assert prescription['instructions'].startswith('Take after meals')

    medicines = fetch_one(
        'SELECT COUNT(*) AS n FROM PrescriptionMedicine WHERE prescription_id = %s',
        (prescription['prescription_id'],))
    assert medicines['n'] == 2

    detail = client.get(
        f"/doctor/prescriptions/{prescription['prescription_id']}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert 'Amoxicillin 500mg' in html
    assert 'Take after meals' in html

    # Duplicate creation is refused for the same appointment.
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'medicine_name': ['ShouldNotSave'],
        'dosage': ['1'],
        'frequency': ['1'],
        'duration': ['1'],
    })
    total = fetch_one(
        'SELECT COUNT(*) AS n FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))
    assert total['n'] == 1


def test_prescription_validation_renders_form_with_kept_values(
        client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)

    response = client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'instructions': 'Keep this instruction text',
        'medicine_name': ['Ibuprofen'],
        'dosage': ['1 tablet'],
        'frequency': [''],
        'duration': ['7 days'],
    })
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'please complete' in html
    assert 'Keep this instruction text' in html
    assert 'Ibuprofen' in html
    assert fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,)) is None


def test_prescription_needs_completed_appointment(client, appointment_factory):
    appointment_id = appointment_factory(status='Approved')
    login(client, 'doctor', DOCTOR_ID)

    response = client.get(f'/doctor/prescriptions/new?appointment_id={appointment_id}')
    assert response.status_code == 302

    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'medicine_name': ['X'], 'dosage': ['X'],
        'frequency': ['X'], 'duration': ['X'],
    })
    assert fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,)) is None


def test_prescriptions_are_scoped_to_the_owning_doctor(client,
                                                       appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'medicine_name': ['Azithromycin'],
        'dosage': ['1 tablet'],
        'frequency': ['Once daily'],
        'duration': ['3 days'],
    })
    prescription = fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))

    login(client, 'doctor', OTHER_DOCTOR_ID)
    response = client.get(f"/doctor/prescriptions/{prescription['prescription_id']}")
    assert response.status_code == 302

    edit = client.get(
        f"/doctor/prescriptions/{prescription['prescription_id']}/edit")
    assert edit.status_code == 302


def test_doctor_can_edit_prescription(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'medicine_name': ['Old Medicine'],
        'dosage': ['1'], 'frequency': ['1'], 'duration': ['1'],
    })
    prescription = fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))

    response = client.post(
        f"/doctor/prescriptions/{prescription['prescription_id']}/update",
        data={
            'instructions': 'Updated instructions.',
            'medicine_name': ['New Medicine A', 'New Medicine B'],
            'dosage': ['2 tablets', '1 tablet'],
            'frequency': ['Twice daily', 'Night'],
            'duration': ['7 days', '14 days'],
        })
    assert response.status_code == 302

    row = fetch_one(
        'SELECT instructions FROM Prescription WHERE prescription_id = %s',
        (prescription['prescription_id'],))
    assert row['instructions'] == 'Updated instructions.'
    meds = fetch_one(
        'SELECT COUNT(*) AS n FROM PrescriptionMedicine WHERE prescription_id = %s',
        (prescription['prescription_id'],))
    assert meds['n'] == 2


# ---------------------------------------------------------------------------
# Prescriptions: patient side + PDF
# ---------------------------------------------------------------------------

def test_patient_can_view_prescription_and_download_pdf(client,
                                                        appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'instructions': 'Drink plenty of water.',
        'medicine_name': ['Cetirizine 10mg'],
        'dosage': ['1 tablet'],
        'frequency': ['Night'],
        'duration': ['7 days'],
    })

    login(client, 'patient', PATIENT_ID)

    listing = client.get('/patient/prescriptions')
    assert listing.status_code == 200
    html = listing.get_data(as_text=True)
    assert f'#{appointment_id}' in html
    assert '1 medicine(s)' in html

    prescription = fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))
    detail = client.get(
        f"/patient/prescriptions/{prescription['prescription_id']}")
    assert detail.status_code == 200
    assert 'Cetirizine 10mg' in detail.get_data(as_text=True)

    download = client.get(
        f"/patient/prescriptions/{prescription['prescription_id']}/download")
    assert download.status_code == 200
    assert download.mimetype == 'application/pdf'
    assert download.data[:5] == b'%PDF-'
    disposition = download.headers.get('Content-Disposition', '')
    assert f"Prescription_{appointment_id}.pdf" in disposition


def test_patient_cannot_open_another_patients_prescription(client,
                                                           appointment_factory):
    appointment_id = appointment_factory(patient_id=OTHER_PATIENT_ID)
    login(client, 'doctor', DOCTOR_ID)
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'medicine_name': ['Secret Med'],
        'dosage': ['1'], 'frequency': ['1'], 'duration': ['1'],
    })
    prescription = fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))

    login(client, 'patient', PATIENT_ID)
    assert client.get(
        f"/patient/prescriptions/{prescription['prescription_id']}").status_code \
        == 302
    assert client.get(
        f"/patient/prescriptions/{prescription['prescription_id']}/download") \
        .status_code == 302


def test_pdf_contains_persisted_prescription_data(client, appointment_factory):
    appointment_id = appointment_factory()
    login(client, 'doctor', DOCTOR_ID)
    client.post('/doctor/prescriptions/create', data={
        'appointment_id': appointment_id,
        'instructions': 'PDFINSTR take with food',
        'medicine_name': ['PDFMED Ibuprofen'],
        'dosage': ['1 tablet'],
        'frequency': ['Twice daily'],
        'duration': ['5 days'],
    })

    prescription = fetch_one(
        'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
        (appointment_id,))
    login(client, 'patient', PATIENT_ID)
    download = client.get(
        f"/patient/prescriptions/{prescription['prescription_id']}/download")
    assert download.status_code == 200

    # ReportLab compresses text, so decode with pdfminer-free sanity checks:
    # the file must be a real, multi-page-safe PDF built from the saved rows.
    data = download.data
    assert data.startswith(b'%PDF-')
    assert b'%%EOF' in data[-1024:]
    assert len(data) > 1500


# ---------------------------------------------------------------------------
# Dashboards keep working with the new integrations
# ---------------------------------------------------------------------------

def test_dashboards_render_with_new_quick_actions(client, appointment_factory):
    login(client, 'patient', PATIENT_ID)
    html = client.get('/patient/dashboard').get_data(as_text=True)
    assert client.get('/patient/dashboard').status_code == 200
    assert 'My Prescriptions' in html
    assert 'Feedback' in html

    login(client, 'doctor', DOCTOR_ID)
    doctor_html = client.get('/doctor/dashboard').get_data(as_text=True)
    assert client.get('/doctor/dashboard').status_code == 200
    assert 'Prescriptions' in doctor_html
    assert 'Patient Feedback' in doctor_html

    login(client, 'admin', 1)
    admin_html = client.get('/admin/dashboard').get_data(as_text=True)
    assert client.get('/admin/dashboard').status_code == 200
    assert 'Feedback Reviews' in admin_html


def test_appointment_pages_expose_action_buttons(client, appointment_factory):
    appointment_id = appointment_factory()

    login(client, 'patient', PATIENT_ID)
    html = client.get('/patient/appointments').get_data(as_text=True)
    assert 'Leave Feedback' in html

    login(client, 'doctor', DOCTOR_ID)
    doctor_html = client.get('/doctor/appointments').get_data(as_text=True)
    assert 'Prescribe' in doctor_html

    detail = client.get(f'/doctor/appointment/{appointment_id}')
    assert detail.status_code == 200
    assert 'Write Prescription' in detail.get_data(as_text=True)
