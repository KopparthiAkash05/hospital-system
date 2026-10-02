import os
import pymysql
from markupsafe import Markup
pymysql.install_as_MySQLdb()

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
import MySQLdb.cursors
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone  # ← MUST HAVE timezone
import re
import secrets  # ← MUST HAVE secrets
import math
import requests # Make sure this is imported at the very top of your app.py
from flask_mail import Mail, Message
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
load_dotenv()

# ========== FLASK APP CONFIGURATION ==========
app = Flask(__name__)

# Secret key for session management (Loaded from Render Environment Variables)
app.secret_key = os.environ.get('SECRET_KEY', '202005')

# MySQL configuration (Loaded from Render Environment Variables)
app.config['MYSQL_HOST'] = os.environ.get('MYSQL_HOST', 'sql.freedb.tech')
app.config['MYSQL_USER'] = os.environ.get('MYSQL_USER', 'u_F3klJk')
app.config['MYSQL_PASSWORD'] = os.environ.get('MYSQL_PASSWORD', 'LtjrF5EQqhGw')
app.config['MYSQL_DB'] = os.environ.get('MYSQL_DB', 'freedb_xor9rxN1')

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

# Email configuration (Loaded from Render Environment Variables)
# Email configuration - ADD TIMEOUT SETTINGS
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'akashkopparthi@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'bgxkfhkwadntjkvr')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', 'akashkopparthi@gmail.com')
app.config['MAIL_TIMEOUT'] = 10  # Add this - 10 second timeout


mail = Mail(app)
scheduler = BackgroundScheduler()
scheduler.start()

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
    """Converts 12-hour format (10AM, 5PM) to 24-hour format (10:00, 17:00)"""
    try:
        time_str = time_str.strip().upper()
        
        # Handle formats like "10AM", "10:00AM", "5PM"
        if 'AM' in time_str:
            time_str = time_str.replace('AM', '').strip()
            if ':' in time_str:
                hour, minute = time_str.split(':')
            else:
                hour = time_str
                minute = '00'
            hour = int(hour)
            if hour == 12:
                hour = 0
        elif 'PM' in time_str:
            time_str = time_str.replace('PM', '').strip()
            if ':' in time_str:
                hour, minute = time_str.split(':')
            else:
                hour = time_str
                minute = '00'
            hour = int(hour)
            if hour != 12:
                hour += 12
        else:
            # Already in 24-hour format
            if ':' in time_str:
                hour, minute = time_str.split(':')
            else:
                hour = time_str
                minute = '00'
            hour = int(hour)
        
        return f"{hour:02d}:{minute}"
    except Exception as e:
        print(f"Error converting time {time_str}: {e}")
        return "09:00"  # Default fallback

def parse_doctor_slots(slots_str):
    """
    Parses doctor's available slots from text like:
    - "Mon-Fri: 10AM-5PM"
    - "Mon, Wed, Fri: 9AM-12PM"
    - "10:00, 11:00, 14:00"
    Returns list of 30-minute time slots in 24-hour format.
    """
    if not slots_str:
        # Default: 9 AM to 5 PM
        return generate_30min_slots("09:00", "17:00")
    
    slots_str = slots_str.strip()
    
    # Check if it contains day names and time range (e.g., "Mon-Fri: 10AM-5PM")
    if ':' in slots_str and ('AM' in slots_str.upper() or 'PM' in slots_str.upper()):
        # Extract the time range part after the colon
        parts = slots_str.split(':')
        time_part = parts[-1].strip()  # Get "10AM-5PM"
        
        # Split by hyphen or dash to get start and end times
        if '-' in time_part:
            times = time_part.split('-')
            if len(times) == 2:
                start_time = convert_12hr_to_24hr(times[0].strip())
                end_time = convert_12hr_to_24hr(times[1].strip())
                return generate_30min_slots(start_time, end_time)
    
    # Check if it's a simple comma-separated list of times
    if ',' in slots_str:
        times = [t.strip() for t in slots_str.split(',')]
        result = []
        for t in times:
            if 'AM' in t.upper() or 'PM' in t.upper():
                result.append(convert_12hr_to_24hr(t))
            elif ':' in t:
                result.append(t)  # Already in 24-hour format
        if result:
            return result
    
    # Default fallback
    return generate_30min_slots("09:00", "17:00")

