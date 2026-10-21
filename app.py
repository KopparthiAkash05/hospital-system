import os
import sys
import pymysql
from markupsafe import Markup
pymysql.install_as_MySQLdb()

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g, send_file
import MySQLdb.cursors
from symptom_checker import analyze_symptoms, suggest_department
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone, date, time as dt_time  # ← MUST HAVE timezone
from decimal import Decimal
import re
import secrets  # ← MUST HAVE secrets
import math
import requests # Make sure this is imported at the very top of your app.py
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Local config lives in the gitignored `details.env`; production injects real
# environment variables. Never bake credentials into source as a fallback.
load_dotenv()
load_dotenv('details.env')

# Hospital local time (IST, UTC +05:30). Every slot listing, availability
# check and booking validation compares against this one timezone.
from zoneinfo import ZoneInfo

HOSPITAL_TZ = ZoneInfo('Asia/Kolkata')


def local_now():
    """Current wall-clock time in the hospital's configured timezone."""
    return datetime.now(HOSPITAL_TZ)


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Set it in the environment or in the project 'details.env' file."
        )
    return value


# ========== FLASK APP CONFIGURATION ==========
app = Flask(__name__)

# Secret key for session management
app.secret_key = _required_env('SECRET_KEY')

# MySQL configuration
app.config['MYSQL_HOST'] = _required_env('MYSQL_HOST')
app.config['MYSQL_USER'] = _required_env('MYSQL_USER')
app.config['MYSQL_PASSWORD'] = _required_env('MYSQL_PASSWORD')
app.config['MYSQL_DB'] = _required_env('MYSQL_DB')

# Custom MySQL wrapper to avoid mysqlclient C-extension issues on Render
class MySQL:
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)
            
    def init_app(self, app):
        self.app = app
        
    @property
    def connection(self):
        # Reuses the same connection for the duration of the web request
        if 'mysql_db' not in g:
            g.mysql_db = pymysql.connect(
                host=self.app.config['MYSQL_HOST'],
                user=self.app.config['MYSQL_USER'],
                password=self.app.config['MYSQL_PASSWORD'],
                database=self.app.config['MYSQL_DB'],
                cursorclass=MySQLdb.cursors.DictCursor
            )
        return g.mysql_db

mysql = MySQL(app)

# Automatically close the database connection when the web request ends
@app.teardown_appcontext
def close_connection(exception):
    db = g.pop('mysql_db', None)
    if db is not None:
        db.close()

# Email is sent exclusively through the Brevo HTTP API helper
# (send_email_via_sendgrid). The previous Flask-Mail / Gmail SMTP setup was
# dead code and has been removed.

# Single scheduler handle; populated by start_scheduler() near the end of file.
scheduler = None

# Windows consoles default to cp1252 and cannot encode the emoji used in log
# messages. Without this the scheduler jobs (and some routes) die with
# UnicodeEncodeError, so reminders are never sent.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Add these helper functions near the top of your app.py


def get_coordinates_from_address(address):
    """Fetches latitude and longitude from an address using Nominatim API."""
    try:
        if not address:
            return None, None
            
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': address, 
            'format': 'json', 
            'limit': 1
        }
        # Nominatim requires a custom User-Agent to prevent blocking
        headers = {
            'User-Agent': 'MediCareHospitalApp/1.0 (your_email@example.com)' 
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()
        
        if data:
            lat = float(data[0]['lat'])
            lon = float(data[0]['lon'])
            print(f"✅ Geocoded '{address}' to Lat: {lat}, Lon: {lon}")
            return lat, lon
        else:
            print(f"⚠️ Could not find coordinates for: {address}")
            return None, None
            
    except Exception as e:
        print(f"❌ Geocoding API error: {e}")
        return None, None

def convert_12hr_to_24hr(time_str):
    """
    Converts a 12-hour time such as "10AM", "5PM", "9:30PM" to 24-hour format
    ("10:00", "17:00", "21:30"). Returns None when the input is not a valid time
    so callers can skip it instead of silently booking the wrong slot.
    """
    text = (time_str or '').strip().upper()
    if not text:
        return None

    minute = '00'
    match = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$', text)
    if not match:
        print(f"Error converting time {time_str}: unrecognised format")
        return None

    hour = int(match.group(1))
    minute = match.group(2) or '00'
    meridiem = match.group(3)

    if meridiem == 'AM':
        if hour == 12:
            hour = 0
        elif not 1 <= hour <= 12:
            print(f"Error converting time {time_str}: hour out of range for AM")
            return None
    elif meridiem == 'PM':
        if hour == 12:
            hour = 12
        elif 1 <= hour <= 12:
            hour += 12
        else:
            print(f"Error converting time {time_str}: hour out of range for PM")
            return None
    elif not 0 <= hour <= 23:
        print(f"Error converting time {time_str}: hour out of range")
        return None

    if not (0 <= int(minute) <= 59):
        print(f"Error converting time {time_str}: minute out of range")
        return None

    return f"{hour:02d}:{minute}"

# Weekday tokens accepted in the free-text availability field. Values are
# ISO weekday numbers so they line up with datetime.date().isoweekday().
SLOT_DAY_TOKENS = {
    'mon': 1, 'monday': 1,
    'tue': 2, 'tues': 2, 'tuesday': 2,
    'wed': 3, 'weds': 3, 'wednesday': 3,
    'thu': 4, 'thur': 4, 'thurs': 4, 'thursday': 4,
    'fri': 5, 'friday': 5,
    'sat': 6, 'saturday': 6,
    'sun': 7, 'sunday': 7,
}


def _days_from_spec(day_spec):
    """
    Turns a day specification into ISO weekday numbers.

    Supports "Mon", "Mon-Fri", "Fri-Mon" (wraps the week) and
    "Mon, Wed, Fri". Returns [] when nothing recognisable is present and
    None when the specification was left blank.
    """
    spec = (day_spec or '').strip()
    if not spec:
        return None

    days = set()
    for chunk in spec.split(','):
        chunk = chunk.strip().lower()
        if not chunk:
            continue

        if '-' in chunk:
            start_raw, _, end_raw = chunk.partition('-')
            start_day = SLOT_DAY_TOKENS.get(start_raw.strip())
            end_day = SLOT_DAY_TOKENS.get(end_raw.strip())
            if not start_day or not end_day:
                return []
            if start_day <= end_day:
                days.update(range(start_day, end_day + 1))
            else:
                # Wrapped range such as "Fri-Mon" spans the weekend.
                days.update(range(start_day, 8))
                days.update(range(1, end_day + 1))
            continue

        day = SLOT_DAY_TOKENS.get(chunk)
        if not day:
            return []
        days.add(day)

    return sorted(days)


def _time_window(time_part):
    """'9AM-5PM' -> ('09:00', '17:00'); returns None when not a usable range."""
    if '-' not in time_part:
        return None
    start_raw, _, end_raw = time_part.partition('-')
    start = convert_12hr_to_24hr(start_raw)
    end = convert_12hr_to_24hr(end_raw)
    if not start or not end:
        return None
    return start, end


def parse_doctor_slots(slots_str, date_str=None):
    """
    Parses a doctor's free-text availability, for example:
    - "Mon-Fri: 10AM-5PM"
    - "Mon, Wed, Fri: 9AM-12PM"
    - "Mon-Fri: 9AM-5PM, Sat: 10AM-4PM"
    - "10:00, 11:00, 14:00"
    Returns a sorted list of 30-minute slot strings in 24-hour format.

    When date_str ('YYYY-MM-DD') is supplied only the entries whose day names
    match that weekday are used, so a Saturday-only window is never offered on
    Sunday and a weekday window does not leak into the weekend.
    """
    if not slots_str or not str(slots_str).strip():
        # Default: 9 AM to 5 PM
        return generate_30min_slots("09:00", "17:00")

    target_day = None
    if date_str:
        try:
            target_day = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date().isoweekday()
        except (ValueError, TypeError):
            target_day = None

    windows = []      # (days_or_None, start, end)
    plain_times = []  # day-agnostic explicit times
    saw_day_scope = False

    text = str(slots_str).replace('\r\n', '\n').replace('\r', '\n')
    for line in text.split('\n'):
        line = line.strip().strip(',').strip()
        if not line:
            continue

        # Day-scoped window, e.g. "Mon-Fri: 9AM-5PM".
        if ':' in line and ('am' in line.lower() or 'pm' in line.lower()):
            day_spec, _, time_part = line.partition(':')
            window = _time_window(time_part)
            if not window:
                continue
            days = _days_from_spec(day_spec)
            if days is None:
                # No day given: applies to every day.
                windows.append((None, window[0], window[1]))
                continue
            if not days:
                # Unrecognised day name: ignore instead of applying to all days.
                print(f"Skipping availability line with unknown day: {line}")
                continue
            saw_day_scope = True
            windows.append((days, window[0], window[1]))
            continue

        # Plain list of times such as "10:00, 11:00, 14:00".
        for token in line.split(','):
            token = token.strip()
            if not token:
                continue
            value24 = convert_12hr_to_24hr(token)
            if value24:
                plain_times.append(value24)

    if windows:
        slots = []
        for days, start, end in windows:
            if target_day is not None and days is not None and target_day not in days:
                continue
            slots.extend(generate_30min_slots(start, end))
        if slots:
            return sorted(set(slots))
        # Day names were present and none matched: the doctor is off that day.
        if saw_day_scope:
            return []

    if plain_times:
        return sorted(set(plain_times))

    # Default fallback
    return generate_30min_slots("09:00", "17:00")


def generate_30min_slots(start_str, end_str):
    """
    Generates 30-minute slot strings for [start_str, end_str).

    The end time is the point at which availability stops, so it is not itself
    offered: a 9AM-5PM window ends at 16:30.
    """
    try:
        start = datetime.strptime(str(start_str).strip(), '%H:%M')
        end = datetime.strptime(str(end_str).strip(), '%H:%M')
        slots = []
        current = start
        while current < end:
            slots.append(current.strftime('%H:%M'))
            current += timedelta(minutes=30)
        return slots
    except Exception as e:
        print(f"Error generating slots: {e}")
        # Fallback default slots
        return ['09:00', '09:30', '10:00', '10:30', '11:00', '11:30', '12:00', 
                '12:30', '13:00', '13:30', '14:00', '14:30', '15:00', '15:30', 
                '16:00', '16:30']


def filter_future_slots(slots, date_str, now=None):
    """
    Drop slots that have already elapsed when date_str is today.

    Slots are 'HH:MM' strings; a slot stays bookable only while its start
    time is still strictly in the future. Future dates pass through
    unchanged so only the selected day's clock is ever compared.
    """
    if now is None:
        now = local_now()
    try:
        selected = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return list(slots)
    if selected != now.date():
        return list(slots)
    cutoff = now.strftime('%H:%M')
    return [s for s in slots if s > cutoff]

# ========== WEB DECORATORS ==========
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session:
            flash('Please login first!', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session or session.get('user_type') != 'admin':
            flash('Admin access required!', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def doctor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session or session.get('user_type') != 'doctor':
            flash('Doctor access required!', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def patient_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session or session.get('user_type') != 'patient':
            flash('Patient access required!', 'danger')
            # Remember the page the patient was trying to open (for example the
            # feedback link in a Brevo email) so login can send them straight
            # there instead of dropping them on the dashboard.
            if 'loggedin' not in session and request.path.startswith('/patient/'):
                target = request.full_path[:-1] if request.full_path.endswith('?') else request.full_path
                if target.startswith('/') and not target.startswith('//'):
                    session['next_url'] = target
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ========== API DECORATORS ==========
def api_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session:
            return jsonify({'success': False, 'message': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function

def api_patient_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session:
            return jsonify({'success': False, 'message': 'Authentication required'}), 401
        if session.get('user_type') != 'patient':
            return jsonify({'success': False, 'message': 'Patient access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def api_doctor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session:
            return jsonify({'success': False, 'message': 'Authentication required'}), 401
        if session.get('user_type') != 'doctor':
            return jsonify({'success': False, 'message': 'Doctor access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def api_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session:
            return jsonify({'success': False, 'message': 'Authentication required'}), 401
        if session.get('user_type') != 'admin':
            return jsonify({'success': False, 'message': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ========== SMART HOSPITAL HELPERS ==========
# Notification / audit / schedule helpers used by the routes below.
# Every helper is defensive: if the new tables have not been created yet the
# feature degrades quietly instead of breaking the pages that call it.

SCHEDULE_DAY_NAMES = {
    1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday',
    5: 'Friday', 6: 'Saturday', 7: 'Sunday'
}

# The status stored in the DB is 'Approved'; the notification copy uses the
# spec's wording ("confirmed") so the two never drift apart.
NOTIFICATION_EVENTS = {
    'booked': 'Appointment Booked',
    'approved': 'Appointment Confirmed',
    'rejected': 'Appointment Rejected',
    'cancelled': 'Appointment Cancelled',
    'completed': 'Appointment Completed',
    'rescheduled': 'Appointment Rescheduled',
}


def log_audit(action, entity=None, entity_id=None, description=None):
    """Writes one row to AuditLog. Never raises: auditing must not block a request."""
    try:
        cursor = mysql.connection.cursor()
        cursor.execute('''
            INSERT INTO AuditLog (user_type, user_id, user_name, action, entity, entity_id, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (
            session.get('user_type'),
            session.get('id'),
            session.get('name'),
            action,
            entity,
            str(entity_id) if entity_id is not None else None,
            description
        ))
        mysql.connection.commit()
        cursor.close()
    except Exception as e:
        print(f"Audit log skipped: {e}")


def create_notification(user_type, user_id, title, message, appointment_id=None):
    """Creates one in-app notification. Never raises."""
    if not user_id:
        return
    try:
        cursor = mysql.connection.cursor()
        cursor.execute('''
            INSERT INTO Notification (user_type, user_id, title, message, appointment_id)
            VALUES (%s, %s, %s, %s, %s)
        ''', (user_type, user_id, title, message, appointment_id))
        mysql.connection.commit()
        cursor.close()
    except Exception as e:
        print(f"Notification skipped: {e}")


def notify_appointment_event(appointment_id, event, extra=''):
    """Fans one appointment event out to both the patient and the doctor."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute('''
            SELECT a.appointment_id, a.patient_id, a.doctor_id,
                   a.date, a.time, a.reason,
                   p.name AS patient_name, d.name AS doctor_name
            FROM Appointment a
            LEFT JOIN Patient p ON a.patient_id = p.patient_id
            LEFT JOIN Doctor  d ON a.doctor_id  = d.doctor_id
            WHERE a.appointment_id = %s
        ''', (appointment_id,))
        appt = cursor.fetchone()
    except Exception as e:
        print(f"Notification lookup failed: {e}")
        appt = None
    finally:
        cursor.close()

    if not appt:
        return

    title = NOTIFICATION_EVENTS.get(event, 'Appointment Update')
    when = f"{appt['date']} at {get_12hr_time(appt['time'])}"

    create_notification(
        'patient', appt['patient_id'], title,
        f"{extra}Your appointment with Dr. {appt['doctor_name']} is scheduled for {when}.".strip(),
        appt['appointment_id']
    )
    create_notification(
        'doctor', appt['doctor_id'], title,
        f"{extra}Appointment with {appt['patient_name']} on {when}.".strip(),
        appt['appointment_id']
    )


def unread_notification_count(user_type, user_id):
    try:
        cursor = mysql.connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) AS total FROM Notification WHERE user_type = %s AND user_id = %s AND is_read = 0',
            (user_type, user_id)
        )
        return cursor.fetchone()['total']
    except Exception as e:
        print(f"Unread count unavailable: {e}")
        return 0
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def get_departments(active_only=True):
    """Departments ordered by name. Returns [] if the table is missing."""
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        sql = 'SELECT * FROM Department'
        if active_only:
            sql += ' WHERE is_active = 1'
        sql += ' ORDER BY name'
        cursor.execute(sql)
        departments = cursor.fetchall()
        cursor.close()
        return departments
    except Exception as e:
        print(f"Departments unavailable: {e}")
        return []


# Check-up information shown on the patient dashboard for each department.
# Keys are matched against the department name, so a department added later
# without a description still gets a sensible starting point.
DEPARTMENT_CHECKUPS = {
    'allergy & immunology': 'Allergy panel, skin prick test and IgE screening',
    'infectious disease': 'Infection screening, blood culture and swab tests',
    'emergency medicine': 'Triage vitals, ECG, blood work and emergency imaging',
    'general medicine': 'Blood pressure, blood sugar and routine blood work',
    'plastic surgery': 'Consultation, imaging and surgical planning review',
    'gastroenterology': 'Endoscopy, liver function test and stool analysis',
    'hematology': 'Complete blood count, iron studies and clotting profile',
    'immunology': 'Allergy panel, skin prick test and IgE screening',
    'ophthalmology': 'Eye pressure, visual acuity and retinal screening',
    'orthopedics': 'X-ray, joint imaging and bone density scan',
    'pulmonology': 'Chest X-ray, spirometry and oxygen saturation test',
    'rheumatology': 'Autoimmune panel, arthritis profile and joint imaging',
    'hepatology': 'Liver function test, hepatitis panel and abdominal ultrasound',
    'endocrinology': 'Thyroid panel, HbA1c, blood sugar and hormone tests',
    'dermatology': 'Skin examination, patch testing and dermoscopy',
    'psychiatry': 'Mental health assessment, mood screening and sleep review',
    'nephrology': 'Kidney function test, urine analysis and protein panel',
    'cardiology': 'ECG, echocardiogram, blood pressure and lipid profile',
    'gynecology': 'Ultrasound, Pap smear and hormone profile',
    'neurology': 'EEG, nerve conduction study and brain imaging',
    'oncology': 'Tumour markers, biopsy review and staging scans',
    'pediatrics': 'Growth chart, vaccination review and blood work',
    'radiology': 'X-ray, ultrasound and scan report review',
    'vascular': 'Doppler study, blood pressure and circulation tests',
    'dentistry': 'Dental examination, bite X-ray and scaling',
    'urology': 'Urinalysis, ultrasound and prostate screening',
    'emergency': 'Triage vitals, ECG, blood work and emergency imaging',
    'dental': 'Dental examination, bite X-ray and scaling',
    'allergy': 'Skin prick test, patch testing and allergy blood panel',
    'ent': 'Hearing test, nasal endoscopy and throat examination',
}

# Longest key first: 'neurology' contains 'urology' and 'gastroenterology'
# contains 'ent', so a specific name must be checked before a shorter one.
DEPARTMENT_CHECKUP_LOOKUP = sorted(
    DEPARTMENT_CHECKUPS.items(), key=lambda item: len(item[0]), reverse=True
)


def department_checkup(department):
    """Check-up information for a department card.

    Falls back to a generic line when the department is not one we know about.
    """
    if not department:
        return 'Routine check-up and consultation'

    name = (department.get('name') or '').strip().lower()
    for key, checkup in DEPARTMENT_CHECKUP_LOOKUP:
        if name == key or key in name:
            return checkup
    return 'Routine check-up and consultation'


HOSPITAL_INFO_DEFAULTS = {
    'info_id': None,
    'name': 'MediCare Hospital',
    'tagline': 'Providing quality healthcare services with modern technology.',
    'address': '123 Health Street, Medical City',
    'phone': '+1 234 567 890',
    'emergency_phone': '+1 234 567 911',
    'email': 'info@smarthospital.com',
    'opening_hours': 'Mon - Sat: 8:00 AM - 8:00 PM',
    'about': None,
}


def get_hospital_info():
    """The single HospitalInfo row, or safe defaults if the table is missing."""
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM HospitalInfo ORDER BY info_id LIMIT 1')
        row = cursor.fetchone()
        cursor.close()
        if row:
            return dict(row)
    except Exception as e:
        print(f"Hospital info unavailable: {e}")
    return dict(HOSPITAL_INFO_DEFAULTS)


def get_doctor_schedules(doctor_id):
    """Structured working windows for a doctor (empty when none are defined)."""
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('''
            SELECT * FROM DoctorSchedule
            WHERE doctor_id = %s AND is_active = 1
            ORDER BY day_of_week, start_time
        ''', (doctor_id,))
        schedules = cursor.fetchall()
        cursor.close()
        return schedules
    except Exception as e:
        print(f"Schedules unavailable: {e}")
        return []


def slots_from_range(start_str, end_str, duration_minutes=30):
    """
    Expands a start/end window into discrete 'HH:MM' slot strings.

    end_str is the time availability stops, so it is not offered itself.
    """
    try:
        duration = int(duration_minutes or 30)
    except (TypeError, ValueError):
        duration = 30
    if duration < 5:
        duration = 5

    try:
        start = datetime.strptime(str(start_str)[:5], '%H:%M')
        end = datetime.strptime(str(end_str)[:5], '%H:%M')
    except (ValueError, TypeError):
        return []

    slots = []
    current = start
    while current < end:
        slots.append(current.strftime('%H:%M'))
        current += timedelta(minutes=duration)
    return slots


def get_slots_for_date(doctor, date_str):
    """
    Slots a doctor can accept on the given date.

    Prefers the structured DoctorSchedule rows for that weekday; falls back to
    the existing free-text available_slots field so doctors who have not set up
    structured days keep exactly the slots they had before.
    """
    schedules = get_doctor_schedules(doctor['doctor_id'])

    if schedules:
        try:
            target_day = datetime.strptime(date_str, '%Y-%m-%d').date().isoweekday()
        except (ValueError, TypeError):
            target_day = None

        slots = []
        for row in schedules:
            if target_day is not None and row['day_of_week'] != target_day:
                continue
            slots.extend(slots_from_range(
                row['start_time'], row['end_time'], row['slot_duration']
            ))

        if slots:
            # De-duplicate while keeping chronological order.
            return sorted(set(slots))

    return parse_doctor_slots(doctor.get('available_slots'), date_str)


def describe_schedule(doctor_id):
    """Human readable summary of a doctor's structured working days."""
    schedules = get_doctor_schedules(doctor_id)
    if not schedules:
        return None

    grouped = {}
    for row in schedules:
        key = row['day_of_week']
        grouped.setdefault(key, []).append(row)

    parts = []
    for day in sorted(grouped):
        windows = ', '.join(
            f"{get_12hr_time(r['start_time'])} - {get_12hr_time(r['end_time'])}"
            for r in grouped[day]
        )
        parts.append(f"{SCHEDULE_DAY_NAMES.get(day, 'Day')} {day}: {windows}")
    return '; '.join(parts)


# ========== SCHEDULE BASED AVAILABILITY ENGINE ==========
# The patient facing "available / unavailable" state is derived from the
# doctor's weekly working windows (DoctorSchedule) evaluated against the
# hospital's local time (IST). The stored Doctor.availability_status stays a
# manual switch: this computation never writes it back to the database.

SCHEDULE_DAY_ABBR = {
    1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat', 7: 'Sun'
}


def schedule_time_to_minutes(value):
    """Minutes past midnight for a TIME value.

    Accepts the datetime.timedelta values PyMySQL returns for TIME columns,
    datetime.time objects and 'HH:MM' / 'HH:MM:SS' strings.
    """
    if value is None:
        return None

    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        return (total // 3600) * 60 + (total % 3600) // 60

    if isinstance(value, dt_time):
        return value.hour * 60 + value.minute

    parts = str(value).strip().split(':')
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    return (hours % 24) * 60 + minutes


def minutes_to_12hr(minutes):
    """Minutes past midnight as '09:30 AM'."""
    if minutes is None:
        return ''
    minutes = int(minutes) % (24 * 60)
    hours, mins = divmod(minutes, 60)
    hour_12 = hours % 12 or 12
    return f"{hour_12:02d}:{mins:02d} {'AM' if hours < 12 else 'PM'}"


def schedule_windows(schedules):
    """Working windows as [(iso_day, start_minutes, end_minutes)].

    A window whose end is not after its start is a night shift: the end is
    pushed to the next day so the end minute is always greater than the start.
    """
    windows = []
    for row in schedules or []:
        if not row.get('is_active', 1):
            continue
        day = row.get('day_of_week')
        start = schedule_time_to_minutes(row.get('start_time'))
        end = schedule_time_to_minutes(row.get('end_time'))
        if day is None or start is None or end is None:
            continue
        if end <= start:
            end += 24 * 60
        windows.append((int(day), start, end))
    return windows


def _windows_for_day(windows, iso_day):
    return [(start, end) for (day, start, end) in windows if day == iso_day]


def _as_hospital_time(moment):
    """Normalises a datetime to the hospital's timezone (IST)."""
    if moment is None:
        return local_now()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=HOSPITAL_TZ)
    return moment.astimezone(HOSPITAL_TZ)


def schedule_covers_moment(windows, moment):
    """True when `moment` falls inside one of the working windows.

    The start minute is inclusive and the end minute is exclusive, so a
    09:00 - 17:00 window still counts as available at 16:59 and not at 17:00.
    """
    if not windows:
        return False

    moment = _as_hospital_time(moment)
    iso_day = moment.isoweekday()
    minutes = moment.hour * 60 + moment.minute

    for start, end in _windows_for_day(windows, iso_day):
        if end > 24 * 60:
            # Night shift starting today: only the part before midnight is today.
            if start <= minutes < 24 * 60:
                return True
        elif start <= minutes < end:
            return True

    # A night shift that started yesterday is still running after midnight.
    previous_day = iso_day - 1 if iso_day > 1 else 7
    for start, end in _windows_for_day(windows, previous_day):
        if end > 24 * 60 and minutes < end - 24 * 60:
            return True

    return False


def next_schedule_start(windows, moment):
    """Next datetime (hospital time) a working window opens, or None."""
    if not windows:
        return None

    moment = _as_hospital_time(moment)
    for offset in range(0, 8):
        for iso_day, start, end in windows:
            if iso_day != (moment + timedelta(days=offset)).isoweekday():
                continue
            start_dt = moment.replace(
                hour=start // 60, minute=start % 60, second=0, microsecond=0
            ) + timedelta(days=offset)
            if start_dt <= moment:
                continue
            return start_dt
    return None


def current_schedule_end(windows, moment):
    """End datetime of the window that covers `moment`, or None."""
    if not windows:
        return None

    moment = _as_hospital_time(moment)
    iso_day = moment.isoweekday()
    minutes = moment.hour * 60 + moment.minute

    for start, end in _windows_for_day(windows, iso_day):
        if end > 24 * 60:
            if start <= minutes < 24 * 60:
                end_minutes = end - 24 * 60
                return moment.replace(
                    hour=end_minutes // 60, minute=end_minutes % 60,
                    second=0, microsecond=0
                ) + timedelta(days=1)
        elif start <= minutes < end:
            return moment.replace(
                hour=end // 60, minute=end % 60, second=0, microsecond=0
            )

    previous_day = iso_day - 1 if iso_day > 1 else 7
    for start, end in _windows_for_day(windows, previous_day):
        if end > 24 * 60 and minutes < end - 24 * 60:
            end_minutes = end - 24 * 60
            return moment.replace(
                hour=end_minutes // 60, minute=end_minutes % 60,
                second=0, microsecond=0
            )

    return None


def _format_day_range(days):
    """'Mon-Sat' for consecutive days, 'Mon, Wed, Fri' otherwise."""
    unique = sorted(set(days))
    if not unique:
        return ''
    if len(unique) == 1:
        return SCHEDULE_DAY_ABBR.get(unique[0], str(unique[0]))
    if unique == list(range(unique[0], unique[0] + len(unique))):
        return (f"{SCHEDULE_DAY_ABBR.get(unique[0], unique[0])}-"
                f"{SCHEDULE_DAY_ABBR.get(unique[-1], unique[-1])}")
    return ', '.join(SCHEDULE_DAY_ABBR.get(day, str(day)) for day in unique)


def format_working_hours(schedules):
    """Compact summary of a doctor's week, e.g. 'Mon-Sat: 09:00 AM - 05:00 PM'."""
    windows = schedule_windows(schedules)
    if not windows:
        return ''

    grouped = {}
    for iso_day, start, end in windows:
        grouped.setdefault((start, end), []).append(iso_day)

    parts = []
    for (start, end), days in sorted(grouped.items(), key=lambda item: min(item[1])):
        parts.append(
            f"{_format_day_range(days)}: "
            f"{minutes_to_12hr(start)} - {minutes_to_12hr(end)}"
        )
    return '; '.join(parts)


def get_schedules_for_doctors(doctor_ids):
    """{doctor_id: [schedule rows]} for many doctors in a single query."""
    ids = [int(doctor_id) for doctor_id in (doctor_ids or []) if doctor_id]
    if not ids:
        return {}

    placeholders = ', '.join(['%s'] * len(ids))
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute(f'''
            SELECT * FROM DoctorSchedule
            WHERE doctor_id IN ({placeholders}) AND is_active = 1
            ORDER BY doctor_id, day_of_week, start_time
        ''', tuple(ids))
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"Schedules unavailable: {e}")
        return {}

    grouped = {}
    for row in rows:
        grouped.setdefault(row['doctor_id'], []).append(row)
    return grouped


def doctor_availability(doctor, schedules, now=None):
    """Effective patient facing availability for a doctor.

    Returns a dict with:
      status   - 'Available' or 'Unavailable' (the stored enum value)
      label    - 'Available' or 'Not Available' (what the UI shows)
      reason   - short explanation shown next to the status
      until    - '05:00 PM' when the running shift ends today
      next     - 'Mon 09:00 AM' when the next shift starts
      on_shift - True / False, or None when the doctor has no schedule rows
      source   - 'schedule' | 'manual' | 'inactive'
    """
    moment = _as_hospital_time(now or local_now())

    if not doctor:
        return {'status': 'Unavailable', 'label': 'Not Available',
                'reason': 'Doctor not found.',
                'until': None, 'next': None, 'on_shift': False, 'source': 'inactive'}

    if not doctor.get('is_active', 1):
        return {'status': 'Unavailable', 'label': 'Not Available',
                'reason': 'Not accepting appointments right now.',
                'until': None, 'next': None, 'on_shift': False, 'source': 'inactive'}

    if (doctor.get('availability_status') or 'Available') == 'Unavailable':
        return {'status': 'Unavailable', 'label': 'Not Available',
                'reason': 'Marked unavailable by the doctor.',
                'until': None, 'next': None, 'on_shift': False, 'source': 'manual'}

    windows = schedule_windows(schedules)
    if not windows:
        # No structured week on file: fall back to the manual switch.
        return {'status': 'Available', 'label': 'Available',
                'reason': 'Available for appointments.',
                'until': None, 'next': None, 'on_shift': None, 'source': 'manual'}

    if schedule_covers_moment(windows, moment):
        end_dt = current_schedule_end(windows, moment)
        return {'status': 'Available', 'label': 'Available', 'reason': 'Available now',
                'until': minutes_to_12hr(end_dt.hour * 60 + end_dt.minute) if end_dt else None,
                'next': None, 'on_shift': True, 'source': 'schedule'}

    next_dt = next_schedule_start(windows, moment)
    next_label = None
    if next_dt:
        next_label = (f"{next_dt.strftime('%a')} "
                      f"{minutes_to_12hr(next_dt.hour * 60 + next_dt.minute)}")

    return {'status': 'Unavailable', 'label': 'Not Available',
            'reason': 'Not working right now',
            'until': None, 'next': next_label, 'on_shift': False, 'source': 'schedule'}


def attach_availability(doctors, now=None):
    """Adds 'availability' and 'working_hours' to each doctor row."""
    if not doctors:
        return doctors

    moment = now or local_now()
    schedules = get_schedules_for_doctors(
        [doctor.get('doctor_id') for doctor in doctors]
    )

    for doctor in doctors:
        rows = schedules.get(doctor.get('doctor_id'), [])
        doctor['availability'] = doctor_availability(doctor, rows, moment)
        doctor['working_hours'] = format_working_hours(rows)
    return doctors


def appointment_is_active(status):
    return status in ('Pending', 'Approved')


def slot_is_free(cursor, doctor_id, date_str, time_str, exclude_id=None):
    """
    True when no live appointment already holds this slot.
    Reuses the existing TIME() comparison so 'HH:MM' and 'HH:MM:SS' match.
    """
    sql = '''
        SELECT appointment_id FROM Appointment
        WHERE doctor_id = %s AND date = %s AND TIME(time) = TIME(%s)
          AND status IN ('Pending', 'Approved')
    '''
    params = [doctor_id, date_str, time_str]
    if exclude_id:
        sql += ' AND appointment_id <> %s'
        params.append(exclude_id)

    cursor.execute(sql, tuple(params))
    return cursor.fetchone() is None


def doctor_is_bookable(doctor):
    """Doctors flagged unavailable (or deactivated) cannot receive bookings."""
    if not doctor:
        return False, 'Doctor not found.'
    if not doctor.get('is_active', 1):
        return False, 'This doctor is no longer accepting appointments.'
    if doctor.get('availability_status') == 'Unavailable':
        return False, 'This doctor is currently unavailable for appointments.'
    return True, ''


# Must stay in sync with the Appointment.status ENUM in MySQL, which also
# allows 'No-Show'; otherwise those appointments disappear from filters and
# status counts.
APPOINTMENT_STATUSES = ('Pending', 'Approved', 'Completed', 'Cancelled',
                        'Rejected', 'No-Show')


def json_safe_row(row):
    """
    Make a DB row safe for jsonify.

    PyMySQL returns TIME columns as datetime.timedelta, which Flask's JSON
    encoder cannot serialise, so any endpoint returning them raises a 500.
    """
    if row is None:
        return None
    safe = {}
    for key, value in row.items():
        if isinstance(value, timedelta):
            total = int(value.total_seconds())
            safe[key] = '%02d:%02d:%02d' % (total // 3600, (total % 3600) // 60, total % 60)
        elif isinstance(value, (datetime,)):
            safe[key] = value.isoformat()
        elif isinstance(value, date):
            safe[key] = value.isoformat()
        elif isinstance(value, (bytes, bytearray)):
            safe[key] = value.decode('utf-8', 'replace')
        elif isinstance(value, Decimal):
            safe[key] = float(value)
        else:
            safe[key] = value
    return safe


def appointment_status_counts(where_sql='', params=()):
    """{'Pending': n, 'Approved': n, ...} for the given WHERE clause."""
    counts = {status: 0 for status in APPOINTMENT_STATUSES}

    try:
        cursor = mysql.connection.cursor()
        sql = 'SELECT status, COUNT(*) AS total FROM Appointment'
        if where_sql:
            sql += ' WHERE ' + where_sql
        sql += ' GROUP BY status'
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            counts[row['status']] = row['total']
        cursor.close()
    except Exception as e:
        print(f"Status counts unavailable: {e}")
    return counts

# ========== HOME ROUTE ==========
@app.route('/')
def index():
    return render_template('index.html', hospital=get_hospital_info())

# ========== AUTHENTICATION ROUTES ==========
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Patient login. Doctors and administrators must use their own login pages."""
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        # Only patients may authenticate on the patient login page.
        cursor.execute('SELECT * FROM Patient WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            if not account.get('is_active', 1):
                cursor.close()
                flash('Your account has been deactivated. Please contact the hospital.', 'danger')
                return render_template('login.html')
            session['loggedin'] = True
            session['id'] = account['patient_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'patient'
            flash('Login successful!', 'success')
            cursor.close()
            # Return the patient to the page they originally requested (for
            # example a feedback link from an email) when it is a safe
            # same-site path; otherwise fall back to the dashboard.
            next_url = session.pop('next_url', '')
            if next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
            return redirect(url_for('patient_dashboard'))
        
        # Credentials are valid for another role: send them to the right page.
        cursor.execute('SELECT password FROM Doctor WHERE email = %s', (email,))
        doctor = cursor.fetchone()
        if doctor and check_password_hash(doctor['password'], password):
            cursor.close()
            flash('Doctors must log in using the Doctor Login page.', 'warning')
            return render_template('login.html')
        
        cursor.execute('SELECT password FROM Admin WHERE email = %s', (email,))
        admin = cursor.fetchone()
        if admin and check_password_hash(admin['password'], password):
            cursor.close()
            flash('Administrators must log in using the Admin Login page.', 'warning')
            return render_template('login.html')
        
        # If no match found
        flash('Invalid email or password!', 'danger')
        cursor.close()
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        phone = request.form['phone']
        age = request.form['age']
        gender = request.form['gender']
        address = request.form.get('address', '')
        date_of_birth = request.form.get('date_of_birth', '') or None
        emergency_contact_name = request.form.get('emergency_contact_name', '') or None
        emergency_contact_phone = request.form.get('emergency_contact_phone', '') or None
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Patient WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account:
            flash('Account already exists!', 'warning')
        elif not re.match(r'[^@]+@[^@]+\.[^@]+', email):
            flash('Invalid email address!', 'warning')
        elif not password:
            flash('Password is required!', 'warning')
        else:
            hashed_password = generate_password_hash(password)
            cursor.execute(
                '''
                INSERT INTO Patient
                    (name, email, password, phone, age, gender, address,
                     date_of_birth, emergency_contact_name, emergency_contact_phone)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (name, email, hashed_password, phone, age, gender, address,
                 date_of_birth, emergency_contact_name, emergency_contact_phone)
            )
            mysql.connection.commit()
            cursor.close()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        
        cursor.close()
    
    return render_template('register.html')

@app.route('/doctor/register', methods=['GET', 'POST'])
def doctor_register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        phone = request.form['phone']
        specialization = request.form['specialization']
        experience = request.form['experience']
        available_slots = request.form['available_slots']
        address = request.form.get('address', '')
        department_id = request.form.get('department_id', '') or None
        qualification = request.form.get('qualification', '').strip()
        license_number = request.form.get('license_number', '').strip()
        consultation_fee = request.form.get('consultation_fee', '').strip()
        profile_description = request.form.get('profile_description', '').strip()

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account:
            flash('Doctor account already exists!', 'warning')
        elif not re.match(r'[^@]+@[^@]+\.[^@]+', email):
            flash('Invalid email address!', 'warning')
        else:
            hashed_password = generate_password_hash(password)
            
            # 🌍 AUTOMATIC GEOCODING
            latitude, longitude = get_coordinates_from_address(address)
            
            # Insert doctor with address and coordinates
            cursor.execute('''
                INSERT INTO Doctor
                    (name, email, password, phone, specialization, experience,
                     available_slots, address, latitude, longitude, department_id,
                     qualification, license_number, consultation_fee,
                     profile_description, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            ''', (name, email, hashed_password, phone, specialization, experience,
                  available_slots, address, latitude, longitude, department_id,
                  qualification or None, license_number or None,
                  consultation_fee or None, profile_description or None))
            
            mysql.connection.commit()
            new_id = cursor.lastrowid
            cursor.close()
            log_audit('REGISTER_DOCTOR', 'Doctor', new_id,
                      f"{name} registered as a doctor")
            flash('Doctor registration successful! Please login.', 'success')
            return redirect(url_for('doctor_login'))
        
        cursor.close()
    
    return render_template('doctor/register.html', departments=get_departments())
@app.route('/doctor/settings', methods=['GET', 'POST'])
@doctor_required
def doctor_settings():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (session['id'],))
    doctor = cursor.fetchone()

    if request.method == 'POST':
        # Get form data
        name = request.form['name']
        phone = request.form['phone']
        address = request.form.get('address', '')
        available_slots = request.form.get('available_slots', '')
        email = request.form.get('email', doctor['email']).strip()
        specialization = request.form.get('specialization', doctor['specialization'])
        qualification = request.form.get('qualification', '') or None
        experience = request.form.get('experience', doctor['experience'])
        license_number = request.form.get('license_number', '') or None
        consultation_fee = request.form.get('consultation_fee', '') or None
        profile_description = request.form.get('profile_description', '') or None
        department_id = request.form.get('department_id', '') or None
        availability_status = request.form.get('availability_status', 'Available')

        # A doctor may only pick an active department.
        if department_id:
            cursor.execute('SELECT department_id FROM Department WHERE department_id = %s AND is_active = 1',
                           (department_id,))
            if cursor.fetchone() is None:
                department_id = None

        # 🌍 AUTOMATIC GEOCODING (Fetch new coordinates if address changed)
        latitude, longitude = get_coordinates_from_address(address)
        
        # If geocoding fails but we already have old coordinates, keep the old ones
        if latitude is None and doctor['latitude'] is not None:
            latitude = doctor['latitude']
            longitude = doctor['longitude']

        # Update Profile Details in Database
        cursor.execute('''
            UPDATE Doctor 
            SET name=%s, phone=%s, email=%s, address=%s, latitude=%s, longitude=%s,
                available_slots=%s, specialization=%s, qualification=%s,
                experience=%s, license_number=%s, consultation_fee=%s,
                profile_description=%s, department_id=%s, availability_status=%s
            WHERE doctor_id=%s
        ''', (name, phone, email, address, latitude, longitude, available_slots,
              specialization, qualification, experience, license_number,
              consultation_fee, profile_description, department_id,
              availability_status, session['id']))
        mysql.connection.commit()
        
        # Update session variables so the navbar reflects the new name
        session['name'] = name
        session['email'] = email

        log_audit('UPDATE_DOCTOR_PROFILE', 'Doctor', session['id'],
                  'Doctor updated their own profile')

        flash('Profile and schedule updated successfully!', 'success')
        cursor.close()
        return redirect(url_for('doctor_settings'))

    cursor.close()
    return render_template('doctor/settings.html', doctor=doctor, departments=get_departments())

@app.route('/doctor/change_password', methods=['POST'])
@doctor_required
def doctor_change_password():
    """Doctors had no way to change their own password before."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT password FROM Doctor WHERE doctor_id = %s', (session['id'],))
    account = cursor.fetchone()
    cursor.close()

    old_password = request.form.get('old_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not account or not check_password_hash(account['password'], old_password):
        flash('Old password is incorrect!', 'danger')
        return redirect(url_for('doctor_settings'))

    if not new_password:
        flash('New password is required!', 'warning')
        return redirect(url_for('doctor_settings'))

    if new_password != confirm_password:
        flash('New passwords do not match!', 'warning')
        return redirect(url_for('doctor_settings'))

    if len(new_password) < 6:
        flash('Password must be at least 6 characters long!', 'warning')
        return redirect(url_for('doctor_settings'))

    cursor = mysql.connection.cursor()
    cursor.execute('UPDATE Doctor SET password = %s WHERE doctor_id = %s',
                   (generate_password_hash(new_password), session['id']))
    mysql.connection.commit()
    cursor.close()

    log_audit('CHANGE_PASSWORD', 'Doctor', session['id'], 'Doctor changed their password')
    flash('Password changed successfully!', 'success')
    return redirect(url_for('doctor_settings'))

@app.route('/doctor/login', methods=['GET', 'POST'])
def doctor_login():
    """Doctor login. Patients and administrators must use their own login pages."""
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        # Only doctors may authenticate on the doctor login page.
        cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            if not account.get('is_active', 1):
                cursor.close()
                flash('Your account has been deactivated. Please contact the hospital.', 'danger')
                return render_template('doctor/login.html')
            session['loggedin'] = True
            session['id'] = account['doctor_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'doctor'
            flash('Login successful!', 'success')
            cursor.close()
            return redirect(url_for('doctor_dashboard'))
        
        # Credentials are valid for another role: send them to the right page.
        cursor.execute('SELECT password FROM Patient WHERE email = %s', (email,))
        patient = cursor.fetchone()
        if patient and check_password_hash(patient['password'], password):
            cursor.close()
            flash('Patients must log in using the Patient Login page.', 'warning')
            return render_template('doctor/login.html')
        
        cursor.execute('SELECT password FROM Admin WHERE email = %s', (email,))
        admin = cursor.fetchone()
        if admin and check_password_hash(admin['password'], password):
            cursor.close()
            flash('Administrators must log in using the Admin Login page.', 'warning')
            return render_template('doctor/login.html')
        
        flash('Invalid email or password!', 'danger')
        cursor.close()
    
    return render_template('doctor/login.html')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Administrator login. Patients and doctors must use their own login pages."""
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        # Only administrators may authenticate on the admin login page.
        cursor.execute('SELECT * FROM Admin WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            if not account.get('is_active', 1):
                cursor.close()
                flash('Your admin account has been deactivated. Please contact the hospital.', 'danger')
                return render_template('admin/login.html')
            session['loggedin'] = True
            session['id'] = account['admin_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'admin'
            flash('Login successful!', 'success')
            cursor.close()
            return redirect(url_for('admin_dashboard'))
        
        # Credentials are valid for another role: send them to the right page.
        cursor.execute('SELECT password FROM Patient WHERE email = %s', (email,))
        patient = cursor.fetchone()
        if patient and check_password_hash(patient['password'], password):
            cursor.close()
            flash('Patients must log in using the Patient Login page.', 'warning')
            return render_template('admin/login.html')
        
        cursor.execute('SELECT password FROM Doctor WHERE email = %s', (email,))
        doctor = cursor.fetchone()
        if doctor and check_password_hash(doctor['password'], password):
            cursor.close()
            flash('Doctors must log in using the Doctor Login page.', 'warning')
            return render_template('admin/login.html')
        
        # If no match found
        flash('Invalid email or password!', 'danger')
        cursor.close()
    
    return render_template('admin/login.html')


@app.route('/admin/register', methods=['GET', 'POST'])
def admin_register():
    """Administrator self-registration, gated by a server-side secret key."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        submitted_key = request.form.get('admin_secret_key', '')
        
        configured_key = os.environ.get('ADMIN_SECRET_KEY', '')
        
        # The secret key is only ever read/compared on the server.
        if not configured_key:
            flash('Admin registration is unavailable: the server is missing the admin secret key configuration.', 'danger')
            return render_template('admin/register.html')
        
        if not submitted_key or not secrets.compare_digest(
                submitted_key.encode('utf-8'), configured_key.encode('utf-8')):
            flash('Admin registration failed: invalid admin secret key.', 'danger')
            return render_template('admin/register.html')
        
        if not name or not email or not password:
            flash('Name, email and password are required!', 'warning')
            return render_template('admin/register.html')
        
        if not re.match(r'[^@]+@[^@]+\.[^@]+', email):
            flash('Invalid email address!', 'warning')
            return render_template('admin/register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long!', 'warning')
            return render_template('admin/register.html')
        
        if password != confirm_password:
            flash('Passwords do not match!', 'warning')
            return render_template('admin/register.html')
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT admin_id FROM Admin WHERE email = %s', (email,))
        if cursor.fetchone():
            cursor.close()
            flash('An admin account with this email already exists!', 'warning')
            return render_template('admin/register.html')
        
        # The account is always created with the administrator role.
        hashed_password = generate_password_hash(password)
        cursor.execute(
            'INSERT INTO Admin (name, email, password, is_active) VALUES (%s, %s, %s, 1)',
            (name, email, hashed_password)
        )
        mysql.connection.commit()
        new_id = cursor.lastrowid
        cursor.close()
        
        log_audit('REGISTER_ADMIN', 'Admin', new_id, f"{name} registered as an administrator")
        flash('Admin registration successful! Please login.', 'success')
        return redirect(url_for('admin_login'))
    
    return render_template('admin/register.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out!', 'info')
    return redirect(url_for('index'))

# ========== FORGOT PASSWORD ROUTES ==========
@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        try:
            from markupsafe import Markup
            import secrets
            from datetime import datetime, timedelta, timezone

            email = request.form.get('email', '').strip()

            if not email:
                flash('Please enter your email.', 'warning')
                return redirect(url_for('forgot_password'))

            cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Check Patient
            cursor.execute('SELECT * FROM Patient WHERE email = %s', (email,))
            user = cursor.fetchone()
            table = 'Patient'
            id_col = 'patient_id'

            # Check Doctor
            if not user:
                cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
                user = cursor.fetchone()
                table = 'Doctor'
                id_col = 'doctor_id'

            if user:
                # Generate token
                token = secrets.token_urlsafe(16)
                expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')

                # Save token
                cursor.execute(
                    f'UPDATE {table} SET reset_token = %s, reset_token_expiry = %s WHERE {id_col} = %s',
                    (token, expiry, user[id_col])
                )
                mysql.connection.commit()

                # Generate reset link
                reset_url = url_for('reset_password', token=token, _external=True)

                # ========== SEND EMAIL VIA GMAIL SMTP ==========
                email_sent = False

                try:
                    email_body = f'''Hello {user.get('name', 'User')},

You have requested to reset your password. Click the link below to set a new password:

{reset_url}

This link will expire in 1 hour. If you did not make this request, please ignore this email.

Best regards,
MediCare Hospital Team'''

                    email_sent = send_email_via_sendgrid(
                        email,
                        "MediCare Hospital: Password reset link",
                        email_body
                    )

                    if email_sent:
                        flash(
                            'Password reset link sent to your email! Please check your inbox and spam folder.',
                            'success'
                        )

                except Exception as email_err:
                    print(f"❌ Gmail email error: {email_err}")
                    email_sent = False

                # If email failed, show link on screen
                if not email_sent:
                    flash(Markup(f'''
                        <div style="background:#fff3cd; padding:15px; border-radius:5px; border:1px solid #ffeeba; margin:10px 0;">
                            <strong style="color:#856404;">⚠️ Email could not be sent. Use the link below:</strong><br><br>
                            <b>Reset Link:</b> <a href="{reset_url}" target="_blank" style="color:#007bff; text-decoration:underline;">{reset_url}</a><br><br>
                            <small style="color:#856404;">
                                <i>Email sending failed. Please check the Gmail SMTP configuration.</i><br>
                                <b>Required:</b> Set EMAIL_ADDRESS and EMAIL_PASSWORD in Render Environment Variables.
                            </small>
                        </div>
                    '''), 'warning')
            else:
                flash('If an account exists with that email, a reset link has been sent.', 'info')

            cursor.close()

        except Exception as e:
            print(f"❌ FORGOT PASSWORD ERROR: {e}")
            import traceback
            traceback.print_exc()
            flash('An error occurred. Please try again.', 'danger')

        return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html')
                
@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        from datetime import datetime, timezone
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        print(f"🔍 Checking reset token: {token[:10]}...")
        
        # Format current time as string for SQL comparison
        current_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        # Check token in Patient table
        cursor.execute('''
            SELECT * FROM Patient 
            WHERE reset_token = %s AND reset_token_expiry > %s AND reset_token IS NOT NULL
        ''', (token, current_time))
        user = cursor.fetchone()
        
        user_type = 'patient'
        id_col = 'patient_id'
        table = 'Patient'

        # If not found in Patient, check Doctor
        if not user:
            cursor.execute('''
                SELECT * FROM Doctor 
                WHERE reset_token = %s AND reset_token_expiry > %s AND reset_token IS NOT NULL
            ''', (token, current_time))
            user = cursor.fetchone()
            user_type = 'doctor'
            id_col = 'doctor_id'
            table = 'Doctor'

        if not user:
            print(f"❌ Invalid or expired token: {token[:10]}...")
            flash('Invalid or expired reset link. Please request a new password reset.', 'danger')
            cursor.close()
            return redirect(url_for('login'))

        print(f"✅ Valid token found for user in {table} table")

        if request.method == 'POST':
            password = request.form.get('password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()

            if not password:
                flash('Password is required.', 'warning')
            elif password != confirm_password:
                flash('Passwords do not match!', 'danger')
            elif len(password) < 6:
                flash('Password must be at least 6 characters.', 'warning')
            else:
                try:
                    hashed_password = generate_password_hash(password)
                    print(f"🔐 Updating password for user...")
                    
                    cursor.execute(
                        f'UPDATE {table} SET password = %s, reset_token = NULL, reset_token_expiry = NULL WHERE {id_col} = %s',
                        (hashed_password, user[id_col])
                    )
                    mysql.connection.commit()
                    
                    print(f"✅ Password updated successfully!")
                    flash('Password updated successfully! You can now login with your new password.', 'success')
                    cursor.close()
                    return redirect(url_for('login'))
                    
                except Exception as update_error:
                    print(f"❌ Error updating password: {str(update_error)}")
                    flash('An error occurred while updating your password. Please try again.', 'danger')
                    mysql.connection.rollback()

        cursor.close()
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR in reset_password: {str(e)}")
        import traceback
        traceback.print_exc()
        flash('An unexpected error occurred. Please try again.', 'danger')
        
    return render_template('reset_password.html', token=token)

# ========== PATIENT ROUTES ==========
@app.route('/patient/dashboard')
@patient_required
def patient_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # Changed JOIN to LEFT JOIN to prevent missing appointments
    cursor.execute('''
        SELECT a.*, d.name as doctor_name, d.specialization 
        FROM Appointment a 
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id 
        WHERE a.patient_id = %s AND a.status IN ('Pending', 'Approved')
        ORDER BY a.date, a.time
    ''', (session['id'],))
    upcoming_appointments = cursor.fetchall()
    
    cursor.execute('''
        SELECT a.*, d.name as doctor_name, d.specialization 
        FROM Appointment a 
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id 
        WHERE a.patient_id = %s AND a.status IN ('Completed', 'Rejected', 'Cancelled')
        ORDER BY a.date DESC, a.time DESC
    ''', (session['id'],))
    appointment_history = cursor.fetchall()

    # Today's appointments, shown as its own card on the dashboard.
    today = local_now().date()
    cursor.execute('''
        SELECT a.*, d.name as doctor_name, d.specialization
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.patient_id = %s AND a.date = %s
          AND a.status IN ('Pending', 'Approved')
        ORDER BY a.time
    ''', (session['id'], today))
    today_appointments = cursor.fetchall()

    # Recent history, newest first, for the "recent appointments" card.
    cursor.execute('''
        SELECT a.*, d.name as doctor_name, d.specialization
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.patient_id = %s
        ORDER BY a.date DESC, a.time DESC
        LIMIT 5
    ''', (session['id'],))
    recent_appointments = cursor.fetchall()

    # A short list of doctors patients can book with right away.
    cursor.execute('''
        SELECT d.doctor_id, d.name, d.specialization, d.experience,
               d.is_active, d.availability_status,
               dp.name AS department_name
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE d.is_active = 1 AND d.availability_status = 'Available'
        ORDER BY d.experience DESC
        LIMIT 4
    ''')
    available_doctors = cursor.fetchall()
    attach_availability(available_doctors)

    cursor.execute('''
        SELECT dp.department_id, dp.name, dp.description,
               COUNT(d.doctor_id) AS doctor_count
        FROM Department dp
        LEFT JOIN Doctor d ON d.department_id = dp.department_id AND d.is_active = 1
        WHERE dp.is_active = 1
        GROUP BY dp.department_id, dp.name, dp.description
        ORDER BY dp.name
    ''')
    departments = cursor.fetchall()

    # Each department card carries its own check-up information.
    for department in departments:
        department['checkup'] = department_checkup(department)

    # The patient's active queue seats, shown as a live status card.
    my_queue_entries = _load_patient_queue(session['id'])

    cursor.close()
    
    for appt in upcoming_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])
    
    for appt in appointment_history:
        appt['time_formatted'] = get_12hr_time(appt['time'])

    for appt in today_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])

    for appt in recent_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])

    return render_template('patient/dashboard.html', 
                         upcoming_appointments=upcoming_appointments,
                         appointment_history=appointment_history,
                         today_appointments=today_appointments,
                         recent_appointments=recent_appointments,
                         available_doctors=available_doctors,
                         departments=departments,
                         my_queue_entries=my_queue_entries,
                         unread_notifications=unread_notification_count('patient', session['id']))

@app.route('/patient/settings', methods=['GET', 'POST'])
@patient_required
def patient_settings():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Patient WHERE patient_id = %s', (session['id'],))
    patient = cursor.fetchone()

    if request.method == 'POST':
        name = request.form['name']
        phone = request.form['phone']
        age = request.form['age']
        address = request.form['address']
        email = request.form['email']
        date_of_birth = request.form.get('date_of_birth', '') or None
        emergency_contact_name = request.form.get('emergency_contact_name', '') or None
        emergency_contact_phone = request.form.get('emergency_contact_phone', '') or None
        
        cursor.execute('''
            UPDATE Patient 
            SET name=%s, phone=%s, age=%s, address=%s, email=%s,
                date_of_birth=%s, emergency_contact_name=%s, emergency_contact_phone=%s
            WHERE patient_id=%s
        ''', (name, phone, age, address, email, date_of_birth,
              emergency_contact_name, emergency_contact_phone, session['id']))
        mysql.connection.commit()
        
        session['name'] = name
        session['email'] = email

        old_password = request.form.get('old_password', '').strip()
        new_password = request.form.get('new_password', '').strip()

        if old_password and new_password:
            if check_password_hash(patient['password'], old_password):
                hashed_new = generate_password_hash(new_password)
                cursor.execute('UPDATE Patient SET password=%s WHERE patient_id=%s', (hashed_new, session['id']))
                mysql.connection.commit()
                flash('Profile and password updated successfully!', 'success')
            else:
                flash('Old password is incorrect! Profile updated, but password was not changed.', 'danger')
        else:
            flash('Profile updated successfully!', 'success')

        cursor.close()
        return redirect(url_for('patient_settings'))

    cursor.close()
    return render_template('patient/settings.html', patient=patient)

@app.route('/patient/doctors')
@patient_required
def view_doctors():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    specialization = request.args.get('specialization')
    department_id = request.args.get('department', '').strip()
    search = request.args.get('search', '').strip()
    show_inactive = request.args.get('show_all') == '1'

    query = '''
        SELECT d.*, dp.name AS department_name, dp.is_active AS department_active
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE 1 = 1
    '''
    params = []

    # Inactive doctors stay hidden unless the patient opts into seeing them,
    # so previously visible listings keep behaving the same.
    if not show_inactive:
        query += ' AND (d.is_active IS NULL OR d.is_active = 1)'

    if specialization:
        query += ' AND d.specialization = %s'
        params.append(specialization)

    if department_id:
        query += ' AND d.department_id = %s'
        params.append(department_id)

    if search:
        query += ' AND (d.name LIKE %s OR d.specialization LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%'])

    query += ' ORDER BY d.experience DESC, d.name'

    cursor.execute(query, tuple(params))
    doctors = cursor.fetchall()

    cursor.execute('SELECT DISTINCT specialization FROM Doctor')
    specializations = [row['specialization'] for row in cursor.fetchall()]

    cursor.close()

    # Schedule based availability (IST) for every listed doctor.
    attach_availability(doctors)

    return render_template('patient/view_doctors.html',
                           doctors=doctors,
                           specializations=specializations,
                           departments=get_departments(),
                           selected_department=department_id,
                           search=search,
                           show_inactive=show_inactive)

@app.route('/patient/doctor/<int:doctor_id>')
@patient_required
def doctor_profile(doctor_id):
    """Full public profile for one doctor, including their availability."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT d.*, dp.name AS department_name, dp.description AS department_description
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE d.doctor_id = %s
    ''', (doctor_id,))
    doctor = cursor.fetchone()

    if not doctor:
        flash('Doctor not found!', 'danger')
        cursor.close()
        return redirect(url_for('view_doctors'))

    # Upcoming days the patient can still book, with the slots left on each.
    availability = []
    base_day = local_now().date()
    for offset in range(1, 15):
        day = base_day + timedelta(days=offset)
        if day.isoweekday() == 7:  # Sunday, already closed for all doctors
            continue
        slots = get_slots_for_date(doctor, day.strftime('%Y-%m-%d'))
        if not slots:
            continue

        date_str = day.strftime('%Y-%m-%d')
        placeholders = ', '.join(['%s'] * len(slots))
        cursor.execute(f'''
            SELECT TIME(time) AS slot_time FROM Appointment
            WHERE doctor_id = %s AND date = %s AND status IN ('Pending', 'Approved')
              AND TIME(time) IN ({placeholders})
        ''', tuple([doctor_id, date_str] + slots))
        taken = {str(row['slot_time'])[:5] for row in cursor.fetchall()}

        free_slots = [s for s in slots if s not in taken]
        if free_slots:
            availability.append({
                'date': date_str,
                'date_label': day.strftime('%a, %d %b %Y'),
                'slots': [{'24h': s, '12h': get_12hr_time(s)} for s in free_slots]
            })

    cursor.close()

    schedules = get_doctor_schedules(doctor_id)
    availability_info = doctor_availability(doctor, schedules)
    working_hours = format_working_hours(schedules)

    return render_template('patient/doctor_profile.html',
                           doctor=doctor,
                           availability=availability,
                           availability_info=availability_info,
                           working_hours=working_hours,
                           schedule_summary=describe_schedule(doctor_id))


@app.route('/api/available_slots/<int:doctor_id>')
@api_patient_required
def api_available_slots(doctor_id):
    """
    Free slots for one doctor on one date.

    The booking and reschedule forms call this whenever the date changes so the
    time list always matches the doctor's real availability and existing bookings.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT d.*, dp.is_active AS department_active
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE d.doctor_id = %s
    ''', (doctor_id,))
    doctor = cursor.fetchone()

    if not doctor:
        cursor.close()
        return jsonify({'success': False, 'message': 'Doctor not found'}), 404

    bookable, blocked_reason = doctor_is_bookable(doctor)
    if not bookable or doctor.get('department_active') == 0:
        cursor.close()
        return jsonify({
            'success': False,
            'message': blocked_reason or 'This doctor\'s department is currently not accepting appointments.'
        }), 409

    date_str = (request.args.get('date') or '').strip()[:10]
    exclude_id = request.args.get('exclude', type=int)

    try:
        selected = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        cursor.close()
        return jsonify({'success': False, 'message': 'Invalid date'}), 400

    if selected.isoweekday() == 7:
        cursor.close()
        return jsonify({'success': False, 'message': 'Doctors are not available on Sundays.'}), 409

    slots = get_slots_for_date(doctor, date_str)
    # Slots that have already elapsed today are never offered.
    slots = filter_future_slots(slots, date_str)
    if not slots:
        cursor.close()
        message = ('No upcoming slots available for today. Please select another date.'
                   if selected == local_now().date()
                   else 'No availability on this date.')
        return jsonify({'success': True, 'slots': [], 'message': message})

    # Times already held by a live appointment are hidden.
    placeholders = ', '.join(['%s'] * len(slots))
    params = [doctor_id, date_str] + slots
    exclude_sql = ''
    if exclude_id:
        exclude_sql = ' AND appointment_id <> %s'
        params.append(exclude_id)

    cursor.execute(f'''
        SELECT TIME(time) AS slot_time FROM Appointment
        WHERE doctor_id = %s AND date = %s AND status IN ('Pending', 'Approved')
          AND TIME(time) IN ({placeholders}){exclude_sql}
    ''', tuple(params))
    taken = {str(row['slot_time'])[:5] for row in cursor.fetchall()}
    cursor.close()

    free_slots = [s for s in slots if s not in taken]

    if not free_slots and selected == local_now().date():
        message = 'No upcoming slots available for today. Please select another date.'
    else:
        message = 'No free slots left on this date.' if not free_slots else ''

    return jsonify({
        'success': True,
        'date': date_str,
        'slots': [{'24h': s, '12h': get_12hr_time(s)} for s in free_slots],
        'message': message
    })


@app.route('/api/doctor_availability')
@api_patient_required
def api_doctor_availability():
    """
    Live availability for every doctor.

    The doctor listings render the schedule based status server side; this
    endpoint lets an open page pick up a shift starting or ending without a
    full reload, so a stale "Available" badge never lingers.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute('''
            SELECT doctor_id, name, is_active, availability_status
            FROM Doctor
            ORDER BY doctor_id
        ''')
        doctors = cursor.fetchall()
    except Exception as e:
        print(f"Doctor availability unavailable: {e}")
        doctors = []
    finally:
        cursor.close()

    moment = local_now()
    schedules = get_schedules_for_doctors([d['doctor_id'] for d in doctors])

    payload = []
    for doctor in doctors:
        info = doctor_availability(doctor, schedules.get(doctor['doctor_id'], []), moment)
        payload.append({
            'doctor_id': doctor['doctor_id'],
            'status': info['status'],
            'label': info['label'],
            'reason': info['reason'],
            'until': info['until'],
            'next': info['next'],
        })

    response = jsonify({
        'success': True,
        'server_time': moment.strftime('%Y-%m-%d %I:%M %p'),
        'doctors': payload,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/patient/departments')
@patient_required
def patient_departments():
    """Department directory with the doctors working in each one."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT dp.department_id, dp.name, dp.description,
               COUNT(d.doctor_id) AS doctor_count
        FROM Department dp
        LEFT JOIN Doctor d ON d.department_id = dp.department_id AND d.is_active = 1
        WHERE dp.is_active = 1
        GROUP BY dp.department_id, dp.name, dp.description
        ORDER BY dp.name
    ''')
    departments = cursor.fetchall()

    cursor.execute('''
        SELECT d.doctor_id, d.name, d.specialization, d.experience,
               d.consultation_fee, d.availability_status, d.is_active,
               dp.department_id, dp.name AS department_name
        FROM Doctor d
        JOIN Department dp ON d.department_id = dp.department_id
        WHERE dp.is_active = 1 AND d.is_active = 1
        ORDER BY dp.name, d.experience DESC
    ''')
    doctors = cursor.fetchall()

    cursor.close()

    attach_availability(doctors)

    grouped = {}
    for doctor in doctors:
        grouped.setdefault(doctor['department_id'], []).append(doctor)

    return render_template('patient/departments.html',
                           departments=departments,
                           doctors_by_department=grouped)

@app.route('/patient/department/<int:department_id>')
@patient_required
def patient_department_detail(department_id):
    """Doctors belonging to a single department."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('SELECT * FROM Department WHERE department_id = %s', (department_id,))
    department = cursor.fetchone()

    if not department:
        flash('Department not found!', 'danger')
        cursor.close()
        return redirect(url_for('patient_departments'))

    cursor.execute('''
        SELECT d.*, dp.name AS department_name
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE d.department_id = %s AND (d.is_active IS NULL OR d.is_active = 1)
        ORDER BY d.experience DESC
    ''', (department_id,))
    doctors = cursor.fetchall()

    cursor.close()

    attach_availability(doctors)

    return render_template('patient/department_detail.html',
                           department=department,
                           doctors=doctors)

# Helper function to convert 24h to 12h AM/PM
def get_12hr_time(time_24):
    """
    Formats a 24-hour time as '09:30 AM' (zero-padded, consistent everywhere).

    Accepts 'HH:MM', 'HH:MM:SS' and the datetime.timedelta values PyMySQL
    returns for TIME columns (whose str() is unpadded, e.g. '9:30:00', so a
    naive [:5] slice produces '9:30:' and drops the AM/PM suffix).
    """
    if time_24 is None:
        return ''

    if isinstance(time_24, timedelta):
        total = int(time_24.total_seconds())
        hours = total // 3600
        minutes = (total % 3600) // 60
        hour_12 = hours % 12 or 12
        return f"{hour_12:02d}:{minutes:02d} {'AM' if hours < 12 else 'PM'}"

    text = str(time_24).strip()
    try:
        parts = text.split(':')
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        hour_12 = hours % 12 or 12
        return f"{hour_12:02d}:{minutes:02d} {'AM' if hours < 12 else 'PM'}"
    except (ValueError, IndexError) as e:
        print(f"Error converting time: {e}")
        return text

def get_24hr_hm(value):
    """Normalise a time value to a zero-padded 24-hour 'HH:MM' string.

    Used for ``<input type="time">`` values and ``data-*`` attributes, which
    need a machine-readable 24-hour value regardless of the 12-hour
    presentation shown to users.
    """
    if value is None:
        return ''
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        return f"{(total // 3600) % 24:02d}:{(total % 3600) // 60:02d}"

    text = str(value).strip()
    parts = text.split(':')
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return text[:5]
    return f"{hours % 24:02d}:{minutes:02d}"


def get_datetime_12h(value):
    """Render a database timestamp in IST (UTC→IST) with a 12-hour time.

    All ``created_at``/``updated_at`` columns are written by the MySQL
    server in UTC, so naive values are interpreted as UTC and converted
    once to Asia/Kolkata before formatting.
    """
    if not value:
        return ''
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        try:
            moment = datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo('UTC'))
    moment = moment.astimezone(HOSPITAL_TZ)
    return moment.strftime('%Y-%m-%d %I:%M %p')


app.jinja_env.filters['time12'] = get_12hr_time
app.jinja_env.filters['hm24'] = get_24hr_hm
app.jinja_env.filters['dt12'] = get_datetime_12h


@app.route('/patient/book/<int:doctor_id>', methods=['GET', 'POST'])
@patient_required
def book_appointment(doctor_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    cursor.execute('''
        SELECT d.*, dp.name AS department_name, dp.is_active AS department_active
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE d.doctor_id = %s
    ''', (doctor_id,))
    doctor = cursor.fetchone()
    
    if not doctor:
        flash('Doctor not found!', 'danger')
        cursor.close()
        return redirect(url_for('view_doctors'))

# Inactive doctor or inactive department must not accept bookings.
    bookable, blocked_reason = doctor_is_bookable(doctor)
    if not bookable:
        flash(blocked_reason, 'danger')
        cursor.close()
        return redirect(url_for('view_doctors'))
    if doctor.get('department_active') == 0:
        flash('This doctor\'s department is currently not accepting appointments.', 'danger')
        cursor.close()
        return redirect(url_for('view_doctors'))

    # Day-aware slots for the date being viewed; re-derived on GET because the
    # date can change between page loads.
    requested_date = request.form.get('date') if request.method == 'POST' else request.args.get('date')
    try:
        slot_date = (datetime.strptime(requested_date, '%Y-%m-%d').date()
                     if requested_date else local_now().date())
        slot_date_str = slot_date.isoformat()
    except (ValueError, TypeError):
        slot_date_str = local_now().date().isoformat()

    raw_slots_24h = filter_future_slots(
        get_slots_for_date(doctor, slot_date_str), slot_date_str)
    available_slots = [{'24h': slot, '12h': get_12hr_time(slot)} for slot in raw_slots_24h]
    booked_slot_times = set()
    cursor.execute('''
        SELECT TIME(time) AS slot_time FROM Appointment
        WHERE doctor_id = %s AND date = %s AND status IN ('Pending', 'Approved')
    ''', (doctor_id, slot_date_str))
    for row in cursor.fetchall():
        booked_slot_times.add(str(row['slot_time'])[:5])

    if request.method == 'POST':
        date = request.form['date']
        time_24 = request.form['time']
        reason = request.form.get('reason', '').strip() or None
        
        # 1. PAST DATE CHECK (checked first so a past Sunday reports the
        #    more informative "past date" message rather than "no Sundays")
        selected_date = datetime.strptime(date, '%Y-%m-%d').date()
        now = local_now()
        if selected_date < now.date():
            flash('You cannot book an appointment for a past date.', 'danger')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))

        # 1b. PAST TIME CHECK — a slot that has already elapsed today can
        #     never be booked, even if the form was manipulated.
        if selected_date == now.date() and str(time_24)[:5] <= now.strftime('%H:%M'):
            flash('That time has already passed for today. Please choose a later slot or another date.', 'danger')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))

        # 2. SUNDAY CHECK
        if selected_date.weekday() == 6:
            flash('Doctors are not available on Sundays. Please choose another day.', 'warning')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))

        # 3. THE REQUESTED TIME MUST BE INSIDE THE DOCTOR'S AVAILABILITY
        allowed_slots = get_slots_for_date(doctor, date)
        if not allowed_slots or time_24 not in allowed_slots:
            flash('That time is outside the doctor\'s availability. Please pick one of the listed slots.', 'danger')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))

        # 3. CHECK IF SLOT IS ALREADY BOOKED (Bulletproof Query)
        # We use TIME() to ensure format mismatches (10:00 vs 10:00:00) don't cause issues
        cursor.execute('''
            SELECT appointment_id, status FROM Appointment 
            WHERE doctor_id = %s AND date = %s AND TIME(time) = TIME(%s)
            AND status IN ('Pending', 'Approved')
        ''', (doctor_id, date, time_24))
        
        existing = cursor.fetchone()
        
        if existing:
            # Create a beautiful, clear warning message
            time_12hr = get_12hr_time(time_24)
            message = Markup(f'''
                <div style="background-color: #fff3cd; color: #856404; padding: 15px; border-radius: 5px; border: 1px solid #ffeeba;">
                    <strong>️ Slot Unavailable!</strong><br>
                    Dr. {doctor['name']} is already booked at <strong>{time_12hr}</strong> on <strong>{date}</strong>.<br><br>
                    Please select a different time slot or 
                    <a href="{url_for('view_doctors', specialization=doctor['specialization'])}" style="color: #004085; font-weight: bold;">find another {doctor['specialization']} doctor</a>.
                </div>
            ''')
            flash(message, 'warning')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))

        # 4. THE SAME PATIENT CANNOT BOOK THE SAME DOCTOR TWICE IN ONE DAY
        cursor.execute('''
            SELECT appointment_id FROM Appointment
            WHERE patient_id = %s AND doctor_id = %s AND date = %s
              AND status IN ('Pending', 'Approved')
        ''', (session['id'], doctor_id, date))

        if cursor.fetchone():
            flash('You already have an appointment with this doctor on that date.', 'warning')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))
        else:
            # Book the appointment
            cursor.execute(
                '''
                INSERT INTO Appointment
                    (patient_id, doctor_id, date, time, status, reason, department_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                (session['id'], doctor_id, date, time_24, 'Pending', reason,
                 doctor.get('department_id'))
            )
            mysql.connection.commit()
            appointment_id = cursor.lastrowid
            cursor.close()

            notify_appointment_event(appointment_id, 'booked')
            log_audit('BOOK_APPOINTMENT', 'Appointment', appointment_id,
                      f"Patient booked {date} {get_12hr_time(time_24)} with doctor {doctor_id}")

            flash('Appointment booked successfully! Waiting for doctor approval.', 'success')
            return redirect(url_for('patient_dashboard'))
    
    cursor.close()
    now = local_now()
    today = now.strftime('%Y-%m-%d')
    slots_hint = None
    if slot_date_str == today and not raw_slots_24h:
        slots_hint = 'No upcoming slots available for today. Please select another date.'

    return render_template('patient/book_appointment.html', doctor=doctor, today=today,
                           selected_date=slot_date_str,
                           available_slots=available_slots,
                           slots_hint=slots_hint,
                           booked_slots=sorted(booked_slot_times))

@app.route('/patient/book_appointment', methods=['GET', 'POST'])
@patient_required
def patient_book_appointment_page():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    if request.method == 'POST':
        doctor_id = request.form['doctor_id']
        date = request.form['date']
        time_24 = request.form['time']
        reason = request.form.get('reason', '').strip() or None
        
        # Get doctor name for the message
        cursor.execute('''
            SELECT d.name, d.specialization, d.department_id, d.is_active,
                   d.availability_status, dp.is_active AS department_active
            FROM Doctor d
            LEFT JOIN Department dp ON d.department_id = dp.department_id
            WHERE d.doctor_id = %s
        ''', (doctor_id,))
        doc_info = cursor.fetchone()

        if not doc_info:
            flash('Doctor not found!', 'danger')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        bookable, blocked_reason = doctor_is_bookable(doc_info)
        if not bookable or doc_info.get('department_active') == 0:
            flash(blocked_reason or 'This doctor is currently not accepting appointments.', 'danger')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 1. SUNDAY CHECK
        selected_date = datetime.strptime(date, '%Y-%m-%d').date()
        if selected_date.weekday() == 6:
            flash('Doctors are not available on Sundays. Please choose another day.', 'warning')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 2. PAST DATE CHECK
        now = local_now()
        if selected_date < now.date():
            flash('You cannot book an appointment for a past date.', 'danger')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 2a. PAST TIME CHECK — today's elapsed slots cannot be booked even
        #     if the request bypasses the form.
        if selected_date == now.date() and str(time_24)[:5] <= now.strftime('%H:%M'):
            flash('That time has already passed for today. Please choose a later slot or another date.', 'danger')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 2b. REQUESTED TIME MUST BE INSIDE THE DOCTOR'S AVAILABILITY
        allowed_slots = get_slots_for_date(doc_info, date)
        if time_24 not in allowed_slots:
            flash('That time is outside the doctor\'s availability. Please pick one of the listed slots.', 'danger')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 3. CHECK IF SLOT IS ALREADY BOOKED
        cursor.execute('''
            SELECT appointment_id, status FROM Appointment 
            WHERE doctor_id = %s AND date = %s AND TIME(time) = TIME(%s)
            AND status IN ('Pending', 'Approved')
        ''', (doctor_id, date, time_24))
        
        existing = cursor.fetchone()
        
        if existing:
            time_12hr = get_12hr_time(time_24)
            message = Markup(f'''
                <div style="background-color: #fff3cd; color: #856404; padding: 15px; border-radius: 5px; border: 1px solid #ffeeba;">
                    <strong>⚠️ Slot Unavailable!</strong><br>
                    Dr. {doc_info['name']} is already booked at <strong>{time_12hr}</strong> on <strong>{date}</strong>.<br><br>
                    Please select a different time slot or 
                    <a href="{url_for('view_doctors', specialization=doc_info['specialization'])}" style="color: #004085; font-weight: bold;">find another {doc_info['specialization']} doctor</a>.
                </div>
            ''')
            flash(message, 'warning')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 4. NO DUPLICATE BOOKING WITH THE SAME DOCTOR ON THE SAME DAY
        cursor.execute('''
            SELECT appointment_id FROM Appointment
            WHERE patient_id = %s AND doctor_id = %s AND date = %s
              AND status IN ('Pending', 'Approved')
        ''', (session['id'], doctor_id, date))

        if cursor.fetchone():
            flash('You already have an appointment with this doctor on that date.', 'warning')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))
        else:
            cursor.execute(
                '''
                INSERT INTO Appointment
                    (patient_id, doctor_id, date, time, status, reason, department_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                (session['id'], doctor_id, date, time_24, 'Pending', reason,
                 doc_info.get('department_id'))
            )
            mysql.connection.commit()
            appointment_id = cursor.lastrowid
            cursor.close()

            notify_appointment_event(appointment_id, 'booked')
            log_audit('BOOK_APPOINTMENT', 'Appointment', appointment_id,
                      f"Patient booked {date} {get_12hr_time(time_24)} with doctor {doctor_id}")

            flash('Appointment booked successfully! Waiting for doctor approval.', 'success')
            return redirect(url_for('patient_dashboard'))
    
    # Only bookable doctors are offered here.
    cursor.execute('''
        SELECT d.*, dp.name AS department_name
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE (d.is_active IS NULL OR d.is_active = 1)
          AND d.availability_status = 'Available'
          AND (dp.is_active IS NULL OR dp.is_active = 1)
        ORDER BY d.name
    ''')
    doctors = cursor.fetchall()
    cursor.close()
    
    today = local_now().strftime('%Y-%m-%d')
    default_slots_24h = filter_future_slots(
        generate_30min_slots("09:00", "17:00"), today)
    available_slots = [{'24h': slot, '12h': get_12hr_time(slot)} for slot in default_slots_24h]
    
    return render_template('patient/book_appoinment_page.html', doctors=doctors, today=today, available_slots=available_slots, departments=get_departments())

@app.route('/patient/cancel/<int:appointment_id>', methods=['GET', 'POST'])
@patient_required
def cancel_appointment(appointment_id):
    cursor = mysql.connection.cursor()
    # Fixed: Only allow cancellation of Pending or Approved appointments
    cursor.execute(
        "UPDATE Appointment SET status = 'Cancelled' WHERE appointment_id = %s AND patient_id = %s AND status IN ('Pending', 'Approved')",
        (appointment_id, session['id'])
    )
    appointment_cancelled = cursor.rowcount > 0
    if appointment_cancelled:
        # Free the queue seat before the appointment change commits.
        cancel_appointment_queue_entries(appointment_id)
    mysql.connection.commit()
    
    if appointment_cancelled:
        notify_appointment_event(appointment_id, 'cancelled')
        log_audit('CANCEL_APPOINTMENT', 'Appointment', appointment_id,
                  'Patient cancelled the appointment')
        email_sent = send_appointment_cancelled_email(appointment_id)
        if email_sent:
            flash('Appointment cancelled successfully! A confirmation email has been sent.', 'success')
        else:
            flash('Appointment cancelled, but the confirmation email could not be sent.', 'warning')
    else:
        flash('Could not cancel appointment. It may have already been completed or cancelled.', 'warning')
    
    cursor.close()
    return redirect(url_for('patient_dashboard'))

@app.route('/patient/appointments')
@patient_required
def patient_appointments():
    """
    Full appointment list for the patient: upcoming and history separated,
    with filters for status and search. The dashboard cards stay as they are.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    status_filter = request.args.get('status', '').strip()
    search = request.args.get('search', '').strip()

    query = '''
        SELECT a.*, d.name AS doctor_name, d.specialization,
               dp.name AS department_name
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        LEFT JOIN Department dp ON a.department_id = dp.department_id
        WHERE a.patient_id = %s
    '''
    params = [session['id']]

    if status_filter in APPOINTMENT_STATUSES:
        query += ' AND a.status = %s'
        params.append(status_filter)

    if search:
        query += ' AND (d.name LIKE %s OR dp.name LIKE %s OR a.reason LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])

    # Upcoming first, then history newest first.
    query += '''
        ORDER BY
            CASE WHEN a.status IN ('Pending', 'Approved') THEN 0 ELSE 1 END,
            CASE WHEN a.status IN ('Pending', 'Approved') THEN a.date END ASC,
            CASE WHEN a.status IN ('Pending', 'Approved') THEN a.time END ASC,
            a.date DESC, a.time DESC
    '''

    cursor.execute(query, tuple(params))
    appointments = cursor.fetchall()

    # Feedback/prescription state per appointment, for the row action buttons.
    cursor.execute('SELECT appointment_id FROM Feedback WHERE patient_id = %s',
                   (session['id'],))
    feedback_appointment_ids = {row['appointment_id'] for row in cursor.fetchall()}
    cursor.execute('SELECT appointment_id, prescription_id FROM Prescription WHERE patient_id = %s',
                   (session['id'],))
    prescriptions_by_appointment = {
        row['appointment_id']: row['prescription_id']
        for row in cursor.fetchall()
    }
    cursor.close()

    for appt in appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])
        appt['is_upcoming'] = appointment_is_active(appt['status'])
        appt['has_feedback'] = appt['appointment_id'] in feedback_appointment_ids
        appt['prescription_id'] = prescriptions_by_appointment.get(appt['appointment_id'])

    return render_template('patient/appointments.html',
                           appointments=appointments,
                           status_filter=status_filter,
                           search=search,
                           statuses=APPOINTMENT_STATUSES)

@app.route('/patient/appointment/<int:appointment_id>')
@patient_required
def patient_appointment_detail(appointment_id):
    """Read-only detail view for one of the patient's own appointments."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT a.*, d.name AS doctor_name, d.specialization,
               d.qualification, d.experience, d.consultation_fee,
               d.phone AS doctor_phone, d.email AS doctor_email,
               dp.name AS department_name
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        LEFT JOIN Department dp ON a.department_id = dp.department_id
        WHERE a.appointment_id = %s AND a.patient_id = %s
    ''', (appointment_id, session['id']))
    appointment = cursor.fetchone()

    if not appointment:
        cursor.close()
        flash('Appointment not found!', 'danger')
        return redirect(url_for('patient_dashboard'))

    appointment['time_formatted'] = get_12hr_time(appointment['time'])

    # Review / prescription shortcuts for this appointment.
    cursor.execute('SELECT feedback_id FROM Feedback WHERE appointment_id = %s AND patient_id = %s',
                   (appointment_id, session['id']))
    feedback_row = cursor.fetchone()
    cursor.execute('SELECT prescription_id FROM Prescription WHERE appointment_id = %s AND patient_id = %s',
                   (appointment_id, session['id']))
    prescription_row = cursor.fetchone()
    cursor.close()

    return render_template('patient/appointment_detail.html',
                           appointment=appointment,
                           feedback_id=feedback_row['feedback_id'] if feedback_row else None,
                           prescription_id=prescription_row['prescription_id'] if prescription_row else None)

@app.route('/patient/reschedule/<int:appointment_id>', methods=['GET', 'POST'])
@patient_required
def reschedule_appointment(appointment_id):
    """
    Move an appointment to a new date/time.
    The whole availability check runs again for the new slot, and the old slot
    is only released once the new one has been confirmed available.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT a.*, d.name AS doctor_name, d.specialization, d.available_slots,
               d.is_active, d.availability_status
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.appointment_id = %s AND a.patient_id = %s
    ''', (appointment_id, session['id']))
    appointment = cursor.fetchone()

    if not appointment:
        flash('Appointment not found!', 'danger')
        cursor.close()
        return redirect(url_for('patient_dashboard'))

    # Completed appointments cannot be moved, and neither can past visits.
    if appointment['status'] not in ('Pending', 'Approved'):
        flash('Only pending or confirmed appointments can be rescheduled.', 'warning')
        cursor.close()
        return redirect(url_for('patient_dashboard'))

    now = local_now()
    today = now.date()
    if appointment['date'] < today:
        flash('Past appointments cannot be rescheduled.', 'warning')
        cursor.close()
        return redirect(url_for('patient_dashboard'))

    if request.method == 'POST':
        new_date = request.form['date']
        new_time = request.form['time']

        selected_date = datetime.strptime(new_date, '%Y-%m-%d').date()
        if selected_date.weekday() == 6:
            flash('Doctors are not available on Sundays. Please choose another day.', 'warning')
            cursor.close()
            return redirect(url_for('reschedule_appointment', appointment_id=appointment_id))

        if selected_date < today:
            flash('You cannot reschedule to a past date.', 'danger')
            cursor.close()
            return redirect(url_for('reschedule_appointment', appointment_id=appointment_id))

        # A new time that has already elapsed today is not selectable.
        if selected_date == today and str(new_time)[:5] <= now.strftime('%H:%M'):
            flash('That time has already passed for today. Please choose a later slot.', 'danger')
            cursor.close()
            return redirect(url_for('reschedule_appointment', appointment_id=appointment_id))

        # Re-run availability for the new slot.
        allowed_slots = get_slots_for_date(appointment, new_date)
        if new_time not in allowed_slots:
            flash('That time is outside the doctor\'s availability. Please pick one of the listed slots.', 'danger')
            cursor.close()
            return redirect(url_for('reschedule_appointment', appointment_id=appointment_id))

        # The slot must be free once this appointment is excluded.
        if not slot_is_free(cursor, appointment['doctor_id'], new_date, new_time,
                            exclude_id=appointment_id):
            flash('That slot has already been booked. Please choose another time.', 'warning')
            cursor.close()
            return redirect(url_for('reschedule_appointment', appointment_id=appointment_id))

        previous = f"{appointment['date']} {get_12hr_time(appointment['time'])}"
        cursor.execute('''
            UPDATE Appointment
            SET date = %s, time = %s, status = 'Pending'
            WHERE appointment_id = %s AND patient_id = %s
        ''', (new_date, new_time, appointment_id, session['id']))
        rescheduled = cursor.rowcount > 0
        if rescheduled:
            # The old day's queue seat is void; re-approval assigns a new
            # number for the new date.
            cancel_appointment_queue_entries(appointment_id)
        mysql.connection.commit()
        cursor.close()

        notify_appointment_event(appointment_id, 'rescheduled',
                                 f"Moved from {previous}. ")
        log_audit('RESCHEDULE_APPOINTMENT', 'Appointment', appointment_id,
                  f"Moved from {previous} to {new_date} {new_time}")

        flash('Appointment rescheduled successfully. It is pending doctor approval again.', 'success')
        return redirect(url_for('patient_dashboard'))

    cursor.close()
    appointment_date = str(appointment['date'])[:10]
    available_slots = [{'24h': s, '12h': get_12hr_time(s)}
                       for s in filter_future_slots(
                           get_slots_for_date(appointment, appointment['date']),
                           appointment_date)]

    return render_template('patient/reschedule.html',
                           appointment=appointment,
                           available_slots=available_slots,
                           today=local_now().strftime('%Y-%m-%d'))

@app.route('/patient/notifications')
@patient_required
def patient_notifications():
    return render_notifications('patient')

@app.route('/doctor/notifications')
@doctor_required
def doctor_notifications():
    return render_notifications('doctor')

@app.route('/notifications', methods=['POST'])
def mark_notification_read():
    """Shared endpoint for the single-read and read-all actions."""
    if 'loggedin' not in session:
        return redirect(url_for('login'))

    user_type = session.get('user_type')
    user_id = session.get('id')

    if user_type not in ('patient', 'doctor', 'admin'):
        flash('Notifications are not available for this account.', 'warning')
        return redirect(url_for('index'))

    action = request.form.get('action', '')
    cursor = mysql.connection.cursor()

    if action == 'read_all':
        cursor.execute(
            'UPDATE Notification SET is_read = 1 WHERE user_type = %s AND user_id = %s AND is_read = 0',
            (user_type, user_id)
        )
        mysql.connection.commit()
        cursor.close()
        flash('All notifications marked as read.', 'success')
    else:
        notification_id = request.form.get('notification_id')
        cursor.execute(
            'UPDATE Notification SET is_read = 1 WHERE notification_id = %s AND user_type = %s AND user_id = %s',
            (notification_id, user_type, user_id)
        )
        mysql.connection.commit()
        cursor.close()
        flash('Notification marked as read.', 'success')

    return redirect(url_for('view_notifications'))

@app.route('/notifications')
def view_notifications():
    """One notifications page shared by patient, doctor and admin."""
    if 'loggedin' not in session:
        flash('Please login first!', 'warning')
        return redirect(url_for('login'))

    return render_notifications(session.get('user_type'))

def render_notifications(user_type):
    user_id = session.get('id')

    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('''
            SELECT n.*, a.date AS appointment_date, a.time AS appointment_time,
                   a.status AS appointment_status
            FROM Notification n
            LEFT JOIN Appointment a ON n.appointment_id = a.appointment_id
            WHERE n.user_type = %s AND n.user_id = %s
            ORDER BY n.is_read ASC, n.created_at DESC
            LIMIT 100
        ''', (user_type, user_id))
        notifications = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"Notification list unavailable: {e}")
        notifications = []

    for note in notifications:
        if note.get('appointment_time'):
            note['appointment_time_formatted'] = get_12hr_time(note['appointment_time'])

    return render_template('notifications.html',
                           notifications=notifications,
                           unread_count=unread_notification_count(user_type, user_id),
                           user_type=user_type)

# ========== DOCTOR ROUTES ==========
@app.route('/doctor/dashboard')
@doctor_required
def doctor_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    today = local_now().date()
    
    # Get search parameters from URL
    search_patient = request.args.get('search', '').strip()
    filter_date = request.args.get('date_filter', '').strip()
    
    # Build base query for completed appointments
    completed_query = '''
        SELECT a.*, p.name as patient_name, p.email as patient_email, 
               p.phone as patient_phone, p.age as patient_age, 
               p.gender as patient_gender, p.address as patient_address,
               d.specialization
        FROM Appointment a 
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.doctor_id = %s AND a.status = 'Completed'
    '''
    
    query_params = [session['id']]
    
    # Add search filter if provided
    if search_patient:
        completed_query += ' AND (p.name LIKE %s OR p.email LIKE %s OR p.phone LIKE %s)'
        search_param = f'%{search_patient}%'
        query_params.extend([search_param, search_param, search_param])
    
    # Add date filter if provided
    if filter_date:
        completed_query += ' AND a.date = %s'
        query_params.append(filter_date)
    
    completed_query += ' ORDER BY a.date DESC, a.time DESC'
    
    # Execute query for completed appointments
    cursor.execute(completed_query, tuple(query_params))
    completed_appointments = cursor.fetchall()
    
    # Get today's appointments
    cursor.execute('''
        SELECT a.*, p.name as patient_name, p.email as patient_email, 
               p.phone as patient_phone, p.age as patient_age, 
               p.gender as patient_gender, p.address as patient_address,
               d.specialization
        FROM Appointment a 
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.doctor_id = %s AND a.date = %s AND a.status = 'Approved'
        ORDER BY a.time
    ''', (session['id'], today))
    today_appointments = cursor.fetchall()
    
    # Get pending appointments
    cursor.execute('''
        SELECT a.*, p.name as patient_name, p.email as patient_email, 
               p.phone as patient_phone, d.specialization
        FROM Appointment a 
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.doctor_id = %s AND a.status = 'Pending'
        ORDER BY a.date, a.time
    ''', (session['id'],))
    pending_appointments = cursor.fetchall()

    # Upcoming = confirmed appointments dated after today.
    cursor.execute('''
        SELECT a.*, p.name as patient_name, p.email as patient_email,
               p.phone as patient_phone, d.specialization
        FROM Appointment a
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.doctor_id = %s AND a.status = 'Approved' AND a.date > %s
        ORDER BY a.date, a.time
    ''', (session['id'], today))
    upcoming_appointments = cursor.fetchall()

    # Cancelled and rejected are counted separately from completed.
    cursor.execute('''
        SELECT a.status, COUNT(*) AS total
        FROM Appointment a
        WHERE a.doctor_id = %s AND a.status IN ('Cancelled', 'Rejected')
        GROUP BY a.status
    ''', (session['id'],))
    cancelled_rejected = {row['status']: row['total'] for row in cursor.fetchall()}

    # Today's live queue summary for the dashboard's queue card.
    cursor.execute('''
        SELECT status, COUNT(*) AS total
        FROM QueueEntry
        WHERE doctor_id = %s AND queue_date = %s
        GROUP BY status
    ''', (session['id'], today))
    queue_counts = {row['status']: row['total'] for row in cursor.fetchall()}

    cursor.close()
    
    for appt in today_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])
        
    for appt in pending_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])
        
    for appt in completed_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])

    for appt in upcoming_appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])
    
    return render_template('doctor/dashboard.html', 
                         today_appointments=today_appointments,
                         pending_appointments=pending_appointments,
                         completed_appointments=completed_appointments,
                         upcoming_appointments=upcoming_appointments,
                         cancelled_count=cancelled_rejected.get('Cancelled', 0),
                         rejected_count=cancelled_rejected.get('Rejected', 0),
                         queue_counts=queue_counts,
                         search_patient=search_patient,
                         filter_date=filter_date,
                         unread_notifications=unread_notification_count('doctor', session['id']))


@app.route('/doctor/complete_appointment', methods=['POST'])
@doctor_required
def complete_appointment():
    appointment_id = request.form.get('appointment_id')

    if not appointment_id:
        flash('Appointment ID is missing.', 'danger')
        return redirect(url_for('doctor_appointments'))

    try:
        follow_up_days = int(request.form.get('follow_up_days', 0) or 0)
    except (ValueError, TypeError):
        follow_up_days = 0

    if follow_up_days < 0 or follow_up_days > 15:
        flash('Follow-up days must be between 0 and 15.', 'warning')
        return redirect(url_for('doctor_appointments'))

    follow_up_date = None

    if 1 <= follow_up_days <= 15:
        follow_up_date = (
            get_server_time_with_offset() + timedelta(days=follow_up_days)
        ).date()

    cursor = mysql.connection.cursor()

    try:
        cursor.execute("""
            UPDATE Appointment
            SET status = 'Completed',
                follow_up_date = %s
            WHERE appointment_id = %s
              AND doctor_id = %s
              AND status = 'Approved'
        """, (follow_up_date, appointment_id, session['id']))

        updated = cursor.rowcount > 0
        if updated:
            # The visit is over: mark the queue seat served with it.
            complete_appointment_queue_entries(int(appointment_id))
        mysql.connection.commit()

    except Exception as e:
        mysql.connection.rollback()
        print(f"❌ Appointment completion error: {e}")
        flash('Unable to complete the appointment.', 'danger')
        return redirect(url_for('doctor_appointments'))

    finally:
        cursor.close()

    if not updated:
        flash(
            'Appointment was not found, is not assigned to you, '
            'or is already completed.',
            'warning'
        )
        return redirect(url_for('doctor_appointments'))

    log_audit('COMPLETE_APPOINTMENT', 'Appointment', appointment_id,
              f"Doctor completed appointment (follow-up: {follow_up_date or 'none'})")

    # Send the thank-you email after the appointment is completed.
    email_sent = send_appointment_completed_email(int(appointment_id))
    notify_appointment_event(int(appointment_id), 'completed')
    # Ask the patient to rate the visit; deduped via feedback_email_sent.
    send_feedback_invitation_email(int(appointment_id))

    if follow_up_date:
        if email_sent:
            flash(
                f'Appointment completed! Follow-up scheduled for '
                f'{follow_up_date.strftime("%d-%m-%Y")}. '
                'The patient was sent the confirmation email.',
                'success'
            )
        else:
            flash(
                f'Appointment completed and follow-up scheduled for '
                f'{follow_up_date.strftime("%d-%m-%Y")}, '
                'but the thank-you email could not be sent.',
                'warning'
            )
    else:
        if email_sent:
            flash(
                'Appointment completed! Thank-you email sent. '
                'No follow-up scheduled.',
                'success'
            )
        else:
            flash(
                'Appointment completed, but the thank-you email '
                'could not be sent. No follow-up scheduled.',
                'warning'
            )

    return redirect(url_for('doctor_appointments'))
@app.route('/doctor/appointments')
@doctor_required
def doctor_appointments():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    status_filter = request.args.get('status', '').strip()
    search = request.args.get('search', '').strip()
    date_filter = request.args.get('date_filter', '').strip()

    query = '''
        SELECT a.*, p.name as patient_name, p.email as patient_email,
               p.phone as patient_phone, p.age as patient_age,
               p.gender as patient_gender, p.address as patient_address,
               dp.name AS department_name
        FROM Appointment a 
        LEFT JOIN Patient p ON a.patient_id = p.patient_id 
        LEFT JOIN Department dp ON a.department_id = dp.department_id
        WHERE a.doctor_id = %s
    '''
    params = [session['id']]

    if status_filter in APPOINTMENT_STATUSES:
        query += ' AND a.status = %s'
        params.append(status_filter)

    if search:
        query += ' AND (p.name LIKE %s OR p.email LIKE %s OR p.phone LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])

    if date_filter:
        query += ' AND a.date = %s'
        params.append(date_filter)

    query += ' ORDER BY a.date DESC, a.time DESC'
    
    cursor.execute(query, tuple(params))
    appointments = cursor.fetchall()

    # Which completed appointments already have a prescription (action button).
    cursor.execute('SELECT appointment_id, prescription_id FROM Prescription WHERE doctor_id = %s',
                   (session['id'],))
    prescriptions_by_appointment = {
        row['appointment_id']: row['prescription_id']
        for row in cursor.fetchall()
    }
    cursor.close()
    
    # Add formatted time to each appointment
    for appt in appointments:
        appt['time_formatted'] = get_12hr_time(appt.get('time'))
        appt['prescription_id'] = prescriptions_by_appointment.get(appt['appointment_id'])
    
    return render_template('doctor/appointments.html',
                           appointments=appointments,
                           status_filter=status_filter,
                           search=search,
                           filter_date=date_filter,
                           statuses=APPOINTMENT_STATUSES)

@app.route('/doctor/appointment/<int:appointment_id>')
@doctor_required
def doctor_appointment_detail(appointment_id):
    """
    Appointment detail with basic patient contact details only.
    No medical records or diagnosis are exposed here; the doctor's own
    prescription for this appointment is linked separately.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT a.*, d.name AS doctor_name, d.specialization,
               p.name AS patient_name, p.email AS patient_email,
               p.phone AS patient_phone, p.age AS patient_age,
               p.gender AS patient_gender, p.address AS patient_address,
               p.emergency_contact_phone AS patient_emergency_phone,
               p.emergency_contact_name AS patient_emergency_contact,
               dp.name AS department_name
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Department dp ON a.department_id = dp.department_id
        WHERE a.appointment_id = %s AND a.doctor_id = %s
    ''', (appointment_id, session['id']))
    appointment = cursor.fetchone()

    if not appointment:
        flash('Appointment not found!', 'danger')
        cursor.close()
        return redirect(url_for('doctor_appointments'))

    # This patient's history with this doctor only.
    cursor.execute('''
        SELECT * FROM Appointment
        WHERE patient_id = %s AND doctor_id = %s
        ORDER BY date DESC, time DESC
    ''', (appointment['patient_id'], session['id']))
    history = cursor.fetchall()

    # Prescription shortcut for this appointment.
    cursor.execute('SELECT prescription_id FROM Prescription WHERE appointment_id = %s AND doctor_id = %s',
                   (appointment_id, session['id']))
    prescription_row = cursor.fetchone()

    cursor.close()

    appointment['time_formatted'] = get_12hr_time(appointment['time'])
    for row in history:
        row['time_formatted'] = get_12hr_time(row['time'])

    return render_template('doctor/appointment_detail.html',
                           appointment=appointment,
                           history=history,
                           prescription_id=prescription_row['prescription_id'] if prescription_row else None)

@app.route('/doctor/cancel/<int:appointment_id>')
@doctor_required
def doctor_cancel_appointment(appointment_id):
    """
    Doctors can cancel their own pending or confirmed appointments.
    Completed appointments are excluded so the slot stays closed.
    """
    cursor = mysql.connection.cursor()
    cursor.execute('''
        UPDATE Appointment SET status = 'Cancelled'
        WHERE appointment_id = %s AND doctor_id = %s AND status IN ('Pending', 'Approved')
    ''', (appointment_id, session['id']))
    appointment_cancelled = cursor.rowcount > 0
    if appointment_cancelled:
        # Free the queue seat before the appointment change commits.
        cancel_appointment_queue_entries(appointment_id)
    mysql.connection.commit()
    cursor.close()

    if appointment_cancelled:
        notify_appointment_event(appointment_id, 'cancelled', 'Cancelled by the doctor. ')
        log_audit('CANCEL_APPOINTMENT', 'Appointment', appointment_id,
                  'Doctor cancelled the appointment')
        email_sent = send_appointment_cancelled_email(appointment_id)
        if email_sent:
            flash('Appointment cancelled successfully! A confirmation email has been sent.', 'success')
        else:
            flash('Appointment cancelled, but the confirmation email could not be sent.', 'warning')
    else:
        flash('Could not cancel appointment. Completed appointments cannot be cancelled.', 'warning')

    return redirect(url_for('doctor_appointments'))

""" @app.route('/doctor/approve/<int:appointment_id>')
@doctor_required
def approve_appointment(appointment_id):
    cursor = mysql.connection.cursor()
    cursor.execute(
        "UPDATE Appointment SET status = 'Approved' WHERE appointment_id = %s AND doctor_id = %s",
        (appointment_id, session['id'])
    )
    mysql.connection.commit()
    cursor.close()
    flash('Appointment approved!', 'success')
    return redirect(url_for('doctor_dashboard')) """


@app.route('/doctor/approve/<int:appointment_id>')
@doctor_required
def approve_appointment(appointment_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    try:
        cursor.execute(
            """
            UPDATE Appointment
            SET status = 'Approved'
            WHERE appointment_id = %s
              AND doctor_id = %s
            """,
            (appointment_id, session['id'])
        )

        appointment_updated = cursor.rowcount > 0
        appointment = None
        if appointment_updated:
            # Approval satisfied -> the patient earns a queue number for
            # that day's session (same transaction, committed below).
            cursor.execute(
                'SELECT appointment_id, doctor_id, date, patient_id '
                'FROM Appointment WHERE appointment_id = %s',
                (appointment_id,)
            )
            appointment = cursor.fetchone()
            try:
                ensure_appointment_queue_entry(
                    appointment['doctor_id'], appointment['date'],
                    appointment['appointment_id'], appointment['patient_id'])
            except Exception as queue_error:
                mysql.connection.rollback()
                # Approval itself stays primary; the queue page sync
                # repairs a missing entry later.
                print(f'❌ Queue assignment error: {queue_error}')
                cursor.execute(
                    """
                    UPDATE Appointment
                    SET status = 'Approved'
                    WHERE appointment_id = %s
                      AND doctor_id = %s
                    """,
                    (appointment_id, session['id'])
                )
                appointment_updated = cursor.rowcount > 0

        mysql.connection.commit()

    except Exception as e:
        mysql.connection.rollback()
        print(f"❌ Appointment approval error: {e}")
        flash('Unable to approve appointment.', 'danger')
        return redirect(url_for('doctor_dashboard'))

    finally:
        cursor.close()

    if not appointment_updated:
        flash('Appointment not found or already unavailable.', 'warning')
        return redirect(url_for('doctor_dashboard'))

    log_audit('APPROVE_APPOINTMENT', 'Appointment', appointment_id,
              'Doctor confirmed the appointment')
    notify_appointment_event(appointment_id, 'approved')

    # Send the confirmation email after the database update succeeds.
    email_sent = send_appointment_accepted_email(appointment_id)

    if email_sent:
        flash('Appointment approved and confirmation email sent.', 'success')
    else:
        flash(
            'Appointment approved, but the confirmation email could not be sent.',
            'warning'
        )

    return redirect(url_for('doctor_dashboard'))

@app.route('/doctor/reject/<int:appointment_id>')
@doctor_required
def reject_appointment(appointment_id):
    cursor = mysql.connection.cursor()
    cursor.execute(
        '''
        UPDATE Appointment SET status = 'Rejected'
        WHERE appointment_id = %s AND doctor_id = %s AND status = 'Pending'
        ''',
        (appointment_id, session['id'])
    )
    mysql.connection.commit()
    rejected = cursor.rowcount > 0
    cursor.close()

    if not rejected:
        flash('Only pending appointments can be rejected.', 'warning')
        return redirect(url_for('doctor_dashboard'))

    # Releasing the slot is handled by the generated slot_key column: a
    # rejected appointment drops out of the unique slot index.
    notify_appointment_event(appointment_id, 'rejected', 'The doctor could not take this visit. ')
    log_audit('REJECT_APPOINTMENT', 'Appointment', appointment_id,
              'Doctor rejected the appointment')
    flash('Appointment rejected!', 'info')
    return redirect(url_for('doctor_dashboard'))

@app.route('/doctor/schedule', methods=['GET', 'POST'])
@doctor_required
def manage_schedule():
    if request.method == 'POST':
        available_slots = request.form['available_slots']
        cursor = mysql.connection.cursor()
        cursor.execute(
            'UPDATE Doctor SET available_slots = %s WHERE doctor_id = %s',
            (available_slots, session['id'])
        )
        mysql.connection.commit()
        cursor.close()
        log_audit('UPDATE_SCHEDULE_TEXT', 'Doctor', session['id'],
                  'Doctor updated their availability summary text')
        flash('Schedule updated successfully!', 'success')
        return redirect(url_for('manage_schedule'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (session['id'],))
    doctor = cursor.fetchone()
    cursor.close()

    return render_template('doctor/schedule.html',
                           doctor=doctor,
                           schedules=get_doctor_schedules(session['id']),
                           day_names=SCHEDULE_DAY_NAMES)

@app.route('/doctor/schedule/add', methods=['POST'])
@doctor_required
def doctor_schedule_add():
    """Adds one working window (day + start + end + slot duration)."""
    day_of_week = request.form.get('day_of_week')
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')

    try:
        day_of_week = int(day_of_week)
        duration = int(request.form.get('slot_duration', 30))
    except (TypeError, ValueError):
        flash('Invalid schedule values.', 'danger')
        return redirect(url_for('manage_schedule'))

    if day_of_week not in SCHEDULE_DAY_NAMES:
        flash('Please choose a valid working day.', 'warning')
        return redirect(url_for('manage_schedule'))

    if not start_time or not end_time or start_time >= end_time:
        flash('End time must be later than start time.', 'warning')
        return redirect(url_for('manage_schedule'))

    if duration < 5 or duration > 240:
        flash('Slot duration must be between 5 and 240 minutes.', 'warning')
        return redirect(url_for('manage_schedule'))

    cursor = mysql.connection.cursor()
    try:
        cursor.execute('''
            INSERT INTO DoctorSchedule (doctor_id, day_of_week, start_time, end_time, slot_duration)
            VALUES (%s, %s, %s, %s, %s)
        ''', (session['id'], day_of_week, start_time, end_time, duration))
        mysql.connection.commit()
        cursor.close()
        log_audit('CREATE_SCHEDULE', 'DoctorSchedule', None,
                  f"Added {SCHEDULE_DAY_NAMES[day_of_week]} {get_12hr_time(start_time)} - {get_12hr_time(end_time)}")
        flash('Availability added.', 'success')
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        if 'Duplicate' in str(e):
            flash('That window already exists for this day.', 'warning')
        else:
            flash('Could not add availability.', 'danger')

    return redirect(url_for('manage_schedule'))

@app.route('/doctor/schedule/<int:schedule_id>/update', methods=['POST'])
@doctor_required
def doctor_schedule_update(schedule_id):
    """Updates the hours and slot duration of one existing window."""
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')

    if not start_time or not end_time or start_time >= end_time:
        flash('End time must be later than start time.', 'warning')
        return redirect(url_for('manage_schedule'))

    try:
        duration = int(request.form.get('slot_duration', 30))
    except (TypeError, ValueError):
        flash('Invalid slot duration.', 'danger')
        return redirect(url_for('manage_schedule'))

    if duration < 5 or duration > 240:
        flash('Slot duration must be between 5 and 240 minutes.', 'warning')
        return redirect(url_for('manage_schedule'))

    cursor = mysql.connection.cursor()
    try:
        cursor.execute('''
            UPDATE DoctorSchedule
            SET start_time = %s, end_time = %s, slot_duration = %s
            WHERE schedule_id = %s AND doctor_id = %s
        ''', (start_time, end_time, duration, schedule_id, session['id']))
        mysql.connection.commit()
        updated = cursor.rowcount > 0
        cursor.close()

        if updated:
            log_audit('UPDATE_SCHEDULE', 'DoctorSchedule', schedule_id,
                      f"Window now {get_12hr_time(start_time)} - {get_12hr_time(end_time)} ({duration} min slots)")
            flash('Availability updated.', 'success')
        else:
            flash('Availability not found.', 'warning')
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        flash('Could not update availability.', 'danger')

    return redirect(url_for('manage_schedule'))

@app.route('/doctor/schedule/<int:schedule_id>/delete', methods=['POST'])
@doctor_required
def doctor_schedule_delete(schedule_id):
    """Removes one working window from the doctor's schedule."""
    cursor = mysql.connection.cursor()
    cursor.execute(
        'DELETE FROM DoctorSchedule WHERE schedule_id = %s AND doctor_id = %s',
        (schedule_id, session['id'])
    )
    mysql.connection.commit()
    deleted = cursor.rowcount > 0
    cursor.close()

    if deleted:
        log_audit('DELETE_SCHEDULE', 'DoctorSchedule', schedule_id,
                  'Doctor removed an availability window')
        flash('Availability removed.', 'success')
    else:
        flash('Availability not found.', 'warning')

    return redirect(url_for('manage_schedule'))

# ========== ADMIN ROUTES ==========
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # --- Counts required by the spec: totals + per-status breakdown ---
    def scalar(sql, params=()):
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return list(row.values())[0] if row else 0

    total_patients = scalar('SELECT COUNT(*) AS total FROM Patient')
    total_doctors = scalar('SELECT COUNT(*) AS total FROM Doctor')
    total_departments = scalar('SELECT COUNT(*) AS total FROM Department')
    today_appointments = scalar(
        'SELECT COUNT(*) AS total FROM Appointment WHERE date = %s',
        (local_now().date(),)
    )

    status_counts = appointment_status_counts()

    # 1. Total Appointments & Revenue (Assuming 500 INR per completed appointment)
    cursor.execute("SELECT COUNT(*) as total FROM Appointment WHERE status = 'Completed'")
    completed = cursor.fetchone()['total']
    revenue = completed * 500

    cursor.execute("SELECT COUNT(*) as total FROM Appointment")
    total_appointments = cursor.fetchone()['total']

    # 2. Department Distribution (For Pie Chart)
    cursor.execute('''
        SELECT d.specialization, COUNT(a.appointment_id) as count
        FROM Appointment a
        JOIN Doctor d ON a.doctor_id = d.doctor_id
        GROUP BY d.specialization
        ORDER BY count DESC
    ''')
    dept_data = cursor.fetchall()
    dept_labels = [row['specialization'] for row in dept_data]
    dept_counts = [row['count'] for row in dept_data]

    # 3. Monthly Patient Growth (For Bar Chart)
    cursor.execute('''
        SELECT DATE_FORMAT(date, '%Y-%m') as month, COUNT(*) as count
        FROM Appointment
        GROUP BY month
        ORDER BY month ASC
    ''')
    monthly_data = cursor.fetchall()
    month_labels = [row['month'] for row in monthly_data]
    month_counts = [row['count'] for row in monthly_data]

    # 4. Busiest Doctors
    cursor.execute('''
        SELECT d.name, COUNT(a.appointment_id) as count
        FROM Appointment a
        JOIN Doctor d ON a.doctor_id = d.doctor_id
        GROUP BY d.doctor_id
        ORDER BY count DESC LIMIT 5
    ''')
    top_doctors = cursor.fetchall()

    # 5. Patient Feedback summary (shown as quick-action cards).
    cursor.execute('SELECT COUNT(*) AS total, COALESCE(AVG(rating), 0) AS average '
                   'FROM Feedback')
    feedback_summary = cursor.fetchone()
    total_feedback = feedback_summary['total']
    average_rating = round(float(feedback_summary['average']), 1)
    cursor.execute('SELECT COUNT(*) AS total FROM Feedback WHERE is_hidden = 1')
    hidden_feedback = cursor.fetchone()['total']

    cursor.close()

    return render_template('admin/dashboard.html',
                           total_appointments=total_appointments,
                           revenue=revenue,
                           dept_labels=dept_labels,
                           dept_counts=dept_counts,
                           month_labels=month_labels,
                           month_counts=month_counts,
                           top_doctors=top_doctors,
                           total_patients=total_patients,
                           total_doctors=total_doctors,
                           total_departments=total_departments,
                           today_appointments=today_appointments,
                           status_counts=status_counts,
                           total_feedback=total_feedback,
                           average_rating=average_rating,
                           hidden_feedback=hidden_feedback)

@app.route('/admin/doctors')
@admin_required
def admin_doctors():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    search = request.args.get('search', '').strip()
    department_id = request.args.get('department', '').strip()
    status_filter = request.args.get('status', '').strip()

    query = '''
        SELECT d.*, dp.name AS department_name
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE 1 = 1
    '''
    params = []

    if search:
        query += ' AND (d.name LIKE %s OR d.email LIKE %s OR d.specialization LIKE %s OR d.license_number LIKE %s)'
        params.extend([f'%{search}%'] * 4)

    if department_id:
        query += ' AND d.department_id = %s'
        params.append(department_id)

    if status_filter == 'active':
        query += ' AND d.is_active = 1'
    elif status_filter == 'inactive':
        query += ' AND d.is_active = 0'

    query += ' ORDER BY d.name'

    cursor.execute(query, tuple(params))
    doctors = cursor.fetchall()

    # Active / inactive totals for the summary strip.
    cursor.execute('SELECT is_active, COUNT(*) AS total FROM Doctor GROUP BY is_active')
    doctor_totals = {row['is_active']: row['total'] for row in cursor.fetchall()}

    cursor.close()

    return render_template('admin/doctors.html',
                           doctors=doctors,
                           departments=get_departments(active_only=False),
                           search=search,
                           selected_department=department_id,
                           status_filter=status_filter,
                           active_count=doctor_totals.get(1, 0),
                           inactive_count=doctor_totals.get(0, 0))

@app.route('/admin/doctors/add', methods=['GET', 'POST'])
@admin_required
def admin_add_doctor():
    """Creates a doctor account on the doctor's behalf."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        specialization = request.form.get('specialization', '').strip()
        experience = request.form.get('experience', '').strip()
        qualification = request.form.get('qualification', '').strip()
        license_number = request.form.get('license_number', '').strip()
        consultation_fee = request.form.get('consultation_fee', '').strip()
        profile_description = request.form.get('profile_description', '').strip()
        address = request.form.get('address', '').strip()
        department_id = request.form.get('department_id', '') or None
        availability_status = request.form.get('availability_status', 'Available')

        if not name or not email or not password:
            flash('Name, email and password are required!', 'danger')
            return render_template('admin/doctor_form.html',
                                   doctor=None,
                                   departments=get_departments(),
                                   form_data=request.form)

        if not re.match(r'[^@]+@[^@]+\.[^@]+', email):
            flash('Invalid email address!', 'warning')
            return render_template('admin/doctor_form.html',
                                   doctor=None,
                                   departments=get_departments(),
                                   form_data=request.form)

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT doctor_id FROM Doctor WHERE email = %s', (email,))
        if cursor.fetchone():
            cursor.close()
            flash('A doctor with that email already exists!', 'warning')
            return render_template('admin/doctor_form.html',
                                   doctor=None,
                                   departments=get_departments(),
                                   form_data=request.form)
        cursor.close()

        latitude, longitude = get_coordinates_from_address(address)

        cursor = mysql.connection.cursor()
        cursor.execute('''
            INSERT INTO Doctor
                (name, email, password, phone, specialization, experience,
                 qualification, license_number, consultation_fee,
                 profile_description, department_id, availability_status,
                 address, latitude, longitude, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
        ''', (name, email, generate_password_hash(password), phone, specialization,
              experience or None, qualification or None, license_number or None,
              consultation_fee or None, profile_description or None,
              department_id, availability_status, address, latitude, longitude))
        mysql.connection.commit()
        new_id = cursor.lastrowid
        cursor.close()

        log_audit('CREATE_DOCTOR', 'Doctor', new_id, f"Admin added doctor {name}")
        flash('Doctor added successfully!', 'success')
        return redirect(url_for('admin_doctors'))

    return render_template('admin/doctor_form.html',
                           doctor=None,
                           departments=get_departments(),
                           form_data=None)

@app.route('/admin/doctors/<int:doctor_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_doctor(doctor_id):
    """Edits any doctor's profile, including department and availability."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (doctor_id,))
    doctor = cursor.fetchone()
    cursor.close()

    if not doctor:
        flash('Doctor not found!', 'danger')
        return redirect(url_for('admin_doctors'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        specialization = request.form.get('specialization', '').strip()
        experience = request.form.get('experience', '').strip()
        qualification = request.form.get('qualification', '').strip()
        license_number = request.form.get('license_number', '').strip()
        consultation_fee = request.form.get('consultation_fee', '').strip()
        profile_description = request.form.get('profile_description', '').strip()
        address = request.form.get('address', '').strip()
        available_slots = request.form.get('available_slots', '')
        department_id = request.form.get('department_id', '') or None
        availability_status = request.form.get('availability_status', 'Available')
        is_active = 1 if request.form.get('is_active') else 0

        if not name or not email:
            flash('Name and email are required!', 'danger')
            return render_template('admin/doctor_form.html',
                                   doctor=doctor,
                                   departments=get_departments(active_only=False),
                                   form_data=request.form)

        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT doctor_id FROM Doctor WHERE email = %s AND doctor_id <> %s',
                       (email, doctor_id))
        if cursor.fetchone():
            cursor.close()
            flash('Another doctor already uses that email!', 'warning')
            return render_template('admin/doctor_form.html',
                                   doctor=doctor,
                                   departments=get_departments(active_only=False),
                                   form_data=request.form)
        cursor.close()

        latitude, longitude = get_coordinates_from_address(address)
        if latitude is None and doctor['latitude'] is not None:
            latitude = doctor['latitude']
            longitude = doctor['longitude']

        cursor = mysql.connection.cursor()
        cursor.execute('''
            UPDATE Doctor SET
                name=%s, email=%s, phone=%s, specialization=%s, experience=%s,
                qualification=%s, license_number=%s, consultation_fee=%s,
                profile_description=%s, department_id=%s, availability_status=%s,
                available_slots=%s, address=%s, latitude=%s, longitude=%s, is_active=%s
            WHERE doctor_id=%s
        ''', (name, email, phone, specialization, experience or None,
              qualification or None, license_number or None, consultation_fee or None,
              profile_description or None, department_id, availability_status,
              available_slots, address, latitude, longitude, is_active, doctor_id))
        mysql.connection.commit()
        cursor.close()

        log_audit('UPDATE_DOCTOR', 'Doctor', doctor_id, f"Admin updated doctor {name}")
        flash('Doctor updated successfully!', 'success')
        return redirect(url_for('admin_doctors'))

    return render_template('admin/doctor_form.html',
                           doctor=doctor,
                           departments=get_departments(active_only=False),
                           form_data=None)

@app.route('/admin/doctors/<int:doctor_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_doctor(doctor_id):
    """Activates or deactivates a doctor."""
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT is_active FROM Doctor WHERE doctor_id = %s', (doctor_id,))
    row = cursor.fetchone()
    cursor.close()

    if not row:
        flash('Doctor not found!', 'danger')
        return redirect(url_for('admin_doctors'))

    new_state = 0 if row['is_active'] else 1
    cursor = mysql.connection.cursor()
    cursor.execute('UPDATE Doctor SET is_active = %s WHERE doctor_id = %s',
                   (new_state, doctor_id))
    mysql.connection.commit()
    cursor.close()

    log_audit('TOGGLE_DOCTOR', 'Doctor', doctor_id,
              'Deactivated' if not new_state else 'Activated')
    flash('Doctor deactivated. They can no longer receive appointments.'
          if not new_state else 'Doctor activated.', 'success')
    return redirect(url_for('admin_doctors'))

@app.route('/admin/doctors/<int:doctor_id>/schedule', methods=['GET', 'POST'])
@admin_required
def admin_doctor_schedules(doctor_id):
    """Admin-side management of one doctor's working days and hours."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (doctor_id,))
    doctor = cursor.fetchone()
    cursor.close()

    if not doctor:
        flash('Doctor not found!', 'danger')
        return redirect(url_for('admin_doctors'))

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            try:
                day_of_week = int(request.form.get('day_of_week'))
                duration = int(request.form.get('slot_duration', 30))
            except (TypeError, ValueError):
                flash('Invalid schedule values.', 'danger')
                return redirect(url_for('admin_doctor_schedules', doctor_id=doctor_id))

            start_time = request.form.get('start_time')
            end_time = request.form.get('end_time')

            if day_of_week not in SCHEDULE_DAY_NAMES:
                flash('Please choose a valid working day.', 'warning')
            elif not start_time or not end_time or start_time >= end_time:
                flash('End time must be later than start time.', 'warning')
            elif duration < 5 or duration > 240:
                flash('Slot duration must be between 5 and 240 minutes.', 'warning')
            else:
                cursor = mysql.connection.cursor()
                try:
                    cursor.execute('''
                        INSERT INTO DoctorSchedule
                            (doctor_id, day_of_week, start_time, end_time, slot_duration)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (doctor_id, day_of_week, start_time, end_time, duration))
                    mysql.connection.commit()
                    cursor.close()
                    log_audit('CREATE_SCHEDULE', 'DoctorSchedule', None,
                              f"Admin added {SCHEDULE_DAY_NAMES[day_of_week]} "
                              f"{get_12hr_time(start_time)} - {get_12hr_time(end_time)} for doctor {doctor_id}")
                    flash('Availability added.', 'success')
                except Exception as e:
                    mysql.connection.rollback()
                    cursor.close()
                    flash('That window already exists for this day.'
                          if 'Duplicate' in str(e) else 'Could not add availability.', 'warning')

        elif action == 'update':
            schedule_id = request.form.get('schedule_id')
            start_time = request.form.get('start_time')
            end_time = request.form.get('end_time')
            try:
                duration = int(request.form.get('slot_duration', 30))
            except (TypeError, ValueError):
                duration = 30

            if not start_time or not end_time or start_time >= end_time:
                flash('End time must be later than start time.', 'warning')
            else:
                cursor = mysql.connection.cursor()
                cursor.execute('''
                    UPDATE DoctorSchedule SET start_time = %s, end_time = %s, slot_duration = %s
                    WHERE schedule_id = %s AND doctor_id = %s
                ''', (start_time, end_time, duration, schedule_id, doctor_id))
                mysql.connection.commit()
                updated = cursor.rowcount > 0
                cursor.close()

                if updated:
                    log_audit('UPDATE_SCHEDULE', 'DoctorSchedule', schedule_id,
                              f"Admin set {get_12hr_time(start_time)} - {get_12hr_time(end_time)} for doctor {doctor_id}")
                    flash('Availability updated.', 'success')
                else:
                    flash('Availability not found.', 'warning')

        elif action == 'delete':
            schedule_id = request.form.get('schedule_id')
            cursor = mysql.connection.cursor()
            cursor.execute(
                'DELETE FROM DoctorSchedule WHERE schedule_id = %s AND doctor_id = %s',
                (schedule_id, doctor_id)
            )
            mysql.connection.commit()
            deleted = cursor.rowcount > 0
            cursor.close()

            if deleted:
                log_audit('DELETE_SCHEDULE', 'DoctorSchedule', schedule_id,
                          f"Admin removed a window for doctor {doctor_id}")
                flash('Availability removed.', 'success')
            else:
                flash('Availability not found.', 'warning')

        return redirect(url_for('admin_doctor_schedules', doctor_id=doctor_id))

    return render_template('admin/doctor_schedules.html',
                           doctor=doctor,
                           schedules=get_doctor_schedules(doctor_id),
                           day_names=SCHEDULE_DAY_NAMES)

@app.route('/admin/patients')
@admin_required
def admin_patients():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    search = request.args.get('search', '').strip()
    status_filter = request.args.get('status', '').strip()

    query = 'SELECT * FROM Patient WHERE 1 = 1'
    params = []

    if search:
        query += ' AND (name LIKE %s OR email LIKE %s OR phone LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])

    if status_filter == 'active':
        query += ' AND is_active = 1'
    elif status_filter == 'inactive':
        query += ' AND is_active = 0'

    query += ' ORDER BY name'

    cursor.execute(query, tuple(params))
    patients = cursor.fetchall()

    cursor.execute('SELECT is_active, COUNT(*) AS total FROM Patient GROUP BY is_active')
    patient_totals = {row['is_active']: row['total'] for row in cursor.fetchall()}

    cursor.close()

    return render_template('admin/patients.html',
                           patients=patients,
                           search=search,
                           status_filter=status_filter,
                           active_count=patient_totals.get(1, 0),
                           inactive_count=patient_totals.get(0, 0))

@app.route('/admin/patients/<int:patient_id>')
@admin_required
def admin_patient_detail(patient_id):
    """Patient profile plus their appointment history."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('SELECT * FROM Patient WHERE patient_id = %s', (patient_id,))
    patient = cursor.fetchone()

    if not patient:
        cursor.close()
        flash('Patient not found!', 'danger')
        return redirect(url_for('admin_patients'))

    cursor.execute('''
        SELECT a.*, d.name AS doctor_name, d.specialization
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.patient_id = %s
        ORDER BY a.date DESC, a.time DESC
    ''', (patient_id,))
    appointments = cursor.fetchall()
    cursor.close()

    for appt in appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])

    return render_template('admin/patient_detail.html',
                           patient=patient,
                           appointments=appointments)

@app.route('/admin/patients/<int:patient_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_patient(patient_id):
    """Activates or deactivates a patient."""
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT is_active FROM Patient WHERE patient_id = %s', (patient_id,))
    row = cursor.fetchone()
    cursor.close()

    if not row:
        flash('Patient not found!', 'danger')
        return redirect(url_for('admin_patients'))

    new_state = 0 if row['is_active'] else 1
    cursor = mysql.connection.cursor()
    cursor.execute('UPDATE Patient SET is_active = %s WHERE patient_id = %s',
                   (new_state, patient_id))
    mysql.connection.commit()
    cursor.close()

    log_audit('TOGGLE_PATIENT', 'Patient', patient_id,
              'Deactivated' if not new_state else 'Activated')
    flash('Patient deactivated.' if not new_state else 'Patient activated.', 'success')
    return redirect(url_for('admin_patients'))

@app.route('/admin/reports')
@admin_required
def admin_reports():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT a.*, p.name as patient_name, d.name as doctor_name
        FROM Appointment a
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        ORDER BY a.date DESC, a.time DESC
    ''')
    appointments = cursor.fetchall()
    for appointment in appointments:
        appointment['time_formatted'] = get_12hr_time(appointment.get('time'))

    cursor.close()
    return render_template('admin/reports.html', appointments=appointments)

@app.route('/admin/appointments')
@admin_required
def admin_appointments():
    """
    Full appointment management: search plus filters for doctor, patient,
    department, date and status, with a cancel action per row.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    search = request.args.get('search', '').strip()
    doctor_id = request.args.get('doctor', '').strip()
    patient_id = request.args.get('patient', '').strip()
    department_id = request.args.get('department', '').strip()
    date_filter = request.args.get('date', '').strip()
    status_filter = request.args.get('status', '').strip()

    query = '''
        SELECT a.*, p.name AS patient_name, d.name AS doctor_name,
               d.specialization, dp.name AS department_name
        FROM Appointment a
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        LEFT JOIN Department dp ON a.department_id = dp.department_id
        WHERE 1 = 1
    '''
    params = []

    if search:
        query += ' AND (p.name LIKE %s OR d.name LIKE %s OR a.reason LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])

    if doctor_id:
        query += ' AND a.doctor_id = %s'
        params.append(doctor_id)

    if patient_id:
        query += ' AND a.patient_id = %s'
        params.append(patient_id)

    if department_id:
        query += ' AND a.department_id = %s'
        params.append(department_id)

    if date_filter:
        query += ' AND a.date = %s'
        params.append(date_filter)

    if status_filter in APPOINTMENT_STATUSES:
        query += ' AND a.status = %s'
        params.append(status_filter)

    query += ' ORDER BY a.date DESC, a.time DESC'

    cursor.execute(query, tuple(params))
    appointments = cursor.fetchall()

    cursor.execute('SELECT doctor_id, name FROM Doctor ORDER BY name')
    doctors = cursor.fetchall()

    cursor.execute('SELECT patient_id, name FROM Patient ORDER BY name')
    patients = cursor.fetchall()

    cursor.close()

    for appt in appointments:
        appt['time_formatted'] = get_12hr_time(appt['time'])

    return render_template('admin/appointments.html',
                           appointments=appointments,
                           doctors=doctors,
                           patients=patients,
                           departments=get_departments(active_only=False),
                           statuses=APPOINTMENT_STATUSES,
                           search=search,
                           doctor_filter=doctor_id,
                           patient_filter=patient_id,
                           department_filter=department_id,
                           date_filter=date_filter,
                           status_filter=status_filter)

@app.route('/admin/appointments/<int:appointment_id>/cancel', methods=['POST'])
@admin_required
def admin_cancel_appointment(appointment_id):
    """Admin cancel. Completed appointments are never cancelled."""
    cursor = mysql.connection.cursor()
    cursor.execute('''
        UPDATE Appointment SET status = 'Cancelled'
        WHERE appointment_id = %s AND status IN ('Pending', 'Approved')
    ''', (appointment_id,))
    appointment_cancelled = cursor.rowcount > 0
    if appointment_cancelled:
        # Free the queue seat before the appointment change commits.
        cancel_appointment_queue_entries(appointment_id)
    mysql.connection.commit()
    cursor.close()

    if appointment_cancelled:
        notify_appointment_event(appointment_id, 'cancelled', 'Cancelled by the hospital. ')
        log_audit('CANCEL_APPOINTMENT', 'Appointment', appointment_id, 'Admin cancelled the appointment')
        email_sent = send_appointment_cancelled_email(appointment_id)
        if email_sent:
            flash('Appointment cancelled successfully! A confirmation email has been sent.', 'success')
        else:
            flash('Appointment cancelled, but the confirmation email could not be sent.', 'warning')
    else:
        flash('Could not cancel appointment. Completed appointments cannot be cancelled.', 'warning')

    return redirect(url_for('admin_appointments'))

@app.route('/admin/departments')
@admin_required
def admin_departments():
    """Department CRUD plus the doctors assigned to each department."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT dp.*, COUNT(d.doctor_id) AS doctor_count
        FROM Department dp
        LEFT JOIN Doctor d ON d.department_id = dp.department_id
        GROUP BY dp.department_id, dp.name, dp.description, dp.is_active, dp.created_at
        ORDER BY dp.name
    ''')
    departments = cursor.fetchall()

    cursor.execute('''
        SELECT doctor_id, name, specialization, is_active, department_id
        FROM Doctor ORDER BY name
    ''')
    doctors = cursor.fetchall()

    cursor.close()

    doctors_by_department = {}
    for doctor in doctors:
        doctors_by_department.setdefault(doctor['department_id'], []).append(doctor)

    return render_template('admin/departments.html',
                           departments=departments,
                           doctors_by_department=doctors_by_department)

@app.route('/admin/departments/add', methods=['POST'])
@admin_required
def admin_add_department():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        flash('Department name is required!', 'danger')
        return redirect(url_for('admin_departments'))

    cursor = mysql.connection.cursor()
    try:
        cursor.execute('INSERT INTO Department (name, description) VALUES (%s, %s)',
                       (name, description or None))
        mysql.connection.commit()
        new_id = cursor.lastrowid
        cursor.close()
        log_audit('CREATE_DEPARTMENT', 'Department', new_id, f"Admin added department {name}")
        flash('Department added successfully!', 'success')
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        flash('A department with that name already exists.'
              if 'Duplicate' in str(e) else 'Could not add department.', 'warning')

    return redirect(url_for('admin_departments'))

@app.route('/admin/departments/<int:department_id>/edit', methods=['POST'])
@admin_required
def admin_edit_department(department_id):
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        flash('Department name is required!', 'danger')
        return redirect(url_for('admin_departments'))

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            'UPDATE Department SET name = %s, description = %s WHERE department_id = %s',
            (name, description or None, department_id)
        )
        mysql.connection.commit()
        cursor.close()
        log_audit('UPDATE_DEPARTMENT', 'Department', department_id, f"Admin renamed to {name}")
        flash('Department updated successfully!', 'success')
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        flash('A department with that name already exists.'
              if 'Duplicate' in str(e) else 'Could not update department.', 'warning')

    return redirect(url_for('admin_departments'))

@app.route('/admin/departments/<int:department_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_department(department_id):
    """
    Deactivates or reactivates a department. A deactivated department stops
    accepting new bookings; existing appointments are left untouched.
    """
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT is_active FROM Department WHERE department_id = %s', (department_id,))
    row = cursor.fetchone()
    cursor.close()

    if not row:
        flash('Department not found!', 'danger')
        return redirect(url_for('admin_departments'))

    new_state = 0 if row['is_active'] else 1
    cursor = mysql.connection.cursor()
    cursor.execute('UPDATE Department SET is_active = %s WHERE department_id = %s',
                   (new_state, department_id))
    mysql.connection.commit()
    cursor.close()

    log_audit('TOGGLE_DEPARTMENT', 'Department', department_id,
              'Deactivated' if not new_state else 'Activated')
    flash('Department deactivated. It can no longer receive appointments.'
          if not new_state else 'Department activated.', 'success')
    return redirect(url_for('admin_departments'))

@app.route('/admin/departments/<int:department_id>/delete', methods=['POST'])
@admin_required
def admin_delete_department(department_id):
    """
    Deletes a department. Doctors keep their profile but lose the link
    (the foreign key uses ON DELETE SET NULL).
    """
    cursor = mysql.connection.cursor()
    cursor.execute('SELECT name FROM Department WHERE department_id = %s', (department_id,))
    row = cursor.fetchone()
    cursor.close()

    if not row:
        flash('Department not found!', 'danger')
        return redirect(url_for('admin_departments'))

    cursor = mysql.connection.cursor()
    try:
        cursor.execute('DELETE FROM Department WHERE department_id = %s', (department_id,))
        mysql.connection.commit()
        cursor.close()
        log_audit('DELETE_DEPARTMENT', 'Department', department_id,
                  f"Admin deleted department {row['name']}")
        flash('Department deleted. Assigned doctors are now unassigned.', 'success')
    except Exception:
        mysql.connection.rollback()
        cursor.close()
        flash('Could not delete department.', 'danger')

    return redirect(url_for('admin_departments'))

@app.route('/admin/schedules')
@admin_required
def admin_schedules():
    """All doctor schedules in one place, with working days and hours."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT s.*, d.name AS doctor_name, d.is_active AS doctor_active
        FROM DoctorSchedule s
        JOIN Doctor d ON s.doctor_id = d.doctor_id
        ORDER BY d.name, s.day_of_week, s.start_time
    ''')
    schedules = cursor.fetchall()

    cursor.execute('''
        SELECT doctor_id, name, specialization, is_active
        FROM Doctor ORDER BY name
    ''')
    doctors = cursor.fetchall()

    cursor.close()

    return render_template('admin/schedules.html',
                           schedules=schedules,
                           doctors=doctors,
                           day_names=SCHEDULE_DAY_NAMES)

@app.route('/admin/schedules/add', methods=['POST'])
@admin_required
def admin_add_schedule():
    """Creates a schedule entry for any doctor."""
    try:
        doctor_id = int(request.form.get('doctor_id'))
        day_of_week = int(request.form.get('day_of_week'))
        duration = int(request.form.get('slot_duration', 30))
    except (TypeError, ValueError):
        flash('Invalid schedule values.', 'danger')
        return redirect(url_for('admin_schedules'))

    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')

    if day_of_week not in SCHEDULE_DAY_NAMES:
        flash('Please choose a valid working day.', 'warning')
    elif not start_time or not end_time or start_time >= end_time:
        flash('End time must be later than start time.', 'warning')
    elif duration < 5 or duration > 240:
        flash('Slot duration must be between 5 and 240 minutes.', 'warning')
    else:
        cursor = mysql.connection.cursor()
        cursor.execute('SELECT doctor_id FROM Doctor WHERE doctor_id = %s', (doctor_id,))
        if not cursor.fetchone():
            cursor.close()
            flash('Doctor not found!', 'danger')
            return redirect(url_for('admin_schedules'))

        try:
            cursor.execute('''
                INSERT INTO DoctorSchedule
                    (doctor_id, day_of_week, start_time, end_time, slot_duration)
                VALUES (%s, %s, %s, %s, %s)
            ''', (doctor_id, day_of_week, start_time, end_time, duration))
            mysql.connection.commit()
            cursor.close()
            log_audit('CREATE_SCHEDULE', 'DoctorSchedule', None,
                      f"Admin added {SCHEDULE_DAY_NAMES[day_of_week]} "
                      f"{get_12hr_time(start_time)} - {get_12hr_time(end_time)} for doctor {doctor_id}")
            flash('Schedule created successfully!', 'success')
        except Exception as e:
            mysql.connection.rollback()
            cursor.close()
            flash('That window already exists for this day.'
                  if 'Duplicate' in str(e) else 'Could not create schedule.', 'warning')

    return redirect(url_for('admin_schedules'))

@app.route('/admin/schedules/<int:schedule_id>/update', methods=['POST'])
@admin_required
def admin_update_schedule(schedule_id):
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')

    try:
        duration = int(request.form.get('slot_duration', 30))
    except (TypeError, ValueError):
        duration = 30

    if not start_time or not end_time or start_time >= end_time:
        flash('End time must be later than start time.', 'warning')
        return redirect(url_for('admin_schedules'))

    if duration < 5 or duration > 240:
        flash('Slot duration must be between 5 and 240 minutes.', 'warning')
        return redirect(url_for('admin_schedules'))

    cursor = mysql.connection.cursor()
    try:
        cursor.execute('''
            UPDATE DoctorSchedule SET start_time = %s, end_time = %s, slot_duration = %s
            WHERE schedule_id = %s
        ''', (start_time, end_time, duration, schedule_id))
        mysql.connection.commit()
        updated = cursor.rowcount > 0
        cursor.close()

        if updated:
            log_audit('UPDATE_SCHEDULE', 'DoctorSchedule', schedule_id,
                      f"Admin set {get_12hr_time(start_time)} - {get_12hr_time(end_time)} ({duration} min slots)")
            flash('Schedule updated successfully!', 'success')
        else:
            flash('Schedule not found.', 'warning')
    except Exception:
        mysql.connection.rollback()
        cursor.close()
        flash('Could not update schedule.', 'danger')

    return redirect(url_for('admin_schedules'))

@app.route('/admin/schedules/<int:schedule_id>/delete', methods=['POST'])
@admin_required
def admin_delete_schedule(schedule_id):
    cursor = mysql.connection.cursor()
    cursor.execute('DELETE FROM DoctorSchedule WHERE schedule_id = %s', (schedule_id,))
    mysql.connection.commit()
    deleted = cursor.rowcount > 0
    cursor.close()

    if deleted:
        log_audit('DELETE_SCHEDULE', 'DoctorSchedule', schedule_id, 'Admin deleted schedule')
        flash('Schedule deleted successfully!', 'success')
    else:
        flash('Schedule not found.', 'warning')

    return redirect(url_for('admin_schedules'))

@app.route('/admin/audit_logs')
@admin_required
def admin_audit_logs():
    """Audit trail: user, action, entity, entity id, description, date/time."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    action_filter = request.args.get('action', '').strip()
    entity_filter = request.args.get('entity', '').strip()
    user_type_filter = request.args.get('user_type', '').strip()
    search = request.args.get('search', '').strip()

    query = 'SELECT * FROM AuditLog WHERE 1 = 1'
    params = []

    if action_filter:
        query += ' AND action = %s'
        params.append(action_filter)

    if entity_filter:
        query += ' AND entity = %s'
        params.append(entity_filter)

    if user_type_filter in ('patient', 'doctor', 'admin'):
        query += ' AND user_type = %s'
        params.append(user_type_filter)

    if search:
        query += ' AND (user_name LIKE %s OR description LIKE %s OR action LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])

    query += ' ORDER BY audit_id DESC LIMIT 500'

    cursor.execute(query, tuple(params))
    logs = cursor.fetchall()

    # Distinct values so the filter dropdowns match what is actually recorded.
    cursor.execute('SELECT DISTINCT action FROM AuditLog ORDER BY action')
    actions = [row['action'] for row in cursor.fetchall()]

    cursor.execute('SELECT DISTINCT entity FROM AuditLog WHERE entity IS NOT NULL ORDER BY entity')
    entities = [row['entity'] for row in cursor.fetchall()]

    cursor.close()

    return render_template('admin/audit_logs.html',
                           logs=logs,
                           actions=actions,
                           entities=entities,
                           action_filter=action_filter,
                           entity_filter=entity_filter,
                           user_type_filter=user_type_filter,
                           search=search)

@app.route('/admin/profile', methods=['GET', 'POST'])
@admin_required
def admin_profile():
    """Admin profile view. No public admin registration is offered."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Admin WHERE admin_id = %s', (session['id'],))
    admin = cursor.fetchone()

    if not admin:
        cursor.close()
        flash('Admin account not found!', 'danger')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'password':
            old_password = request.form.get('old_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not check_password_hash(admin['password'], old_password):
                flash('Old password is incorrect!', 'danger')
            elif new_password != confirm_password:
                flash('New passwords do not match!', 'warning')
            elif len(new_password) < 6:
                flash('Password must be at least 6 characters long!', 'warning')
            else:
                cursor = mysql.connection.cursor()
                cursor.execute('UPDATE Admin SET password = %s WHERE admin_id = %s',
                               (generate_password_hash(new_password), session['id']))
                mysql.connection.commit()
                cursor.close()
                log_audit('CHANGE_PASSWORD', 'Admin', session['id'], 'Admin changed their password')
                flash('Password changed successfully!', 'success')
        else:
            name = request.form.get('name', '').strip()
            if not name:
                flash('Name is required!', 'warning')
            else:
                cursor = mysql.connection.cursor()
                cursor.execute('UPDATE Admin SET name = %s WHERE admin_id = %s',
                               (name, session['id']))
                mysql.connection.commit()
                cursor.close()
                session['name'] = name
                log_audit('UPDATE_ADMIN_PROFILE', 'Admin', session['id'], 'Admin updated their profile')
                flash('Profile updated successfully!', 'success')

        return redirect(url_for('admin_profile'))

    cursor.close()
    return render_template('admin/profile.html', admin=admin)


# ========== ADMIN: HOSPITAL INFORMATION ==========
@app.route('/admin/hospital_info', methods=['GET', 'POST'])
@admin_required
def admin_hospital_info():
    """Lets the admin maintain the hospital's public details."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()

        if not name:
            flash('Hospital name is required!', 'danger')
            return redirect(url_for('admin_hospital_info'))

        values = (
            name,
            request.form.get('tagline', '').strip() or None,
            request.form.get('address', '').strip() or None,
            request.form.get('phone', '').strip() or None,
            request.form.get('emergency_phone', '').strip() or None,
            request.form.get('email', '').strip() or None,
            request.form.get('opening_hours', '').strip() or None,
            request.form.get('about', '').strip() or None,
        )

        current = get_hospital_info()
        cursor = mysql.connection.cursor()
        if current.get('info_id'):
            cursor.execute('''
                UPDATE HospitalInfo SET
                    name=%s, tagline=%s, address=%s, phone=%s, emergency_phone=%s,
                    email=%s, opening_hours=%s, about=%s
                WHERE info_id=%s
            ''', values + (current['info_id'],))
            entity_id = current['info_id']
        else:
            cursor.execute('''
                INSERT INTO HospitalInfo
                    (name, tagline, address, phone, emergency_phone, email,
                     opening_hours, about)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', values)
            entity_id = cursor.lastrowid
        mysql.connection.commit()
        cursor.close()

        log_audit('UPDATE_HOSPITAL_INFO', 'HospitalInfo', entity_id,
                  'Admin updated hospital information')
        flash('Hospital information updated successfully!', 'success')
        return redirect(url_for('admin_hospital_info'))

    return render_template('admin/hospital_info.html',
                           hospital=get_hospital_info())


# ========== ADMIN: NOTIFICATIONS / REMINDER MONITORING ==========
@app.route('/admin/notifications')
@admin_required
def admin_notifications():
    """Read-only view of every in-app notification plus reminder health."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    user_type_filter = request.args.get('user_type', '').strip()
    read_filter = request.args.get('read', '').strip()
    search = request.args.get('search', '').strip()

    query = 'SELECT * FROM Notification WHERE 1 = 1'
    params = []

    if user_type_filter in ('patient', 'doctor', 'admin'):
        query += ' AND user_type = %s'
        params.append(user_type_filter)

    if read_filter == 'read':
        query += ' AND is_read = 1'
    elif read_filter == 'unread':
        query += ' AND is_read = 0'

    if search:
        query += ' AND (title LIKE %s OR message LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%'])

    query += ' ORDER BY notification_id DESC LIMIT 500'

    notifications = []
    type_totals = {}
    unread_total = 0
    try:
        cursor.execute(query, tuple(params))
        notifications = cursor.fetchall()

        cursor.execute('SELECT user_type, COUNT(*) AS total FROM Notification GROUP BY user_type')
        type_totals = {row['user_type']: row['total'] for row in cursor.fetchall()}

        cursor.execute('SELECT COUNT(*) AS total FROM Notification WHERE is_read = 0')
        unread_total = cursor.fetchone()['total']
    except Exception as e:
        print(f"Notifications unavailable: {e}")

    def safe_count(sql):
        try:
            cursor.execute(sql)
            return cursor.fetchone()['total']
        except Exception as e:
            print(f"Admin reminder count unavailable: {e}")
            return 0

    reminder_pending = safe_count("""
        SELECT COUNT(*) AS total FROM Appointment
        WHERE status IN ('Pending', 'Approved')
          AND date >= CURDATE()
          AND (reminder_sent = 0 OR reminder_sent IS NULL)
    """)
    follow_up_pending = safe_count("""
        SELECT COUNT(*) AS total FROM Appointment
        WHERE follow_up_date IS NOT NULL
          AND follow_up_date >= CURDATE()
          AND (follow_up_reminder_sent = 0 OR follow_up_reminder_sent IS NULL)
    """)
    follow_up_total = safe_count("""
        SELECT COUNT(*) AS total FROM Appointment
        WHERE follow_up_date IS NOT NULL
    """)

    cursor.close()

    return render_template('admin/notifications.html',
                           notifications=notifications,
                           type_totals=type_totals,
                           unread_total=unread_total,
                           total_notifications=sum(type_totals.values()),
                           reminder_pending=reminder_pending,
                           follow_up_pending=follow_up_pending,
                           follow_up_total=follow_up_total,
                           user_type_filter=user_type_filter,
                           read_filter=read_filter,
                           search=search)


# ========== ADMIN: FOLLOW-UP APPOINTMENTS ==========
@app.route('/admin/follow_ups')
@admin_required
def admin_follow_ups():
    """Every appointment a doctor has flagged for follow-up."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    search = request.args.get('search', '').strip()
    window = request.args.get('window', 'all').strip()

    query = '''
        SELECT a.appointment_id, a.follow_up_date, a.follow_up_reminder_sent,
               a.date AS visit_date, a.time AS visit_time, a.status,
               p.name AS patient_name, p.phone AS patient_phone,
               d.name AS doctor_name, d.specialization
        FROM Appointment a
        LEFT JOIN Patient p ON a.patient_id = p.patient_id
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        WHERE a.follow_up_date IS NOT NULL
    '''
    params = []

    if search:
        query += ' AND (p.name LIKE %s OR d.name LIKE %s)'
        params.extend([f'%{search}%', f'%{search}%'])

    if window == 'upcoming':
        query += ' AND a.follow_up_date >= CURDATE()'
    elif window == 'past':
        query += ' AND a.follow_up_date < CURDATE()'

    query += ' ORDER BY a.follow_up_date DESC, a.appointment_id DESC'

    follow_ups = []
    try:
        cursor.execute(query, tuple(params))
        follow_ups = cursor.fetchall()
    except Exception as e:
        print(f"Follow-ups unavailable: {e}")

    for follow_up in follow_ups:
        follow_up['visit_time_formatted'] = get_12hr_time(follow_up['visit_time'])

    cursor.close()

    return render_template('admin/follow_ups.html',
                           follow_ups=follow_ups,
                           search=search,
                           window=window)


# ========== ADMIN: ADMIN ACCOUNT MANAGEMENT ==========
@app.route('/admin/admins')
@admin_required
def admin_admins():
    """Lists administrator accounts so more admins can be added or disabled."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute('''
            SELECT admin_id, name, email, is_active, created_at
            FROM Admin ORDER BY name
        ''')
        admins = cursor.fetchall()
    except Exception as e:
        # is_active column not migrated yet: still list the accounts.
        print(f"Admin is_active unavailable: {e}")
        cursor.execute('SELECT admin_id, name, email, created_at FROM Admin ORDER BY name')
        admins = cursor.fetchall()
        for admin in admins:
            admin['is_active'] = 1
    cursor.close()

    return render_template('admin/admins.html',
                           admins=admins,
                           current_admin_id=session.get('id'))


@app.route('/admin/admins/add', methods=['POST'])
@admin_required
def admin_add_admin():
    """Creates a new administrator account."""
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')

    if not name or not email or not password:
        flash('Name, email and password are all required!', 'danger')
        return redirect(url_for('admin_admins'))

    if not re.match(r'[^@]+@[^@]+\.[^@]+', email):
        flash('Invalid email address!', 'warning')
        return redirect(url_for('admin_admins'))

    if len(password) < 6:
        flash('Password must be at least 6 characters long!', 'warning')
        return redirect(url_for('admin_admins'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT admin_id FROM Admin WHERE email = %s', (email,))
    if cursor.fetchone():
        cursor.close()
        flash('An account with that email already exists!', 'warning')
        return redirect(url_for('admin_admins'))
    cursor.close()

    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            'INSERT INTO Admin (name, email, password) VALUES (%s, %s, %s)',
            (name, email, generate_password_hash(password))
        )
        mysql.connection.commit()
        new_id = cursor.lastrowid
        cursor.close()
        log_audit('CREATE_ADMIN', 'Admin', new_id, f"Admin created account {email}")
        flash('Administrator account created successfully!', 'success')
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        flash('An account with that email already exists.'
              if 'Duplicate' in str(e) else 'Could not create administrator account.',
              'warning')

    return redirect(url_for('admin_admins'))


@app.route('/admin/admins/<int:admin_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_admin(admin_id):
    """Enables or disables another administrator account."""
    if admin_id == session.get('id'):
        flash('You cannot deactivate your own account!', 'warning')
        return redirect(url_for('admin_admins'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT name, is_active FROM Admin WHERE admin_id = %s', (admin_id,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        flash('Administrator not found!', 'danger')
        return redirect(url_for('admin_admins'))

    new_state = 0 if row['is_active'] else 1

    if not new_state:
        cursor.execute('SELECT COUNT(*) AS total FROM Admin WHERE is_active = 1')
        if cursor.fetchone()['total'] <= 1:
            cursor.close()
            flash('At least one active administrator must remain.', 'warning')
            return redirect(url_for('admin_admins'))

    cursor.execute('UPDATE Admin SET is_active = %s WHERE admin_id = %s', (new_state, admin_id))
    mysql.connection.commit()
    cursor.close()

    log_audit('TOGGLE_ADMIN', 'Admin', admin_id,
              'Deactivated' if not new_state else 'Activated')
    flash('Administrator deactivated.' if not new_state
          else 'Administrator activated.', 'success')
    return redirect(url_for('admin_admins'))


# ========== ADMIN: EMAIL SETTINGS ==========
@app.route('/admin/email_settings')
@admin_required
def admin_email_settings():
    """Read-only view of the environment-driven email reminder configuration."""
    settings = {
        'brevo_configured': bool(os.environ.get('BREVO_API_KEY')),
        'sender_configured': bool(os.environ.get('EMAIL_FROM')),
        'sender_address': os.environ.get('EMAIL_FROM', ''),
        'smtp_configured': bool(app.config.get('MAIL_USERNAME')
                                and app.config.get('MAIL_PASSWORD')),
        'reminder_time': 'Every minute (appointment reminders fire 30 minutes ahead)',
        'follow_up_window': 'Every minute (sent on the follow-up date)',
        'scheduler_enabled': os.environ.get('DISABLE_SCHEDULER', '').strip()
                             not in ('1', 'true', 'True', 'yes'),
    }

    return render_template('admin/email_settings.html', settings=settings)


# ========== PATIENT FEEDBACK & RATINGS ==========
FEEDBACK_RATING_LABELS = {
    1: 'Poor',
    2: 'Fair',
    3: 'Good',
    4: 'Very Good',
    5: 'Excellent',
}
MAX_FEEDBACK_COMMENT_LENGTH = 1000


def clean_feedback_comment(raw):
    """Normalise submitted feedback text. Returns (comment, error)."""
    comment = (raw or '').strip()
    if len(comment) > MAX_FEEDBACK_COMMENT_LENGTH:
        return '', (f'Written feedback must be '
                    f'{MAX_FEEDBACK_COMMENT_LENGTH} characters or fewer.')
    return comment, None


@app.route('/patient/feedback')
@patient_required
def patient_feedback():
    """Feedback hub: leave a review for a completed appointment, or manage
    reviews already given."""
    preselect = request.args.get('appointment_id', type=int)

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Completed appointments that do not have a review yet.
    cursor.execute('''
        SELECT a.appointment_id, a.date, a.time,
               d.name AS doctor_name, d.specialization
        FROM Appointment a
        LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id
        LEFT JOIN Feedback f ON f.appointment_id = a.appointment_id
        WHERE a.patient_id = %s
          AND a.status = 'Completed'
          AND f.feedback_id IS NULL
        ORDER BY a.date DESC, a.time DESC
    ''', (session['id'],))
    eligible = cursor.fetchall()

    # Reviews this patient has already submitted (editable until hidden).
    cursor.execute('''
        SELECT f.feedback_id, f.appointment_id, f.rating, f.comment,
               f.is_hidden, f.created_at, f.updated_at,
               a.date AS appointment_date, a.time AS appointment_time,
               d.name AS doctor_name
        FROM Feedback f
        JOIN Appointment a ON a.appointment_id = f.appointment_id
        LEFT JOIN Doctor d ON d.doctor_id = f.doctor_id
        WHERE f.patient_id = %s
        ORDER BY f.created_at DESC, f.feedback_id DESC
    ''', (session['id'],))
    history = cursor.fetchall()
    cursor.close()

    for row in history:
        row['appointment_time_formatted'] = get_12hr_time(row['appointment_time'])

    eligible_ids = {row['appointment_id'] for row in eligible}
    return render_template(
        'patient/feedback.html',
        eligible_appointments=eligible,
        feedback_history=history,
        rating_labels=FEEDBACK_RATING_LABELS,
        max_comment_length=MAX_FEEDBACK_COMMENT_LENGTH,
        preselect_appointment_id=preselect if preselect in eligible_ids else None,
    )


@app.route('/patient/feedback/submit', methods=['POST'])
@patient_required
def submit_feedback():
    appointment_id = request.form.get('appointment_id', type=int)
    rating = request.form.get('rating', type=int)
    comment, comment_error = clean_feedback_comment(request.form.get('comment'))

    if not appointment_id:
        flash('Please choose the appointment you want to review.', 'warning')
        return redirect(url_for('patient_feedback'))
    if rating not in FEEDBACK_RATING_LABELS:
        flash('Please select a rating between 1 and 5 stars.', 'warning')
        return redirect(url_for('patient_feedback', appointment_id=appointment_id))
    if comment_error:
        flash(comment_error, 'warning')
        return redirect(url_for('patient_feedback', appointment_id=appointment_id))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Ownership and status are re-verified on the server; nothing that the
    # browser submits is trusted on its own.
    cursor.execute('''
        SELECT a.appointment_id, a.doctor_id, a.status
        FROM Appointment a
        WHERE a.appointment_id = %s AND a.patient_id = %s
    ''', (appointment_id, session['id']))
    appointment = cursor.fetchone()

    if not appointment:
        cursor.close()
        flash('Appointment not found.', 'danger')
        return redirect(url_for('patient_feedback'))

    if appointment['status'] != 'Completed':
        cursor.close()
        flash('Feedback can only be submitted for completed appointments.',
              'warning')
        return redirect(url_for('patient_feedback'))

    cursor.execute('SELECT feedback_id FROM Feedback WHERE appointment_id = %s',
                   (appointment_id,))
    if cursor.fetchone():
        cursor.close()
        flash('You have already shared feedback for this appointment.',
              'warning')
        return redirect(url_for('patient_feedback'))

    try:
        cursor.execute('''
            INSERT INTO Feedback (appointment_id, patient_id, doctor_id,
                                  rating, comment)
            VALUES (%s, %s, %s, %s, %s)
        ''', (appointment_id, session['id'], appointment['doctor_id'],
              rating, comment or None))
        mysql.connection.commit()
        feedback_id = cursor.lastrowid
    except Exception as e:
        mysql.connection.rollback()
        cursor.close()
        if 'Duplicate' in str(e):
            flash('You have already shared feedback for this appointment.',
                  'warning')
        else:
            print(f"❌ Feedback insert error: {e}")
            flash('Unable to save your feedback. Please try again.', 'danger')
        return redirect(url_for('patient_feedback'))

    cursor.close()
    log_audit('SUBMIT_FEEDBACK', 'Feedback', feedback_id,
              f"Patient gave {rating}-star feedback for appointment {appointment_id}")
    flash('Thank you for sharing your feedback!', 'success')
    return redirect(url_for('patient_feedback'))


@app.route('/patient/feedback/<int:feedback_id>/update', methods=['POST'])
@patient_required
def update_feedback(feedback_id):
    rating = request.form.get('rating', type=int)
    comment, comment_error = clean_feedback_comment(request.form.get('comment'))

    if rating not in FEEDBACK_RATING_LABELS:
        flash('Please select a rating between 1 and 5 stars.', 'warning')
        return redirect(url_for('patient_feedback'))
    if comment_error:
        flash(comment_error, 'warning')
        return redirect(url_for('patient_feedback'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('''
        SELECT feedback_id, appointment_id
        FROM Feedback
        WHERE feedback_id = %s AND patient_id = %s AND is_hidden = 0
    ''', (feedback_id, session['id']))
    existing = cursor.fetchone()

    if not existing:
        cursor.close()
        flash('Feedback not found or can no longer be edited.', 'danger')
        return redirect(url_for('patient_feedback'))

    cursor.execute('''
        UPDATE Feedback
        SET rating = %s, comment = %s
        WHERE feedback_id = %s AND patient_id = %s
    ''', (rating, comment or None, feedback_id, session['id']))
    mysql.connection.commit()
    cursor.close()

    log_audit('UPDATE_FEEDBACK', 'Feedback', feedback_id,
              f"Patient updated feedback for appointment {existing['appointment_id']}")
    flash('Your feedback has been updated.', 'success')
    return redirect(url_for('patient_feedback'))


@app.route('/doctor/feedback')
@doctor_required
def doctor_feedback():
    """Feedback received by the logged-in doctor with live statistics."""
    sort = request.args.get('sort', 'newest')
    sort = 'oldest' if sort == 'oldest' else 'newest'
    try:
        rating_filter = int(request.args.get('rating', ''))
    except (TypeError, ValueError):
        rating_filter = None
    if rating_filter not in FEEDBACK_RATING_LABELS:
        rating_filter = None

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    where = 'f.doctor_id = %s AND f.is_hidden = 0'
    params = [session['id']]
    if rating_filter:
        where += ' AND f.rating = %s'
        params.append(rating_filter)

    order = 'ASC' if sort == 'oldest' else 'DESC'
    cursor.execute(f'''
        SELECT f.feedback_id, f.appointment_id, f.rating, f.comment,
               f.created_at, f.updated_at,
               a.date AS appointment_date, a.time AS appointment_time,
               p.name AS patient_name
        FROM Feedback f
        JOIN Appointment a ON a.appointment_id = f.appointment_id
        LEFT JOIN Patient p ON p.patient_id = f.patient_id
        WHERE {where}
        ORDER BY f.created_at {order}, f.feedback_id {order}
    ''', tuple(params))
    feedback_list = cursor.fetchall()

    # Statistics are computed from the database, never hardcoded.
    cursor.execute('''
        SELECT COUNT(*) AS total, COALESCE(AVG(rating), 0) AS average
        FROM Feedback
        WHERE doctor_id = %s AND is_hidden = 0
    ''', (session['id'],))
    stats = cursor.fetchone()

    cursor.execute('''
        SELECT rating, COUNT(*) AS total
        FROM Feedback
        WHERE doctor_id = %s AND is_hidden = 0
        GROUP BY rating
    ''', (session['id'],))
    distribution = {row['rating']: row['total'] for row in cursor.fetchall()}
    cursor.close()

    for row in feedback_list:
        row['appointment_time_formatted'] = get_12hr_time(row['appointment_time'])

    return render_template(
        'doctor/feedback.html',
        feedback_list=feedback_list,
        stats=stats,
        distribution=distribution,
        rating_labels=FEEDBACK_RATING_LABELS,
        selected_sort=sort,
        selected_rating=rating_filter,
    )


@app.route('/admin/feedback')
@admin_required
def admin_feedback():
    """Feedback moderation: search, filter and hide/restore reviews."""
    search = request.args.get('search', '').strip()
    try:
        rating_filter = int(request.args.get('rating', ''))
    except (TypeError, ValueError):
        rating_filter = None
    if rating_filter not in FEEDBACK_RATING_LABELS:
        rating_filter = None
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    query = '''
        SELECT f.feedback_id, f.appointment_id, f.rating, f.comment,
               f.is_hidden, f.created_at, f.updated_at,
               a.date AS appointment_date, a.status AS appointment_status,
               p.name AS patient_name, d.name AS doctor_name
        FROM Feedback f
        JOIN Appointment a ON a.appointment_id = f.appointment_id
        LEFT JOIN Patient p ON p.patient_id = f.patient_id
        LEFT JOIN Doctor d ON d.doctor_id = f.doctor_id
        WHERE 1 = 1
    '''
    params = []

    if search:
        query += ' AND (p.name LIKE %s OR d.name LIKE %s OR f.comment LIKE %s)'
        term = f'%{search}%'
        params.extend([term, term, term])
    if rating_filter:
        query += ' AND f.rating = %s'
        params.append(rating_filter)
    if date_from:
        try:
            datetime.strptime(date_from, '%Y-%m-%d')
            query += ' AND f.created_at >= %s'
            params.append(date_from)
        except ValueError:
            date_from = ''
    if date_to:
        try:
            datetime.strptime(date_to, '%Y-%m-%d')
            query += ' AND f.created_at < DATE_ADD(%s, INTERVAL 1 DAY)'
            params.append(date_to)
        except ValueError:
            date_to = ''

    query += ' ORDER BY f.created_at DESC, f.feedback_id DESC'

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(query, tuple(params))
    feedback_list = cursor.fetchall()

    cursor.execute('''
        SELECT COUNT(*) AS total, COALESCE(AVG(rating), 0) AS average
        FROM Feedback
    ''')
    stats = cursor.fetchone()

    cursor.execute('''
        SELECT rating, COUNT(*) AS total
        FROM Feedback
        GROUP BY rating
    ''')
    distribution = {row['rating']: row['total'] for row in cursor.fetchall()}

    cursor.execute('SELECT COUNT(*) AS hidden FROM Feedback WHERE is_hidden = 1')
    hidden_count = cursor.fetchone()['hidden']
    cursor.close()

    return render_template(
        'admin/feedback.html',
        feedback_list=feedback_list,
        stats=stats,
        distribution=distribution,
        hidden_count=hidden_count,
        rating_labels=FEEDBACK_RATING_LABELS,
        search=search,
        selected_rating=rating_filter,
        date_from=date_from,
        date_to=date_to,
    )


@app.route('/admin/feedback/<int:feedback_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_feedback(feedback_id):
    """Hide a review from patient and doctor views, or restore it."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT feedback_id, is_hidden FROM Feedback WHERE feedback_id = %s',
                   (feedback_id,))
    feedback = cursor.fetchone()

    if not feedback:
        cursor.close()
        flash('Feedback not found.', 'danger')
        return redirect(url_for('admin_feedback'))

    new_state = 0 if feedback['is_hidden'] else 1
    cursor.execute('UPDATE Feedback SET is_hidden = %s WHERE feedback_id = %s',
                   (new_state, feedback_id))
    mysql.connection.commit()
    cursor.close()

    log_audit('MODERATE_FEEDBACK', 'Feedback', feedback_id,
              'Hidden feedback' if new_state else 'Restored feedback')
    flash('Feedback hidden from patient and doctor views.' if new_state
          else 'Feedback restored.', 'success')
    return redirect(url_for('admin_feedback'))


# ========== PRESCRIPTION MANAGEMENT ==========
MAX_MEDICINE_NAME_LENGTH = 150
MAX_MEDICINE_FIELD_LENGTH = 100
# Dropdown choices offered on the prescription form; any other value arrives
# through the "Custom" option and is validated like free text.
PRESCRIPTION_FREQUENCY_OPTIONS = ('Once daily', 'Twice daily',
                                  'Three times daily')
PRESCRIPTION_CUSTOM_FREQUENCY = 'Custom'
MAX_PRESCRIPTION_INSTRUCTIONS_LENGTH = 2000
PRESCRIPTION_INSTRUCTION_SUGGESTIONS = (
    'Take after meals',
    'Take before meals',
    'Complete the full course even if symptoms improve',
    "Follow the doctor's specified instructions",
)


def raw_prescription_rows(form):
    """Rebuild the submitted medicine rows for redisplay after a validation
    error so the doctor never loses typed data."""
    names = form.getlist('medicine_name')
    dosages = form.getlist('dosage')
    frequencies = form.getlist('frequency')
    frequency_customs = form.getlist('frequency_custom')
    durations = form.getlist('duration')
    count = max(len(names), len(dosages), len(frequencies), len(durations))
    rows = []
    for index in range(count):
        frequency = frequencies[index].strip() if index < len(frequencies) else ''
        if frequency == PRESCRIPTION_CUSTOM_FREQUENCY:
            frequency = (frequency_customs[index].strip()
                         if index < len(frequency_customs) else '')
        rows.append({
            'medicine_name': names[index].strip() if index < len(names) else '',
            'dosage': dosages[index].strip() if index < len(dosages) else '',
            'frequency': frequency,
            'duration': durations[index].strip() if index < len(durations) else '',
        })
    if not rows:
        rows.append({'medicine_name': '', 'dosage': '', 'frequency': '',
                     'duration': ''})
    return rows


def parse_prescription_medicines(form):
    """Validate the dynamic medicine rows posted by the doctor's form.

    The form posts parallel arrays (medicine_name[], dosage[], frequency[],
    duration[]). Completely blank rows are ignored so the empty starter rows
    in the UI do not block submission. Returns (medicines, error): medicines
    is a list of clean dicts and error is None when the submission is valid.
    """
    names = form.getlist('medicine_name')
    dosages = form.getlist('dosage')
    frequencies = form.getlist('frequency')
    frequency_customs = form.getlist('frequency_custom')
    durations = form.getlist('duration')

    count = max(len(names), len(dosages), len(frequencies), len(durations))
    medicines = []

    for index in range(count):
        name = names[index].strip() if index < len(names) else ''
        dosage = dosages[index].strip() if index < len(dosages) else ''
        frequency = frequencies[index].strip() if index < len(frequencies) else ''
        if frequency == PRESCRIPTION_CUSTOM_FREQUENCY:
            # The "Custom" dropdown option stores the free-text value from
            # the sibling input; blank there -> reported as a missing field.
            frequency = (frequency_customs[index].strip()
                         if index < len(frequency_customs) else '')
        duration = durations[index].strip() if index < len(durations) else ''

        if not any((name, dosage, frequency, duration)):
            continue

        missing = [label for label, value in (
            ('medicine name', name),
            ('dosage', dosage),
            ('frequency', frequency),
            ('duration', duration),
        ) if not value]
        if missing:
            return [], (f'Medicine row {index + 1}: please complete '
                        f'{", ".join(missing)}.')
        if len(name) > MAX_MEDICINE_NAME_LENGTH:
            return [], (f'Medicine row {index + 1}: name must be '
                        f'{MAX_MEDICINE_NAME_LENGTH} characters or fewer.')
        for label, value in (('dosage', dosage), ('frequency', frequency),
                             ('duration', duration)):
            if len(value) > MAX_MEDICINE_FIELD_LENGTH:
                return [], (f'Medicine row {index + 1}: {label} must be '
                            f'{MAX_MEDICINE_FIELD_LENGTH} characters or fewer.')

        medicines.append({
            'medicine_name': name,
            'dosage': dosage,
            'frequency': frequency,
            'duration': duration,
        })

    if not medicines:
        return [], ('Add at least one prescribed medicine with name, dosage, '
                    'frequency and duration.')
    return medicines, None


def _load_doctor_appointment(cursor, appointment_id):
    """Appointment row only when it belongs to the logged-in doctor."""
    cursor.execute('''
        SELECT a.appointment_id, a.patient_id, a.doctor_id, a.date, a.time,
               a.status, p.name AS patient_name, d.name AS doctor_name
        FROM Appointment a
        LEFT JOIN Patient p ON p.patient_id = a.patient_id
        LEFT JOIN Doctor d ON d.doctor_id = a.doctor_id
        WHERE a.appointment_id = %s AND a.doctor_id = %s
    ''', (appointment_id, session['id']))
    return cursor.fetchone()


def _load_doctor_prescription(cursor, prescription_id):
    """Prescription + medicine rows, restricted to the logged-in doctor."""
    cursor.execute('''
        SELECT pr.*, a.date AS appointment_date, a.time AS appointment_time,
               p.name AS patient_name, d.name AS doctor_name
        FROM Prescription pr
        JOIN Appointment a ON a.appointment_id = pr.appointment_id
        LEFT JOIN Patient p ON p.patient_id = pr.patient_id
        LEFT JOIN Doctor d ON d.doctor_id = pr.doctor_id
        WHERE pr.prescription_id = %s AND pr.doctor_id = %s
    ''', (prescription_id, session['id']))
    prescription = cursor.fetchone()
    if not prescription:
        return None, []
    cursor.execute('''
        SELECT * FROM PrescriptionMedicine
        WHERE prescription_id = %s
        ORDER BY medicine_id
    ''', (prescription_id,))
    return prescription, cursor.fetchall()


def _load_patient_prescription(cursor, prescription_id):
    """Prescription + medicine rows, restricted to the logged-in patient."""
    cursor.execute('''
        SELECT pr.*, a.date AS appointment_date, a.time AS appointment_time,
               p.name AS patient_name, d.name AS doctor_name, d.specialization
        FROM Prescription pr
        JOIN Appointment a ON a.appointment_id = pr.appointment_id
        LEFT JOIN Patient p ON p.patient_id = pr.patient_id
        LEFT JOIN Doctor d ON d.doctor_id = pr.doctor_id
        WHERE pr.prescription_id = %s AND pr.patient_id = %s
    ''', (prescription_id, session['id']))
    prescription = cursor.fetchone()
    if not prescription:
        return None, []
    cursor.execute('''
        SELECT * FROM PrescriptionMedicine
        WHERE prescription_id = %s
        ORDER BY medicine_id
    ''', (prescription_id,))
    return prescription, cursor.fetchall()


@app.route('/doctor/prescriptions')
@doctor_required
def doctor_prescriptions():
    """Prescriptions written by this doctor plus completed appointments that
    still need one."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cursor.execute('''
        SELECT pr.prescription_id, pr.appointment_id, pr.patient_id,
               pr.created_at,
               a.date AS appointment_date, a.time AS appointment_time,
               p.name AS patient_name,
               (SELECT COUNT(*) FROM PrescriptionMedicine m
                WHERE m.prescription_id = pr.prescription_id) AS medicine_count
        FROM Prescription pr
        JOIN Appointment a ON a.appointment_id = pr.appointment_id
        LEFT JOIN Patient p ON p.patient_id = pr.patient_id
        WHERE pr.doctor_id = %s
        ORDER BY pr.created_at DESC, pr.prescription_id DESC
    ''', (session['id'],))
    prescriptions = cursor.fetchall()

    cursor.execute('''
        SELECT a.appointment_id, a.date, a.time, p.name AS patient_name
        FROM Appointment a
        LEFT JOIN Patient p ON p.patient_id = a.patient_id
        LEFT JOIN Prescription pr ON pr.appointment_id = a.appointment_id
        WHERE a.doctor_id = %s
          AND a.status = 'Completed'
          AND pr.prescription_id IS NULL
        ORDER BY a.date DESC, a.time DESC
    ''', (session['id'],))
    eligible = cursor.fetchall()
    cursor.close()

    for row in prescriptions:
        row['appointment_time_formatted'] = get_12hr_time(row['appointment_time'])
    for row in eligible:
        row['time_formatted'] = get_12hr_time(row['time'])

    return render_template('doctor/prescriptions.html',
                           prescriptions=prescriptions,
                           eligible_appointments=eligible)


def _render_prescription_form(appointment, prescription=None, medicines=None,
                              instructions='', error=None):
    if appointment and not appointment.get('time_formatted'):
        appointment['time_formatted'] = get_12hr_time(appointment.get('time'))
    return render_template(
        'doctor/prescription_form.html',
        appointment=appointment,
        prescription=prescription,
        medicines=medicines if medicines is not None else [],
        instructions=instructions,
        error=error,
        suggestions=PRESCRIPTION_INSTRUCTION_SUGGESTIONS,
        frequency_options=PRESCRIPTION_FREQUENCY_OPTIONS,
        custom_frequency_value=PRESCRIPTION_CUSTOM_FREQUENCY,
        max_name_length=MAX_MEDICINE_NAME_LENGTH,
        max_field_length=MAX_MEDICINE_FIELD_LENGTH,
        max_instructions_length=MAX_PRESCRIPTION_INSTRUCTIONS_LENGTH,
    )


@app.route('/doctor/prescriptions/new')
@doctor_required
def doctor_prescription_new():
    appointment_id = request.args.get('appointment_id', type=int)
    if not appointment_id:
        flash('Choose a completed appointment to prescribe for.', 'warning')
        return redirect(url_for('doctor_prescriptions'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    appointment = _load_doctor_appointment(cursor, appointment_id)
    if not appointment:
        cursor.close()
        flash('Appointment not found or not assigned to you.', 'danger')
        return redirect(url_for('doctor_prescriptions'))
    if appointment['status'] != 'Completed':
        cursor.close()
        flash('Prescriptions can only be created after an appointment is completed.',
              'warning')
        return redirect(url_for('doctor_prescriptions'))

    cursor.execute('SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
                   (appointment_id,))
    existing = cursor.fetchone()
    cursor.close()

    if existing:
        flash('A prescription already exists for this appointment.', 'info')
        return redirect(url_for('doctor_prescription_detail',
                                prescription_id=existing['prescription_id']))

    return _render_prescription_form(
        appointment,
        medicines=[{'medicine_name': '', 'dosage': '', 'frequency': '',
                    'duration': ''}] * 3,
        instructions=request.args.get('instructions', ''),
    )


@app.route('/doctor/prescriptions/create', methods=['POST'])
@doctor_required
def doctor_prescription_create():
    appointment_id = request.form.get('appointment_id', type=int)
    instructions = (request.form.get('instructions') or '').strip()
    medicines, medicine_error = parse_prescription_medicines(request.form)

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    appointment = (_load_doctor_appointment(cursor, appointment_id)
                   if appointment_id else None)
    if not appointment:
        cursor.close()
        flash('Appointment not found or not assigned to you.', 'danger')
        return redirect(url_for('doctor_prescriptions'))
    if appointment['status'] != 'Completed':
        cursor.close()
        flash('Prescriptions can only be created after an appointment is completed.',
              'warning')
        return redirect(url_for('doctor_prescriptions'))

    if len(instructions) > MAX_PRESCRIPTION_INSTRUCTIONS_LENGTH:
        cursor.close()
        return _render_prescription_form(
            appointment,
            medicines=raw_prescription_rows(request.form),
            instructions=instructions,
            error=(f'Instructions must be {MAX_PRESCRIPTION_INSTRUCTIONS_LENGTH} '
                   f'characters or fewer.'),
        )

    if medicine_error:
        cursor.close()
        return _render_prescription_form(
            appointment,
            medicines=raw_prescription_rows(request.form),
            instructions=instructions,
            error=medicine_error,
        )

    cursor.execute('SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
                   (appointment_id,))
    existing = cursor.fetchone()
    if existing:
        cursor.close()
        flash('A prescription already exists for this appointment.', 'warning')
        return redirect(url_for('doctor_prescription_detail',
                                prescription_id=existing['prescription_id']))

    try:
        cursor.execute('''
            INSERT INTO Prescription (appointment_id, patient_id, doctor_id,
                                      instructions)
            VALUES (%s, %s, %s, %s)
        ''', (appointment_id, appointment['patient_id'], session['id'],
              instructions or None))
        prescription_id = cursor.lastrowid
        for medicine in medicines:
            cursor.execute('''
                INSERT INTO PrescriptionMedicine
                    (prescription_id, medicine_name, dosage, frequency, duration)
                VALUES (%s, %s, %s, %s, %s)
            ''', (prescription_id, medicine['medicine_name'], medicine['dosage'],
                  medicine['frequency'], medicine['duration']))
        mysql.connection.commit()
    except Exception as e:
        mysql.connection.rollback()
        if 'Duplicate' in str(e):
            cursor.close()
            flash('A prescription already exists for this appointment.', 'warning')
            cursor2 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cursor2.execute(
                'SELECT prescription_id FROM Prescription WHERE appointment_id = %s',
                (appointment_id,))
            duplicate = cursor2.fetchone()
            cursor2.close()
            if duplicate:
                return redirect(url_for('doctor_prescription_detail',
                                        prescription_id=duplicate['prescription_id']))
            return redirect(url_for('doctor_prescriptions'))
        print(f"❌ Prescription insert error: {e}")
        cursor.close()
        return _render_prescription_form(
            appointment,
            medicines=raw_prescription_rows(request.form),
            instructions=instructions,
            error='Unable to save the prescription. Please try again.',
        )

    cursor.close()
    log_audit('CREATE_PRESCRIPTION', 'Prescription', prescription_id,
              f"Doctor created prescription for appointment {appointment_id}")
    flash('Prescription saved successfully!', 'success')
    return redirect(url_for('doctor_prescription_detail',
                            prescription_id=prescription_id))


@app.route('/doctor/prescriptions/<int:prescription_id>')
@doctor_required
def doctor_prescription_detail(prescription_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    prescription, medicines = _load_doctor_prescription(cursor, prescription_id)
    cursor.close()

    if not prescription:
        flash('Prescription not found!', 'danger')
        return redirect(url_for('doctor_prescriptions'))

    prescription['appointment_time_formatted'] = get_12hr_time(
        prescription['appointment_time'])
    return render_template('doctor/prescription_detail.html',
                           prescription=prescription,
                           medicines=medicines)


@app.route('/doctor/prescriptions/<int:prescription_id>/edit')
@doctor_required
def doctor_prescription_edit(prescription_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    prescription, medicines = _load_doctor_prescription(cursor, prescription_id)
    cursor.close()

    if not prescription:
        flash('Prescription not found!', 'danger')
        return redirect(url_for('doctor_prescriptions'))

    if not medicines:
        medicines = [{'medicine_name': '', 'dosage': '', 'frequency': '',
                      'duration': ''}] * 3

    # Present the appointment context in the same shape the form expects.
    appointment_ctx = {
        'appointment_id': prescription['appointment_id'],
        'patient_id': prescription['patient_id'],
        'patient_name': prescription['patient_name'],
        'date': prescription['appointment_date'],
        'time': prescription['appointment_time'],
        'status': 'Completed',
    }
    return _render_prescription_form(
        appointment_ctx,
        prescription=prescription,
        medicines=medicines,
        instructions=prescription.get('instructions') or '',
    )


@app.route('/doctor/prescriptions/<int:prescription_id>/update', methods=['POST'])
@doctor_required
def doctor_prescription_update(prescription_id):
    instructions = (request.form.get('instructions') or '').strip()
    medicines, medicine_error = parse_prescription_medicines(request.form)

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    prescription, _ = _load_doctor_prescription(cursor, prescription_id)
    if not prescription:
        cursor.close()
        flash('Prescription not found!', 'danger')
        return redirect(url_for('doctor_prescriptions'))

    appointment = _load_doctor_appointment(cursor, prescription['appointment_id'])
    if len(instructions) > MAX_PRESCRIPTION_INSTRUCTIONS_LENGTH:
        cursor.close()
        return _render_prescription_form(
            appointment, prescription=prescription,
            medicines=raw_prescription_rows(request.form),
            instructions=instructions,
            error=(f'Instructions must be {MAX_PRESCRIPTION_INSTRUCTIONS_LENGTH} '
                   f'characters or fewer.'),
        )
    if medicine_error:
        cursor.close()
        return _render_prescription_form(
            appointment, prescription=prescription,
            medicines=raw_prescription_rows(request.form),
            instructions=instructions,
            error=medicine_error,
        )

    try:
        cursor.execute('''
            UPDATE Prescription
            SET instructions = %s
            WHERE prescription_id = %s AND doctor_id = %s
        ''', (instructions or None, prescription_id, session['id']))
        # Replace the medicine rows wholesale; the prescription's contents are
        # always edited as a single unit.
        cursor.execute('DELETE FROM PrescriptionMedicine WHERE prescription_id = %s',
                       (prescription_id,))
        for medicine in medicines:
            cursor.execute('''
                INSERT INTO PrescriptionMedicine
                    (prescription_id, medicine_name, dosage, frequency, duration)
                VALUES (%s, %s, %s, %s, %s)
            ''', (prescription_id, medicine['medicine_name'], medicine['dosage'],
                  medicine['frequency'], medicine['duration']))
        mysql.connection.commit()
    except Exception as e:
        mysql.connection.rollback()
        print(f"❌ Prescription update error: {e}")
        cursor.close()
        return _render_prescription_form(
            appointment, prescription=prescription,
            medicines=raw_prescription_rows(request.form),
            instructions=instructions,
            error='Unable to update the prescription. Please try again.',
        )

    cursor.close()
    log_audit('UPDATE_PRESCRIPTION', 'Prescription', prescription_id,
              'Doctor updated prescription')
    flash('Prescription updated successfully!', 'success')
    return redirect(url_for('doctor_prescription_detail',
                            prescription_id=prescription_id))


@app.route('/patient/prescriptions')
@patient_required
def patient_prescriptions():
    """All prescriptions issued to the logged-in patient."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('''
        SELECT pr.prescription_id, pr.appointment_id, pr.created_at,
               a.date AS appointment_date, a.time AS appointment_time,
               d.name AS doctor_name, d.specialization,
               (SELECT COUNT(*) FROM PrescriptionMedicine m
                WHERE m.prescription_id = pr.prescription_id) AS medicine_count
        FROM Prescription pr
        JOIN Appointment a ON a.appointment_id = pr.appointment_id
        LEFT JOIN Doctor d ON d.doctor_id = pr.doctor_id
        WHERE pr.patient_id = %s
        ORDER BY pr.created_at DESC, pr.prescription_id DESC
    ''', (session['id'],))
    prescriptions = cursor.fetchall()
    cursor.close()

    for row in prescriptions:
        row['appointment_time_formatted'] = get_12hr_time(row['appointment_time'])

    return render_template('patient/prescriptions.html',
                           prescriptions=prescriptions)


@app.route('/patient/prescriptions/<int:prescription_id>')
@patient_required
def patient_prescription_detail(prescription_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    prescription, medicines = _load_patient_prescription(cursor, prescription_id)
    cursor.close()

    if not prescription:
        flash('Prescription not found!', 'danger')
        return redirect(url_for('patient_prescriptions'))

    prescription['appointment_time_formatted'] = get_12hr_time(
        prescription['appointment_time'])
    return render_template('patient/prescription_detail.html',
                           prescription=prescription,
                           medicines=medicines)


def build_prescription_pdf(hospital, prescription, medicines):
    """Render one prescription as an in-memory PDF using only the saved data
    (no CSS and no client-side CSS)."""
    from io import BytesIO
    from xml.sax.saxutils import escape as xml_escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Prescription - Appointment {prescription['appointment_id']}",
    )

    base = getSampleStyleSheet()
    title_style = ParagraphStyle('RxTitle', parent=base['Title'],
                                 fontName='Helvetica-Bold', fontSize=16,
                                 leading=20, alignment=TA_CENTER, spaceAfter=2)
    subtitle_style = ParagraphStyle('RxSubtitle', parent=base['Heading2'],
                                    fontName='Helvetica-Bold', fontSize=13,
                                    leading=16, alignment=TA_CENTER,
                                    spaceAfter=4)
    contact_style = ParagraphStyle('RxContact', parent=base['Normal'],
                                   fontSize=9, leading=12, alignment=TA_CENTER,
                                   textColor=colors.HexColor('#475569'),
                                   spaceAfter=10)
    section_style = ParagraphStyle('RxSection', parent=base['Heading2'],
                                   fontName='Helvetica-Bold', fontSize=11,
                                   leading=14, alignment=TA_LEFT,
                                   spaceBefore=12, spaceAfter=6)
    body_style = ParagraphStyle('RxBody', parent=base['Normal'], fontSize=10,
                                leading=14)
    label_style = ParagraphStyle('RxLabel', parent=base['Normal'],
                                 fontName='Helvetica-Bold', fontSize=10,
                                 leading=15)
    value_style = ParagraphStyle('RxValue', parent=base['Normal'], fontSize=10,
                                 leading=15)

    hospital = hospital or {}
    hospital_name = hospital.get('name') or 'SMART HOSPITAL MANAGEMENT SYSTEM'

    created = prescription.get('created_at')
    if hasattr(created, 'strftime'):
        created = created.strftime('%d-%m-%Y')
    else:
        created = str(created or '')

    appointment_date = prescription.get('appointment_date')
    if hasattr(appointment_date, 'strftime'):
        appointment_date = appointment_date.strftime('%d-%m-%Y')
    else:
        appointment_date = str(appointment_date or '')

    story = [
        Paragraph(xml_escape(hospital_name.upper() if hospital_name.isupper()
                             else hospital_name), title_style),
        Paragraph('PRESCRIPTION', subtitle_style),
    ]

    contact_line = ' '.join(
        str(hospital[key]) for key in ('phone', 'email', 'address')
        if hospital.get(key)
    )
    if contact_line:
        story.append(Paragraph(xml_escape(contact_line), contact_style))

    meta_rows = [
        ('Patient', prescription.get('patient_name') or ''),
        ('Doctor', f"Dr. {prescription.get('doctor_name') or ''}".strip()),
        ('Department / Specialization', prescription.get('specialization') or ''),
        ('Appointment Date', appointment_date),
        ('Prescription Date', created),
    ]
    meta_table = Table(
        [[Paragraph(xml_escape(label), label_style),
          Paragraph(xml_escape(str(value)), value_style)]
         for label, value in meta_rows if str(value).strip()],
        colWidths=[55 * mm, None],
    )
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    story.append(meta_table)

    story.append(Paragraph('Prescribed Medicines', section_style))

    medicine_data = [[
        Paragraph('<b>Medicine</b>', body_style),
        Paragraph('<b>Dosage</b>', body_style),
        Paragraph('<b>Frequency</b>', body_style),
        Paragraph('<b>Duration</b>', body_style),
    ]]
    for medicine in medicines:
        medicine_data.append([
            Paragraph(xml_escape(medicine['medicine_name']), body_style),
            Paragraph(xml_escape(medicine['dosage']), body_style),
            Paragraph(xml_escape(medicine['frequency']), body_style),
            Paragraph(xml_escape(medicine['duration']), body_style),
        ])

    medicine_table = Table(medicine_data, colWidths=[55 * mm, 35 * mm, 42 * mm, 32 * mm],
                           repeatRows=1)
    medicine_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1d4ed8')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.white, colors.HexColor('#f8fafc')]),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(medicine_table)

    story.append(Paragraph('Instructions', section_style))
    story.append(Paragraph(
        xml_escape(prescription.get('instructions') or 'No special instructions.'),
        body_style,
    ))

    story.append(Spacer(1, 18 * mm))
    story.append(Paragraph('Signature: ______________________________',
                           body_style))

    doc.build(story)
    buffer.seek(0)
    return buffer


@app.route('/patient/prescriptions/<int:prescription_id>/download')
@patient_required
def patient_prescription_download(prescription_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    prescription, medicines = _load_patient_prescription(cursor, prescription_id)
    cursor.close()

    if not prescription:
        flash('Prescription not found!', 'danger')
        return redirect(url_for('patient_prescriptions'))

    try:
        pdf_buffer = build_prescription_pdf(get_hospital_info(), prescription,
                                             medicines)
    except Exception as e:
        print(f"❌ Prescription PDF error: {e}")
        flash('Unable to generate the prescription PDF right now.', 'danger')
        return redirect(url_for('patient_prescription_detail',
                                prescription_id=prescription_id))

    filename = f"Prescription_{prescription['appointment_id']}.pdf"
    return send_file(pdf_buffer, mimetype='application/pdf', as_attachment=True,
                     download_name=filename)


# ========== LIVE QUEUE MANAGEMENT ==========
# One queue session per doctor per date; the sequence resets every day.
# Numbers are allocated inside a transaction that locks the (doctor, date)
# QueueSequence row, so concurrent approvals and walk-in registrations can
# never hand out the same number. Queue entries are only created after the
# normal approval rules have been satisfied, so pending or rejected bookings
# never consume a number.

QUEUE_STATUS_WAITING = 'Waiting'
QUEUE_STATUS_IN_PROGRESS = 'In Progress'
QUEUE_STATUS_COMPLETED = 'Completed'
QUEUE_STATUS_CANCELLED = 'Cancelled'
QUEUE_STATUS_SKIPPED = 'Skipped'
QUEUE_ACTIVE_STATUSES = (QUEUE_STATUS_WAITING, QUEUE_STATUS_IN_PROGRESS,
                         QUEUE_STATUS_SKIPPED)


def format_queue_number(number):
    """Human readable queue number, e.g. 3 -> Q003."""
    try:
        return 'Q%03d' % int(number)
    except (TypeError, ValueError):
        return ''


def _queue_date_or_today(value):
    """Parse a YYYY-MM-DD request value, falling back to today."""
    if value:
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            pass
    return get_server_time_with_offset().date()


def create_queue_entry(cursor, doctor_id, queue_date, appointment_id=None,
                       patient_id=None, walk_in_name=None,
                       walk_in_phone=None):
    """Insert a queue entry with a freshly allocated, unique number.

    `cursor` must belong to the caller's open transaction; the caller
    commits. Allocation serialises on the (doctor, date) QueueSequence row:
    the plain INSERT creates the row when missing (a concurrent creator
    simply waits for it and retries), and the single UPDATE below takes the
    row's exclusive lock while computing the next number — never a shared
    lock followed by an exclusive one, so concurrent callers queue up
    instead of deadlocking. The unique key on (doctor_id, queue_date,
    queue_number) stays as the database level backstop; any collision or
    lock timeout retries with the next number.
    """
    for _ in range(8):
        try:
            try:
                cursor.execute(
                    'INSERT INTO QueueSequence (doctor_id, queue_date, '
                    'last_number) VALUES (%s, %s, 0)',
                    (doctor_id, queue_date)
                )
            except Exception as exc:
                if 'Duplicate' not in str(exc) and '1062' not in str(exc):
                    raise
                # Row already exists (the normal case) — the failed INSERT
                # does not abort the transaction.

            # Atomically take the exclusive lock and advance the counter,
            # never below the highest number already handed out.
            cursor.execute(
                'UPDATE QueueSequence SET last_number = LAST_INSERT_ID('
                'GREATEST(last_number + 1, ('
                '    SELECT COALESCE(MAX(queue_number), 0) + 1'
                '    FROM QueueEntry'
                '    WHERE QueueEntry.doctor_id = QueueSequence.doctor_id'
                '      AND QueueEntry.queue_date = QueueSequence.queue_date'
                '))) '
                'WHERE doctor_id = %s AND queue_date = %s',
                (doctor_id, queue_date)
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    'Queue sequence is unavailable for this doctor.')

            cursor.execute('SELECT LAST_INSERT_ID() AS next_number')
            number_row = cursor.fetchone()
            queue_number = int(number_row['next_number'])
            if queue_number < 1:
                raise RuntimeError(
                    'Could not allocate a queue number; please retry.')

            cursor.execute(
                'INSERT INTO QueueEntry '
                '(doctor_id, queue_date, queue_number, appointment_id, '
                ' patient_id, walk_in_name, walk_in_phone, status) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (doctor_id, queue_date, queue_number, appointment_id,
                 patient_id, walk_in_name, walk_in_phone,
                 QUEUE_STATUS_WAITING)
            )
            return {'queue_id': cursor.lastrowid,
                    'queue_number': queue_number,
                    'queue_date': queue_date,
                    'status': QUEUE_STATUS_WAITING}
        except Exception as exc:
            message = str(exc)
            retryable = ('Duplicate' in message or '1062' in message or
                         'Deadlock' in message or '1213' in message or
                         'Lock wait timeout' in message or '1205' in message)
            if not retryable:
                raise

    raise RuntimeError('Could not allocate a queue number; please retry.')


def ensure_appointment_queue_entry(doctor_id, queue_date, appointment_id,
                                   patient_id):
    """Create the queue entry for an approved appointment (idempotent).

    Repeated approvals and re-approvals after a reschedule never produce a
    second active entry for the same appointment. Does NOT commit; the
    caller owns the transaction.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        placeholders = ', '.join(['%s'] * len(QUEUE_ACTIVE_STATUSES))
        cursor.execute(
            'SELECT queue_id, queue_number, status FROM QueueEntry '
            f'WHERE appointment_id = %s AND status IN ({placeholders}) '
            'ORDER BY queue_id DESC LIMIT 1',
            (appointment_id, *QUEUE_ACTIVE_STATUSES)
        )
        existing = cursor.fetchone()
        if existing:
            return existing
        return create_queue_entry(cursor, doctor_id, queue_date,
                                  appointment_id=appointment_id,
                                  patient_id=patient_id)
    finally:
        cursor.close()


def cancel_appointment_queue_entries(appointment_id):
    """Free every active queue seat held by an appointment. No commit."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        placeholders = ', '.join(['%s'] * len(QUEUE_ACTIVE_STATUSES))
        cursor.execute(
            'UPDATE QueueEntry SET status = %s '
            f'WHERE appointment_id = %s AND status IN ({placeholders})',
            (QUEUE_STATUS_CANCELLED, appointment_id, *QUEUE_ACTIVE_STATUSES)
        )
        return cursor.rowcount
    finally:
        cursor.close()


def complete_appointment_queue_entries(appointment_id):
    """Mark the queue seats held by an appointment as served. No commit."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        placeholders = ', '.join(['%s'] * len(QUEUE_ACTIVE_STATUSES))
        cursor.execute(
            'UPDATE QueueEntry SET status = %s, '
            'started_at = COALESCE(started_at, NOW()), '
            'completed_at = COALESCE(completed_at, NOW()) '
            f'WHERE appointment_id = %s AND status IN ({placeholders})',
            (QUEUE_STATUS_COMPLETED, appointment_id, *QUEUE_ACTIVE_STATUSES)
        )
        return cursor.rowcount
    finally:
        cursor.close()


def sync_doctor_queue_entries(doctor_id, queue_date):
    """Assign numbers to approved appointments that have no active queue
    seat yet (approved before the feature existed, or while queue creation
    was unavailable). Idempotent; commits on success."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    created = []
    try:
        placeholders = ', '.join(['%s'] * len(QUEUE_ACTIVE_STATUSES))
        cursor.execute(
            'SELECT a.appointment_id, a.patient_id '
            'FROM Appointment a '
            'LEFT JOIN (SELECT queue_id, appointment_id FROM QueueEntry '
            f'          WHERE status IN ({placeholders}) '
            '           AND appointment_id IS NOT NULL) q '
            '  ON q.appointment_id = a.appointment_id '
            "WHERE a.doctor_id = %s AND a.date = %s AND a.status = 'Approved' "
            'AND q.queue_id IS NULL '
            'ORDER BY a.appointment_id',
            (*QUEUE_ACTIVE_STATUSES, doctor_id, queue_date)
        )
        missing = cursor.fetchall()
        for row in missing:
            created.append(create_queue_entry(
                cursor, doctor_id, queue_date,
                appointment_id=row['appointment_id'],
                patient_id=row['patient_id']))
        if created:
            mysql.connection.commit()
        return created
    except Exception as exc:
        mysql.connection.rollback()
        print(f'❌ Queue sync error: {exc}')
        return []
    finally:
        cursor.close()


def _load_doctor_queue(doctor_id, queue_date):
    """All queue entries for one doctor session, with display fields."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            'SELECT q.queue_id, q.queue_number, q.queue_date, q.status, '
            '       q.appointment_id, q.patient_id, q.walk_in_name, '
            '       q.walk_in_phone, q.created_at, q.started_at, '
            '       q.completed_at, a.time AS appointment_time, '
            '       p.name AS appointment_patient_name, '
            '       a.status AS appointment_status, '
            '       COALESCE(q.walk_in_name, p.name) AS display_name '
            'FROM QueueEntry q '
            'LEFT JOIN Appointment a ON a.appointment_id = q.appointment_id '
            'LEFT JOIN Patient p ON p.patient_id = COALESCE(q.patient_id, '
            '                                               a.patient_id) '
            'WHERE q.doctor_id = %s AND q.queue_date = %s '
            'ORDER BY q.queue_number',
            (doctor_id, queue_date)
        )
        entries = cursor.fetchall()
    finally:
        cursor.close()

    for entry in entries:
        entry['label'] = format_queue_number(entry['queue_number'])
        entry['kind'] = 'walk-in' if entry['appointment_id'] is None \
            else 'appointment'
        entry['display_name'] = entry['display_name'] or 'Unknown patient'
        entry['appointment_time_12hr'] = (
            get_12hr_time(entry['appointment_time'])
            if entry['appointment_time'] else ''
        )
    return entries


def _queue_counts(entries):
    counts = {status: 0 for status in (
        QUEUE_STATUS_WAITING, QUEUE_STATUS_IN_PROGRESS,
        QUEUE_STATUS_COMPLETED, QUEUE_STATUS_SKIPPED,
        QUEUE_STATUS_CANCELLED)}
    for entry in entries:
        counts[entry['status']] = counts.get(entry['status'], 0) + 1
    return counts


def _load_owned_queue_entry(queue_id, doctor_id):
    """Queue entry only when it belongs to the logged-in doctor."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            'SELECT queue_id, doctor_id, queue_date, queue_number, status, '
            '       appointment_id, walk_in_name '
            'FROM QueueEntry WHERE queue_id = %s AND doctor_id = %s',
            (queue_id, doctor_id)
        )
        entry = cursor.fetchone()
    finally:
        cursor.close()
    if entry:
        entry['label'] = format_queue_number(entry['queue_number'])
    return entry


def _queue_redirect_date(entry):
    queue_date = entry.get('queue_date') if entry else None
    return queue_date.isoformat() if hasattr(queue_date, 'isoformat') \
        else _queue_date_or_today(None).isoformat()


def _load_patient_queue(patient_id):
    """The patient's active queue seats with position-in-line data."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        placeholders = ', '.join(['%s'] * len(QUEUE_ACTIVE_STATUSES))
        cursor.execute(
            'SELECT q.queue_id, q.queue_number, q.queue_date, q.status, '
            '       q.created_at, d.name AS doctor_name, '
            '       a.date AS appointment_date, a.time AS appointment_time, '
            '       (SELECT COUNT(*) FROM QueueEntry w '
            '        WHERE w.doctor_id = q.doctor_id '
            '          AND w.queue_date = q.queue_date '
            '          AND w.queue_number < q.queue_number '
            "          AND w.status = 'Waiting') AS ahead_count "
            'FROM QueueEntry q '
            'JOIN Appointment a ON a.appointment_id = q.appointment_id '
            'LEFT JOIN Doctor d ON d.doctor_id = q.doctor_id '
            f'WHERE a.patient_id = %s AND q.status IN ({placeholders}) '
            'AND q.queue_date >= CURDATE() '
            'ORDER BY q.queue_date, q.queue_number',
            (patient_id, *QUEUE_ACTIVE_STATUSES)
        )
        entries = cursor.fetchall()
    finally:
        cursor.close()

    for entry in entries:
        entry['label'] = format_queue_number(entry['queue_number'])
        entry['appointment_time_12hr'] = (
            get_12hr_time(entry['appointment_time'])
            if entry['appointment_time'] else ''
        )
        if hasattr(entry['queue_date'], 'strftime'):
            entry['queue_date_str'] = entry['queue_date'].strftime('%d-%m-%Y')
        else:
            entry['queue_date_str'] = str(entry['queue_date'])
    return entries


@app.route('/doctor/queue')
@doctor_required
def doctor_queue_page():
    queue_date = _queue_date_or_today(request.args.get('date'))
    # Also covers appointments approved before this feature existed.
    sync_doctor_queue_entries(session['id'], queue_date)
    entries = _load_doctor_queue(session['id'], queue_date)
    counts = _queue_counts(entries)

    return render_template('doctor/queue.html',
                           entries=entries,
                           counts=counts,
                           queue_date=queue_date,
                           queue_date_str=queue_date.strftime('%Y-%m-%d'),
                           queue_date_pretty=queue_date.strftime('%d-%m-%Y'),
                           last_number=max(
                               (e['queue_number'] for e in entries), default=0),
                           in_progress_entry=next(
                               (e for e in entries
                                if e['status'] == QUEUE_STATUS_IN_PROGRESS),
                               None),
                           now_label=get_12hr_time(
                               get_server_time_with_offset().strftime(
                                   '%H:%M:%S')))


@app.route('/doctor/queue/status')
@doctor_required
def doctor_queue_status():
    """Lightweight JSON payload for the queue dashboard's polling."""
    queue_date = _queue_date_or_today(request.args.get('date'))
    entries = _load_doctor_queue(session['id'], queue_date)

    payload_entries = []
    for entry in entries:
        payload_entries.append({
            'queue_id': entry['queue_id'],
            'queue_number': entry['queue_number'],
            'label': entry['label'],
            'status': entry['status'],
            'kind': entry['kind'],
            'display_name': entry['display_name'],
            'appointment_id': entry['appointment_id'],
            'appointment_time_12hr': entry['appointment_time_12hr'],
            'appointment_status': entry['appointment_status'],
            'walk_in_phone': entry['walk_in_phone'],
        })

    return jsonify({
        'date': queue_date.strftime('%Y-%m-%d'),
        'last_number': max((e['queue_number'] for e in entries), default=0),
        'counts': _queue_counts(entries),
        'entries': payload_entries,
    })


@app.route('/doctor/queue/walk-in', methods=['POST'])
@doctor_required
def queue_add_walk_in():
    """Register a walk-in patient into the viewed queue session."""
    queue_date = _queue_date_or_today(request.form.get('queue_date'))
    name = (request.form.get('walk_in_name') or '').strip()
    phone = (request.form.get('walk_in_phone') or '').strip()

    if not name:
        flash('Patient name is required to register a walk-in.', 'danger')
        return redirect(url_for('doctor_queue_page',
                                date=queue_date.strftime('%Y-%m-%d')))

    name = name[:150]
    phone = phone[:30] or None

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        entry = create_queue_entry(cursor, session['id'], queue_date,
                                   walk_in_name=name, walk_in_phone=phone)
        mysql.connection.commit()
    except Exception as exc:
        mysql.connection.rollback()
        print(f'❌ Walk-in queue error: {exc}')
        flash('Could not register the walk-in patient. Please try again.',
              'danger')
        return redirect(url_for('doctor_queue_page',
                                date=queue_date.strftime('%Y-%m-%d')))
    finally:
        cursor.close()

    label = format_queue_number(entry['queue_number'])
    log_audit('QUEUE_WALK_IN', 'QueueEntry', entry['queue_id'],
              f'Walk-in "{name}" registered as {label} for '
              f'{queue_date.strftime("%d-%m-%Y")}')
    flash(f'{name} added to the queue as {label}.', 'success')
    return redirect(url_for('doctor_queue_page',
                            date=queue_date.strftime('%Y-%m-%d')))


def _queue_action_redirect(entry):
    return redirect(url_for('doctor_queue_page',
                            date=_queue_redirect_date(entry)))


@app.route('/doctor/queue/entry/<int:queue_id>/start', methods=['POST'])
@doctor_required
def queue_start_entry(queue_id):
    entry = _load_owned_queue_entry(queue_id, session['id'])
    if not entry:
        flash('Queue entry not found.', 'danger')
        return redirect(url_for('doctor_queue_page'))

    if entry['status'] == QUEUE_STATUS_IN_PROGRESS:
        flash(f"{entry['label']} is already with you.", 'info')
        return _queue_action_redirect(entry)

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        # Atomic: the NOT EXISTS guard keeps at most one patient in
        # progress per doctor session even under double clicks.
        cursor.execute(
            'UPDATE QueueEntry SET status = %s, '
            'started_at = COALESCE(started_at, NOW()) '
            'WHERE queue_id = %s AND doctor_id = %s AND queue_date = %s '
            'AND status = %s '
            'AND NOT EXISTS (SELECT 1 FROM (SELECT queue_id FROM QueueEntry '
            '               WHERE doctor_id = %s AND queue_date = %s '
            '               AND status = %s LIMIT 1) current_visit)',
            (QUEUE_STATUS_IN_PROGRESS, queue_id, session['id'],
             entry['queue_date'], QUEUE_STATUS_WAITING,
             session['id'], entry['queue_date'], QUEUE_STATUS_IN_PROGRESS)
        )
        started = cursor.rowcount > 0
        mysql.connection.commit()
    except Exception as exc:
        mysql.connection.rollback()
        print(f'❌ Queue start error: {exc}')
        flash('Unable to start this queue entry.', 'danger')
        return _queue_action_redirect(entry)
    finally:
        cursor.close()

    if started:
        log_audit('QUEUE_START', 'QueueEntry', queue_id,
                  f"Doctor started patient at {entry['label']}")
        flash(f"Started {entry['label']}.", 'success')
    else:
        refreshed = _load_owned_queue_entry(queue_id, session['id'])
        if refreshed and refreshed['status'] == QUEUE_STATUS_IN_PROGRESS:
            flash(f"{refreshed['label']} is already with you.", 'info')
        else:
            flash('Another patient is already in progress. '
                  'Finish them before starting the next one.', 'warning')
    return _queue_action_redirect(entry)


@app.route('/doctor/queue/entry/<int:queue_id>/complete', methods=['POST'])
@doctor_required
def queue_complete_entry(queue_id):
    entry = _load_owned_queue_entry(queue_id, session['id'])
    if not entry:
        flash('Queue entry not found.', 'danger')
        return redirect(url_for('doctor_queue_page'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        placeholders = ', '.join(['%s'] * 2)
        cursor.execute(
            'UPDATE QueueEntry SET status = %s, '
            'started_at = COALESCE(started_at, NOW()), '
            'completed_at = COALESCE(completed_at, NOW()) '
            f'WHERE queue_id = %s AND doctor_id = %s AND status IN '
            f'({placeholders})',
            (QUEUE_STATUS_COMPLETED, queue_id, session['id'],
             QUEUE_STATUS_WAITING, QUEUE_STATUS_IN_PROGRESS)
        )
        completed = cursor.rowcount > 0
        mysql.connection.commit()
    except Exception as exc:
        mysql.connection.rollback()
        print(f'❌ Queue complete error: {exc}')
        flash('Unable to complete this queue entry.', 'danger')
        return _queue_action_redirect(entry)
    finally:
        cursor.close()

    if completed:
        log_audit('QUEUE_COMPLETE', 'QueueEntry', queue_id,
                  f"Doctor completed patient at {entry['label']}")
        flash(f'{entry['label']} marked as served.', 'success')
    else:
        flash('That queue entry has already finished.', 'warning')
    return _queue_action_redirect(entry)


@app.route('/doctor/queue/entry/<int:queue_id>/skip', methods=['POST'])
@doctor_required
def queue_skip_entry(queue_id):
    entry = _load_owned_queue_entry(queue_id, session['id'])
    if not entry:
        flash('Queue entry not found.', 'danger')
        return redirect(url_for('doctor_queue_page'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            'UPDATE QueueEntry SET status = %s '
            'WHERE queue_id = %s AND doctor_id = %s AND status = %s',
            (QUEUE_STATUS_SKIPPED, queue_id, session['id'],
             QUEUE_STATUS_WAITING)
        )
        skipped = cursor.rowcount > 0
        mysql.connection.commit()
    except Exception as exc:
        mysql.connection.rollback()
        print(f'❌ Queue skip error: {exc}')
        flash('Unable to skip this queue entry.', 'danger')
        return _queue_action_redirect(entry)
    finally:
        cursor.close()

    if skipped:
        log_audit('QUEUE_SKIP', 'QueueEntry', queue_id,
                  f"Doctor skipped patient at {entry['label']}")
        flash(f'{entry['label']} skipped for now.', 'info')
    else:
        flash('Only waiting patients can be skipped.', 'warning')
    return _queue_action_redirect(entry)


@app.route('/doctor/queue/entry/<int:queue_id>/cancel', methods=['POST'])
@doctor_required
def queue_cancel_entry(queue_id):
    entry = _load_owned_queue_entry(queue_id, session['id'])
    if not entry:
        flash('Queue entry not found.', 'danger')
        return redirect(url_for('doctor_queue_page'))

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        placeholders = ', '.join(['%s'] * 3)
        cursor.execute(
            'UPDATE QueueEntry SET status = %s '
            f'WHERE queue_id = %s AND doctor_id = %s AND status IN '
            f'({placeholders})',
            (QUEUE_STATUS_CANCELLED, queue_id, session['id'],
             QUEUE_STATUS_WAITING, QUEUE_STATUS_SKIPPED,
             QUEUE_STATUS_IN_PROGRESS)
        )
        cancelled = cursor.rowcount > 0
        mysql.connection.commit()
    except Exception as exc:
        mysql.connection.rollback()
        print(f'❌ Queue cancel error: {exc}')
        flash('Unable to remove this queue entry.', 'danger')
        return _queue_action_redirect(entry)
    finally:
        cursor.close()

    if cancelled:
        log_audit('QUEUE_CANCEL', 'QueueEntry', queue_id,
                  f"Doctor removed {entry['label']} from the queue")
        flash(f'{entry['label']} removed from the queue.', 'info')
    else:
        flash('That queue entry has already finished.', 'warning')
    return _queue_action_redirect(entry)


@app.route('/patient/queue/status')
@patient_required
def patient_queue_status():
    """JSON payload for the patient's live queue section."""
    entries = _load_patient_queue(session['id'])
    payload = []
    for entry in entries:
        payload.append({
            'queue_id': entry['queue_id'],
            'label': entry['label'],
            'queue_number': entry['queue_number'],
            'queue_date_str': entry['queue_date_str'],
            'status': entry['status'],
            'doctor_name': entry['doctor_name'] or 'Doctor',
            'appointment_time_12hr': entry['appointment_time_12hr'],
            'ahead_count': int(entry['ahead_count'] or 0),
        })
    return jsonify({'entries': payload})


# ========== NEW FEATURES ==========
@app.route('/patient/nearby_doctors')
@patient_required
def nearby_doctors():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Active doctors are always listed, whether or not they have coordinates.
    # Coordinates only affect distance sorting; no doctor is hidden for missing them.
    cursor.execute('''
        SELECT d.*, dp.name AS department_name
        FROM Doctor d
        LEFT JOIN Department dp ON d.department_id = dp.department_id
        WHERE (d.is_active IS NULL OR d.is_active = 1)
    ''')
    doctors = cursor.fetchall()

    cursor.execute('SELECT * FROM Patient WHERE patient_id = %s', (session['id'],))
    patient = cursor.fetchone()
    cursor.close()

    attach_availability(doctors)

    def calculate_distance(lat1, lon1, lat2, lon2):
        """Great-circle distance in km (straight line, not driving distance)."""
        R = 6371
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    has_patient_location = bool(
        patient and patient.get('latitude') is not None and patient.get('longitude') is not None
    )

    located, unlocated = [], []
    if has_patient_location:
        for doctor in doctors:
            lat, lon = doctor.get('latitude'), doctor.get('longitude')
            if lat is not None and lon is not None:
                try:
                    doctor['distance_km'] = round(calculate_distance(
                        float(patient['latitude']), float(patient['longitude']),
                        float(lat), float(lon)
                    ), 2)
                    # Google Maps directions use real road routing; nothing is estimated here.
                    doctor['directions_url'] = (
                        'https://www.google.com/maps/dir/?api=1'
                        f'&origin={float(patient["latitude"])},{float(patient["longitude"])}'
                        f'&destination={float(lat)},{float(lon)}&travelmode=driving'
                    )
                    doctor['map_url'] = f'https://www.google.com/maps/search/?api=1&query={float(lat)},{float(lon)}'
                    located.append(doctor)
                    continue
                except (TypeError, ValueError):
                    pass
            doctor['directions_url'] = None
            doctor['map_url'] = None
            unlocated.append(doctor)
        located.sort(key=lambda d: d['distance_km'])
    else:
        for doctor in doctors:
            lat, lon = doctor.get('latitude'), doctor.get('longitude')
            doctor['directions_url'] = None
            if lat is not None and lon is not None:
                try:
                    doctor['map_url'] = f'https://www.google.com/maps/search/?api=1&query={float(lat)},{float(lon)}'
                except (TypeError, ValueError):
                    doctor['map_url'] = None
            else:
                doctor['map_url'] = None
            unlocated.append(doctor)

    return render_template(
        'patient/nearby_doctors.html',
        doctors=located + unlocated,
        located_count=len(located),
        patient=patient,
        has_patient_location=has_patient_location,
    )

@app.route('/patient/update_location', methods=['POST'])
@patient_required
def update_location():
    latitude = request.form.get('latitude')
    longitude = request.form.get('longitude')
    address = request.form.get('address')
    
    cursor = mysql.connection.cursor()
    cursor.execute(
        'UPDATE Patient SET latitude = %s, longitude = %s, address = %s WHERE patient_id = %s',
        (latitude, longitude, address, session['id'])
    )
    mysql.connection.commit()
    cursor.close()
    
    flash('Location updated successfully!', 'success')
    return redirect(url_for('nearby_doctors'))

# ========== SYMPTOM CHECKER ==========
# Matching logic lives in symptom_checker.py (see tests/ for the labelled test sets).
# `analyze_symptoms` / `suggest_department` are imported at the top of the module so
# the chatbot API and this route can share the exact same triage logic.


@app.route('/symptom_checker', methods=['GET', 'POST'])
def symptom_checker():
    suggested_department = None
    analysis = None
    symptoms = ''

    if request.method == 'POST':
        symptoms = request.form.get('symptoms', '').strip()
        if symptoms:
            analysis = analyze_symptoms(symptoms, top_n=3)
            suggested_department = analysis['primary']

    return render_template('symptom_checker.html',
                           suggested_department=suggested_department,
                           analysis=analysis,
                           symptoms=symptoms)

# ==========================================
# EMAIL REMINDER SYSTEM (SENDGRID VERSION)
# ==========================================


from datetime import datetime, timedelta, timezone
# India Standard Time (IST): UTC +05:30 — see HOSPITAL_TZ / local_now().
def get_server_time_with_offset():
    """Return the current time in the configured local timezone."""
    return local_now()


def send_email_via_sendgrid(to_email, subject, body):
    """Helper function to send email using Brevo HTTP API."""
    try:
        import requests

        api_key = os.environ.get('BREVO_API_KEY')
        from_email = os.environ.get('EMAIL_FROM')

        if not api_key or not from_email:
            print("❌ BREVO_API_KEY or EMAIL_FROM not set in environment variables!")
            return False

        url = "https://api.brevo.com/v3/smtp/email"

        headers = {
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json"
        }

        payload = {
            "sender": {
                "name": "MediCare Hospital",
                "email": from_email
            },
            "to": [
                {
                    "email": to_email
                }
            ],
            "subject": subject,
            "textContent": body
        }

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=20
        )

        if response.status_code in (200, 201):
            print(f"✅ Brevo accepted email for {to_email}.")
            return True

        print(
            f"❌ Brevo API error: "
            f"{response.status_code} - {response.text}"
        )
        return False

    except Exception as e:
        print(f"❌ Brevo email error sending to {to_email}: {e}")
        return False


def send_appointment_completed_email(appointment_id):
    """Send a thank-you email when a doctor completes an appointment."""

    conn = None
    cursor = None

    try:
        import pymysql

        conn = pymysql.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                a.appointment_id,
                a.date,
                a.follow_up_date,
                p.name AS patient_name,
                p.email AS patient_email,
                d.name AS doctor_name
            FROM Appointment a
            JOIN Patient p ON a.patient_id = p.patient_id
            JOIN Doctor d ON a.doctor_id = d.doctor_id
            WHERE a.appointment_id = %s
              AND a.status = 'Completed'
        """, (appointment_id,))

        appointment = cursor.fetchone()

        if not appointment:
            print(
                f"⚠️ Completed appointment {appointment_id} not found."
            )
            return False

        patient_email = appointment.get('patient_email')

        if not patient_email:
            print("⚠️ Patient email address is missing.")
            return False

        appointment_date = appointment['date']

        if hasattr(appointment_date, 'strftime'):
            appointment_date = appointment_date.strftime('%d-%m-%Y')

        follow_up_date = appointment.get('follow_up_date')
        if hasattr(follow_up_date, 'strftime'):
            follow_up_date = follow_up_date.strftime('%d-%m-%Y')

        subject = "Thank You for Visiting MediCare Hospital"

        body = f"""Dear {appointment['patient_name']},

Thank you for visiting MediCare Hospital!

We appreciate your trust in our hospital and in Dr. {appointment['doctor_name']}.

Your appointment on {appointment_date} has been marked as completed.

{f"Your follow-up appointment is scheduled for {follow_up_date}. Please visit MediCare Hospital on that date." if follow_up_date else "If your doctor has recommended a follow-up visit, please contact MediCare Hospital to schedule your next appointment."}

Thank you for choosing MediCare Hospital. We look forward to assisting you again whenever you need us.

Best regards,
MediCare Hospital Team"""

        email_sent = send_email_via_sendgrid(
            patient_email,
            subject,
            body
        )

        if email_sent:
            print(
                f"✅ Thank-you email accepted for {patient_email}."
            )
            return True

        print("⚠️ Failed to send appointment completion email.")
        return False

    except Exception as e:
        print(f"❌ Appointment completion email error: {e}")
        return False

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    

def send_feedback_invitation_email(appointment_id):
    """Invite the patient to rate a completed appointment (Brevo, deduped).

    The link points at the patient feedback page for that appointment. If the
    patient is not signed in yet, the login page remembers the link and drops
    them straight back on it afterwards. Sends at most once per appointment
    and never after a review already exists.
    """
    conn = None
    cursor = None

    try:
        import pymysql

        conn = pymysql.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                a.appointment_id,
                a.feedback_email_sent,
                p.name AS patient_name,
                p.email AS patient_email,
                d.name AS doctor_name
            FROM Appointment a
            JOIN Patient p ON a.patient_id = p.patient_id
            JOIN Doctor d ON a.doctor_id = d.doctor_id
            WHERE a.appointment_id = %s
              AND a.status = 'Completed'
        """, (appointment_id,))

        appointment = cursor.fetchone()

        if not appointment:
            print(f"⚠️ Feedback invitation: appointment {appointment_id} not found.")
            return False

        if appointment.get('feedback_email_sent'):
            print(f"ℹ️ Feedback invitation already sent for appointment {appointment_id}.")
            return False

        patient_email = appointment.get('patient_email')
        if not patient_email:
            print("⚠️ Feedback invitation: patient email address is missing.")
            return False

        # Never invite someone who has already reviewed the appointment.
        cursor.execute("SELECT feedback_id FROM Feedback WHERE appointment_id = %s",
                       (appointment_id,))
        if cursor.fetchone():
            print(f"ℹ️ Feedback already submitted for appointment {appointment_id}.")
            return False

        try:
            feedback_url = url_for('patient_feedback',
                                   appointment_id=appointment_id,
                                   _external=True)
        except Exception:
            feedback_url = '/patient/feedback'

        subject = "How was your visit? Share your feedback"

        body = f"""Dear {appointment['patient_name']},

Thank you for visiting MediCare Hospital for your appointment with Dr. {appointment['doctor_name']}.

We would love to hear about your experience! Your feedback helps us improve our services and helps other patients find the right care.

Share your feedback in less than a minute:
{feedback_url}

If the link asks you to sign in first, you will be taken straight to your feedback page after logging in.

Thank you for choosing MediCare Hospital.

Best regards,
MediCare Hospital Team"""

        email_sent = send_email_via_sendgrid(patient_email, subject, body)

        if email_sent:
            # Persisted only after Brevo accepts the message, so a failed send
            # never marks the invitation as delivered.
            cursor.execute(
                "UPDATE Appointment SET feedback_email_sent = 1 WHERE appointment_id = %s",
                (appointment_id,))
            conn.commit()
            print(f"✅ Feedback invitation accepted for {patient_email}.")
            return True

        print("⚠️ Failed to send feedback invitation email.")
        return False

    except Exception as e:
        print(f"❌ Feedback invitation email error: {e}")
        return False

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def check_and_send_reminders():
    """Check for appointments 30 minutes from now and send reminders via Brevo."""
    print("⏰ [SCHEDULER] Running 30-min reminder check...")

    conn = None
    cursor = None

    try:
        import pymysql
        from datetime import timedelta, time as dt_time

        conn = pymysql.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        # Calculate the target time using the application's timezone.
        now_local = get_server_time_with_offset()
        target_time = now_local + timedelta(minutes=30)

        target_time_str = target_time.strftime('%H:%M:%S')
        target_date_str = target_time.strftime('%Y-%m-%d')

        print(
            f"⏰ Looking for appointments on {target_date_str} "
            f"at {target_time_str}"
        )

        # Find approved appointments approximately 30 minutes away.
        # The scheduler should run every minute.
        window_end = target_time + timedelta(minutes=1)

        cursor.execute("""
            SELECT
                a.*,
                p.name AS patient_name,
                p.email AS patient_email,
                d.name AS doctor_name
            FROM Appointment a
            JOIN Patient p ON a.patient_id = p.patient_id
            JOIN Doctor d ON a.doctor_id = d.doctor_id
            WHERE a.date = %s
              AND a.time >= %s
              AND a.time < %s
              AND a.status = 'Approved'
              AND a.reminder_sent = 0
        """, (
            target_time.strftime('%Y-%m-%d'),
            target_time.strftime('%H:%M:%S'),
            window_end.strftime('%H:%M:%S')
        ))

        appointments = cursor.fetchall()
        print(f"✅ Found {len(appointments)} appointments to remind.")

        for appt in appointments:
            patient_email = appt.get('patient_email')

            if not patient_email:
                print(
                    f"⚠️ No email found for appointment "
                    f"{appt['appointment_id']}. Skipping."
                )
                continue

            # Format the appointment time for the email.
            appointment_time = appt['time']

            if isinstance(appointment_time, timedelta):
                total_seconds = int(appointment_time.total_seconds())
                hours = (total_seconds // 3600) % 24
                minutes = (total_seconds % 3600) // 60
                time_str = dt_time(hours, minutes).strftime('%I:%M %p')
            elif isinstance(appointment_time, dt_time):
                time_str = appointment_time.strftime('%I:%M %p')
            else:
                time_parts = str(appointment_time).split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1])
                time_str = dt_time(hour % 24, minute).strftime('%I:%M %p')

            subject = (
                f"MediCare Hospital Appointment Reminder: "
                f"Dr. {appt['doctor_name']} in 30 minutes"
            )

            # Include the queue number when one has been assigned.
            cursor.execute(
                'SELECT queue_number FROM QueueEntry '
                'WHERE appointment_id = %s '
                "AND status IN ('Waiting', 'In Progress') "
                'ORDER BY queue_id DESC LIMIT 1',
                (appt['appointment_id'],)
            )
            queue_row = cursor.fetchone()
            queue_section = (
                f"\nQueue Number: {format_queue_number(queue_row['queue_number'])}"
                if queue_row else ''
            )

            body = f"""Hello {appt['patient_name']},

This is a reminder that you have an appointment with Dr. {appt['doctor_name']} in approximately 30 minutes.

Date: {appt['date']}
Time: {time_str}{queue_section}

Please arrive on time.

If you need to cancel or reschedule, please contact MediCare Hospital.

Best regards,
MediCare Hospital Team"""

            print(f"📧 Sending appointment reminder to {patient_email}...")

            try:
                # Uses the existing helper, which must call the Brevo API.
                email_sent = send_email_via_sendgrid(
                    patient_email, subject, body
                )

                if email_sent:
                    cursor.execute("""
                        UPDATE Appointment
                        SET reminder_sent = 1
                        WHERE appointment_id = %s
                          AND reminder_sent = 0
                    """, (appt['appointment_id'],))

                    conn.commit()
                    print(
                        f"✅ Reminder accepted by the email service "
                        f"and marked for appointment "
                        f"{appt['appointment_id']}."
                    )
                else:
                    print(
                        f"⚠️ Email failed for {patient_email}; "
                        "it can be retried on the next scheduler run."
                    )

            except Exception as email_error:
                conn.rollback()
                print(
                    f"❌ Error processing reminder for {patient_email}: "
                    f"{email_error}"
                )

    except Exception as e:
        print(f"❌ CRITICAL ERROR in 30-min scheduler: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def send_follow_up_reminder():
    """Send email reminders one day before scheduled follow-ups via Brevo."""
    print("⏰ [SCHEDULER] Running follow-up reminder check...")

    conn = None
    cursor = None

    try:
        import pymysql
        from datetime import timedelta

        conn = pymysql.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        # Calculate tomorrow's date using the application's timezone.
        tomorrow_local = get_server_time_with_offset() + timedelta(days=1)
        tomorrow_date_str = tomorrow_local.strftime('%Y-%m-%d')

        print(
            f"⏰ Looking for follow-ups scheduled for tomorrow: "
            f"{tomorrow_date_str}"
        )

        cursor.execute("""
            SELECT
                a.*,
                p.name AS patient_name,
                p.email AS patient_email,
                d.name AS doctor_name
            FROM Appointment a
            JOIN Patient p ON a.patient_id = p.patient_id
            JOIN Doctor d ON a.doctor_id = d.doctor_id
            WHERE a.follow_up_date = %s
              AND a.status = 'Completed'
              AND a.follow_up_reminder_sent = 0
        """, (tomorrow_date_str,))

        appointments = cursor.fetchall()
        print(f"✅ Found {len(appointments)} follow-up reminders.")

        for appt in appointments:
            patient_email = appt.get('patient_email')

            if not patient_email:
                print(
                    f"⚠️ No email found for appointment "
                    f"{appt['appointment_id']}. Skipping."
                )
                continue

            subject = (
                f"MediCare Hospital Follow-up Reminder: "
                f"Dr. {appt['doctor_name']} - Tomorrow"
            )

            body = f"""Hello {appt['patient_name']},

This is a reminder that you have a scheduled follow-up checkup
with Dr. {appt['doctor_name']} tomorrow.

Date: {appt['follow_up_date']}
Doctor: Dr. {appt['doctor_name']}

If you need to reschedule, please contact MediCare Hospital.

Best regards,
MediCare Hospital Team"""

            print(f"📧 Sending follow-up reminder to {patient_email}...")

            try:
                # This helper must use the Brevo HTTPS API.
                email_sent = send_email_via_sendgrid(
                    patient_email, subject, body
                )

                if email_sent:
                    cursor.execute("""
                        UPDATE Appointment
                        SET follow_up_reminder_sent = 1
                        WHERE appointment_id = %s
                          AND follow_up_reminder_sent = 0
                    """, (appt['appointment_id'],))

                    conn.commit()
                    print(
                        f"✅ Reminder sent and marked for appointment "
                        f"{appt['appointment_id']}."
                    )
                else:
                    print(
                        f"⚠️ Email failed for {patient_email}. "
                        "It can be retried on the next scheduler run."
                    )

            except Exception as email_error:
                conn.rollback()
                print(
                    f"❌ Error processing reminder for {patient_email}: "
                    f"{email_error}"
                )

    except Exception as e:
        print(f"❌ CRITICAL ERROR in follow-up scheduler: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def send_appointment_accepted_email(appointment_id):
    """Send an appointment confirmation email when a doctor accepts."""

    conn = None
    cursor = None

    try:
        import pymysql

        conn = pymysql.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                a.appointment_id,
                a.date,
                a.time,
                p.name AS patient_name,
                p.email AS patient_email,
                d.name AS doctor_name
            FROM Appointment a
            JOIN Patient p ON a.patient_id = p.patient_id
            JOIN Doctor d ON a.doctor_id = d.doctor_id
            WHERE a.appointment_id = %s
              AND a.status = 'Approved'
        """, (appointment_id,))

        appt = cursor.fetchone()

        if not appt:
            print(
                f"⚠️ Approved appointment {appointment_id} "
                "not found."
            )
            return False

        if not appt.get('patient_email'):
            print("⚠️ Patient email address is missing.")
            return False

        appointment_date = appt['date']
        appointment_time = appt['time']

        if hasattr(appointment_date, 'strftime'):
            appointment_date = appointment_date.strftime('%d-%m-%Y')

        # TIME columns arrive as timedelta; format once as 'hh:mm AM/PM'.
        appointment_time = get_12hr_time(appointment_time)

        # The approval just reserved this patient's queue number.
        cursor.execute(
            'SELECT queue_number FROM QueueEntry '
            'WHERE appointment_id = %s '
            "AND status IN ('Waiting', 'In Progress', 'Skipped') "
            'ORDER BY queue_id DESC LIMIT 1',
            (appointment_id,)
        )
        queue_row = cursor.fetchone()
        queue_section = (
            f"\nQueue Number: {format_queue_number(queue_row['queue_number'])}"
            if queue_row else ''
        )

        subject = "MediCare Hospital - Appointment Confirmed"

        body = f"""Hello {appt['patient_name']},

Good news! Your appointment has been accepted by the doctor.

Doctor: Dr. {appt['doctor_name']}
Appointment Date: {appointment_date}
Appointment Time: {appointment_time}{queue_section}

Please arrive at MediCare Hospital on time for your scheduled appointment.

If you need to reschedule, please contact the hospital.

Thank you for choosing MediCare Hospital.

Best regards,
MediCare Hospital Team"""

        email_sent = send_email_via_sendgrid(
            appt['patient_email'],
            subject,
            body
        )

        if email_sent:
            print(
                f"✅ Appointment confirmation email accepted "
                f"for {appt['patient_email']}."
            )
            return True

        print("⚠️ Failed to send appointment confirmation email.")
        return False

    except Exception as e:
        print(f"❌ Appointment confirmation error: {e}")
        return False

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()



def send_appointment_cancelled_email(appointment_id):
    """Send an email notification when an appointment is cancelled."""

    conn = None
    cursor = None

    try:
        import pymysql

        conn = pymysql.connect(
            host=app.config['MYSQL_HOST'],
            user=app.config['MYSQL_USER'],
            password=app.config['MYSQL_PASSWORD'],
            database=app.config['MYSQL_DB'],
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                a.appointment_id,
                a.date,
                a.time,
                p.name AS patient_name,
                p.email AS patient_email,
                d.name AS doctor_name
            FROM Appointment a
            JOIN Patient p ON a.patient_id = p.patient_id
            JOIN Doctor d ON a.doctor_id = d.doctor_id
            WHERE a.appointment_id = %s
              AND a.status = 'Cancelled'
        """, (appointment_id,))

        appt = cursor.fetchone()

        if not appt:
            print(
                f"⚠️ Cancelled appointment {appointment_id} "
                "not found."
            )
            return False

        if not appt.get('patient_email'):
            print("⚠️ Patient email address is missing.")
            return False

        appointment_date = appt['date']
        appointment_time = appt['time']

        if hasattr(appointment_date, 'strftime'):
            appointment_date = appointment_date.strftime('%d-%m-%Y')

        # TIME columns arrive as timedelta; format once as 'hh:mm AM/PM'.
        appointment_time = get_12hr_time(appointment_time)

        # Any queue number issued for this visit went away with it.
        cursor.execute(
            'SELECT queue_number FROM QueueEntry '
            'WHERE appointment_id = %s '
            'ORDER BY queue_id DESC LIMIT 1',
            (appointment_id,)
        )
        queue_row = cursor.fetchone()
        queue_section = (
            f"\nQueue Number: {format_queue_number(queue_row['queue_number'])}"
            if queue_row else ''
        )

        subject = "MediCare Hospital - Appointment Cancelled"

        body = f"""Hello {appt['patient_name']},

Your appointment at MediCare Hospital has been cancelled.

Doctor: Dr. {appt['doctor_name']}
Appointment Date: {appointment_date}
Appointment Time: {appointment_time}{queue_section}

You can book another appointment on a different day through
the MediCare Hospital appointment system.

We apologize for any inconvenience and look forward to assisting you.

Best regards,
MediCare Hospital Team"""

        email_sent = send_email_via_sendgrid(
            appt['patient_email'],
            subject,
            body
        )

        if email_sent:
            print(
                f"✅ Cancellation email accepted "
                f"for {appt['patient_email']}."
            )
            return True

        print("⚠️ Failed to send appointment cancellation email.")
        return False

    except Exception as e:
        print(f"❌ Appointment cancellation email error: {e}")
        return False

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
# ==========================================
# SCHEDULER SETUP
# ==========================================
# Only one scheduler may exist per process, otherwise every reminder job runs
# multiple times and patients receive duplicate emails.
def start_scheduler():
    """Start the single reminder scheduler exactly once per process."""
    global scheduler
    if scheduler is not None:
        return scheduler

    scheduler = BackgroundScheduler()
    # Run 30-min check every minute
    scheduler.add_job(func=check_and_send_reminders, trigger="cron", minute='*')
    # Run follow-up check every minute too (so it catches the exact date)
    scheduler.add_job(func=send_follow_up_reminder, trigger="cron", minute='*')
    scheduler.start()
    return scheduler
        
# ========== REST APIs ==========
@app.route('/api/patient/register', methods=['POST'])
def api_register():
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        password = data.get('password', '')
        phone = data.get('phone', '').strip()
        age = data.get('age')
        gender = data.get('gender', '')
        if not all([name, email, password, phone, age, gender]):
            return jsonify({'success': False, 'message': 'All fields are required'}), 400
        if not re.match(r'[^@]+@[^@]+\.[^@]+', email):
            return jsonify({'success': False, 'message': 'Invalid email address'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Patient WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account:
            cursor.close()
            return jsonify({'success': False, 'message': 'Account already exists'}), 409
        
        hashed_password = generate_password_hash(password)
        cursor.execute(
            'INSERT INTO Patient (name, email, password, phone, age, gender) VALUES (%s, %s, %s, %s, %s, %s)',
            (name, email, hashed_password, phone, age, gender)
        )
        mysql.connection.commit()
        cursor.close()
        
        return jsonify({'success': True, 'message': 'Registration successful', 'patient': {'name': name, 'email': email}}), 201
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/patient/login', methods=['POST'])
def api_patient_login():
    try:
        data = request.get_json()
        email = data.get('email', '').strip()
        password = data.get('password', '')
        
        if not email or not password:
            return jsonify({'success': False, 'message': 'Email and password required'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Patient WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            if not account.get('is_active', 1):
                cursor.close()
                return jsonify({'success': False, 'message': 'This account has been deactivated'}), 403
            session['loggedin'] = True
            session['id'] = account['patient_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'patient'
            cursor.close()
            return jsonify({'success': True, 'message': 'Login successful', 'patient': {'id': account['patient_id'], 'name': account['name'], 'email': account['email']}}), 200
        else:
            cursor.close()
            return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/doctors', methods=['GET'])
def api_get_doctors():
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        specialization = request.args.get('specialization')

        # Never expose credentials/password hashes or reset tokens publicly.
        public_columns = (
            'doctor_id, name, specialization, experience, email, phone, city, '
            'available_slots, latitude, longitude, created_at, address, '
            'department_id, qualification, license_number, consultation_fee, '
            'profile_description, availability_status, is_active'
        )

        if specialization:
            cursor.execute(
                f'SELECT {public_columns} FROM Doctor WHERE specialization = %s',
                (specialization,)
            )
        else:
            cursor.execute(f'SELECT {public_columns} FROM Doctor')

        doctors = cursor.fetchall()
        cursor.execute('SELECT DISTINCT specialization FROM Doctor')
        specializations = [row['specialization'] for row in cursor.fetchall()]
        cursor.close()

        return jsonify({'success': True, 'doctors': doctors, 'specializations': specializations, 'count': len(doctors)}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/patient/bookAppointment', methods=['POST'])
@api_patient_required
def api_book_appointment():
    try:
        data = request.get_json(silent=True) or {}
        doctor_id = data.get('doctor_id')
        date = data.get('date')
        time = data.get('time')
        reason = (data.get('reason') or '').strip()
        
        if not all([doctor_id, date, time]):
            return jsonify({'success': False, 'message': 'Doctor ID, date, and time are required'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (doctor_id,))
        doctor = cursor.fetchone()
        
        if not doctor:
            cursor.close()
            return jsonify({'success': False, 'message': 'Doctor not found'}), 404
        
        bookable, blocked_reason = doctor_is_bookable(doctor)
        if not bookable:
            cursor.close()
            return jsonify({
                'success': False,
                'message': blocked_reason or 'This doctor is not accepting new appointments.'
            }), 409
        
        try:
            date_str = str(date)[:10]
            time_str = str(time)[:5]
            book_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            cursor.close()
            return jsonify({'success': False, 'message': 'Invalid appointment date or time'}), 400

        now = local_now()
        if book_date < now.date():
            cursor.close()
            return jsonify({'success': False, 'message': 'Cannot book an appointment in the past'}), 400

        # Today's elapsed slots are rejected server-side as well.
        if book_date == now.date() and time_str[:5] <= now.strftime('%H:%M'):
            cursor.close()
            return jsonify({
                'success': False,
                'message': 'That time has already passed for today. Please choose a future slot.'
            }), 400

        available_slots = filter_future_slots(get_slots_for_date(doctor, date_str), date_str)
        if not available_slots or time_str not in available_slots:
            cursor.close()
            return jsonify({
                'success': False,
                'message': ('That time is outside the doctor availability.'
                            if available_slots
                            else 'The doctor has no availability on that date.'),
                'available_slots': available_slots
            }), 409

        if not slot_is_free(cursor, doctor_id, date_str, time_str):
            cursor.close()
            return jsonify({
                'success': False,
                'message': f'Dr. {doctor["name"]} is busy at this time. Please find another {doctor["specialization"]} doctor.',
                'suggested_action': 'search_other_doctors',
                'specialization': doctor['specialization']
            }), 409

        cursor.execute('''
            SELECT appointment_id FROM Appointment
            WHERE patient_id = %s AND doctor_id = %s AND date = %s
              AND status IN ('Pending', 'Approved')
        ''', (session['id'], doctor_id, date_str))
        if cursor.fetchone():
            cursor.close()
            return jsonify({
                'success': False,
                'message': 'You already have an appointment with this doctor on that date.'
            }), 409
        
        cursor.execute(
            '''INSERT INTO Appointment
                   (patient_id, doctor_id, date, time, status, reason, department_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)''',
            (session['id'], doctor_id, date_str, time_str, 'Pending',
             reason or None, doctor.get('department_id'))
        )
        mysql.connection.commit()
        appointment_id = cursor.lastrowid
        cursor.close()
        
        notify_appointment_event(appointment_id, 'booked')
        log_audit('BOOK_APPOINTMENT', 'Appointment', appointment_id,
                  f"Patient {session['id']} booked Dr. {doctor['name']} on {date_str} at {get_12hr_time(time_str)}")

        return jsonify({'success': True, 'message': 'Appointment booked successfully', 'appointment': {'appointment_id': appointment_id, 'doctor_id': doctor_id, 'date': date, 'time': time, 'status': 'Pending'}}), 201
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/patient/appointments', methods=['GET'])
@api_patient_required
def api_get_patient_appointments():
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('''
            SELECT a.*, d.name as doctor_name, d.specialization FROM Appointment a LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id WHERE a.patient_id = %s ORDER BY a.date DESC, a.time DESC
''', (session['id'],))
        appointments = [json_safe_row(r) for r in cursor.fetchall()]
        cursor.close()
        return jsonify({'success': True, 'appointments': appointments, 'count': len(appointments)}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/doctor/login', methods=['POST'])
def api_doctor_login():
    try:
        data = request.get_json()
        email = data.get('email', '').strip()
        password = data.get('password', '')
        
        if not email or not password:
            return jsonify({'success': False, 'message': 'Email and password required'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            if not account.get('is_active', 1):
                cursor.close()
                return jsonify({'success': False, 'message': 'This account has been deactivated'}), 403
            session['loggedin'] = True
            session['id'] = account['doctor_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'doctor'
            cursor.close()
            return jsonify({'success': True, 'message': 'Login successful', 'doctor': {'id': account['doctor_id'], 'name': account['name'], 'email': account['email'], 'specialization': account['specialization']}}), 200
        else:
            cursor.close()
            return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/doctor/appointments', methods=['GET'])
@api_doctor_required
def api_get_doctor_appointments():
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        status_filter = request.args.get('status')
        
        if status_filter:
            cursor.execute('''
                SELECT a.*, p.name as patient_name, p.email as patient_email, p.phone as patient_phone FROM Appointment a LEFT JOIN Patient p ON a.patient_id = p.patient_id WHERE a.doctor_id = %s AND a.status = %s ORDER BY a.date, a.time
            ''', (session['id'], status_filter))
        else:
            cursor.execute('''
                SELECT a.*, p.name as patient_name, p.email as patient_email, p.phone as patient_phone FROM Appointment a LEFT JOIN Patient p ON a.patient_id = p.patient_id WHERE a.doctor_id = %s ORDER BY a.date DESC, a.time DESC
''', (session['id'],))
        
        appointments = [json_safe_row(r) for r in cursor.fetchall()]
        cursor.close()
        return jsonify({'success': True, 'appointments': appointments, 'count': len(appointments)}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/approveAppointment', methods=['PUT'])
@api_doctor_required
def api_approve_appointment():
    try:
        data = request.get_json()
        appointment_id = data.get('appointment_id')
        action = data.get('action')
        
        if not appointment_id or not action:
            return jsonify({'success': False, 'message': 'Appointment ID and action required'}), 400
        if action not in ['approve', 'reject']:
            return jsonify({'success': False, 'message': 'Action must be "approve" or "reject"'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Appointment WHERE appointment_id = %s AND doctor_id = %s', (appointment_id, session['id']))
        appointment = cursor.fetchone()
        
        if not appointment:
            cursor.close()
            return jsonify({'success': False, 'message': 'Appointment not found'}), 404
        
        new_status = 'Approved' if action == 'approve' else 'Rejected'
        cursor.execute('UPDATE Appointment SET status = %s WHERE appointment_id = %s AND doctor_id = %s', (new_status, appointment_id, session['id']))

        queue_created = False
        if action == 'approve' and appointment:
            # Approval satisfied -> reserve a queue number atomically.
            try:
                ensure_appointment_queue_entry(
                    appointment['doctor_id'], appointment['date'],
                    appointment['appointment_id'], appointment['patient_id'])
                queue_created = True
            except Exception as queue_error:
                print(f'❌ Queue assignment error: {queue_error}')

        mysql.connection.commit()
        cursor.close()

        # Mirror the doctor-approve route: send the confirmation email after
        # the commit. Email failure never changes the API response.
        if action == 'approve':
            try:
                send_appointment_accepted_email(appointment_id)
            except Exception as email_error:
                print(f"❌ Confirmation email error: {email_error}")

        return jsonify({'success': True, 'message': f'Appointment {action}d successfully', 'new_status': new_status, 'queue_assigned': queue_created}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/admin/dashboard', methods=['GET'])
@api_admin_required
def api_admin_dashboard():
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT COUNT(*) as total FROM Patient')
        total_patients = cursor.fetchone()['total']
        cursor.execute('SELECT COUNT(*) as total FROM Doctor')
        total_doctors = cursor.fetchone()['total']
        today = local_now().date()
        cursor.execute('SELECT COUNT(*) as total FROM Appointment WHERE date = %s', (today,))
        today_appointments = cursor.fetchone()['total']
        cursor.execute("SELECT COUNT(*) as total FROM Appointment WHERE status = 'Pending'")
        pending_requests = cursor.fetchone()['total']
        cursor.close()
        
        return jsonify({'success': True, 'dashboard': {'total_patients': total_patients, 'total_doctors': total_doctors, 'today_appointments': today_appointments, 'pending_requests': pending_requests}}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/admin/addDoctor', methods=['POST'])
@api_admin_required
def api_add_doctor():
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        specialization = data.get('specialization', '').strip()
        experience = data.get('experience')
        email = data.get('email', '').strip()
        password = data.get('password', '')
        phone = data.get('phone', '').strip()
        available_slots = data.get('available_slots', '')
        
        if not all([name, specialization, experience, email, password, phone]):
            return jsonify({'success': False, 'message': 'All fields are required'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
        existing = cursor.fetchone()
        
        if existing:
            cursor.close()
            return jsonify({'success': False, 'message': 'Doctor with this email already exists'}), 409
        
        hashed_password = generate_password_hash(password)
        cursor.execute(
            'INSERT INTO Doctor (name, specialization, experience, email, password, phone, available_slots) VALUES (%s, %s, %s, %s, %s, %s, %s)',
            (name, specialization, experience, email, hashed_password, phone, available_slots)
        )
        mysql.connection.commit()
        doctor_id = cursor.lastrowid
        cursor.close()
        
        return jsonify({'success': True, 'message': 'Doctor added successfully', 'doctor': {'id': doctor_id, 'name': name, 'email': email, 'specialization': specialization}}), 201
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/admin/removeDoctor', methods=['DELETE'])
@api_admin_required
def api_remove_doctor():
    try:
        data = request.get_json()
        doctor_id = data.get('doctor_id')
        
        if not doctor_id:
            return jsonify({'success': False, 'message': 'Doctor ID required'}), 400
        
        cursor = mysql.connection.cursor()
        cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (doctor_id,))
        doctor = cursor.fetchone()
        
        if not doctor:
            cursor.close()
            return jsonify({'success': False, 'message': 'Doctor not found'}), 404
        
        cursor.execute('DELETE FROM Doctor WHERE doctor_id = %s', (doctor_id,))
        mysql.connection.commit()
        cursor.close()
        
        return jsonify({'success': True, 'message': 'Doctor removed successfully'}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully'}), 200

@app.route('/api/health', methods=['GET'])
def api_health_check():
    return jsonify({'status': 'OK', 'message': 'Hospital API is running', 'timestamp': datetime.now().isoformat()}), 200

_EMERGENCY_KEYWORDS = (
    'emergency', 'ambulance', 'chest pain', 'heart attack', 'cant breathe',
    'cannot breathe', 'not breathing', 'unconscious', 'unresponsive',
    'severe bleeding', 'heavy bleeding', 'bleeding a lot', 'stroke',
    'suicidal', 'want to die', 'kill myself', 'self harm', 'overdose',
    'poisoning', 'choking', 'seizure', 'fits', 'road accident', 'collapsed',
)

_EMERGENCY_ASSISTANT_REPLY = (
    "This sounds like it could be a medical emergency. Please act now:\n"
    "1. Call your local emergency number (for example 112 in India) or the "
    "hospital emergency line right away.\n"
    "2. If the person is unconscious or not breathing normally, start CPR if "
    "you are trained and keep them still.\n"
    "3. Do not drive yourself \u2014 ask someone else to drive, or wait for the "
    "ambulance.\n"
    "If you are not sure how serious it is, call the emergency line and let "
    "them decide."
)

_NON_EMERGENCY_INTENTS = (
    ('nearby', ('nearby', 'near me', 'near by', 'closest doctor', 'closest hospital',
                'doctors around', 'map', 'direction', 'directions', 'how to reach')),
    ('hours', ('opening hours', 'open hours', 'open today', 'closing time',
               'what time', 'timings', 'timing', 'working hours', 'open now')),
    ('address', ('address', 'where is the hospital', 'location of hospital',
                 'where are you located', 'where are you', 'your location',
                 'located', 'how do i get to')),
    ('contact', ('phone', 'phone number', 'contact', 'call you', 'email', 'reach you',
                 'talk to someone', 'talk to human', 'speak to human', 'appointment help')),
    ('greeting', ('hello', 'hi ', 'hey', 'good morning', 'good evening', 'namaste')),
)

_HUMAN_ESCALATION_REPLY = (
    "I can help with triage, but I don't want to guess about anything clinical. "
    "For a definite answer please contact the hospital reception directly \u2014 "
    "you can find the phone number and opening hours in the 'Visit' section of "
    "the home page."
)


def _chatbot_emergency_reply(hospital):
    """Safety-first answer for anything that reads like an emergency."""
    emergency_phone = (hospital or {}).get('emergency_phone')
    if emergency_phone:
        first_line = (
            "This sounds like it could be a medical emergency. Call the hospital "
            f"emergency line right now: {emergency_phone}."
        )
    else:
        first_line = (
            "This sounds like it could be a medical emergency. Call your local "
            "emergency number right now (for example 112 in India)."
        )
    guidance = (
        "\nWhile you wait for help:\n"
        "\u2022 If the person is unconscious or not breathing normally, start CPR "
        "only if you are trained, and keep them still.\n"
        "\u2022 Do not drive yourself \u2014 have someone else drive or wait for the "
        "ambulance.\n"
        "\u2022 Stay with the person and keep them calm.\n"
        "If you are not sure how serious it is, call the emergency line and let "
        "them decide."
    )
    return first_line + guidance


def _hospital_contact_text(hospital):
    """Plain-text contact details, only from real HospitalInfo values."""
    name = (hospital or {}).get('name') or 'the hospital'
    parts = []
    phone = (hospital or {}).get('phone')
    emergency = (hospital or {}).get('emergency_phone')
    email = (hospital or {}).get('email')
    hours = (hospital or {}).get('opening_hours')
    address = (hospital or {}).get('address')
    if phone:
        parts.append(f"Reception phone: {phone}")
    if emergency:
        parts.append(f"Emergency phone: {emergency}")
    if email:
        parts.append(f"Email: {email}")
    if hours:
        parts.append(f"Opening hours: {hours}")
    if address:
        parts.append(f"Address: {address}")
    if not parts:
        return (f"I don't have verified contact details for {name} available right "
                "now. Please check the hospital's official website or local directory.")
    return f"Here is how to reach {name}:\n" + "\n".join(parts)


def _detect_non_emergency_intent(text):
    for intent, phrases in _NON_EMERGENCY_INTENTS:
        if any(phrase in text for phrase in phrases):
            return intent
    return None


def _chatbot_triage_reply(user_input, hospital):
    """Triage via the shared symptom checker. Never invents a diagnosis."""
    analysis = analyze_symptoms(user_input, top_n=3)
    dept = analysis['primary']
    alternatives = [d for d in analysis['alternatives'] if d != dept]

    if analysis['urgent'] or analysis['self_harm']:
        return {
            'intent': 'emergency',
            'message': _chatbot_emergency_reply(hospital),
            'options': [],
        }

    if not analysis['recognised']:
        return {
            'intent': 'clarify',
            'message': (
                "I couldn't match what you described to a specific department, so I "
                "won't guess. Could you add more detail (for example where it hurts "
                "and how long it has lasted), or contact reception for help?"
            ),
            'options': ['Talk to Human', 'Nearby doctors', 'Start over'],
        }

    message = (
        f"Thanks \u2014 based on that, the most relevant department is {dept}. "
        "This is general guidance, not a diagnosis."
    )
    if alternatives:
        message += f" Other departments that may also be relevant: {', '.join(alternatives)}."
    message += " Would you like to book an appointment?"

    return {
        'intent': 'triage',
        'message': message,
        'options': ['Book Appointment', 'Nearby doctors', 'Talk to Human'],
        'department': dept,
    }


@app.route('/api/chatbot', methods=['POST'])
def chatbot_api():
    data = request.get_json(silent=True) or {}
    user_input = (data.get('input') or '').strip()
    text = user_input.lower()

    if not text:
        return jsonify({
            'intent': 'greeting',
            'message': "Hi, I'm the MediCare triage assistant. Describe your symptoms, "
                       "or ask about nearby doctors, opening hours or how to reach us.",
            'options': ['Nearby doctors', 'Opening hours', 'Talk to Human'],
        })

    # 1. Hard safety gate: anything emergency-shaped is answered first, every turn.
    if any(keyword in text for keyword in _EMERGENCY_KEYWORDS):
        return jsonify({
            'intent': 'emergency',
            'message': _chatbot_emergency_reply(get_hospital_info()),
            'options': [],
        })

    # 2. Navigation / action intents (no DB lookup needed).
    if 'book' in text or 'appointment' in text:
        dept = data.get('department') or 'General Medicine'
        return jsonify({
            'intent': 'book',
            'message': f"Opening the list of {dept} doctors so you can book an appointment.",
            'redirect': url_for('view_doctors', specialization=dept),
            'options': [],
        })

    intent = _detect_non_emergency_intent(text)
    if intent == 'nearby':
        return jsonify({
            'intent': 'nearby',
            'message': "I'll take you to doctors near you. You can share your location there "
                       "to sort by distance and open map directions.",
            'redirect': url_for('nearby_doctors'),
            'options': [],
        })
    if intent == 'greeting':
        return jsonify({
            'intent': 'greeting',
            'message': "Hello! Describe your symptoms and I'll suggest the right "
                       "department, or ask about nearby doctors, hours or contact details.",
            'options': ['Nearby doctors', 'Opening hours', 'Talk to Human'],
        })
    if 'human' in text or 'reception' in text:
        return jsonify({'intent': 'human', 'message': _HUMAN_ESCALATION_REPLY,
                        'options': ['Nearby doctors']})

    # 3. Informational intents and clinical triage read the hospital profile.
    hospital = get_hospital_info()
    if intent == 'hours':
        hours = hospital.get('opening_hours')
        message = (f"Our opening hours: {hours}." if hours
                   else "I don't have verified opening hours available right now.")
        return jsonify({'intent': 'hours', 'message': message,
                        'options': ['Nearby doctors', 'Talk to Human']})
    if intent == 'address':
        address = hospital.get('address')
        message = (f"We're at: {address}." if address
                   else "I don't have a verified address available right now.")
        return jsonify({'intent': 'contact', 'message': message,
                        'options': ['Talk to Human']})
    if intent == 'contact':
        return jsonify({'intent': 'contact', 'message': _hospital_contact_text(hospital),
                        'options': ['Nearby doctors']})

    # 4. Everything else with clinical wording goes through triage.
    return jsonify(_chatbot_triage_reply(user_input, hospital))

# Start the scheduler once per process, after every job function is defined.
# Import-time start is intentional so the Gunicorn entrypoint (app:app) also
# runs reminders. Set DISABLE_SCHEDULER=1 for CLI tools and tests.
if os.environ.get('DISABLE_SCHEDULER', '').strip() not in ('1', 'true', 'True', 'yes'):
    start_scheduler()

# ========== MAIN EXECUTION ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)