def generate_30min_slots(start_str, end_str):
    """Generates a list of 30-minute time slots between start and end time."""
    try:
        start = datetime.strptime(start_str.strip(), '%H:%M')
        end = datetime.strptime(end_str.strip(), '%H:%M')
        slots = []
        current = start
        while current <= end:
            slots.append(current.strftime('%H:%M'))
            current += timedelta(minutes=30)
        return slots
    except Exception as e:
        print(f"Error generating slots: {e}")
        # Fallback default slots
        return ['09:00', '09:30', '10:00', '10:30', '11:00', '11:30', '12:00', 
                '12:30', '13:00', '13:30', '14:00', '14:30', '15:00', '15:30', 
                '16:00', '16:30', '17:00']
    
def generate_30min_slots(start_str, end_str):
    """Generates a list of 30-minute time slots between start and end time."""
    try:
        start = datetime.strptime(start_str.strip(), '%H:%M')
        end = datetime.strptime(end_str.strip(), '%H:%M')
        slots = []
        current = start
        while current <= end:
            slots.append(current.strftime('%H:%M'))
            current += timedelta(minutes=30)
        return slots
    except Exception as e:
        print(f"Error generating slots: {e}")
        # Fallback default slots
        return ['09:00', '09:30', '10:00', '10:30', '11:00', '11:30', '12:00', 
                '12:30', '13:00', '13:30', '14:00', '14:30', '15:00', '15:30', 
                '16:00', '16:30', '17:00']
    
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
        if 'loggedin' not in session or session.get('user_type') != 'patient':
            return jsonify({'success': False, 'message': 'Patient access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def api_doctor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session or session.get('user_type') != 'doctor':
            return jsonify({'success': False, 'message': 'Doctor access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def api_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'loggedin' not in session or session.get('user_type') != 'admin':
            return jsonify({'success': False, 'message': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ========== HOME ROUTE ==========
@app.route('/')
def index():
    return render_template('index.html')

# ========== AUTHENTICATION ROUTES ==========
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        
        # Try to find user in Patient table first
        cursor.execute('SELECT * FROM Patient WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            session['loggedin'] = True
            session['id'] = account['patient_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'patient'
            flash('Login successful!', 'success')
            cursor.close()
            return redirect(url_for('patient_dashboard'))
        
        # Try Doctor table
        cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            session['loggedin'] = True
            session['id'] = account['doctor_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'doctor'
            flash('Login successful!', 'success')
            cursor.close()
            return redirect(url_for('doctor_dashboard'))
        
        # Try Admin table
        cursor.execute('SELECT * FROM Admin WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            session['loggedin'] = True
            session['id'] = account['admin_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'admin'
            flash('Login successful!', 'success')
            cursor.close()
            return redirect(url_for('admin_dashboard'))
        
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
                'INSERT INTO Patient (name, email, password, phone, age, gender, address) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                (name, email, hashed_password, phone, age, gender, address)
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
                INSERT INTO Doctor (name, email, password, phone, specialization, experience, available_slots, address, latitude, longitude) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (name, email, hashed_password, phone, specialization, experience, available_slots, address, latitude, longitude))
            
            mysql.connection.commit()
            cursor.close()
            flash('Doctor registration successful! Please login.', 'success')
            return redirect(url_for('doctor_login'))
        
        cursor.close()
    
    return render_template('doctor/register.html')
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
        
        # 🌍 AUTOMATIC GEOCODING (Fetch new coordinates if address changed)
        latitude, longitude = get_coordinates_from_address(address)
        
        # If geocoding fails but we already have old coordinates, keep the old ones
        if latitude is None and doctor['latitude'] is not None:
            latitude = doctor['latitude']
            longitude = doctor['longitude']

        # Update Profile Details in Database
        cursor.execute('''
            UPDATE Doctor 
            SET name=%s, phone=%s, address=%s, latitude=%s, longitude=%s, available_slots=%s 
            WHERE doctor_id=%s
        ''', (name, phone, address, latitude, longitude, available_slots, session['id']))
        mysql.connection.commit()
        
        # Update session variables so the navbar reflects the new name
        session['name'] = name

        flash('Profile and schedule updated successfully!', 'success')
        cursor.close()
        return redirect(url_for('doctor_settings'))

    cursor.close()
    return render_template('doctor/settings.html', doctor=doctor)

@app.route('/doctor/login', methods=['GET', 'POST'])
def doctor_login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Doctor WHERE email = %s', (email,))
        account = cursor.fetchone()
        
        if account and check_password_hash(account['password'], password):
            session['loggedin'] = True
            session['id'] = account['doctor_id']
            session['email'] = account['email']
            session['name'] = account['name']
            session['user_type'] = 'doctor'
            flash('Login successful!', 'success')
            cursor.close()
            return redirect(url_for('doctor_dashboard'))
        else:
            flash('Invalid email or password!', 'danger')
        
        cursor.close()
    
    return render_template('doctor/login.html')

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
    
    cursor.close()
    
    # Convert times to 12-hour AM/PM format
    def format_time_12hr(time_obj):
        if time_obj is None:
            return ''
        
        # Handle timedelta objects (MySQL sometimes returns these)
        if isinstance(time_obj, timedelta):
            total_seconds = int(time_obj.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            from datetime import time
            time_obj = time(hours, minutes)
        
        # Handle string time values
        if isinstance(time_obj, str):
            time_obj = datetime.strptime(time_obj, '%H:%M:%S').time()
        
        # Now format as 12-hour time
        return time_obj.strftime('%I:%M %p')
    
    for appt in upcoming_appointments:
        appt['time_formatted'] = format_time_12hr(appt['time'])
    
    for appt in appointment_history:
        appt['time_formatted'] = format_time_12hr(appt['time'])
    
    return render_template('patient/dashboard.html', 
                         upcoming_appointments=upcoming_appointments,
                         appointment_history=appointment_history)

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
        
        cursor.execute('''
            UPDATE Patient 
            SET name=%s, phone=%s, age=%s, address=%s, email=%s 
            WHERE patient_id=%s
        ''', (name, phone, age, address, email, session['id']))
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
    
    if specialization:
        cursor.execute('SELECT * FROM Doctor WHERE specialization = %s', (specialization,))
    else:
        cursor.execute('SELECT * FROM Doctor')
    
    doctors = cursor.fetchall()
    
    cursor.execute('SELECT DISTINCT specialization FROM Doctor')
    specializations = [row['specialization'] for row in cursor.fetchall()]
    
    cursor.close()
    return render_template('patient/view_doctors.html', doctors=doctors, specializations=specializations)

# Helper function to convert 24h to 12h AM/PM
def get_12hr_time(time_24):
    try:
        dt = datetime.strptime(time_24.strip(), '%H:%M')
        hour = dt.hour
        am_pm = "AM" if hour < 12 else "PM"
        hour_12 = hour % 12
        if hour_12 == 0:
            hour_12 = 12
        return f"{hour_12}:{dt.strftime('%M')} {am_pm}"
    except (ValueError, AttributeError) as e:
        # Fixed: Specific exception handling instead of bare except
        print(f"Error converting time: {e}")
        return time_24

@app.route('/patient/book/<int:doctor_id>', methods=['GET', 'POST'])
@patient_required
def book_appointment(doctor_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (doctor_id,))
    doctor = cursor.fetchone()
    
    if not doctor:
        flash('Doctor not found!', 'danger')
        cursor.close()
        return redirect(url_for('view_doctors'))
    
    # Parse Available Slots into 30-min intervals
    raw_slots_24h = parse_doctor_slots(doctor.get('available_slots'))
    available_slots = []
    for slot_24 in raw_slots_24h:
        slot_12 = get_12hr_time(slot_24)
        available_slots.append({'24h': slot_24, '12h': slot_12})

    if request.method == 'POST':
        date = request.form['date']
        time_24 = request.form['time']
        
        # 1. SUNDAY CHECK
        selected_date = datetime.strptime(date, '%Y-%m-%d').date()
        if selected_date.weekday() == 6:
            flash('Doctors are not available on Sundays. Please choose another day.', 'warning')
            cursor.close()
            return redirect(url_for('book_appointment', doctor_id=doctor_id))

        # 2. PAST DATE CHECK
        if selected_date < datetime.now().date():
            flash('You cannot book an appointment for a past date.', 'danger')
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
        else:
            # Book the appointment
            cursor.execute(
                'INSERT INTO Appointment (patient_id, doctor_id, date, time, status) VALUES (%s, %s, %s, %s, %s)',
                (session['id'], doctor_id, date, time_24, 'Pending')
            )
            mysql.connection.commit()
            cursor.close()
            flash('Appointment booked successfully! Waiting for doctor approval.', 'success')
            return redirect(url_for('patient_dashboard'))
    
    cursor.close()
    today = datetime.now().strftime('%Y-%m-%d')
    
    return render_template('patient/book_appointment.html', doctor=doctor, today=today, available_slots=available_slots)

@app.route('/patient/book_appointment', methods=['GET', 'POST'])
@patient_required
def patient_book_appointment_page():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    if request.method == 'POST':
        doctor_id = request.form['doctor_id']
        date = request.form['date']
        time_24 = request.form['time']
        
        # Get doctor name for the message
        cursor.execute('SELECT name, specialization FROM Doctor WHERE doctor_id = %s', (doctor_id,))
        doc_info = cursor.fetchone()

        # 1. SUNDAY CHECK
        selected_date = datetime.strptime(date, '%Y-%m-%d').date()
        if selected_date.weekday() == 6:
            flash('Doctors are not available on Sundays. Please choose another day.', 'warning')
            cursor.close()
            return redirect(url_for('patient_book_appointment_page'))

        # 2. PAST DATE CHECK
        if selected_date < datetime.now().date():
            flash('You cannot book an appointment for a past date.', 'danger')
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
        else:
            cursor.execute(
                'INSERT INTO Appointment (patient_id, doctor_id, date, time, status) VALUES (%s, %s, %s, %s, %s)',
                (session['id'], doctor_id, date, time_24, 'Pending')
            )
            mysql.connection.commit()
            cursor.close()
            flash('Appointment booked successfully! Waiting for doctor approval.', 'success')
            return redirect(url_for('patient_dashboard'))
    
    cursor.execute('SELECT * FROM Doctor')
    doctors = cursor.fetchall()
    cursor.close()
    
    today = datetime.now().strftime('%Y-%m-%d')
    default_slots_24h = generate_30min_slots("09:00", "17:00")
    available_slots = [{'24h': slot, '12h': get_12hr_time(slot)} for slot in default_slots_24h]
    
    return render_template('patient/book_appointment_page.html', doctors=doctors, today=today, available_slots=available_slots)

@app.route('/patient/cancel/<int:appointment_id>')
@patient_required
def cancel_appointment(appointment_id):
    cursor = mysql.connection.cursor()
    # Fixed: Only allow cancellation of Pending or Approved appointments
    cursor.execute(
        "UPDATE Appointment SET status = 'Cancelled' WHERE appointment_id = %s AND patient_id = %s AND status IN ('Pending', 'Approved')",
        (appointment_id, session['id'])
    )
    mysql.connection.commit()
    
    if cursor.rowcount > 0:
        flash('Appointment cancelled successfully!', 'success')
    else:
        flash('Could not cancel appointment. It may have already been completed or cancelled.', 'warning')
    
    cursor.close()
    return redirect(url_for('patient_dashboard'))

# ========== DOCTOR ROUTES ==========
@app.route('/doctor/dashboard')
@doctor_required
def doctor_dashboard():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    today = datetime.now().date()
    
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
    
    cursor.close()
    
    # Format times
    def format_time_12hr(time_obj):
        if time_obj is None:
            return ''
        from datetime import timedelta, time as dt_time
        if isinstance(time_obj, timedelta):
            total_seconds = int(time_obj.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            time_obj = dt_time(hours, minutes)
        if isinstance(time_obj, str):
            time_obj = datetime.strptime(time_obj, '%H:%M:%S').time()
        return time_obj.strftime('%I:%M %p')

    for appt in today_appointments:
        appt['time_formatted'] = format_time_12hr(appt['time'])
        
    for appt in pending_appointments:
        appt['time_formatted'] = format_time_12hr(appt['time'])
        
    for appt in completed_appointments:
        appt['time_formatted'] = format_time_12hr(appt['time'])
    
    return render_template('doctor/dashboard.html', 
                         today_appointments=today_appointments,
                         pending_appointments=pending_appointments,
                         completed_appointments=completed_appointments,
                         search_patient=search_patient,
                         filter_date=filter_date)

@app.route('/doctor/complete_appointment', methods=['POST'])
@doctor_required
def complete_appointment():
    appointment_id = request.form.get('appointment_id')
    
    # Get the days, default to 0 if empty or "-"
    try:
        follow_up_days = int(request.form.get('follow_up_days', 0))
    except ValueError:
        follow_up_days = 0
        
    cursor = mysql.connection.cursor()
    
    # If doctor enters 0, follow_up_date is NULL (No email will be sent)
    # If doctor enters 1-15, calculate the date
    follow_up_date = None
    if 1 <= follow_up_days <= 15:
        follow_up_date = (datetime.now() + timedelta(days=follow_up_days)).strftime('%Y-%m-%d')
    
    # Update status and follow-up date
    cursor.execute('''
        UPDATE Appointment 
        SET status = 'Completed', follow_up_date = %s 
        WHERE appointment_id = %s AND doctor_id = %s
    ''', (follow_up_date, appointment_id, session['id']))
    
    mysql.connection.commit()
    cursor.close()
    
    if follow_up_date:
        flash(f'Completed! Follow-up scheduled in {follow_up_days} days. Patient will be reminded 1 day before.', 'success')
    else:
        flash('Appointment marked as completed! No follow-up scheduled.', 'success')
        
    return redirect(url_for('doctor_appointments'))

@app.route('/doctor/appointments')
@doctor_required
def doctor_appointments():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    cursor.execute('''
        SELECT a.*, p.name as patient_name, p.email as patient_email, p.phone as patient_phone
        FROM Appointment a 
        LEFT JOIN Patient p ON a.patient_id = p.patient_id 
        WHERE a.doctor_id = %s
        ORDER BY a.date DESC, a.time DESC
    ''', (session['id'],))
    
    appointments = cursor.fetchall()
    cursor.close()
    
    # Format times to 12-hour format
    def format_time_12hr(time_obj):
        if time_obj is None:
            return 'N/A'
        from datetime import timedelta, time as dt_time
        if isinstance(time_obj, timedelta):
            total_seconds = int(time_obj.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            time_obj = dt_time(hours, minutes)
        if isinstance(time_obj, str):
            # Handle both 'HH:MM:SS' and 'HH:MM' formats
            if len(time_obj.split(':')) == 3:
                time_obj = datetime.strptime(time_obj, '%H:%M:%S').time()
            else:
                time_obj = datetime.strptime(time_obj, '%H:%M').time()
        return time_obj.strftime('%I:%M %p')  # e.g., "10:00 AM"
    
    # Add formatted time to each appointment
    for appt in appointments:
        appt['time_formatted'] = format_time_12hr(appt.get('time'))
    
    return render_template('doctor/appointments.html', appointments=appointments)


@app.route('/doctor/approve/<int:appointment_id>')
@doctor_required
def approve_appointment(appointment_id):
    cursor = mysql.connection.cursor()

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

        mysql.connection.commit()
        appointment_updated = cursor.rowcount > 0

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

# ========== ADMIN ROUTES ==========
@app.route('/admin/dashboard')
def admin_dashboard():
    # Note: Ensure only admins can access this. 
    # If you have an admin session check, add it here.
    
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
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

    cursor.close()

    return render_template('admin/dashboard.html', 
                           total_appointments=total_appointments, 
                           revenue=revenue,
                           dept_labels=dept_labels, 
                           dept_counts=dept_counts,
                           month_labels=month_labels, 
                           month_counts=month_counts,
                           top_doctors=top_doctors)

@app.route('/admin/doctors')
@admin_required
def admin_doctors():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Doctor')
    doctors = cursor.fetchall()
    cursor.close()
    return render_template('admin/doctors.html', doctors=doctors)

@app.route('/admin/patients')
@admin_required
def admin_patients():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute('SELECT * FROM Patient')
    patients = cursor.fetchall()
    cursor.close()
    return render_template('admin/patients.html', patients=patients)

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
    
    cursor.close()
    return render_template('admin/reports.html', appointments=appointments)

# ========== NEW FEATURES ==========
@app.route('/patient/nearby_doctors')
@patient_required
def nearby_doctors():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    
    # Get all doctors with location
    cursor.execute('SELECT * FROM Doctor WHERE latitude IS NOT NULL AND longitude IS NOT NULL')
    doctors = cursor.fetchall()
    
    # Get patient's location
    cursor.execute('SELECT * FROM Patient WHERE patient_id = %s', (session['id'],))
    patient = cursor.fetchone()
    
    cursor.close()
    
    # Calculate distance for each doctor (Haversine formula)
    def calculate_distance(lat1, lon1, lat2, lon2):
        R = 6371  # Earth's radius in km
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return R * c
    
    # Add distance to each doctor
    if patient and patient.get('latitude') and patient.get('longitude'):
        for doctor in doctors:
            if doctor.get('latitude') and doctor.get('longitude'):
                doctor['distance'] = round(calculate_distance(
                    float(patient['latitude']), 
                    float(patient['longitude']),
                    float(doctor['latitude']), 
                    float(doctor['longitude'])
                ), 2)
        # Sort by distance
        doctors.sort(key=lambda x: x.get('distance', 9999))
    
    return render_template('patient/nearby_doctors.html', doctors=doctors, patient=patient)

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
SYMPTOM_DEPARTMENT_MAP = {
    'fever': ['General Medicine', 'Infectious Disease'],
    'cough': ['General Medicine', 'Pulmonology'],
    'cold': ['General Medicine', 'ENT'],
    'headache': ['General Medicine', 'Neurology'],
    'migraine': ['Neurology'],
    'chest pain': ['Cardiology', 'Emergency'],
    'heart attack': ['Cardiology', 'Emergency'],
    'stomach pain': ['Gastroenterology', 'General Medicine'],
    'abdominal pain': ['Gastroenterology'],
    'back pain': ['Orthopedics', 'Neurology'],
    'skin rash': ['Dermatology'],
    'acne': ['Dermatology'],
    'eye pain': ['Ophthalmology'],
    'blurry vision': ['Ophthalmology'],
    'ear pain': ['ENT'],
    'hearing loss': ['ENT'],
    'tooth pain': ['Dental'],
    'toothache': ['Dental'],
    'joint pain': ['Orthopedics', 'Rheumatology'],
    'arthritis': ['Rheumatology', 'Orthopedics'],
    'diabetes': ['Endocrinology'],
    'high blood sugar': ['Endocrinology'],
    'high blood pressure': ['Cardiology'],
    'hypertension': ['Cardiology'],
    'anxiety': ['Psychiatry', 'Psychology'],
    'depression': ['Psychiatry', 'Psychology'],
    'stress': ['Psychiatry', 'Psychology'],
    'pregnancy': ['Gynecology', 'Obstetrics'],
    'period pain': ['Gynecology'],
    'menstrual cramps': ['Gynecology'],
    'pediatric': ['Pediatrics'],
    'child': ['Pediatrics'],
    'baby': ['Pediatrics'],
    'bone fracture': ['Orthopedics'],
    'broken bone': ['Orthopedics'],
    'allergy': ['Allergy & Immunology'],
    'asthma': ['Pulmonology', 'Allergy & Immunology'],
    'urinary': ['Urology', 'Nephrology'],
    'kidney': ['Nephrology', 'Urology'],
    'kidney stones': ['Urology', 'Nephrology'],
    'heart': ['Cardiology'],
    'palpitations': ['Cardiology'],
    'brain': ['Neurology'],
    'seizure': ['Neurology'],
    'mental health': ['Psychiatry'],
    'insomnia': ['Neurology', 'Psychiatry'],
    'sleep problems': ['Neurology', 'Psychiatry'],
    'dizziness': ['Neurology', 'ENT'],
    'vertigo': ['ENT', 'Neurology'],
    'nausea': ['Gastroenterology', 'General Medicine'],
    'vomiting': ['Gastroenterology', 'General Medicine'],
    'diarrhea': ['Gastroenterology'],
    'constipation': ['Gastroenterology'],
    'weight loss': ['Endocrinology', 'General Medicine'],
    'weight gain': ['Endocrinology', 'General Medicine'],
    'fatigue': ['General Medicine', 'Endocrinology'],
    'tiredness': ['General Medicine', 'Endocrinology'],
    'shortness of breath': ['Pulmonology', 'Cardiology'],
    'breathing problems': ['Pulmonology'],
    'throat pain': ['ENT', 'General Medicine'],
    'sore throat': ['ENT', 'General Medicine'],
    'sinus': ['ENT'],
    'sinusitis': ['ENT'],
    'hair loss': ['Dermatology', 'Endocrinology'],
    'eczema': ['Dermatology'],
    'psoriasis': ['Dermatology'],
    'thyroid': ['Endocrinology'],
    'thyroid problems': ['Endocrinology'],
    'anemia': ['Hematology', 'General Medicine'],
    'bleeding': ['Hematology', 'Emergency'],
    'urine infection': ['Urology', 'Nephrology'],
    'UTI': ['Urology'],
    'prostate': ['Urology'],
    'cancer': ['Oncology'],
    'tumor': ['Oncology'],
    'liver': ['Gastroenterology', 'Hepatology'],
    'jaundice': ['Gastroenterology', 'Hepatology'],
    'hepatitis': ['Gastroenterology', 'Hepatology'],
}

def suggest_department(symptoms):
    symptoms_lower = symptoms.lower()
    department_scores = {}
    
    for symptom, departments in SYMPTOM_DEPARTMENT_MAP.items():
        if symptom in symptoms_lower:
            for dept in departments:
                department_scores[dept] = department_scores.get(dept, 0) + 1
    
    if not department_scores:
        return 'General Medicine'
    
    suggested_dept = max(department_scores, key=department_scores.get)
    return suggested_dept

@app.route('/symptom_checker', methods=['GET', 'POST'])
def symptom_checker():
    suggested_department = None
    symptoms = ''
    
    if request.method == 'POST':
        symptoms = request.form.get('symptoms', '')
        if symptoms:
            suggested_department = suggest_department(symptoms)
    
    return render_template('symptom_checker.html', 
                         suggested_department=suggested_department,
                         symptoms=symptoms)

# ==========================================
# EMAIL REMINDER SYSTEM (SENDGRID VERSION)
# ==========================================


from datetime import datetime, timedelta, timezone
# India Standard Time (IST): UTC +05:30
TIMEZONE_OFFSET_HOURS = 5.5
def get_server_time_with_offset():
    """Return the current time in the configured local timezone."""
    now_utc = datetime.now(timezone.utc)
    return now_utc + timedelta(hours=TIMEZONE_OFFSET_HOURS)


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

            body = f"""Hello {appt['patient_name']},

This is a reminder that you have an appointment with Dr. {appt['doctor_name']} in approximately 30 minutes.

Date: {appt['date']}
Time: {time_str}

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

        if hasattr(appointment_time, 'strftime'):
            appointment_time = appointment_time.strftime('%I:%M %p')
        else:
            appointment_time = str(appointment_time)

        subject = "MediCare Hospital - Appointment Confirmed"

        body = f"""Hello {appt['patient_name']},

Good news! Your appointment has been accepted by the doctor.

Doctor: Dr. {appt['doctor_name']}
Appointment Date: {appointment_date}
Appointment Time: {appointment_time}

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

        if hasattr(appointment_time, 'strftime'):
            appointment_time = appointment_time.strftime('%I:%M %p')
        else:
            appointment_time = str(appointment_time)

        subject = "MediCare Hospital - Appointment Cancelled"

        body = f"""Hello {appt['patient_name']},

Your appointment at MediCare Hospital has been cancelled.

Doctor: Dr. {appt['doctor_name']}
Appointment Date: {appointment_date}
Appointment Time: {appointment_time}

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
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Run 30-min check every minute
scheduler.add_job(func=check_and_send_reminders, trigger="cron", minute='*')

# Run follow-up check every minute too (so it catches the exact date)
scheduler.add_job(func=send_follow_up_reminder, trigger="cron", minute='*')
        
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
        
        if specialization:
            cursor.execute('SELECT * FROM Doctor WHERE specialization = %s', (specialization,))
        else:
            cursor.execute('SELECT * FROM Doctor')
        
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
        data = request.get_json()
        doctor_id = data.get('doctor_id')
        date = data.get('date')
        time = data.get('time')
        
        if not all([doctor_id, date, time]):
            return jsonify({'success': False, 'message': 'Doctor ID, date, and time are required'}), 400
        
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('SELECT * FROM Doctor WHERE doctor_id = %s', (doctor_id,))
        doctor = cursor.fetchone()
        
        if not doctor:
            cursor.close()
            return jsonify({'success': False, 'message': 'Doctor not found'}), 404
        
        cursor.execute('''
            SELECT * FROM Appointment WHERE doctor_id = %s AND date = %s AND time = %s AND status IN ('Pending', 'Approved')
        ''', (doctor_id, date, time))
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute('SELECT name, specialization FROM Doctor WHERE doctor_id = %s', (doctor_id,))
            busy_doc = cursor.fetchone()
            
            cursor.close()
            return jsonify({
                'success': False, 
                'message': f'Dr. {busy_doc["name"]} is busy at this time. Please find another {busy_doc["specialization"]} doctor.',
                'suggested_action': 'search_other_doctors',
                'specialization': busy_doc['specialization']
            }), 409
        
        cursor.execute(
            'INSERT INTO Appointment (patient_id, doctor_id, date, time, status) VALUES (%s, %s, %s, %s, %s)',
            (session['id'], doctor_id, date, time, 'Pending')
        )
        mysql.connection.commit()
        appointment_id = cursor.lastrowid
        cursor.close()
        
        return jsonify({'success': True, 'message': 'Appointment booked successfully', 'appointment': {'appointment_id': appointment_id, 'doctor_id': doctor_id, 'date': date, 'time': time, 'status': 'Pending'}}), 201
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/patient/appointments', methods=['GET'])
@api_patient_required
def api_get_patient_appointments():
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute('''
            SELECT a.*, d.name as doctor_name, d.specialization FROM Appointment a LEFT JOIN Doctor d ON a.doctor_id = d.doctor_id WHERE a.patient_id = %s ORDER BY a.date DESC, a.time DESC
        ''', (session['id'],))
        appointments = cursor.fetchall()
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
        
        appointments = cursor.fetchall()
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
        
        cursor = mysql.connection.cursor()
        cursor.execute('SELECT * FROM Appointment WHERE appointment_id = %s AND doctor_id = %s', (appointment_id, session['id']))
        appointment = cursor.fetchone()
        
        if not appointment:
            cursor.close()
            return jsonify({'success': False, 'message': 'Appointment not found'}), 404
        
        new_status = 'Approved' if action == 'approve' else 'Rejected'
        cursor.execute('UPDATE Appointment SET status = %s WHERE appointment_id = %s AND doctor_id = %s', (new_status, appointment_id, session['id']))
        mysql.connection.commit()
        cursor.close()
        
        return jsonify({'success': True, 'message': f'Appointment {action}d successfully', 'new_status': new_status}), 200
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
        today = datetime.now().date()
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

@app.route('/api/chatbot', methods=['POST'])
def chatbot_api():
    data = request.json
    step = data.get('step', 0)
    user_input = data.get('input', '').lower()

    # Define the decision tree
    if step == 0:
        return jsonify({
            'step': 1,
            'message': "I'm sorry to hear that. To help you better, which area is affected?",
            'options': ['Head / Migraine', 'Chest / Breathing', 'Stomach / Digestion', 'Bones / Joints', 'Skin / Allergy']
        })
    
    elif step == 1:
        # Map body part to department
        department_map = {
            'head': 'Neurology',
            'migraine': 'Neurology',
            'chest': 'Cardiology',
            'breathing': 'Pulmonology',
            'stomach': 'Gastroenterology',
            'digestion': 'Gastroenterology',
            'bones': 'Orthopedics',
            'joints': 'Orthopedics',
            'skin': 'Dermatology',
            'allergy': 'Dermatology'
        }
        
        recommended_dept = 'General Medicine' # Default
        for key, dept in department_map.items():
            if key in user_input:
                recommended_dept = dept
                break
                
        return jsonify({
            'step': 2,
            'message': f"Based on that, you might need a {recommended_dept} specialist. How long have you been experiencing this?",
            'options': ['Less than 24 hours', '1 to 3 days', 'More than 3 days'],
            'department': recommended_dept
        })

    elif step == 2:
        dept = data.get('department', 'General Medicine')
        urgency = "routine"
        if '24 hours' in user_input:
            urgency = "urgent"
            message = f"Since it started recently, please visit our Emergency or {dept} department immediately."
        elif '3 days' in user_input:
            urgency = "chronic"
            message = f"Since it's been more than 3 days, I highly recommend booking a {dept} appointment today."
        else:
            message = f"It's best to get a checkup. I recommend booking an appointment with a {dept} specialist."

        return jsonify({
            'step': 3,
            'message': message,
            'options': ['Book Appointment', 'Talk to Human'],
            'final_dept': dept
        })

    elif step == 3:
        if 'book' in user_input:
            dept = data.get('final_dept', 'General Medicine')
            return jsonify({
                'step': 4,
                'message': f"Great! Redirecting you to find {dept} doctors...",
                'redirect': url_for('view_doctors', specialization=dept)
            })
        else:
            return jsonify({
                'step': 4,
                'message': "Please call our hospital reception at +91-9876543210 for immediate assistance.",
                'options': []
            })

    return jsonify({'message': "I didn't understand that. Let's start over.", 'step': 0, 'options': ['Start Over']})

# Start the scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Run the reminder check every minute
scheduler.add_job(func=check_and_send_reminders, trigger="cron", minute='*')

# ========== MAIN EXECUTION ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)